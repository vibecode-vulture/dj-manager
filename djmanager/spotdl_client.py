"""Thin wrapper around spotdl running in the managed environment.

Downloads use the CLI with a .spotdl save file. Downloads go to a staging folder named by
Spotify track id, which makes the mapping file -> track unambiguous before files are
moved into the collection.

Fetching a playlist uses spotdl's playlist listing directly (FETCH_SCRIPT): `spotdl save`
additionally re-fetches every song one by one (seconds per song, no output), which made
large playlists look stuck. If a future spotdl changes that internal function, fetching
falls back to `spotdl save`.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .audio import read_info
from .deps import DependencyManager, child_env
from .jobs import JobCancelled, current_job
from .procs import release, spawn
from .settings import Settings
from .util import AUDIO_EXTENSIONS

UNSUPPORTED_EXIT = 3
FETCH_SCRIPT = r"""
import json, sys
cfg = json.loads(sys.argv[1])
try:
    from spotdl.utils.config import DEFAULT_CONFIG
    from spotdl.utils.search import get_simple_songs
    from spotdl.utils.spotify import SpotifyClient
except Exception as exc:
    print("DJM_UNSUPPORTED: " + repr(exc), flush=True)
    sys.exit(3)
SpotifyClient.init(
    client_id=cfg["client_id"] or DEFAULT_CONFIG["client_id"],
    client_secret=cfg["client_secret"] or DEFAULT_CONFIG["client_secret"],
    user_auth=cfg["user_auth"], headless=True,
)
print("Reading playlist from Spotify ...", flush=True)
songs = get_simple_songs([cfg["url"]])
with open(cfg["out"], "w", encoding="utf-8") as handle:
    json.dump([song.json for song in songs], handle, ensure_ascii=False)
