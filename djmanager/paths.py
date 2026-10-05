"""Platform specific application directories."""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "DJManager"
IS_WINDOWS = sys.platform.startswith("win")


INSTALLED_MARKER = "installed.marker"  # written next to the exe by the Windows installer
PORTABLE_DATA = "DJManager-data"


def executable() -> Path:
    return Path(sys.executable).resolve()


def install_mode() -> str:
    """'source', 'installed' (Windows installer), 'portable' (Windows exe) or 'binary' (Linux)."""
    if not getattr(sys, "frozen", False):
        return "source"
    if IS_WINDOWS:
        return "installed" if (executable().parent / INSTALLED_MARKER).exists() else "portable"
    return "binary"


def _override() -> Path | None:
    # Tests and portable setups keep everything in one place.
    value = os.environ.get("DJMANAGER_HOME")
    if value:
        return Path(value)
    if install_mode() == "portable":
        return executable().parent / PORTABLE_DATA
    return None


def config_dir() -> Path:
    base = _override()
    if base:
        path = base / "config"
    elif IS_WINDOWS:
        path = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / APP_NAME
    else:
        path = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "dj-manager"
    path.mkdir(parents=True, exist_ok=True)
    return path


def data_dir() -> Path:
    base = _override()
    if base:
        path = base / "data"
    elif IS_WINDOWS:
        path = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP_NAME
    else:
        path = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "dj-manager"
    path.mkdir(parents=True, exist_ok=True)
    return path


def backups_dir() -> Path:
    path = data_dir() / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def deps_dir() -> Path:
    path = data_dir() / "deps"
    path.mkdir(parents=True, exist_ok=True)
    return path


def work_dir() -> Path:
    """Scratch space for spotdl save files and download staging."""
    path = data_dir() / "work"
    path.mkdir(parents=True, exist_ok=True)
    return path
