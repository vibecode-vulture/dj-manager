import pytest

from conftest import FakeSpotifyServer, connected_api
from djmanager.settings import Settings
from djmanager.spotify_api import SpotifyAPI, SpotifyAPIError, playlist_id, to_spotdl_song


def test_requires_client_id_and_login(home):
    api = SpotifyAPI(Settings(), transport=FakeSpotifyServer().transport)
    with pytest.raises(SpotifyAPIError, match="Client ID"):
        api.require()
    api.settings.spotify_client_id = "client"
    with pytest.raises(SpotifyAPIError, match="Connect"):
        api.require()


def test_create_add_and_read_paginated(home):
    server = FakeSpotifyServer()
    api = connected_api(Settings(), server)
    created = api.create_playlist("DJM · techno", "desc")
    assert created["url"].endswith(created["id"])
    assert server.playlists[created["id"]]["public"] is False  # private by default
    ids = [f"{i:022d}" for i in range(230)]
    api.add_tracks(created["id"], ids)  # 3 requests of <= 100
    assert server.playlists[created["id"]]["items"] == ids
    songs = api.playlist_tracks(created["id"])  # 5 pages of 50
    assert [s["song_id"] for s in songs] == ids
    assert songs[0]["duration"] == 200 and songs[0]["album_name"] == "Album"
    assert server.refreshes == 1  # token refreshed once, then reused
    assert api.owner_of(created["id"]) == "dj"


def test_foreign_playlist_items_forbidden(home):
    server = FakeSpotifyServer()
    server.add_foreign("foreign0000000000000000")
    api = connected_api(Settings(), server)
    with pytest.raises(SpotifyAPIError) as err:
        api.playlist_tracks("foreign0000000000000000")
    assert err.value.status == 403


def test_helpers():
    assert playlist_id("https://open.spotify.com/playlist/3z5js7txYknxjc2ZObo3yH?si=x") == "3z5js7txYknxjc2ZObo3yH"
    song = to_spotdl_song({"id": "a" * 22, "name": "T", "artists": [{"name": "A"}, {"name": "B"}], "duration_ms": 61500})
    assert song["artists"] == ["A", "B"] and song["duration"] == 62 and song["url"].endswith("a" * 22)


def test_pkce_login_via_local_callback(home):
    import threading
    import urllib.parse
    import urllib.request

    server = FakeSpotifyServer()
    settings = Settings(spotify_client_id="client")
    api = SpotifyAPI(settings, transport=server.transport)
    lines, result = [], {}
    t = threading.Thread(target=lambda: result.update(name=api.login(lines.append, open_browser=False)))
    t.start()
    while not any(line.startswith("https://accounts.spotify.com/authorize") for line in lines):
        pass
    auth = urllib.parse.parse_qs(urllib.parse.urlparse(lines[-1]).query)
    assert auth["code_challenge_method"] == ["S256"] and auth["redirect_uri"] == ["http://127.0.0.1:9900/"]
    assert "playlist-modify-private" in auth["scope"][0]
    # the browser redirect back to DJ Manager completes the login
    state = auth["state"][0]
    urllib.request.urlopen(f"http://127.0.0.1:9900/?code=abc&state={state}").read()
    t.join(10)
    assert result["name"] == "DJ" and api.connected
    token_body = [c for c in server.calls if "accounts.spotify.com" in c[1]]
    assert token_body  # code exchanged for tokens
    assert SpotifyAPI(settings, transport=server.transport).connected  # persisted


def test_pkce_login_rejects_wrong_state(home):
    import threading
    import urllib.request

    api = SpotifyAPI(Settings(spotify_client_id="client"), transport=FakeSpotifyServer().transport)
    lines, errors = [], []

    def run():
        try:
            api.login(lines.append, open_browser=False)
        except SpotifyAPIError as exc:
            errors.append(str(exc))
    t = threading.Thread(target=run)
    t.start()
    while not any(line.startswith("https://accounts.spotify.com/authorize") for line in lines):
        pass
    urllib.request.urlopen("http://127.0.0.1:9900/?code=abc&state=forged").read()
    t.join(10)
    assert errors and "failed" in errors[0] and not api.connected
