import xml.etree.ElementTree as ET

import pytest

from conftest import FakeSpotdl, song, wait
from djmanager.backup import BackupManager
from djmanager.deps import DependencyManager
from djmanager.library import SOURCE_LOCAL, SOURCE_SPOTIFY
from djmanager.service import Service
from djmanager.settings import SettingsStore

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


def test_initial_import(env):
    svc, fake, music, nml = env
    lib = svc.library
    assert sorted(lib.playlists) == ["House-Music", "techno", "techno_acid"]
    assert lib.playlists["House-Music"].folder == "House Music"
    assert len(lib.tracks) == 2  # the duplicate is stored once
    acid = lib.match(artists=["Phuture"], title="Acid Tracks")
    # folders are walked alphabetically: the House Music copy is the canonical file
    assert acid.path == "House Music/Phuture - Acid Tracks.mp3"
    assert acid.duplicates == ["techno/acid/Phuture - Acid Tracks.mp3"]
    assert acid.id in lib.playlists["techno_acid"].members
    assert set(lib.playlists["House-Music"].members) == {acid.id}

    pls = traktor_playlists(nml)
    assert set(pls) == {"House-Music", "techno", "techno_acid"}
    assert len(pls["techno"]) == 2  # aggregates the sub genre
    assert len(pls["techno_acid"]) == 1
    assert svc.backups.has_initial()


def test_link_existing_playlist_marks_local(env):
    svc, fake, music, nml = env
    fake.playlists[URL_A] = [song("klonk", "Surgeon", "Klonk"), song("new1", "Blawan", "Getting Me Down")]
    wait(svc.submit_edit_link("techno", URL_A))
    pl = svc.library.playlists["techno"]
    klonk = svc.library.match(artists=["Surgeon"], title="Klonk")
    assert pl.members[klonk.id] == SOURCE_SPOTIFY
    assert klonk.spotify_id.startswith("klonk")
    assert fake.downloaded == ["new1".ljust(22, "x")]  # existing song not downloaded again
    assert (music / "techno" / "Blawan - Getting Me Down.mp3").exists()
    assert len(traktor_playlists(nml)["techno"]) == 3


def test_add_remove_and_blacklist(env):
    svc, fake, music, nml = env
    fake.playlists[URL_B] = [song("acid", "Phuture", "Acid Tracks"), song("n2", "DJ Pierre", "Box Energy")]
    wait(svc.submit_add_playlist("techno_acid_chicago", URL_B))
    lib = svc.library
    pl = lib.playlists["techno_acid_chicago"]
    assert pl.folder == "techno/acid/chicago"
    assert len(pl.members) == 2
    assert fake.downloaded == ["n2".ljust(22, "x")]  # Acid Tracks matched by artist/title

    box = lib.match(artists=["DJ Pierre"], title="Box Energy")
    wait(svc.submit_remove_tracks("techno_acid_chicago", [box.id]))
    assert box.spotify_id in pl.blacklist
    assert box in lib.orphans()
    assert "techno_acid_chicago" in traktor_playlists(nml)
    assert len(traktor_playlists(nml)["techno_acid_chicago"]) == 1

    wait(svc.submit_sync("techno_acid_chicago"))  # blacklisted song stays out
    assert box.id not in pl.members

    wait(svc.submit_unblacklist("techno_acid_chicago", [box.spotify_id]))
    wait(svc.submit_sync("techno_acid_chicago"))
    assert pl.members[box.id] == SOURCE_SPOTIFY
    assert fake.downloaded.count("n2".ljust(22, "x")) == 1  # revived, not re-downloaded

    # removed on Spotify -> removed from the genre
    fake.playlists[URL_B] = [song("acid", "Phuture", "Acid Tracks")]
    wait(svc.submit_sync("techno_acid_chicago"))
    assert box.id not in pl.members


