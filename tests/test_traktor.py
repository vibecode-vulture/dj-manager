import xml.etree.ElementTree as ET

from djmanager.traktor import Collection, PathMapper, PlaylistNode, TrackMeta

EXISTING = """<?xml version="1.0" encoding="UTF-8" standalone="no" ?>
<NML VERSION="19"><HEAD COMPANY="www.native-instruments.com" PROGRAM="Traktor"></HEAD>
<MUSICFOLDERS></MUSICFOLDERS>
<COLLECTION ENTRIES="1"><ENTRY TITLE="Old" ARTIST="A"><LOCATION DIR="{dir}" FILE="old.mp3" VOLUME="Z:" VOLUMEID="z:"></LOCATION>
<CUE_V2 NAME="Cue 1" TYPE="0" START="1000.0"></CUE_V2></ENTRY></COLLECTION>
<SETS ENTRIES="0"></SETS>
<PLAYLISTS><NODE TYPE="FOLDER" NAME="$ROOT"><SUBNODES COUNT="1">
<NODE TYPE="PLAYLIST" NAME="My own"><PLAYLIST ENTRIES="1" TYPE="LIST" UUID="abc"><ENTRY><PRIMARYKEY TYPE="TRACK" KEY="Z:{dir}old.mp3"></PRIMARYKEY></ENTRY></PLAYLIST></NODE>
</SUBNODES></NODE></PLAYLISTS><INDEXING></INDEXING></NML>"""


def tdir(path):
    return "/:" + "".join(f"{p}/:" for p in path.parts[1:])


def test_wine_mapping(wine, tmp_path):
    prefix, _ = wine
    mapper = PathMapper("wine", prefix)
    loc = mapper.location(tmp_path / "music" / "a b.mp3")
    assert loc.volume == "Z:"
    assert loc.dir == tdir(tmp_path / "music")
    inside = mapper.location(prefix / "drive_c" / "x" / "y.mp3")
    assert inside.volume == "C:" and inside.dir == "/:x/:" and inside.key == "C:/:x/:y.mp3"


def test_collection_roundtrip(wine, tmp_path):
    prefix, nml = wine
    music = tmp_path / "music"
    music.mkdir()
    nml.write_text(EXISTING.format(dir=tdir(music)), encoding="utf-8")
    (music / "new").mkdir()
    new_file = music / "new" / "song.mp3"
    new_file.write_bytes(b"x")

    col = Collection(nml, PathMapper("wine", prefix))
    assert col.ensure_entry(TrackMeta(new_file, "Song", "Artist", "Alb", 123.4))
    assert not col.ensure_entry(TrackMeta(new_file, "Song", "Artist"))
    assert col.move(music / "old.mp3", music / "new" / "old.mp3")
    col.set_managed_tree("DJ Manager", [
        PlaylistNode("techno", [new_file], [PlaylistNode("techno_acid", [new_file])]),
        PlaylistNode("house", []),
    ])
    col.save()

    root = ET.parse(nml).getroot()
    assert root.find("COLLECTION").get("ENTRIES") == "2"
    old = [e for e in root.iter("ENTRY") if e.get("TITLE") == "Old"][0]
    assert old.find("LOCATION").get("DIR") == tdir(music / "new")
    assert old.find("CUE_V2") is not None  # cue points survive moves
    keys = [pk.get("KEY") for pk in root.iter("PRIMARYKEY")]
    assert f"Z:{tdir(music / 'new')}old.mp3" in keys  # user's playlist follows the move
    top = root.find("PLAYLISTS/NODE/SUBNODES")
    names = [n.get("NAME") for n in top.findall("NODE")]
    assert names == ["My own", "DJ Manager"]
    managed = top.findall("NODE")[1]
    techno = managed.find("SUBNODES/NODE")
    assert techno.get("TYPE") == "FOLDER" and techno.get("NAME") == "techno"
    inner = [(n.get("TYPE"), n.get("NAME")) for n in techno.findall("SUBNODES/NODE")]
    assert inner == [("PLAYLIST", "techno"), ("PLAYLIST", "techno_acid")]
    uuid_before = techno.find("SUBNODES/NODE/PLAYLIST").get("UUID")

    # rewriting keeps UUIDs and does not duplicate the managed folder
    col = Collection(nml, PathMapper("wine", prefix))
    col.set_managed_tree("DJ Manager", [PlaylistNode("techno", [new_file], [PlaylistNode("techno_acid", [])])])
    col.save()
    root = ET.parse(nml).getroot()
    top = root.find("PLAYLISTS/NODE/SUBNODES")
    assert [n.get("NAME") for n in top.findall("NODE")] == ["My own", "DJ Manager"]
    assert top.findall("NODE")[1].find("SUBNODES/NODE/SUBNODES/NODE/PLAYLIST").get("UUID") == uuid_before
