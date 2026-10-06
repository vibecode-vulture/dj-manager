"""The collection: the single source of truth about tracks, playlists and genres.

Stored as JSON in <music folder>/.djmanager/library.json so it travels with the music.

Model
-----
* Track     - one audio file on disk (stored once), identified by an internal id.
* Playlist  - one genre node (key like 'techno_hard-techno'), optionally linked to a
              Spotify playlist. Members are ordered and carry a source:
              'spotify' (present in the Spotify playlist) or 'local' (not in it).
* Genre     - every playlist key plus all its ancestors. A genre's Traktor playlist
              aggregates the members of the genre's own playlist and all descendants.
* Orphan    - a track that is in no playlist anymore ("Removed" view). Files are never
              deleted by the program; orphans disappear once the user deletes the file.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath

from . import genres
from .util import atomic_write_text, normalize_text, now_iso

SOURCE_SPOTIFY = "spotify"
SOURCE_LOCAL = "local"
REMOVED_FOLDER = "_removed"
META_FOLDER = ".djmanager"


@dataclass
class Track:
    id: str
    path: str  # relative to the music folder, posix separators
    title: str = ""
    artists: list[str] = field(default_factory=list)
    album: str = ""
    duration: float = 0.0
    spotify_id: str | None = None
    isrc: str | None = None
    added_at: str = field(default_factory=now_iso)
    # Additional copies of the same song found on disk (relative paths). Never touched.
    duplicates: list[str] = field(default_factory=list)
    rating: int | None = None          # 1-5 stars from the file's tags
    rating_traktor: int | None = None  # 1-5 stars from Traktor's collection (fallback)
    mtime: float = 0.0                 # file modification time when the tags were read
    bpm: float | None = None
    key: str | None = None             # e.g. "A minor", formatted for display by analysis.format_key
    analysis: str = ""                 # "" = pending, "done", "failed"
    analysis_error: str = ""
    analysis_engine: str = ""
    # Split recommendations (optional): energy + sound fingerprint, AI styles.
    # The vectors themselves live in .djmanager/features/<id>.npz.
    energy: float | None = None        # 1-10
    features_status: str = ""          # "" pending, "done", "failed"
    styles_status: str = ""
    styles: list = field(default_factory=list)  # top styles [[label, probability], ...]
    extra_error: str = ""

    @property
    def stars(self) -> int | None:
        return self.rating if self.rating is not None else self.rating_traktor

    @property
    def artist_line(self) -> str:
        return ", ".join(self.artists)


@dataclass
class Playlist:
    key: str
    folder: str  # relative to the music folder, posix separators
    spotify_url: str = ""
    # Spotify user id of the playlist owner, once known. Playlists of the connected
    # account are read through the Web API (works for private ones, spotdl does not).
    spotify_owner: str = ""
    created_at: str = field(default_factory=now_iso)
    last_synced: str = ""
    last_error: str = ""
    # ordered: track id -> source
    members: dict[str, str] = field(default_factory=dict)
    # spotify id -> {"title", "artists", "added_at"}
    blacklist: dict[str, dict] = field(default_factory=dict)


@dataclass
class PendingMove:
    old: str
    new: str


def fuzzy_key(artists: list[str], title: str) -> str | None:
    if not artists or not title:
        return None
    artist = normalize_text(artists[0])
    name = normalize_text(title)
    if not artist or not name:
        return None
    return f"{artist}|{name}"


class Library:
    VERSION = 1

    def __init__(self, music_root: Path) -> None:
        self.root = Path(music_root)
        self.lock = threading.RLock()
        self.tracks: dict[str, Track] = {}
        self.playlists: dict[str, Playlist] = {}
        # File moves not yet applied to the Traktor collection.
        self.pending_moves: list[PendingMove] = []
        self.initialized = False  # initial folder scan done
        self._by_spotify: dict[str, str] = {}
        self._by_isrc: dict[str, str] = {}
        self._by_fuzzy: dict[str, list[str]] = {}
        self._by_path: dict[str, str] = {}

    # ------------------------------------------------------------------ storage
    @property
    def file(self) -> Path:
        return self.root / META_FOLDER / "library.json"

    @classmethod
    def load(cls, music_root: Path) -> "Library":
        lib = cls(music_root)
        if lib.file.exists():
            raw = json.loads(lib.file.read_text(encoding="utf-8"))
            lib.initialized = raw.get("initialized", True)
            lib.tracks = {t["id"]: Track(**t) for t in raw.get("tracks", [])}
            lib.playlists = {p["key"]: Playlist(**p) for p in raw.get("playlists", [])}
            lib.pending_moves = [PendingMove(**m) for m in raw.get("pending_moves", [])]
        lib.reindex()
        return lib

    def to_json(self) -> str:
        data = {
            "version": self.VERSION,
            "initialized": self.initialized,
            "tracks": [asdict(t) for t in self.tracks.values()],
            "playlists": [asdict(p) for p in self.playlists.values()],
            "pending_moves": [asdict(m) for m in self.pending_moves],
        }
        return json.dumps(data, indent=1, ensure_ascii=False)

    def save(self) -> None:
        with self.lock:
            atomic_write_text(self.file, self.to_json())

    # ------------------------------------------------------------------ indexes
    def reindex(self) -> None:
        self._by_spotify.clear()
        self._by_isrc.clear()
        self._by_fuzzy.clear()
        self._by_path.clear()
        for track in self.tracks.values():
            self._index(track)

    def _index(self, track: Track) -> None:
        if track.spotify_id:
            self._by_spotify.setdefault(track.spotify_id, track.id)
        if track.isrc:
            self._by_isrc.setdefault(track.isrc.upper(), track.id)
        fk = fuzzy_key(track.artists, track.title)
        if fk:
            self._by_fuzzy.setdefault(fk, []).append(track.id)
        self._by_path[track.path.lower()] = track.id

    def add_track(self, track: Track) -> Track:
        self.tracks[track.id] = track
        self._index(track)
        return track

    def remove_track(self, track_id: str) -> None:
        self.tracks.pop(track_id, None)
        for pl in self.playlists.values():
            pl.members.pop(track_id, None)
        self.reindex()

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex[:16]

    # ------------------------------------------------------------------ lookup
    def rel(self, path: Path) -> str:
        return PurePosixPath(Path(path).resolve().relative_to(self.root.resolve())).as_posix()

    def abs_path(self, rel_path: str) -> Path:
        return self.root.joinpath(*PurePosixPath(rel_path).parts)

    def track_by_path(self, rel_path: str) -> Track | None:
        tid = self._by_path.get(rel_path.lower())
        return self.tracks.get(tid) if tid else None

    def is_known_path(self, rel_path: str) -> bool:
        low = rel_path.lower()
        if low in self._by_path:
            return True
        return any(low == d.lower() for t in self.tracks.values() for d in t.duplicates)

    def match(
        self,
        spotify_id: str | None = None,
        isrc: str | None = None,
        artists: list[str] | None = None,
        title: str = "",
        duration: float = 0.0,
    ) -> Track | None:
        """Find an existing track for song metadata (Spotify id > ISRC > artist+title)."""
        if spotify_id and spotify_id in self._by_spotify:
            return self.tracks[self._by_spotify[spotify_id]]
        if isrc and isrc.upper() in self._by_isrc:
            # Same ISRC = same recording, even if Spotify lists it under several ids.
            return self.tracks[self._by_isrc[isrc.upper()]]
        fk = fuzzy_key(artists or [], title)
        if fk:
            for tid in self._by_fuzzy.get(fk, []):
                cand = self.tracks[tid]
                if spotify_id and cand.spotify_id and cand.spotify_id != spotify_id:
                    continue
                if duration and cand.duration and abs(duration - cand.duration) > 3:
                    continue
                return cand
        return None

    def link_spotify(self, track: Track, spotify_id: str | None, isrc: str | None) -> None:
        changed = False
        if spotify_id and not track.spotify_id:
            track.spotify_id = spotify_id
            changed = True
        if isrc and not track.isrc:
            track.isrc = isrc
            changed = True
        if changed:
            self.reindex()

    # ------------------------------------------------------------------ playlists
    def find_playlist(self, key: str) -> Playlist | None:
        if key in self.playlists:
            return self.playlists[key]
        low = key.lower()
        for k, pl in self.playlists.items():
            if k.lower() == low:
                return pl
        return None

    def folder_for_key(self, key: str) -> str:
        """Folder for a (new) genre key: reuse folders of existing ancestors."""
        existing = self.find_playlist(key)
        if existing:
            return existing.folder
        parts = key.split(genres.SEPARATOR)
        folder = PurePosixPath()
        for depth in range(1, len(parts) + 1):
            prefix = genres.SEPARATOR.join(parts[:depth])
            pl = self.find_playlist(prefix)
            if pl:
                folder = PurePosixPath(pl.folder)
            else:
                folder = folder / parts[depth - 1]
        return folder.as_posix()

    def playlists_of(self, track_id: str) -> list[Playlist]:
        return [pl for pl in self.playlists.values() if track_id in pl.members]

    def playlist_owning_folder(self, rel_path: str) -> Playlist | None:
        """The playlist whose folder directly contains the file."""
        folder = PurePosixPath(rel_path).parent.as_posix().lower()
        for pl in self.playlists.values():
            if pl.folder.lower() == folder:
                return pl
        return None

    # ------------------------------------------------------------------ genres
    def genre_keys(self) -> list[str]:
        keys: dict[str, str] = {}
        for key in self.playlists:
            for k in genres.lineage(key):
                keys.setdefault(k.lower(), k)
        return sorted(keys.values(), key=str.lower)

    def genre_children(self, key: str | None) -> list[str]:
        want = (key or "").lower()
        return [k for k in self.genre_keys() if (genres.parent(k) or "").lower() == want]

    def genre_track_ids(self, key: str) -> list[str]:
        """All tracks of a genre: own playlist first, then descendants (alphabetically)."""
        seen: dict[str, None] = {}
        for pl_key in sorted(self.playlists, key=lambda k: (k.lower() != key.lower(), k.lower())):
            if genres.is_descendant_or_self(pl_key, key):
                for tid in self.playlists[pl_key].members:
                    if tid in self.tracks:
                        seen.setdefault(tid, None)
        return list(seen)

    # ------------------------------------------------------------------ orphans
    def member_ids(self) -> set[str]:
        ids: set[str] = set()
        for pl in self.playlists.values():
            ids.update(pl.members)
        return ids

    def orphans(self) -> list[Track]:
        members = self.member_ids()
        return [t for t in self.tracks.values() if t.id not in members]

    def purge_missing(self) -> int:
        """Forget orphans and duplicates the user deleted from disk."""
        removed = 0
        members = self.member_ids()
        for track in list(self.tracks.values()):
            before = len(track.duplicates)
            track.duplicates = [d for d in track.duplicates if self.abs_path(d).exists()]
            removed += before - len(track.duplicates)
            if track.id not in members and not self.abs_path(track.path).exists():
                del self.tracks[track.id]
                removed += 1
        if removed:
            self.reindex()
        return removed

    def vector_file(self, track_id: str) -> Path:
        return self.root / META_FOLDER / "features" / f"{track_id}.npz"

    def record_move(self, old_rel: str, new_rel: str) -> None:
        # Collapse chains a->b, b->c into a->c.
        for move in self.pending_moves:
            if move.new.lower() == old_rel.lower():
                move.new = new_rel
                return
        self.pending_moves.append(PendingMove(old_rel, new_rel))
