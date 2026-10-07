import json
from urllib.parse import parse_qs, urlparse

import pytest

from conftest import FakeSpotifyServer, connected_api, wait
from djmanager import discover
from djmanager.discover import Deezer, Seed, pick_spotify, suggest
from djmanager.library import SOURCE_LOCAL, SOURCE_SPOTIFY
from djmanager.service import ServiceError
from djmanager.spotify_api import playlist_id


def dz_track(tid, artist, title, aid, isrc=None, duration=200, rank=1000):
    return {"id": tid, "title": title, "title_short": title, "duration": duration, "rank": rank, "readable": True,
            "artist": {"id": aid, "name": artist}, "album": {"title": "Album"}, "isrc": isrc,
            "preview": f"https://cdn.example/{tid}.mp3"}


class FakeDeezer:
    """Deezer's public API: tracks, search, artist radio / related / top."""

    def __init__(self):
        self.tracks = {}   # id -> track
        self.radio = {}    # artist id -> [track ids]
        self.related = {}  # artist id -> [(artist id, name)]
        self.top = {}      # artist id -> [track ids]
        self.calls = []
        self.quota_errors = 0

    def add(self, *tracks):
        for t in tracks:
            self.tracks[t["id"]] = t

    def transport(self, url):
        self.calls.append(url)
        if self.quota_errors:
            self.quota_errors -= 1
            return 200, b'{"error": {"type": "Exception", "message": "Quota limit exceeded", "code": 4}}'
        u = urlparse(url)
        path, q = u.path, parse_qs(u.query)
        no_data = b'{"error": {"type": "DataException", "message": "no data", "code": 800}}'
        if path.startswith("/track/isrc:"):
            hit = next((t for t in self.tracks.values() if t["isrc"] == path[12:]), None)
            return 200, json.dumps(hit).encode() if hit else no_data
        if path.startswith("/track/"):
            hit = self.tracks.get(int(path[7:]))
            return 200, json.dumps(hit).encode() if hit else no_data
        if path == "/search":
            text = q["q"][0].lower()
            hits = [t for t in self.tracks.values() if t["artist"]["name"].lower() in text and t["title"].lower() in text]
            return 200, json.dumps({"data": hits}).encode()
        kind, aid = path.split("/")[-1], int(path.split("/")[2])
        if kind == "radio":
            return 200, json.dumps({"data": [self.tracks[i] for i in self.radio.get(aid, [])]}).encode()
        if kind == "related":
            return 200, json.dumps({"data": [{"id": i, "name": n, "nb_fan": 1} for i, n in self.related.get(aid, [])]}).encode()
        if kind == "top":
            return 200, json.dumps({"data": [self.tracks[i] for i in self.top.get(aid, [])]}).encode()
        return 404, b"{}"


@pytest.fixture
def deezer():
    fake = FakeDeezer()
    # the collection's songs on Deezer
    fake.add(dz_track(1, "Surgeon", "Klonk", 10), dz_track(2, "Phuture", "Acid Tracks", 20, isrc="USAC1"))
    # suggestions
    fake.add(dz_track(101, "Regis", "Speak to Me", 30, isrc="GBREG1", rank=50),
             dz_track(102, "Mike Parker", "Lustre", 31, isrc="USMP1", rank=2000),
             dz_track(103, "DJ Pierre", "Box Energy", 32, isrc="USDP1"),
             dz_track(104, "Ancient Methods", "Knights", 33, isrc="DEAM1", rank=5000))
    fake.radio = {10: [1, 101, 102], 20: [2, 101, 103]}
    fake.related = {10: [(33, "Ancient Methods"), (20, "Phuture")], 20: [(33, "Ancient Methods")]}
    fake.top = {33: [104]}
    return fake