print(f"Found {len(songs)} songs", flush=True)
"""
PROGRESS = re.compile(r"(\d+)/(\d+) complete")


class SpotdlError(RuntimeError):
    pass


@dataclass
class RemoteSong:
    spotify_id: str
    title: str
    artists: list[str]
    album: str
    duration: float
    isrc: str | None
    url: str
    raw: dict

    @classmethod
    def from_dict(cls, data: dict) -> "RemoteSong":
        return cls(
            spotify_id=data.get("song_id") or "",
            title=data.get("name") or "",
            artists=list(data.get("artists") or ([data["artist"]] if data.get("artist") else [])),
            album=data.get("album_name") or "",
            duration=float(data.get("duration") or 0),
            isrc=data.get("isrc"),
            url=data.get("url") or "",
            raw=data,
        )


def unique_songs(data: list[dict]) -> list[RemoteSong]:
    """Songs of a playlist in order; a song listed twice is kept once."""
    unique: dict[str, RemoteSong] = {}
    for song in (RemoteSong.from_dict(d) for d in data if d.get("song_id")):
        unique.setdefault(song.spotify_id, song)
    return list(unique.values())


class SpotdlClient:
    def __init__(self, deps: DependencyManager, settings: Settings) -> None:
        self.deps = deps
        self.settings = settings

    # ------------------------------------------------------------------ helpers
    def _auth_args(self) -> list[str]:
        s = self.settings
        args: list[str] = []
        if s.spotify_auth_mode == "custom" and s.spotify_client_id and s.spotify_client_secret:
            args += ["--client-id", s.spotify_client_id, "--client-secret", s.spotify_client_secret]
        if s.spotify_user_auth:
            args += ["--user-auth", "--headless"]
        return args

    def _require(self) -> None:
        if not self.deps.is_installed() or not self.deps.installed_versions().get("spotdl"):
            raise SpotdlError("spotdl is not installed - open Dependencies and install it")

    def _stream(self, cmd: list[str], log, on_line=None) -> tuple[int, list[str]]:
        """Run a stoppable child process and forward its output line by line."""
        proc = spawn(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            env=child_env(PYTHONIOENCODING="utf-8", TERM="dumb", COLUMNS="400"),
        )
        lines: list[str] = []
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    lines.append(line)
                    log(line)
                    if on_line:
                        on_line(line)
            code = proc.wait()
        finally:
            release(proc)
        job = current_job.get()
        if job is not None and job.cancel_requested:
            raise JobCancelled()
        return code, lines

    def _run(self, args: list[str], log, on_line=None) -> str:
        self._require()
        shown = " ".join(a if a != self.settings.spotify_client_secret else "***" for a in args)
        log(f"$ spotdl {shown}")
        code, lines = self._stream([str(self.deps.python), "-m", "spotdl", *args], log, on_line)
        if code != 0:
            raise SpotdlError(f"spotdl failed (exit {code}): " + " | ".join(lines[-5:]))
        return "\n".join(lines)

    # ------------------------------------------------------------------ api
    def fetch_playlist(self, url: str, log=print) -> list[RemoteSong]:
        self._require()
        save_file = paths.work_dir() / f"fetch-{uuid.uuid4().hex[:8]}.spotdl"
        try:
            if not self._fetch_fast(url, save_file, log):
                log("Falling back to 'spotdl save' (slow for large playlists)")
                self._run(["save", url, "--save-file", str(save_file), "--lyrics",
                           "--threads", "8", "--log-level", "INFO", *self._auth_args()], log)
            if not save_file.exists():
                raise SpotdlError("spotdl did not produce a save file")
            data = json.loads(save_file.read_text(encoding="utf-8"))
        finally:
            save_file.unlink(missing_ok=True)
        return unique_songs(data)

    def _fetch_fast(self, url: str, save_file: Path, log) -> bool:
        """List the playlist only (no per-song refetch). False = not supported by this spotdl."""
        s = self.settings
        custom = s.spotify_auth_mode == "custom" and s.spotify_client_id and s.spotify_client_secret
        cfg = {
            "url": url, "out": str(save_file), "user_auth": bool(s.spotify_user_auth),
            "client_id": s.spotify_client_id if custom else "",
            "client_secret": s.spotify_client_secret if custom else "",
        }
        log(f"Fetching {url}")
        code, lines = self._stream([str(self.deps.python), "-c", FETCH_SCRIPT, json.dumps(cfg)], log)
        if code == UNSUPPORTED_EXIT and any(line.startswith("DJM_UNSUPPORTED") for line in lines):
            return False
        if code != 0:
            raise SpotdlError(f"Fetching the playlist failed (exit {code}): " + " | ".join(lines[-5:]))
        return True

    def download(self, songs: list[RemoteSong], log=print) -> dict[str, Path]:
        """Download songs into a fresh staging folder. Returns spotify id -> file."""
        if not songs:
            return {}
        staging = paths.work_dir() / f"staging-{uuid.uuid4().hex[:8]}"
        staging.mkdir(parents=True)
        save_file = staging / "songs.spotdl"
        save_file.write_text(json.dumps([s.raw for s in songs], ensure_ascii=False), encoding="utf-8")
        s = self.settings
        args = [
            "download", str(save_file),
            "--output", str(staging / "{track-id}.{output-ext}"),
            "--format", s.audio_format or "mp3",
            "--threads", str(max(1, s.download_threads)),
            "--overwrite", "skip",
            "--lyrics",  # no lyrics lookups: slow and not needed for DJing
            "--log-level", "INFO",
            "--simple-tui",
        ]
        if s.bitrate:
            args += ["--bitrate", s.bitrate]
        if s.cookie_file:
            args += ["--cookie-file", s.cookie_file]
        args += self._auth_args()
        job = current_job.get()

        def progress(line: str) -> None:
            match = PROGRESS.search(line)
            if match and job is not None:
                job.progress = int(match.group(1)) / max(1, int(match.group(2)))

        cancelled = False
        try:
            self._run(args, log, progress)
        except JobCancelled:
            cancelled = True  # keep the songs that finished before Stop
        except SpotdlError as exc:
            # Partial success is normal (some songs not found on YouTube) - keep what we got.
            log(f"Warning: {exc}")
        by_id = {x.spotify_id: x for x in songs}
        result: dict[str, Path] = {}
        for file in staging.iterdir():
            song = by_id.get(file.stem)
            if file.suffix.lower() not in AUDIO_EXTENSIONS or song is None:
                continue
            if self._complete(file, song):
                result[file.stem] = file
            else:
                log(f"Ignoring incomplete download: {', '.join(song.artists)} - {song.title}")
        if cancelled:
            log(f"Stopped - keeping {len(result)} finished downloads")
        return result

    @staticmethod
    def _complete(file: Path, song: RemoteSong) -> bool:
        """A download interrupted by Stop can leave a truncated file behind."""
        duration = read_info(file).duration
        if not duration:
            return False
        return not song.duration or duration >= song.duration * 0.9

    @staticmethod
    def cleanup_staging(files: dict[str, Path]) -> None:
        dirs = {f.parent for f in files.values()}
        for d in dirs:
            if d.name.startswith("staging-"):
                shutil.rmtree(d, ignore_errors=True)

    def login(self, log=print) -> str:
        """Interactive OAuth login; the token is cached by spotdl for later --user-auth runs."""
        s = self.settings
        if s.spotify_auth_mode == "custom" and s.spotify_client_id and s.spotify_client_secret:
            creds = f"cid, secret = {s.spotify_client_id!r}, {s.spotify_client_secret!r}"
        else:
            creds = "from spotdl.utils.config import DEFAULT_CONFIG as C\ncid, secret = C['client_id'], C['client_secret']"
        script = (
            f"{creds}\n"
            "from spotdl.utils.spotify import SpotifyClient\n"
            "c = SpotifyClient.init(client_id=cid, client_secret=secret, user_auth=True)\n"
            "u = c.current_user()\n"
            "print('LOGGED_IN:' + (u.get('display_name') or u.get('id') or ''))\n"
        )
        self._require()
        _, lines = self._stream([str(self.deps.python), "-c", script], log)
        for line in lines:
            if line.startswith("LOGGED_IN:"):
                return line.split(":", 1)[1]
        raise SpotdlError("Spotify login failed - see log")
