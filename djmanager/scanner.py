"""Import an existing folder structure: folders are genres, audio files are tracks.

Every folder that directly contains audio files becomes a playlist without a Spotify
link. Copies of the same song in several folders are stored once: the first file found
is the track, the others are recorded as duplicates (and never touched).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import genres
from .audio import read_info
from .library import META_FOLDER, REMOVED_FOLDER, SOURCE_LOCAL, Library, Playlist, Track
from .util import AUDIO_EXTENSIONS


@dataclass
class ScanResult:
    playlists_created: list[str] = field(default_factory=list)
    tracks_added: int = 0
    duplicates: int = 0
    memberships_added: int = 0
    skipped_root_files: int = 0

    def summary(self) -> str:
        return (
            f"{len(self.playlists_created)} new playlists, {self.tracks_added} new tracks, "
            f"{self.memberships_added} playlist entries, {self.duplicates} duplicate files"
            + (f", {self.skipped_root_files} files directly in the music folder ignored" if self.skipped_root_files else "")
        )


def _key_for_folder(lib: Library, rel_folder: str) -> str:
    parts = [genres.normalize_part(p) for p in PurePosixPath(rel_folder).parts]
    parts = [p or "untitled" for p in parts]
    key = genres.make_key(parts)
    existing = lib.find_playlist(key)
    n = 2
    while existing and existing.folder.lower() != rel_folder.lower():
        candidate = f"{key}-{n}"
        existing = lib.find_playlist(candidate)
        if not existing:
            key = candidate
        n += 1
    return key


def scan(lib: Library, log=print) -> ScanResult:
    result = ScanResult()
    root = lib.root
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if not d.startswith(".") and d not in (META_FOLDER, REMOVED_FOLDER) and d.lower() != REMOVED_FOLDER
        )
        audio_files = sorted(f for f in filenames if Path(f).suffix.lower() in AUDIO_EXTENSIONS and not f.startswith("."))
        if not audio_files:
            continue
        current = Path(dirpath)
        if current.resolve() == root.resolve():
            result.skipped_root_files += len(audio_files)
            continue
        rel_folder = PurePosixPath(current.relative_to(root)).as_posix()

        playlist = lib.playlist_owning_folder(rel_folder + "/x")
        if playlist is None:
            key = _key_for_folder(lib, rel_folder)
            playlist = Playlist(key=key, folder=rel_folder)
            lib.playlists[key] = playlist
            result.playlists_created.append(key)
            log(f"Genre playlist '{key}' from folder {rel_folder}")

        for name in audio_files:
            rel_path = f"{rel_folder}/{name}"
            if lib.is_known_path(rel_path):
                # Already managed; membership is the library's business (the user may
                # have removed it from this playlist on purpose).
                continue
            info = read_info(current / name)
            track = lib.match(info.spotify_id, info.isrc, info.artists, info.title, info.duration)
            if track and lib.abs_path(track.path).exists():
                track.duplicates.append(rel_path)
                lib.link_spotify(track, info.spotify_id, info.isrc)
                result.duplicates += 1
                log(f"Duplicate: {rel_path} = {track.path}")
            else:
                if track:  # known track whose file vanished - adopt this copy
                    track.path = rel_path
                    lib.reindex()
                else:
                    track = lib.add_track(Track(
                        id=lib.new_id(), path=rel_path, title=info.title, artists=info.artists,
                        album=info.album, duration=info.duration, spotify_id=info.spotify_id, isrc=info.isrc,
                    ))
                    result.tracks_added += 1
            if track.id not in playlist.members:
                playlist.members[track.id] = SOURCE_LOCAL
                result.memberships_added += 1
    lib.initialized = True
    return result