def test_suggestions_rank_songs_shared_by_several_artists_first(deezer):
    seeds = [Seed(["Surgeon"], "Klonk", None, 200), Seed(["Phuture"], "Acid Tracks", "USAC1", 200),
             Seed(["Nobody"], "Unknown Song")]
    known = {("surgeon", "klonk"), ("phuture", "acid tracks")}
    result = suggest(Deezer(deezer.transport), seeds, lambda a, t, d: (a[0].lower(), t.lower()) in known)
    assert result["found"] == ["Surgeon - Klonk", "Phuture - Acid Tracks"] and result["missing"] == ["Nobody - Unknown Song"]
    songs = [(s["artists"], s["score"], s["via"]) for s in result["songs"]]
    assert songs[0] == ("Regis", 2.0, ["Surgeon", "Phuture"])  # in both radios
    # Ancient Methods is related to both selected artists: half weight each
    assert ("Ancient Methods", 1.0, ["Surgeon", "Phuture"]) in songs
    # equal scores: the more popular song first; the collection's own songs are left out
    assert [s[0] for s in songs[1:]] == ["Ancient Methods", "Mike Parker", "DJ Pierre"]
    # the seed found by ISRC needed no search
    assert any("/track/isrc:USAC1" in c for c in deezer.calls)
    assert not any("Acid" in c and "/search" in c for c in deezer.calls)


def test_deezer_waits_when_its_quota_is_reached(deezer):
    deezer.quota_errors = 2
    assert Deezer(deezer.transport, pause=0).track(101)["title"] == "Speak to Me"
    assert len(deezer.calls) == 3


def spotify_track(sid, artist, title, isrc=None, duration=200):
    return {"id": sid.ljust(22, "x")[:22], "name": title, "type": "track", "artists": [{"name": artist}],
            "duration_ms": duration * 1000, "album": {"name": "Album", "id": "al"},
            "external_ids": {"isrc": isrc} if isrc else {}, "popularity": 10}


def test_pick_spotify_prefers_the_same_isrc():
    other = spotify_track("a", "Regis", "Speak to Me", "XX1")
    same = spotify_track("b", "Regis", "Speak to Me (Remastered)", "GBREG1")
    assert pick_spotify([other, same], "GBREG1", ["Regis"], "Speak to Me", 200) is same
    # without the ISRC: same artist, title and length
    assert pick_spotify([other], None, ["Regis"], "Speak to Me", 201) is other
    assert pick_spotify([other], None, ["Regis"], "Speak to Me", 230) is None
    assert pick_spotify([other], None, ["Someone"], "Speak to Me", 200) is None


@pytest.fixture
def discover_env(env, deezer):
    svc, fake, music, nml = env
    server = FakeSpotifyServer()
    svc.spotify = connected_api(svc.settings, server)
    svc.deezer = Deezer(deezer.transport, pause=0)
    server.catalog = [spotify_track("regis", "Regis", "Speak to Me", "GBREG1"),
                      spotify_track("parker", "Mike Parker", "Lustre", None)]
    return svc, fake, server, music, nml


def test_discover_leaves_out_songs_of_the_collection(discover_env):
    svc, *_ = discover_env
    lib = svc.library
    klonk = next(t.id for t in lib.tracks.values() if t.title == "Klonk")
    acid = next(t.id for t in lib.tracks.values() if t.title == "Acid Tracks")
    result = svc.discover("techno", [klonk, acid])
    titles = [s["title"] for s in result["songs"]]
    assert "Speak to Me" in titles and "Klonk" not in titles and "Acid Tracks" not in titles
    assert result["used"] == 2
    with pytest.raises(ServiceError):
        svc.discover("techno", [])


