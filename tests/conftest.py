import time
from pathlib import Path

import pytest

from djmanager.spotdl_client import RemoteSong


def song(sid: str, artist: str, title: str, duration: float = 200, isrc: str | None = None) -> RemoteSong:
    sid = sid.ljust(22, "x")[:22]
    raw = {"song_id": sid, "name": title, "artists": [artist], "artist": artist, "album_name": "Album",
           "duration": duration, "isrc": isrc, "url": f"https://open.spotify.com/track/{sid}"}
    return RemoteSong.from_dict(raw)


class FakeSpotdl:
    """Stands in for spotdl: playlists are dicts url -> songs, downloads create files."""

    def __init__(self, staging: Path) -> None:
        self.playlists: dict[str, list[RemoteSong]] = {}
        self.downloaded: list[str] = []
        self.fail: set[str] = set()
        self.staging = staging

    def fetch_playlist(self, url, log=print):
        return list(self.playlists[url])

    def download(self, songs, log=print):
        folder = self.staging / f"staging-{len(self.downloaded)}"
        folder.mkdir(parents=True, exist_ok=True)
        out = {}
        for s in songs:
            if s.spotify_id in self.fail:
                continue
            f = folder / f"{s.spotify_id}.mp3"
            f.write_bytes(b"fake")
            out[s.spotify_id] = f
            self.downloaded.append(s.spotify_id)
        return out

    @staticmethod
    def cleanup_staging(files):
        pass


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("DJMANAGER_HOME", str(tmp_path / "home"))
    return tmp_path


@pytest.fixture
def wine(tmp_path):
    """Fake Wine prefix with z: -> / and a Traktor collection inside drive_c."""
    prefix = tmp_path / "wine"
    (prefix / "dosdevices").mkdir(parents=True)
    (prefix / "drive_c").mkdir()
    (prefix / "dosdevices" / "c:").symlink_to(prefix / "drive_c")
    (prefix / "dosdevices" / "z:").symlink_to("/")
    nml_dir = prefix / "drive_c/users/me/Documents/Native Instruments/Traktor 3.11.1"
    nml_dir.mkdir(parents=True)
    return prefix, nml_dir / "collection.nml"


def wait(job, timeout=20):
    end = time.time() + timeout
    while job.status in ("queued", "running"):
        if time.time() > end:
            raise TimeoutError(job.title)
        time.sleep(0.02)
    if job.status == "failed":
        raise AssertionError(f"{job.title} failed: {job.error}\n" + "\n".join(job.log[-20:]))
    return job
