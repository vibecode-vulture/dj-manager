"""Read and write Traktor's collection.nml.

Only two things are changed in the user's library:
  * tracks are added to the COLLECTION (existing entries incl. cue points are kept;
    when DJ Manager moved a file, the entry's LOCATION is updated in place), and
  * the playlist folder owned by DJ Manager (settings.traktor_root_folder) below
    $ROOT is regenerated. All other playlists stay untouched, except that references to
    files DJ Manager moved are updated so they keep working.

Traktor addresses files as VOLUME + DIR + FILE, e.g. VOLUME="C:" DIR="/:Users/:me/:" FILE="a.mp3"
and playlists reference them with the key "C:/:Users/:me/:a.mp3".
"""

from __future__ import annotations

import glob
import os
import subprocess
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PureWindowsPath

from . import paths

XML_HEADER = '<?xml version="1.0" encoding="UTF-8" standalone="no" ?>\n'


class TraktorError(RuntimeError):
    pass


# --------------------------------------------------------------------------- discovery
def _version_key(path: str) -> tuple:
    name = Path(path).parent.name  # "Traktor 3.11.1"
    nums = [int(x) for x in name.replace("Traktor", "").strip().split(".") if x.isdigit()]
    return tuple(nums)


def find_collections(wine_prefix: str = "") -> list[str]:
    """Candidate collection.nml files, newest Traktor version first."""
    patterns = []
    home = Path.home()
    if paths.IS_WINDOWS:
        docs = [home / "Documents", Path(os.environ.get("USERPROFILE", home)) / "Documents"]
        for d in docs:
            patterns.append(str(d / "Native Instruments" / "Traktor*" / "collection.nml"))
    else:
        prefixes = [Path(wine_prefix).expanduser()] if wine_prefix else []
        prefixes += [home / ".wine"] + [Path(p) for p in glob.glob(str(home / ".local/share/wineprefixes/*"))]
        prefixes += [Path(p) for p in glob.glob(str(home / "Games/*"))]  # Lutris default location
        for prefix in prefixes:
            for docs in ("Documents", "My Documents"):
                patterns.append(str(prefix / "drive_c/users/*" / docs / "Native Instruments/Traktor*/collection.nml"))
    found: list[str] = []
    for pattern in patterns:
        for match in glob.glob(pattern):
            if match not in found:
                found.append(match)
    return sorted(found, key=_version_key, reverse=True)


def wine_prefix_of(nml_path: Path) -> Path | None:
    for parent in Path(nml_path).resolve().parents:
        if (parent / "dosdevices").is_dir() or (parent / "drive_c").is_dir():
            return parent
    return None


def traktor_running() -> bool:
    try:
        if paths.IS_WINDOWS:
            out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Traktor.exe"], capture_output=True, text=True,
                                 creationflags=subprocess.CREATE_NO_WINDOW)  # type: ignore[attr-defined]
            return "traktor.exe" in out.stdout.lower()
        for cmdline in glob.glob("/proc/[0-9]*/cmdline"):
            try:
                if b"traktor.exe" in Path(cmdline).read_bytes().lower():
                    return True
            except OSError:
                continue
    except Exception:
        return False
    return False


# --------------------------------------------------------------------------- paths
@dataclass
class TraktorLocation:
    volume: str
    dir: str
    file: str

    @property
    def key(self) -> str:
        return self.volume + self.dir + self.file


class PathMapper:
    """Maps native file paths to Traktor locations (Windows or Wine)."""

    def __init__(self, mode: str = "auto", wine_prefix: Path | None = None) -> None:
        if mode == "auto":
            mode = "native" if paths.IS_WINDOWS else "wine"
        self.mode = mode
        self.drives: list[tuple[str, Path]] = []
        if mode == "wine":
            prefix = wine_prefix or Path.home() / ".wine"
            dosdevices = prefix / "dosdevices"
            if dosdevices.is_dir():
                for entry in dosdevices.iterdir():
                    name = entry.name.lower()
                    if len(name) == 2 and name[1] == ":":
                        try:
                            self.drives.append((name.upper(), entry.resolve()))
                        except OSError:
                            continue
            if not any(d == "Z:" for d, _ in self.drives):
                self.drives.append(("Z:", Path("/")))
            if not any(d == "C:" for d, _ in self.drives) and (prefix / "drive_c").is_dir():
                self.drives.append(("C:", (prefix / "drive_c").resolve()))
            # longest target first so drive_c wins over z: for files inside the prefix
            self.drives.sort(key=lambda d: len(str(d[1])), reverse=True)

    def windows_path(self, path: Path) -> PureWindowsPath:
        if self.mode == "native":
            return PureWindowsPath(os.path.abspath(path))
        resolved = Path(os.path.abspath(path))
        for drive, target in self.drives:
            try:
                rel = resolved.relative_to(target)
            except ValueError:
                continue
            return PureWindowsPath(drive + "\\", *rel.parts)
        raise TraktorError(f"{path} is not reachable from the Wine prefix")

    def location(self, path: Path) -> TraktorLocation:
        win = self.windows_path(path)
        volume = win.drive
        folders = win.parts[1:-1]
        directory = "/:" + "".join(f"{p}/:" for p in folders)
        return TraktorLocation(volume=volume, dir=directory, file=win.name)

    def key(self, path: Path) -> str:
        return self.location(path).key


