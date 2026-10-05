"""Self-update from GitHub releases.

Release assets (built by .github/workflows/release.yml):
  DJManager-<version>-setup.exe      Windows installer  (install mode 'installed')
  DJManager-<version>-portable.exe   Windows portable   (install mode 'portable')
  dj-manager-<version>-linux-x86_64  Linux binary       (install mode 'binary')
  SHA256SUMS.txt                     checksums of all assets

Installed: the new installer runs silently over the existing installation (same AppId)
and starts DJ Manager again. Portable / Linux: the executable is swapped in place and
restarted. Running from source: updates come from git.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from packaging.version import InvalidVersion, Version

from . import __version__, paths
from .deps import child_env

try:  # written by the release build, see packaging/build.py
    from ._build_info import UPDATE_REPO as BUILD_UPDATE_REPO
except ImportError:  # pragma: no cover - source checkout
    BUILD_UPDATE_REPO = ""

ASSET_SUFFIX = {
    "installed": "-setup.exe",
    "portable": "-portable.exe",
    "binary": "-linux-x86_64",
}


class UpdateError(RuntimeError):
    pass


@dataclass
class UpdateInfo:
    current: str
    latest: str = ""
    available: bool = False
    mode: str = ""
    repo: str = ""
    notes: str = ""
    url: str = ""
    asset_name: str = ""
    asset_url: str = ""
    message: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "dj-manager"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def _parse(tag: str) -> Version | None:
    try:
        return Version(tag.lstrip("vV"))
    except InvalidVersion:
        return None


class Updater:
    def __init__(self, settings) -> None:
        self.settings = settings
        self.last: UpdateInfo | None = None
        self._sums_url = ""

    @property
    def repo(self) -> str:
        return (self.settings.update_repo or BUILD_UPDATE_REPO).strip().strip("/")

    def check(self) -> UpdateInfo:
        mode = paths.install_mode()
        info = UpdateInfo(current=__version__, mode=mode, repo=self.repo)
        self._sums_url = ""
        if not info.repo:
            info.message = "No update source configured (Settings > Updates > GitHub repository)"
            self.last = info
            return info
        release = _get_json(f"https://api.github.com/repos/{info.repo}/releases/latest")
        latest = _parse(release.get("tag_name", ""))
        if latest is None:
            raise UpdateError(f"Latest release has an unexpected tag: {release.get('tag_name')!r}")
        info.latest = str(latest)
        info.notes = release.get("body") or ""
        info.url = release.get("html_url") or ""
        info.available = latest > Version(__version__)
        if info.available and mode in ASSET_SUFFIX:
            asset = next((a for a in release.get("assets", []) if a["name"].endswith(ASSET_SUFFIX[mode])), None)
            if asset:
                info.asset_name, info.asset_url = asset["name"], asset["browser_download_url"]
                self._sums_url = next((a["browser_download_url"] for a in release.get("assets", [])
                                       if a["name"] == "SHA256SUMS.txt"), "")
            else:
                info.message = f"Release {latest} has no download for this platform"
        elif info.available:
            info.message = "Running from source: update with 'git pull' and 'pip install -e .'"
        else:
            info.message = "DJ Manager is up to date"
        self.last = info
        return info

    # ------------------------------------------------------------------ apply
    def _download(self, info: UpdateInfo, log) -> Path:
        target_dir = paths.work_dir() / "update"
        shutil.rmtree(target_dir, ignore_errors=True)
        target_dir.mkdir(parents=True)
        target = target_dir / info.asset_name
        log(f"Downloading {info.asset_name} ...")
        req = urllib.request.Request(info.asset_url, headers={"User-Agent": "dj-manager"})
        digest = hashlib.sha256()
        with urllib.request.urlopen(req, timeout=60) as resp, open(target, "wb") as out:
            while chunk := resp.read(1 << 20):
                digest.update(chunk)
                out.write(chunk)
        sums_url = self._sums_url
        if sums_url:
            with urllib.request.urlopen(urllib.request.Request(sums_url, headers={"User-Agent": "dj-manager"}), timeout=20) as resp:
                sums = resp.read().decode("utf-8", "replace")
            expected = next((line.split()[0] for line in sums.splitlines()
                             if line.strip().endswith(info.asset_name)), None)
            if expected and expected.lower() != digest.hexdigest():
                raise UpdateError("Checksum mismatch - the download is corrupt, update aborted")
            log("Checksum verified" if expected else "No checksum listed for this file")
        return target

    def apply(self, log=print) -> str:
        info = self.last if self.last and self.last.available else self.check()
        if not info.available:
            return info.message
        if not info.asset_url:
            raise UpdateError(info.message or "No update available for this installation")
        file = self._download(info, log)
        exe = paths.executable()
        mode = info.mode

        if mode == "installed":
            log("Starting the installer - DJ Manager restarts when it is done")
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            subprocess.Popen([str(file), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"],
                             creationflags=flags, close_fds=True, env=child_env())
        elif mode == "portable":
            old = exe.with_suffix(".old.exe")
            old.unlink(missing_ok=True)
            os.replace(exe, old)  # a running exe can be renamed on Windows, not overwritten
            shutil.move(str(file), str(exe))
            self._restart(exe)
        elif mode == "binary":
            file.chmod(0o755)
            os.replace(file, exe)  # the running inode stays valid on Linux
            self._restart(exe)
        else:
            raise UpdateError("Self-update is only available for packaged builds")
        self._exit_soon()
        return f"Updating to {info.latest} - DJ Manager restarts"

    @staticmethod
    def _restart(exe: Path) -> None:
        rest, skip = [], False
        for arg in sys.argv[1:]:  # keep the user's flags, replace an old --wait-pid
            if skip:
                skip = False
            elif arg == "--wait-pid":
                skip = True
            else:
                rest.append(arg)
        args = [str(exe), "--wait-pid", str(os.getpid()), *rest]
        kwargs: dict = {"close_fds": True, "env": child_env()}
        if paths.IS_WINDOWS:
            kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(args, **kwargs)

    @staticmethod
    def _exit_soon(delay: float = 2.0) -> None:
        # Give the UI time to read the job result, then quit so files/ports are released.
        threading.Thread(target=lambda: (time.sleep(delay), os._exit(0)), daemon=True).start()


def cleanup_after_update() -> None:
    """Remove the previous exe left behind by a portable update."""
    if paths.install_mode() == "portable":
        old = paths.executable().with_suffix(".old.exe")
        for _ in range(10):
            try:
                old.unlink(missing_ok=True)
                return
            except OSError:
                time.sleep(0.5)
