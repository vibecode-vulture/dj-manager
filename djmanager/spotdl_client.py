"""Thin wrapper around the spotdl CLI running in the managed environment.

Only the CLI and its .spotdl save-file format are used, so spotdl can be updated
independently. Downloads go to a staging folder named by Spotify track id, which makes
the mapping file -> track unambiguous before files are moved into the collection.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .deps import DependencyManager, _no_window
from .settings import Settings
from .util import AUDIO_EXTENSIONS


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

    def _run(self, args: list[str], log, cwd: Path | None = None, timeout: int = 6 * 3600) -> str:
        if not self.deps.is_installed() or not self.deps.installed_versions().get("spotdl"):
            raise SpotdlError("spotdl is not installed - open Dependencies and install it")
        cmd = [str(self.deps.python), "-m", "spotdl", *args]
        shown = " ".join(a if a != self.settings.spotify_client_secret else "***" for a in args)
        log(f"$ spotdl {shown}")
        proc = subprocess.Popen(
            cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "TERM": "dumb", "COLUMNS": "400"}, **_no_window(),
        )
        lines = []
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                lines.append(line)
                log(line)
        code = proc.wait(timeout=timeout)
        if code != 0:
            raise SpotdlError(f"spotdl failed (exit {code}): " + " | ".join(lines[-5:]))
        return "\n".join(lines)

    # ------------------------------------------------------------------ api
    def fetch_playlist(self, url: str, log=print) -> list[RemoteSong]:
        work = paths.work_dir()
        save_file = work / f"fetch-{uuid.uuid4().hex[:8]}.spotdl"
        try:
            self._run(["save", url, "--save-file", str(save_file), "--log-level", "INFO", *self._auth_args()], log)
            if not save_file.exists():
                raise SpotdlError("spotdl did not produce a save file")
            data = json.loads(save_file.read_text(encoding="utf-8"))
        finally:
            save_file.unlink(missing_ok=True)
        songs = [RemoteSong.from_dict(d) for d in data if d.get("song_id")]
        # spotdl may list a song twice if it appears twice in the playlist
        unique: dict[str, RemoteSong] = {}
        for song in songs:
            unique.setdefault(song.spotify_id, song)
        return list(unique.values())

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
            "--log-level", "INFO",
            "--simple-tui",
        ]
        if s.bitrate:
            args += ["--bitrate", s.bitrate]
        if s.cookie_file:
            args += ["--cookie-file", s.cookie_file]
        args += self._auth_args()
        try:
            self._run(args, log)
        except SpotdlError as exc:
            # Partial success is normal (some songs not found on YouTube) - keep what we got.
            log(f"Warning: {exc}")
        result: dict[str, Path] = {}
        for file in staging.iterdir():
            if file.suffix.lower() in AUDIO_EXTENSIONS and file.stem in {x.spotify_id for x in songs}:
                result[file.stem] = file
        return result

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
        out = subprocess.run(
            [str(self.deps.python), "-c", script], capture_output=True, text=True, timeout=600,
            stdin=subprocess.DEVNULL, **_no_window(),
        )
        for line in (out.stdout + out.stderr).splitlines():
            if line.strip():
                log(line)
        for line in out.stdout.splitlines():
            if line.startswith("LOGGED_IN:"):
                return line.split(":", 1)[1]
        raise SpotdlError("Spotify login failed - see log")
