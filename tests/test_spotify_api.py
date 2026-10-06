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