# --------------------------------------------------------------------------- tree
@dataclass
class PlaylistNode:
    name: str
    track_paths: list[Path] = field(default_factory=list)
    children: list["PlaylistNode"] = field(default_factory=list)


@dataclass
class TrackMeta:
    path: Path
    title: str = ""
    artist: str = ""
    album: str = ""
    duration: float = 0.0


@dataclass
class WriteReport:
    added_entries: int = 0
    moved_entries: int = 0
    playlists: int = 0

    def summary(self) -> str:
        return f"{self.added_entries} tracks added to the Traktor collection, {self.moved_entries} relocated, {self.playlists} playlists written"


# --------------------------------------------------------------------------- collection
class Collection:
    def __init__(self, path: Path, mapper: PathMapper) -> None:
        self.path = Path(path)
        self.mapper = mapper
        if self.path.exists():
            try:
                self.tree = ET.parse(self.path)
            except ET.ParseError as exc:
                raise TraktorError(f"Cannot parse {self.path}: {exc}") from exc
            self.root = self.tree.getroot()
        else:
            self.root = self._skeleton()
            self.tree = ET.ElementTree(self.root)
        if self.root.tag != "NML":
            raise TraktorError(f"{self.path} is not a Traktor collection")
        self.collection = self._child(self.root, "COLLECTION", before="SETS")
        self.entries: dict[str, ET.Element] = {}
        self._volume_ids: dict[str, str] = {}
        for entry in self.collection.findall("ENTRY"):
            loc = entry.find("LOCATION")
            if loc is None:
                continue
            key = (loc.get("VOLUME", "") + loc.get("DIR", "") + loc.get("FILE", "")).lower()
            self.entries[key] = entry
            if loc.get("VOLUMEID"):
                self._volume_ids.setdefault(loc.get("VOLUME", ""), loc.get("VOLUMEID", ""))

    @staticmethod
    def _skeleton() -> ET.Element:
        root = ET.Element("NML", {"VERSION": "19"})
        ET.SubElement(root, "HEAD", {"COMPANY": "www.native-instruments.com", "PROGRAM": "Traktor"})
        ET.SubElement(root, "MUSICFOLDERS")
        ET.SubElement(root, "COLLECTION", {"ENTRIES": "0"})
        ET.SubElement(root, "SETS", {"ENTRIES": "0"})
        playlists = ET.SubElement(root, "PLAYLISTS")
        node = ET.SubElement(playlists, "NODE", {"TYPE": "FOLDER", "NAME": "$ROOT"})
        ET.SubElement(node, "SUBNODES", {"COUNT": "0"})
        ET.SubElement(root, "INDEXING")
        return root

    @staticmethod
    def _child(parent: ET.Element, tag: str, before: str | None = None, attrib: dict | None = None) -> ET.Element:
        found = parent.find(tag)
        if found is not None:
            return found
        element = ET.Element(tag, attrib or {})
        children = list(parent)
        index = next((i for i, c in enumerate(children) if c.tag == before), len(children)) if before else len(children)
        parent.insert(index, element)
        return element

    # ------------------------------------------------------------------ entries
    def has(self, path: Path) -> bool:
        return self.mapper.key(path).lower() in self.entries

    def ensure_entry(self, meta: TrackMeta) -> bool:
        loc = self.mapper.location(meta.path)
        if loc.key.lower() in self.entries:
            return False
        now = datetime.now()
        date = f"{now.year}/{now.month}/{now.day}"
        entry = ET.SubElement(self.collection, "ENTRY", {
            "MODIFIED_DATE": date,
            "MODIFIED_TIME": str(now.hour * 3600 + now.minute * 60 + now.second),
            "TITLE": meta.title or meta.path.stem,
            "ARTIST": meta.artist,
        })
        ET.SubElement(entry, "LOCATION", {
            "DIR": loc.dir, "FILE": loc.file, "VOLUME": loc.volume,
            "VOLUMEID": self._volume_ids.get(loc.volume, loc.volume),
        })
        if meta.album:
            ET.SubElement(entry, "ALBUM", {"TITLE": meta.album})
        ET.SubElement(entry, "MODIFICATION_INFO", {"AUTHOR_TYPE": "user"})
        info = {"IMPORT_DATE": date}
        if meta.duration:
            info["PLAYTIME"] = str(int(round(meta.duration)))
            info["PLAYTIME_FLOAT"] = f"{meta.duration:.6f}"
        try:
            info["FILESIZE"] = str(max(1, meta.path.stat().st_size // 1024))
        except OSError:
            pass
        ET.SubElement(entry, "INFO", info)
        self.entries[loc.key.lower()] = entry
        return True

    def ranking(self, path: Path) -> int:
        """Traktor's rating of a file (0-255, 0 = not rated)."""
        entry = self.entries.get(self.mapper.key(path).lower())
        info = entry.find("INFO") if entry is not None else None
        try:
            return int(info.get("RANKING", "0")) if info is not None else 0
        except ValueError:
            return 0

    def move(self, old_path: Path, new_path: Path) -> bool:
        old_key = self.mapper.key(old_path)
        new_loc = self.mapper.location(new_path)
        entry = self.entries.pop(old_key.lower(), None)
        changed = False
        if entry is not None:
            if new_loc.key.lower() in self.entries:
                self.collection.remove(entry)  # target already known - keep that one
            else:
                loc = entry.find("LOCATION")
                assert loc is not None
                loc.set("DIR", new_loc.dir)
                loc.set("FILE", new_loc.file)
                loc.set("VOLUME", new_loc.volume)
                loc.set("VOLUMEID", self._volume_ids.get(new_loc.volume, loc.get("VOLUMEID") or new_loc.volume))
                self.entries[new_loc.key.lower()] = entry
            changed = True
        for pk in self.root.iter("PRIMARYKEY"):
            if pk.get("KEY", "").lower() == old_key.lower():
                pk.set("KEY", new_loc.key)
                changed = True
        return changed

    # ------------------------------------------------------------------ playlists
    def _root_node(self) -> ET.Element:
        playlists = self._child(self.root, "PLAYLISTS", before="INDEXING")
        node = playlists.find("NODE")
        if node is None:
            node = ET.SubElement(playlists, "NODE", {"TYPE": "FOLDER", "NAME": "$ROOT"})
        self._child(node, "SUBNODES", attrib={"COUNT": "0"})
        return node

    def _existing_uuids(self, element: ET.Element | None) -> dict[str, str]:
        result: dict[str, str] = {}
        if element is None:
            return result
        for node in element.iter("NODE"):
            pl = node.find("PLAYLIST")
            if node.get("TYPE") == "PLAYLIST" and pl is not None and pl.get("UUID"):
                result[node.get("NAME", "")] = pl.get("UUID", "")
        return result

    def set_managed_tree(self, folder_name: str, nodes: list[PlaylistNode]) -> int:
        root = self._root_node()
        subnodes = root.find("SUBNODES")
        assert subnodes is not None
        managed = next((n for n in subnodes.findall("NODE")
                        if n.get("TYPE") == "FOLDER" and n.get("NAME") == folder_name), None)
        uuids = self._existing_uuids(managed)
        if managed is None:
            managed = ET.SubElement(subnodes, "NODE", {"TYPE": "FOLDER", "NAME": folder_name})
        for child in list(managed):
            managed.remove(child)
        count = self._write_nodes(managed, nodes, uuids)
        subnodes.set("COUNT", str(len(subnodes.findall("NODE"))))
        return count

    def _write_nodes(self, folder: ET.Element, nodes: list[PlaylistNode], uuids: dict[str, str]) -> int:
        subnodes = ET.SubElement(folder, "SUBNODES", {"COUNT": "0"})
        written = 0
        for node in nodes:
            if node.children:
                sub = ET.SubElement(subnodes, "NODE", {"TYPE": "FOLDER", "NAME": node.name})
                inner = [PlaylistNode(node.name, node.track_paths)] + node.children
                written += self._write_nodes(sub, inner, uuids)
            else:
                self._write_playlist(subnodes, node, uuids)
                written += 1
        subnodes.set("COUNT", str(len(subnodes.findall("NODE"))))
        return written

    def _write_playlist(self, parent: ET.Element, node: PlaylistNode, uuids: dict[str, str]) -> None:
        el = ET.SubElement(parent, "NODE", {"TYPE": "PLAYLIST", "NAME": node.name})
        playlist = ET.SubElement(el, "PLAYLIST", {
            "ENTRIES": str(len(node.track_paths)), "TYPE": "LIST",
            "UUID": uuids.get(node.name) or uuid.uuid4().hex,
        })
        for path in node.track_paths:
            entry = ET.SubElement(playlist, "ENTRY")
            ET.SubElement(entry, "PRIMARYKEY", {"TYPE": "TRACK", "KEY": self.mapper.key(path)})

    # ------------------------------------------------------------------ save
    def save(self, path: Path | None = None) -> None:
        target = Path(path or self.path)
        self.collection.set("ENTRIES", str(len(self.collection.findall("ENTRY"))))
        ET.indent(self.root, space="")
        body = ET.tostring(self.root, encoding="unicode", short_empty_elements=False)
        tmp = target.with_name(target.name + ".djm-tmp")
        tmp.write_text(XML_HEADER + body, encoding="utf-8")
        os.replace(tmp, target)


def mapper_for(settings, nml_path: Path) -> PathMapper:
    prefix = Path(settings.wine_prefix).expanduser() if settings.wine_prefix else wine_prefix_of(nml_path)
    return PathMapper(settings.traktor_path_mode, prefix)
