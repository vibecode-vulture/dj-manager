import xml.etree.ElementTree as ET

import pytest

from conftest import FakeSpotifyServer, connected_api, song, wait
from djmanager.library import SOURCE_LOCAL, SOURCE_SPOTIFY
from djmanager.service import ServiceError
from djmanager.spotify_api import playlist_id
from test_service import URL_B, env, traktor_playlists  # noqa: F401 - fixture reuse


@pytest.fixture
def split_env(env):  # noqa: F811
    svc, fake, music, nml = env
    server = FakeSpotifyServer()
    svc.spotify = connected_api(svc.settings, server)
    # techno_acid: linked to someone else's playlist with three songs
    fake.playlists[URL_B] = [song("acid", "Phuture", "Acid Tracks"), song("n2", "DJ Pierre", "Box Energy"),
                             song("n3", "Armando", "Land of Confusion")]
    wait(svc.submit_edit_link("techno_acid", URL_B))
    return svc, fake, server, music, nml


def tid(svc, title):
    return next(t.id for t in svc.library.tracks.values() if t.title == title)


def test_split_creates_two_new_playlists_and_moves_files(split_env):
    svc, fake, server, music, nml = split_env
    lib = svc.library
    # a song added on Spotify since the last update must end up in the new genre playlist
    fake.playlists[URL_B].append(song("n4", "Mr. Fingers", "Washing Machine"))
    box, acid = tid(svc, "Box Energy"), tid(svc, "Acid Tracks")

    job = wait(svc.submit_split("techno_acid", [box, acid], "chicago"))
    assert "techno_acid_chicago created with 2 songs" in job.result

    # Spotify: two new private playlists, named with the prefix; the old one is untouched
    names = {p["name"]: p for p in server.playlists.values()}
    sub_pl, new_pl = names["DJM · techno_acid_chicago"], names["DJM · techno_acid"]
    assert sub_pl["public"] is False and new_pl["public"] is False
    sids = lambda *titles: [lib.tracks[tid(svc, t)].spotify_id for t in titles]  # noqa: E731
    assert sub_pl["items"] == sids("Acid Tracks", "Box Energy")  # playlist order kept
    assert new_pl["items"] == sids("Land of Confusion", "Washing Machine")
    assert not any(m == "DELETE" for m, _ in server.calls)

    # Library: both genres linked to the new playlists
    acid_pl, sub = lib.playlists["techno_acid"], lib.playlists["techno_acid_chicago"]
    assert playlist_id(sub.spotify_url) in server.playlists and playlist_id(acid_pl.spotify_url) in server.playlists
    assert set(sub.members) == {box, acid} and box not in acid_pl.members
    assert sub.folder == "techno/acid/chicago"

    # Files: Box Energy lived in techno/acid -> moves; Acid Tracks lives in House Music -> stays
    assert lib.tracks[box].path == "techno/acid/chicago/DJ Pierre - Box Energy.mp3"
    assert (music / lib.tracks[box].path).exists()
    assert lib.tracks[acid].path == "House Music/Phuture - Acid Tracks.mp3"
    assert acid in lib.playlists["House-Music"].members  # other genre unaffected

    # Traktor: new playlist inside the genre folder, file location updated
    pls = traktor_playlists(nml)
    assert len(pls["techno_acid_chicago"]) == 2
    assert any(k.endswith("/:techno/:acid/:chicago/:DJ Pierre - Box Energy.mp3") for k in pls["techno_acid_chicago"])
    keys = [l.get("DIR") + l.get("FILE") for l in ET.parse(nml).getroot().iter("LOCATION")]
    assert not any(k.endswith("/:techno/:acid/:DJ Pierre - Box Energy.mp3") for k in keys)

    # Later updates of own (private) playlists go through the Web API, not spotdl
    fake.fetch_playlist = lambda *a, **k: pytest.fail("spotdl must not read own playlists")
    server.playlists[playlist_id(acid_pl.spotify_url)]["items"].remove(sids("Washing Machine")[0])
    wait(svc.submit_sync("techno_acid"))
    assert tid(svc, "Washing Machine") not in acid_pl.members


def test_split_keeps_local_songs_local(split_env):
    svc, fake, server, music, nml = split_env
    lib = svc.library
    local = tid(svc, "Klonk")  # imported song without Spotify id, member of techno
    lib.playlists["techno_acid"].members[local] = SOURCE_LOCAL
    wait(svc.submit_split("techno_acid", [local], "dark"))
    sub = lib.playlists["techno_acid_dark"]
    assert sub.members == {local: SOURCE_LOCAL}
    assert server.playlists[playlist_id(sub.spotify_url)]["items"] == []
    assert all(src == SOURCE_SPOTIFY for src in lib.playlists["techno_acid"].members.values())


def test_split_validation(split_env):
    svc, fake, server, music, nml = split_env
    box = tid(svc, "Box Energy")
    with pytest.raises(ServiceError, match="already exists"):
        wait(svc.submit_add_playlist("techno_acid_x", ""))
        svc.submit_split("techno_acid", [box], "x")
    with pytest.raises(ServiceError, match="Select"):
        svc.submit_split("techno_acid", [], "y")
    svc.spotify.disconnect()
    with pytest.raises(Exception, match="Connect"):
        svc.submit_split("techno_acid", [box], "y")


def test_create_spotify_playlist_for_unlinked_genre(split_env):
    svc, fake, server, music, nml = split_env
    house = svc.library.playlists["House-Music"]
    wait(svc.submit_create_spotify_playlist("House-Music"))
    created = server.playlists[playlist_id(house.spotify_url)]
    assert created["name"] == "DJM · House-Music"
    assert created["items"] == [svc.library.tracks[t].spotify_id for t in house.members]


def test_add_playlist_as_new_empty_spotify_playlist(split_env):
    svc, fake, server, music, nml = split_env
    wait(svc.submit_add_playlist("house_ukg", "", create_on_spotify=True))
    pl = svc.library.playlists["house_ukg"]
    assert server.playlists[playlist_id(pl.spotify_url)]["name"] == "DJM · house_ukg"
    assert pl.spotify_owner == "dj"
