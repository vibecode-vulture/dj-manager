"""Cleaning up duplicates - the only place DJ Manager removes files. Uses real MP3s."""

import os
import shutil
import subprocess
import xml.etree.ElementTree as ET

import pytest

from conftest import URL_B, FakeSpotdl, NoDeps, song, traktor_playlists, wait
from djmanager.backup import BackupManager
from djmanager.service import Service
from djmanager.settings import SettingsStore

FFMPEG = os.environ.get("DJM_FFMPEG") or shutil.which("ffmpeg") or os.path.expanduser("~/.config/spotdl/ffmpeg")
pytestmark = pytest.mark.skipif(not os.path.exists(FFMPEG), reason="ffmpeg needed to create real mp3 files")
SID_A = "A" * 22


def mp3(path, seconds=20, bitrate="128k", freq=440, spotify_id=None, title=None, artist=None):
    from mutagen.id3 import ID3, TIT2, TPE1, WOAS

    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=f={freq}:d={seconds}", "-b:a", bitrate,
                    str(path)], check=True)
    if spotify_id or title:
        tags = ID3()
        if title:
            tags.add(TIT2(encoding=3, text=title))
            tags.add(TPE1(encoding=3, text=artist))
        if spotify_id:
            tags.add(WOAS(url=f"https://open.spotify.com/track/{spotify_id}"))
        tags.save(path)


@pytest.fixture
def dupes(home, wine, tmp_path, monkeypatch):
    prefix, nml = wine
    music = tmp_path / "music"
    # Song A: same Spotify id in two genres, the copy in g2 has the better quality
    mp3(music / "g1" / "Artist A - Song A.mp3", bitrate="128k", spotify_id=SID_A, title="Song A", artist="Artist A")
    mp3(music / "g2" / "Artist A - Song A.mp3", bitrate="320k", spotify_id=SID_A, title="Song A", artist="Artist A")
    # Song B: byte-identical copies, no ids
    mp3(music / "g1" / "Artist B - Song B.mp3", freq=550)
    (music / "g3").mkdir()
    shutil.copy2(music / "g1" / "Artist B - Song B.mp3", music / "g3" / "Artist B - Song B.mp3")
    # Song C: same name, slightly different length - maybe another version -> uncertain
    mp3(music / "g1" / "Artist C - Song C.mp3", seconds=20, freq=660)
    mp3(music / "g2" / "Artist C - Song C.mp3", seconds=22, freq=670)
    # Song D: the "copy" is a hard link to the same file - must never be deleted
    mp3(music / "g1" / "Artist D - Song D.mp3", freq=770)
    os.link(music / "g1" / "Artist D - Song D.mp3", music / "g3" / "Artist D - Song D.mp3")

    settings = SettingsStore()
    settings.update({"traktor_nml": str(nml), "traktor_path_mode": "wine", "wine_prefix": str(prefix)})
    svc = Service(settings=settings, deps=NoDeps(), backups=BackupManager(tmp_path / "backups"),
                  spotdl=FakeSpotdl(tmp_path / "staging"))
    wait(svc.set_music_folder(str(music)))
    trash = tmp_path / "trash"
    trash.mkdir()
    trashed = []

    def fake_trash(path):  # never the real trash in tests
        target = trash / f"{len(trashed)}-{path.name}"
        shutil.move(str(path), str(target))
        trashed.append(str(path))

    monkeypatch.setattr("djmanager.dedupe.move_to_trash", fake_trash)
    return svc, music, nml, trashed


def dedupe_module():
    from djmanager import dedupe
    return dedupe


def track(svc, title):
    return next(t for t in svc.library.tracks.values() if t.title == title)


def report_group(svc, title):
    return next(g for g in svc.duplicate_report()["groups"] if g["title"] == title)


def test_report_classifies_and_picks_the_better_copy(dupes):
    svc, music, nml, trashed = dupes
    a = report_group(svc, "Song A")
    assert a["keep"]["path"] == "g2/Artist A - Song A.mp3" and "quality" in a["reason"]
    assert [c["evidence"] for c in a["remove"]] == ["the file DJ Manager used so far"]
    b = report_group(svc, "Song B")
    assert b["remove"][0]["evidence"] == "identical file"
    c = report_group(svc, "Song C")
    assert not c["remove"] and c["uncertain"] and "same artist and title" in c["uncertain"][0]["evidence"]
    assert trashed == []  # the report changes nothing


