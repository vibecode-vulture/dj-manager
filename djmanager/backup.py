"""Backups of the Traktor collection (and DJ Manager's own library state).

The very first backup is taken before DJ Manager ever touches Traktor and is never
pruned. Afterwards a backup is taken before every write to the collection.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from . import paths
from .util import now_iso


@dataclass
class BackupInfo:
    id: str
    created_at: str
    reason: str
    nml_path: str
    has_library: bool
    initial: bool = False


class BackupManager:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or paths.backups_dir()
        self.root.mkdir(parents=True, exist_ok=True)

    def list(self) -> list[BackupInfo]:
        result = []
        for meta in self.root.glob("*/meta.json"):
            try:
                result.append(BackupInfo(**json.loads(meta.read_text(encoding="utf-8"))))
            except (OSError, ValueError, TypeError):
                continue
        return sorted(result, key=lambda b: b.id, reverse=True)

    def has_initial(self) -> bool:
        return any(b.initial for b in self.list())

    def create(self, nml_path: Path, library_file: Path | None, reason: str, initial: bool = False, keep: int = 30) -> BackupInfo | None:
        nml_path = Path(nml_path)
        if not nml_path.exists() and not (library_file and library_file.exists()):
            return None
        backup_id = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        folder = self.root / backup_id
        folder.mkdir(parents=True)
        if nml_path.exists():
            shutil.copy2(nml_path, folder / "collection.nml")
        has_library = bool(library_file and library_file.exists())
        if has_library:
            shutil.copy2(library_file, folder / "library.json")
        info = BackupInfo(id=backup_id, created_at=now_iso(), reason=reason, nml_path=str(nml_path),
                          has_library=has_library, initial=initial)
        (folder / "meta.json").write_text(json.dumps(asdict(info), indent=1), encoding="utf-8")
        self.prune(keep)
        return info

    def prune(self, keep: int) -> None:
        regular = [b for b in self.list() if not b.initial]
        for old in regular[max(1, keep):]:
            shutil.rmtree(self.root / old.id, ignore_errors=True)

    def restore(self, backup_id: str, library_file: Path | None, restore_library: bool = True) -> BackupInfo:
        info = next((b for b in self.list() if b.id == backup_id), None)
        if info is None:
            raise FileNotFoundError("Backup not found")
        folder = self.root / backup_id
        if (folder / "collection.nml").exists():
            target = Path(info.nml_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(folder / "collection.nml", target)
        if restore_library and library_file and (folder / "library.json").exists():
            shutil.copy2(folder / "library.json", library_file)
        return info
