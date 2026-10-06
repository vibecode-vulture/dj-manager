"""Cleaning up duplicate files - the only place where DJ Manager removes files from disk.

A song can exist several times on disk (e.g. downloaded into several playlist folders
before DJ Manager). The library already stores such a song once: one file is the track,
the others are listed as duplicates, and every genre where a copy was found contains the
song. Cleaning up keeps exactly one file and moves the other copies to the system trash.

Rules
-----
* Only *certain* duplicates are cleaned automatically: identical file, or the same Spotify
  id or ISRC in the tags. Copies that only match by artist, title and duration may be a
  different version (Original vs Extended Mix) and are only removed when selected.
* The kept copy is the one the user worked with in Traktor (cue points, grid), else the
  best audio quality (lossless, then bitrate), else the current one.
* Before a copy is removed: the kept file must exist and be readable audio, the copy must
  be a different file (not the same file through a link or different case), and it must
  be inside the music folder. Removing goes to the trash, never permanently.
* Afterwards every genre that had a copy still contains the song, and Traktor references
  to removed copies (also in the user's own playlists) point to the kept file.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from .audio import read_info
from .library import SOURCE_LOCAL, Library, Track
from .util import move_to_trash

LOSSLESS = {".flac", ".wav", ".aiff", ".aif"}


@dataclass
class Copy:
    path: str              # relative to the music folder
    size: int = 0
    duration: float = 0.0
    bitrate: int = 0       # kbit/s
    lossless: bool = False
    cues: int = 0          # Traktor cue points (+1 for a grid)
    evidence: str = ""     # why it is the same song
    certain: bool = False
    exists: bool = True


@dataclass
class Group:
    track: Track
    keep: Copy
    reason: str
    remove: list[Copy] = field(default_factory=list)    # certain duplicates
    uncertain: list[Copy] = field(default_factory=list)  # removed only when selected


def _quality(path: Path) -> tuple[float, int]:
    try:
        import mutagen

        audio = mutagen.File(path)
        if audio is not None and audio.info is not None:
            return float(getattr(audio.info, "length", 0) or 0), int((getattr(audio.info, "bitrate", 0) or 0) / 1000)
    except Exception:
        pass
    return 0.0, 0


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _describe(lib: Library, rel: str, cue_data) -> Copy:
    path = lib.abs_path(rel)
    if not path.is_file():
        return Copy(rel, exists=False)
    duration, bitrate = _quality(path)
    return Copy(rel, size=path.stat().st_size, duration=duration, bitrate=bitrate,
                lossless=path.suffix.lower() in LOSSLESS, cues=cue_data(path) if cue_data else 0)


def _evidence(lib: Library, track: Track, original: Copy, copy: Copy) -> tuple[str, bool]:
    info = read_info(lib.abs_path(copy.path))
    same_id = bool(info.spotify_id and track.spotify_id and info.spotify_id == track.spotify_id)
    same_isrc = bool(info.isrc and track.isrc and info.isrc.upper() == track.isrc.upper())
    if same_id:
        return "same Spotify id", True
    if same_isrc:
        return "same ISRC", True
    if original.exists and copy.size == original.size and \
            _digest(lib.abs_path(copy.path)) == _digest(lib.abs_path(original.path)):
        return "identical file", True
    if info.spotify_id and track.spotify_id and info.isrc and track.isrc:
        return "different Spotify id and ISRC - probably another version", False
    a, b = copy.duration or info.duration, original.duration or track.duration
    return f"same artist and title, {a:.0f}s vs {b:.0f}s", False


def _keeper(candidates: list[Copy], current: str) -> tuple[Copy, str]:
    best = max(candidates, key=lambda c: (c.cues, c.lossless, c.bitrate, c.path == current))
    others = [c for c in candidates if c is not best]
    if best.cues and all(best.cues > c.cues for c in others):
        return best, "has cue points / beatgrid in Traktor"
    if best.path != current:
        return best, "best audio quality (lossless)" if best.lossless else f"best audio quality ({best.bitrate} kbit/s)"
    return best, "file DJ Manager uses already"


def plan(lib: Library, track_ids=None, cue_data=None) -> list[Group]:
    """What cleaning up would do. Reads tags and Traktor data, changes nothing."""
    groups = []
    for track in lib.tracks.values():
        if not track.duplicates or (track_ids is not None and track.id not in track_ids):
            continue
        original = _describe(lib, track.path, cue_data) if track.path else Copy("", exists=False)
        copies = []
        for rel in track.duplicates:
            copy = _describe(lib, rel, cue_data)
            if copy.exists:
                copy.evidence, copy.certain = _evidence(lib, track, original, copy) if original.exists else ("", False)
                copies.append(copy)
        if not original.exists and not copies:
            continue
        # the kept file is chosen among the original and the certain copies only
        candidates = ([original] if original.exists else []) + [c for c in copies if c.certain]
        if not candidates:
            continue
        keep, reason = _keeper(candidates, track.path)
        group = Group(track, keep, reason)
        for c in ([original] if original.exists and original is not keep else []) + copies:
            if c is keep:
                continue
            if c is original:
                c.evidence, c.certain = "the file DJ Manager used so far", True
            (group.remove if c.certain else group.uncertain).append(c)
        groups.append(group)
    return groups


class CleanupError(RuntimeError):
    pass


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def clean(lib: Library, groups: list[Group], log, selected_uncertain: set[str] | None = None,
          trash=None) -> dict:
    """Move the duplicates of the given groups to the trash. Returns counts."""
    trash = trash or move_to_trash  # looked up at call time (replaceable, e.g. in tests)
    selected_uncertain = selected_uncertain or set()
    root = lib.root.resolve()
    removed, freed, skipped = 0, 0, []
    for group in groups:
        track = group.track
        keep_path = lib.abs_path(group.keep.path)
        targets = group.remove + [c for c in group.uncertain if c.path in selected_uncertain]
        if not targets:
            continue
        # 1. the kept file must really be there and be audio
        if not keep_path.is_file() or keep_path.stat().st_size == 0 or not read_info(keep_path).duration:
            skipped.append(f"{track.artist_line} - {track.title}: kept file {group.keep.path} is missing or not readable")
            continue
        # 2. every genre that had a copy keeps the song (unless it was removed there on purpose)
        for c in targets + [group.keep]:
            owner = lib.playlist_owning_folder(c.path)
            if owner is not None and track.id not in owner.members and track.spotify_id not in owner.blacklist:
                owner.members[track.id] = SOURCE_LOCAL
        # 3. the track now uses the kept file; the previous one becomes a copy to remove
        old_path = track.path
        if group.keep.path != old_path:
            track.path = group.keep.path
            if group.keep.path in track.duplicates:
                track.duplicates.remove(group.keep.path)
            if old_path and old_path not in track.duplicates:
                track.duplicates.append(old_path)
        # 4. remove the copies, each with its own safety checks
        for c in targets:
            path = lib.abs_path(c.path)
            why = None
            if not path.is_file():
                why = "already gone"
            elif _same_file(path, keep_path) or path.resolve() == keep_path.resolve():
                why = "is the kept file itself (link or different case) - kept"
            elif root not in path.resolve().parents:
                why = "outside the music folder - kept"
            if why:
                if why == "already gone" and c.path in track.duplicates:
                    track.duplicates.remove(c.path)
                    lib.record_move(c.path, group.keep.path)
                else:
                    skipped.append(f"{c.path}: {why}")
                continue
            size = path.stat().st_size
            try:
                trash(path)
            except OSError as exc:
                skipped.append(f"{c.path}: {exc}")
                continue
            if c.path in track.duplicates:
                track.duplicates.remove(c.path)
            lib.record_move(c.path, group.keep.path)  # Traktor: references to the copy -> kept file
            removed += 1
            freed += size
            log(f"Trash: {c.path}  (kept {group.keep.path})")
        lib.reindex()
    # 5. verify: every touched song still has its file
    lost = [g.track for g in groups if not lib.has_file(g.track)]
    if lost:
        raise CleanupError("Kept files missing after cleanup (restore them from the trash): "
                           + ", ".join(t.path for t in lost))
    for line in skipped:
        log(f"SKIPPED {line}")
    return {"removed": removed, "freed": freed, "skipped": len(skipped)}