def test_cleanup_keeps_one_file_and_every_genre_reference(dupes):
    svc, music, nml, trashed = dupes
    lib = svc.library
    # the user's own Traktor playlist points to the copy that will be removed
    tree = ET.parse(nml)
    root_node = tree.getroot().find("PLAYLISTS/NODE/SUBNODES")
    own = ET.SubElement(root_node, "NODE", {"TYPE": "PLAYLIST", "NAME": "My set"})
    pl = ET.SubElement(own, "PLAYLIST", {"ENTRIES": "1", "TYPE": "LIST", "UUID": "u1"})
    from djmanager.traktor import mapper_for
    mapper = mapper_for(svc.settings, nml)
    ET.SubElement(ET.SubElement(pl, "ENTRY"), "PRIMARYKEY", {"TYPE": "TRACK", "KEY": mapper.key(music / "g1" / "Artist A - Song A.mp3")})
    tree.write(nml)

    wait(svc.submit_clean_duplicates())
    a, b, c, d = (track(svc, t) for t in ("Song A", "Song B", "Song C", "Song D"))
    # exactly one file per song on disk, the kept one
    assert a.path == "g2/Artist A - Song A.mp3" and lib.has_file(a)
    assert not (music / "g1" / "Artist A - Song A.mp3").exists()
    assert sum((music / g / "Artist B - Song B.mp3").exists() for g in ("g1", "g3")) == 1 and lib.has_file(b)
    assert (music / "g1" / "Artist C - Song C.mp3").exists() and (music / "g2" / "Artist C - Song C.mp3").exists()
    assert (music / "g1" / "Artist D - Song D.mp3").exists() and (music / "g3" / "Artist D - Song D.mp3").exists()
    assert sorted(p.split("/")[-1] for p in trashed) == ["Artist A - Song A.mp3", "Artist B - Song B.mp3"]
    # still in every genre where a copy was, and shown as stored elsewhere there
    assert {p.key for p in lib.playlists_of(a.id)} == {"g1", "g2"}
    from djmanager.api import stored_elsewhere
    assert stored_elsewhere(lib, a, "g1") == "g2" and stored_elsewhere(lib, a, "g2") == ""
    # Traktor: genre playlists and the user's own playlist point to the kept file
    pls = traktor_playlists(nml)
    kept_key = mapper.key(music / a.path)
    assert kept_key in pls["g1"] and kept_key in pls["g2"]
    keys = [pk.get("KEY") for pk in ET.parse(nml).getroot().iter("PRIMARYKEY")]
    assert mapper.key(music / "g1" / "Artist A - Song A.mp3") not in keys
    my_set = [n for n in ET.parse(nml).getroot().iter("NODE") if n.get("NAME") == "My set"][0]
    assert [pk.get("KEY") for pk in my_set.iter("PRIMARYKEY")] == [kept_key]
    # the song is not downloaded again when its playlist is updated
    fake = svc.spotdl
    fake.playlists[URL_B] = [song(SID_A, "Artist A", "Song A")]
    lib.playlists["g1"].spotify_url = URL_B
    wait(svc.submit_sync("g1"))
    assert fake.downloaded == []


def test_uncertain_copies_only_when_selected(dupes):
    svc, music, nml, trashed = dupes
    c = report_group(svc, "Song C")
    wait(svc.submit_clean_duplicates([c["track_id"]], uncertain=[c["uncertain"][0]["path"]]))
    remaining = [g for g in ("g1", "g2") if (music / g / "Artist C - Song C.mp3").exists()]
    assert len(remaining) == 1 and svc.library.has_file(track(svc, "Song C"))


def test_copy_with_traktor_cues_wins_over_quality(dupes):
    svc, music, nml, trashed = dupes
    from djmanager.traktor import mapper_for
    mapper = mapper_for(svc.settings, nml)
    tree = ET.parse(nml)
    worse = mapper.key(music / "g1" / "Artist A - Song A.mp3").lower()
    for entry in tree.getroot().iter("ENTRY"):
        loc = entry.find("LOCATION")
        if loc is not None and (loc.get("VOLUME") + loc.get("DIR") + loc.get("FILE")).lower() == worse:
            ET.SubElement(entry, "CUE_V2", {"NAME": "Drop", "TYPE": "0", "START": "30000"})
            ET.SubElement(entry, "CUE_V2", {"NAME": "Break", "TYPE": "0", "START": "60000"})
    tree.write(nml)
    a = report_group(svc, "Song A")
    assert a["keep"]["path"] == "g1/Artist A - Song A.mp3" and "Traktor" in a["reason"]
    wait(svc.submit_clean_duplicates([a["track_id"]]))
    assert (music / "g1" / "Artist A - Song A.mp3").exists() and not (music / "g2" / "Artist A - Song A.mp3").exists()
    cues = [e for e in ET.parse(nml).getroot().iter("ENTRY") if e.findall("CUE_V2")]
    assert len(cues) == 1 and len(cues[0].findall("CUE_V2")) == 2  # cue points still there


def test_failed_trash_keeps_the_file(dupes, monkeypatch):
    svc, music, nml, trashed = dupes

    def broken(path):
        raise OSError("no trash on this drive")

    monkeypatch.setattr(dedupe_module(), "move_to_trash", broken)
    b = report_group(svc, "Song B")
    job = wait(svc.submit_clean_duplicates([b["track_id"]]))
    assert "1 skipped" in job.result
    assert (music / "g1" / "Artist B - Song B.mp3").exists() and (music / "g3" / "Artist B - Song B.mp3").exists()
    assert track(svc, "Song B").duplicates  # still known as a duplicate