def test_add_to_a_genre_without_link_downloads_the_spotify_songs(discover_env):
    svc, fake, server, music, nml = discover_env
    lib = svc.library
    job = wait(svc.submit_discover_add("techno", ["101", "102", "104"]))
    assert "NOT ON SPOTIFY: Ancient Methods - Knights" in "\n".join(job.log)
    assert "2 songs added, 2 downloaded" in job.result and "1 not on Spotify" in job.result
    regis = next(t for t in lib.tracks.values() if t.title == "Speak to Me")
    assert regis.spotify_id == "regis".ljust(22, "x") and regis.path == "techno/Regis - Speak to Me.mp3"
    assert lib.playlists["techno"].members[regis.id] == SOURCE_LOCAL
    assert (music / regis.path).exists()
    # Lustre has no ISRC on Spotify: found by artist + title + length
    assert any(t.title == "Lustre" for t in lib.tracks.values())
    assert not any(m == "POST" and "/items" in u for m, u in server.calls)  # no playlist to add to


def test_add_to_an_own_linked_playlist_adds_the_songs_on_spotify(discover_env):
    svc, fake, server, music, nml = discover_env
    wait(svc.submit_create_spotify_playlist("techno"))
    pl = svc.library.playlists["techno"]
    pid = playlist_id(pl.spotify_url)
    regis_sid = "regis".ljust(22, "x")
    pl.blacklist[regis_sid] = {"title": "Speak to Me", "artists": ["Regis"]}  # removed earlier
    job = wait(svc.submit_discover_add("techno", ["101"]))
    assert regis_sid in server.playlists[pid]["items"]
    assert regis_sid not in pl.blacklist
    regis = next(t for t in svc.library.tracks.values() if t.title == "Speak to Me")
    assert pl.members[regis.id] == SOURCE_SPOTIFY and (music / regis.path).exists()
    assert "1 downloaded" in job.result
    # adding it again does not add a second copy on Spotify
    wait(svc.submit_discover_add("techno", ["101"]))
    assert server.playlists[pid]["items"].count(regis_sid) == 1


def test_add_to_someone_elses_playlist_keeps_the_songs_local(discover_env):
    svc, fake, server, music, nml = discover_env
    url = "https://open.spotify.com/playlist/FOREIGN"
    server.add_foreign("FOREIGN")
    fake.playlists[url] = []
    wait(svc.submit_edit_link("techno", url))
    job = wait(svc.submit_discover_add("techno", ["101"]))
    assert "not yours" in "\n".join(job.log)
    assert server.playlists["FOREIGN"]["items"] == []
    pl = svc.library.playlists["techno"]
    regis = next(t for t in svc.library.tracks.values() if t.title == "Speak to Me")
    assert pl.members[regis.id] == SOURCE_LOCAL
    wait(svc.submit_sync("techno"))  # the next update keeps it
    assert regis.id in pl.members


def test_failed_downloads_of_added_songs_can_be_retried(discover_env):
    svc, fake, server, music, nml = discover_env
    sid = "regis".ljust(22, "x")
    fake.fail.add(sid)
    wait(svc.submit_discover_add("techno", ["101"]))
    regis = next(t for t in svc.library.tracks.values() if t.title == "Speak to Me")
    assert regis.download_status == "failed" and regis.id in svc.library.playlists["techno"].members
    fake.fail.clear()
    job = wait(svc.submit_retry_downloads("techno"))
    assert "1 downloaded" in job.result and (music / regis.path).exists()


def test_adding_needs_spotify(discover_env):
    svc, *_ = discover_env
    svc.spotify.disconnect()
    with pytest.raises(Exception, match="Spotify"):
        svc.submit_discover_add("techno", ["101"])


def test_preview_is_cached_and_old_ones_are_removed(deezer, tmp_path, monkeypatch):
    monkeypatch.setattr(discover, "PREVIEWS_KEPT", 2)
    fetched = []
    fetch = lambda url: fetched.append(url) or b"mp3"  # noqa: E731
    dz = Deezer(deezer.transport)
    for tid in (101, 102, 101, 103):
        assert discover.cached_preview(dz, str(tid), tmp_path, fetch).read_bytes() == b"mp3"
    assert fetched == ["https://cdn.example/101.mp3", "https://cdn.example/102.mp3", "https://cdn.example/103.mp3"]
    assert len(list(tmp_path.glob("*.mp3"))) == 2
