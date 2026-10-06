import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from djmanager.backup import BackupManager
from djmanager.deps import DependencyManager
from djmanager.service import Service
from djmanager.settings import SettingsStore
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
        self.fail: set[str] = set()          # technical error on every attempt
        self.unavailable: set[str] = set()   # "not found on YouTube"
        self.flaky: dict[str, int] = {}      # spotify id -> attempts that fail before it works
        self.attempts: dict[str, int] = {}
        self.last_unavailable: dict[str, str] = {}
        self.staging = staging

    def fetch_playlist(self, url, log=print):
        return list(self.playlists[url])

    def download(self, songs, log=print):
        folder = self.staging / f"staging-{len(self.downloaded)}"
        folder.mkdir(parents=True, exist_ok=True)
        out = {}
        self.last_unavailable = {}
        for s in songs:
            self.attempts[s.spotify_id] = self.attempts.get(s.spotify_id, 0) + 1
            if s.spotify_id in self.unavailable:
                self.last_unavailable[s.spotify_id] = "not found on YouTube"
                continue
            if s.spotify_id in self.fail:
                continue
            if self.flaky.get(s.spotify_id, 0) >= self.attempts[s.spotify_id]:
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


class FakeSpotifyServer:
    """In-memory Spotify Web API (token, /me, create playlist, add/read items)."""

    def __init__(self, user="dj"):
        self.user = user
        self.playlists = {}  # id -> {"name", "owner", "public", "items": [track ids]}
        self.tracks = {}     # id -> track object
        self.calls = []
        self.refreshes = 0

    def add_foreign(self, pid, owner="someone"):
        self.playlists[pid] = {"name": "foreign", "owner": owner, "public": True, "items": []}

    def transport(self, method, url, headers, body):
        import json as _json
        import re as _re
        from urllib.parse import parse_qs, urlparse
        self.calls.append((method, url))
        u = urlparse(url)
        if url.startswith("https://accounts.spotify.com/api/token"):
            self.refreshes += 1
            return 200, {}, _json.dumps({"access_token": f"tok{self.refreshes}", "expires_in": 3600,
                                          "refresh_token": "refresh"}).encode()
        data = _json.loads(body) if body else {}
        if u.path == "/v1/me":
            return 200, {}, _json.dumps({"id": self.user, "display_name": "DJ"}).encode()
        if u.path == "/v1/me/playlists" and method == "POST":
            pid = f"pl{len(self.playlists):020d}"[:22]
            self.playlists[pid] = {"name": data["name"], "owner": self.user, "public": data.get("public"), "items": []}
            return 201, {}, _json.dumps({"id": pid, "external_urls": {"spotify": f"https://open.spotify.com/playlist/{pid}"}}).encode()
        m = _re.fullmatch(r"/v1/playlists/(\w+)(/items)?", u.path)
        if m:
            pl = self.playlists.get(m.group(1))
            if pl is None:
                return 404, {}, b'{"error": {"message": "not found"}}'
            if not m.group(2):
                return 200, {}, _json.dumps({"owner": {"id": pl["owner"]}}).encode()
            if method == "POST":
                assert pl["owner"] == self.user and len(data["uris"]) <= 100
                pl["items"] += [x.split(":")[-1] for x in data["uris"]]
                return 201, {}, b'{"snapshot_id": "x"}'
            if pl["owner"] != self.user:
                return 403, {}, b'{"error": {"message": "Forbidden"}}'
            q = parse_qs(u.query)
            offset, limit = int(q.get("offset", ["0"])[0]), int(q.get("limit", ["50"])[0])
            ids = pl["items"][offset:offset + limit]
            nxt = f"https://api.spotify.com/v1/playlists/{m.group(1)}/items?offset={offset + limit}&limit={limit}" \
                if offset + limit < len(pl["items"]) else None
            items = [{"item": self.tracks.get(i, {"id": i, "name": f"Song {i}", "type": "track",
                                                  "artists": [{"name": "Artist"}], "duration_ms": 200000,
                                                  "album": {"name": "Album", "id": "al"}})} for i in ids]
            return 200, {}, _json.dumps({"items": items, "next": nxt}).encode()
        return 404, {}, b'{"error": {"message": "unknown endpoint"}}'


def connected_api(settings, server):
    from djmanager.spotify_api import SpotifyAPI
    settings.spotify_client_id = "client"
    api = SpotifyAPI(settings, transport=server.transport)
    api.account.client_id, api.account.refresh_token, api.account.user_id = "client", "refresh", server.user
    return api


# ---------------------------------------------------------------- shared service fixture
URL_A = "https://open.spotify.com/playlist/AAAA"
URL_B = "https://open.spotify.com/playlist/BBBB"


class NoDeps(DependencyManager):
    def mark_current(self, status):
        pass


@pytest.fixture
def env(home, wine, tmp_path):
    prefix, nml = wine
    music = tmp_path / "music"
    (music / "techno" / "acid").mkdir(parents=True)
    (music / "House Music").mkdir(parents=True)
    (music / "techno" / "acid" / "Phuture - Acid Tracks.mp3").write_bytes(b"x")
    (music / "techno" / "Surgeon - Klonk.mp3").write_bytes(b"x")
    (music / "House Music" / "Phuture - Acid Tracks.mp3").write_bytes(b"x")  # duplicate copy

    settings = SettingsStore()
    settings.update({"traktor_nml": str(nml), "traktor_path_mode": "wine", "wine_prefix": str(prefix)})
    fake = FakeSpotdl(tmp_path / "staging")
    svc = Service(settings=settings, deps=NoDeps(), backups=BackupManager(tmp_path / "backups"), spotdl=fake)
    wait(svc.set_music_folder(str(music)))
    return svc, fake, music, nml


def traktor_playlists(nml):
    root = ET.parse(nml).getroot()
    managed = [n for n in root.find("PLAYLISTS/NODE/SUBNODES") if n.get("NAME") == "DJ Manager"][0]
    result = {}
    for node in managed.iter("NODE"):
        if node.get("TYPE") == "PLAYLIST":
            result[node.get("NAME")] = [pk.get("KEY") for pk in node.iter("PRIMARYKEY")]
    return result