def test_remove_playlist_moves_files(env):
    svc, fake, music, nml = env
    fake.playlists[URL_B] = [song("n2", "DJ Pierre", "Box Energy"), song("acid", "Phuture", "Acid Tracks")]
    wait(svc.submit_add_playlist("techno_acid_chicago", URL_B))
    lib = svc.library
    acid = lib.match(artists=["Phuture"], title="Acid Tracks")
    box = lib.match(artists=["DJ Pierre"], title="Box Energy")

    wait(svc.submit_remove_playlist("techno_acid"))
    assert "techno_acid" not in lib.playlists
    # Acid Tracks lives in House Music -> untouched; its duplicate copy in the removed
    # folder is moved to _removed instead of being deleted
    assert acid.path == "House Music/Phuture - Acid Tracks.mp3"
    assert acid.duplicates == ["_removed/techno_acid/Phuture - Acid Tracks.mp3"]
    assert (music / acid.duplicates[0]).exists()
    assert not any((music / "techno" / "acid").glob("*.mp3"))
    # folder still holds the child genre folder, so it is kept
    assert (music / "techno" / "acid" / "chicago").is_dir()
    assert box.path.startswith("techno/acid/chicago/")

    pls = traktor_playlists(nml)
    # the genre still exists through its sub genre, now only aggregating chicago
    assert pls["techno_acid"] == pls["techno_acid_chicago"]
    assert len(pls["techno_acid_chicago"]) == 2

    # a track that lives in the removed folder but is still in another playlist is moved there
    fake.playlists[URL_A] = [song("n2", "DJ Pierre", "Box Energy")]
    wait(svc.submit_edit_link("techno", URL_A))
    wait(svc.submit_add_playlist("techno_acid_detroit", ""))

    wait(svc.submit_remove_playlist("techno_acid_chicago"))
    assert box.path == "techno/DJ Pierre - Box Energy.mp3"
    assert not (music / "techno" / "acid" / "chicago").exists()
    keys = [k for v in traktor_playlists(nml).values() for k in v]
    assert any(k.endswith("/:techno/:DJ Pierre - Box Energy.mp3") for k in keys)

    wait(svc.submit_remove_tracks("techno", [box.id]))
    wait(svc.submit_remove_playlist("techno"))
    assert box.path == "_removed/techno/DJ Pierre - Box Energy.mp3"
    assert box in lib.orphans()

    # user deletes the orphan manually -> it disappears from the removed list
    (music / box.path).unlink()
    lib.purge_missing()
    assert box.id not in lib.tracks


def test_link_change_keeps_old_songs_as_local(env):
    svc, fake, music, nml = env
    fake.playlists[URL_A] = [song("klonk", "Surgeon", "Klonk")]
    fake.playlists[URL_B] = [song("other", "Blawan", "Why They Hide Their Bodies")]
    wait(svc.submit_edit_link("techno", URL_A))
    wait(svc.submit_edit_link("techno", URL_B))
    pl = svc.library.playlists["techno"]
    sources = {svc.library.tracks[t].title: s for t, s in pl.members.items()}
    assert sources == {"Klonk": SOURCE_LOCAL, "Why They Hide Their Bodies": SOURCE_SPOTIFY}


def test_library_persists(env):
    svc, fake, music, nml = env
    from djmanager.library import Library

    again = Library.load(music)
    assert sorted(again.playlists) == sorted(svc.library.playlists)
    assert len(again.tracks) == len(svc.library.tracks)


def test_stop_ends_update_all_after_current_playlist(env):
    svc, fake, music, nml = env
    lib = svc.library
    lib.playlists["techno"].spotify_url = URL_A
    lib.playlists["techno_acid"].spotify_url = URL_B
    fake.playlists[URL_A] = [song("klonk", "Surgeon", "Klonk"), song("new1", "Blawan", "Getting Me Down")]
    fake.playlists[URL_B] = [song("n2", "DJ Pierre", "Box Energy")]
    fetched = []

    def fetch_then_stop(url, log=print):
        fetched.append(url)
        svc.jobs.cancel(svc.jobs.current.id)  # user presses Stop during the first playlist
        return list(fake.playlists[url])

    fake.fetch_playlist = fetch_then_stop
    job = wait(svc.submit_update_all())
    assert job.status == "cancelled"
    assert len(fetched) == 1  # the second playlist was not started
    first = lib.playlists["techno_acid" if fetched[0] == URL_B else "techno"]
    assert first.last_synced  # work done before Stop is kept
    assert "Stopped after" in job.result
