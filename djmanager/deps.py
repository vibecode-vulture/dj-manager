"""Managed runtime for fast-moving dependencies (spotdl, yt-dlp).

They live in their own virtual environment inside the data directory, so they can be
updated at runtime without touching DJ Manager itself. Before every change the exact
package set (pip freeze) is snapshotted; any snapshot can be restored. After a
successful playlist sync the current snapshot is marked as known-good.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from packaging.version import InvalidVersion, Version

from . import paths
from .jobs import JobCancelled, current_job
from .procs import release, spawn
from .util import atomic_write_text, download, now_iso, urlopen

# yt-dlp needs a JavaScript runtime (Deno) and its challenge scripts (yt-dlp-ejs) for
# YouTube since late 2025; Deno is distributed officially on PyPI.
MANAGED_PACKAGES = ["spotdl", "yt-dlp", "yt-dlp-ejs", "deno"]
# Installed on demand for BPM/key analysis (see analysis.py)
OPTIONAL_PACKAGES = ["essentia", "librosa", "onnxruntime"]
ALL_PACKAGES = MANAGED_PACKAGES + OPTIONAL_PACKAGES
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"
# Standalone CPython for packaged builds (which have no interpreter to create a venv with).
PYTHON_RUNTIME_VERSION = "3.12.15+20261003"
PYTHON_RUNTIME_URL = (
    "https://github.com/astral-sh/python-build-standalone/releases/download/20261003/"
    f"cpython-{PYTHON_RUNTIME_VERSION}-{{target}}-install_only.tar.gz"
)


@dataclass
class Snapshot:
    id: str
    created_at: str
    reason: str
    versions: dict[str, str]
    freeze: list[str]
    status: str = "unknown"  # unknown | good | broken


@dataclass
class DepsState:
    snapshots: list[Snapshot] = field(default_factory=list)


class DependencyError(RuntimeError):
    pass


def child_env(**extra: str) -> dict[str, str]:
    """Environment for child processes.

    PyInstaller points LD_LIBRARY_PATH (Linux) at its bundled libraries; a child Python
    must not inherit that, so the original value is restored.
    """
    env = {**os.environ, "PYTHONUTF8": "1", **extra}
    if getattr(sys, "frozen", False):
        for var in ("LD_LIBRARY_PATH", "LD_PRELOAD"):
            orig = env.pop(var + "_ORIG", None)
            if orig is not None:
                env[var] = orig
            else:
                env.pop(var, None)
        env.pop("PYTHONHOME", None)
        env.pop("PYTHONPATH", None)
    return env


def _no_window() -> dict:
    if paths.IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NO_WINDOW}  # type: ignore[attr-defined]
    return {}


class DependencyManager:
    def __init__(self) -> None:
        self.base = paths.deps_dir()
        self.venv = self.base / "venv"
        self.state_file = self.base / "snapshots.json"
        self.state = self._load_state()

    # ------------------------------------------------------------------ state
    def _load_state(self) -> DepsState:
        if not self.state_file.exists():
            return DepsState()
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
            return DepsState(snapshots=[Snapshot(**s) for s in raw.get("snapshots", [])])
        except (OSError, ValueError, TypeError):
            return DepsState()

    def _save_state(self) -> None:
        atomic_write_text(self.state_file, json.dumps({"snapshots": [asdict(s) for s in self.state.snapshots]}, indent=1))

    # ------------------------------------------------------------------ venv
    @property
    def python(self) -> Path:
        if paths.IS_WINDOWS:
            return self.venv / "Scripts" / "python.exe"
        return self.venv / "bin" / "python"

    def is_installed(self) -> bool:
        return self.python.exists()

    def _run(self, args: list[str], log=None) -> str:
        proc = spawn(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", env=child_env(),
        )
        lines = []
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip()
                lines.append(line)
                if log and line:
                    log(line)
            code = proc.wait()
        finally:
            release(proc)
        job = current_job.get()
        if job is not None and job.cancel_requested:
            raise JobCancelled()
        if code != 0:
            raise DependencyError(f"{' '.join(map(str, args[:4]))} ... failed (exit {code}):\n" + "\n".join(lines[-15:]))
        return "\n".join(lines)

    def ensure_venv(self, log=print) -> None:
        if self.is_installed():
            return
        base_python = self._base_python(log)
        log(f"Creating dependency environment in {self.venv}")
        try:
            self._run([base_python, "-m", "venv", str(self.venv)], log)
        except DependencyError:
            # Some distributions ship Python without ensurepip - bootstrap pip manually.
            shutil.rmtree(self.venv, ignore_errors=True)
            self._run([base_python, "-m", "venv", "--without-pip", str(self.venv)], log)
            get_pip = self.base / "get-pip.py"
            log("Downloading get-pip.py")
            download(GET_PIP_URL, get_pip)
            self._run([str(self.python), str(get_pip), "-q"], log)
        self._run([str(self.python), "-m", "pip", "install", "-q", "--upgrade", "pip"], log)

    def _base_python(self, log=print) -> str:
        """Interpreter used to create the venv.

        From source this is the running Python. Packaged builds have none, so a pinned
        standalone CPython (python-build-standalone) is downloaded once into the data dir.
        """
        if not getattr(sys, "frozen", False):
            return sys.executable
        runtime = self.base / "python"
        exe = runtime / ("python.exe" if paths.IS_WINDOWS else "bin/python3")
        if exe.exists():
            return str(exe)
        url = PYTHON_RUNTIME_URL.format(target="x86_64-pc-windows-msvc" if paths.IS_WINDOWS else "x86_64-unknown-linux-gnu")
        archive = self.base / "python-runtime.tar.gz"
        log(f"Downloading Python runtime {PYTHON_RUNTIME_VERSION} ...")
        download(url, archive, timeout=600)
        shutil.rmtree(runtime, ignore_errors=True)
        with tarfile.open(archive) as tar:  # contains a top-level "python/" folder
            try:
                tar.extractall(self.base, filter="data")
            except TypeError:  # Python without tarfile extraction filters
                tar.extractall(self.base)
        archive.unlink(missing_ok=True)
        if not exe.exists():
            raise DependencyError(f"Python runtime download is incomplete ({exe} missing)")
        return str(exe)

    def pip(self, *args: str, log=print) -> str:
        self.ensure_venv(log)
        return self._run([str(self.python), "-m", "pip", "--disable-pip-version-check", *args], log)

    # ------------------------------------------------------------------ info
    def installed_versions(self) -> dict[str, str | None]:
        result: dict[str, str | None] = {p: None for p in ALL_PACKAGES}
        if not self.is_installed():
            return result
        script = (
            "import json, importlib.metadata as m\n"
            "out = {}\n"
            f"for p in {ALL_PACKAGES!r}:\n"
            "    try: out[p] = m.version(p)\n"
            "    except Exception: out[p] = None\n"
            "print(json.dumps(out))"
        )
        try:
            out = subprocess.run([str(self.python), "-c", script], capture_output=True, text=True, timeout=60,
                                 env=child_env(), **_no_window())
            result.update(json.loads(out.stdout.strip().splitlines()[-1]))
        except Exception:
            pass
        return result

    @staticmethod
    def available_versions(package: str, limit: int = 30) -> list[str]:
        with urlopen(f"https://pypi.org/pypi/{package}/json", timeout=20) as resp:
            data = json.load(resp)
        versions = []
        for v, files in data.get("releases", {}).items():
            if not files or all(f.get("yanked") for f in files):
                continue
            try:
                parsed = Version(v)
            except InvalidVersion:
                continue
            if not parsed.is_prerelease:
                versions.append(parsed)
        return [str(v) for v in sorted(versions, reverse=True)[:limit]]

    def status(self) -> dict:
        return {
            "venv": str(self.venv),
            "installed": self.is_installed(),
            "versions": self.installed_versions(),
            "snapshots": [asdict(s) for s in reversed(self.state.snapshots)],
            "ffmpeg": self.ffmpeg_path(),
        }

    def ffmpeg_path(self) -> str | None:
        """ffmpeg as spotdl will find it (its own download location differs between versions)."""
        if self.is_installed():
            script = "from spotdl.utils.ffmpeg import get_ffmpeg_path as g; p = g(); print(p or '')"
            try:
                out = subprocess.run([str(self.python), "-c", script], capture_output=True, text=True,
                                     timeout=30, env=child_env(), **_no_window())
                found = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
                if found:
                    return found
            except Exception:
                pass
        return shutil.which("ffmpeg")

    # ------------------------------------------------------------------ snapshots
    def snapshot(self, reason: str) -> Snapshot | None:
        if not self.is_installed():
            return None
        freeze = self.pip("freeze", "--all", log=None).splitlines()
        freeze = [line for line in freeze if line and not line.startswith(("-e ", "#"))]
        versions = {k: v for k, v in self.installed_versions().items() if v}
        last = self.state.snapshots[-1] if self.state.snapshots else None
        if last and sorted(last.freeze) == sorted(freeze):
            return last
        snap = Snapshot(id=now_iso().replace(":", "").replace("-", ""), created_at=now_iso(),
                        reason=reason, versions=versions, freeze=freeze)
        self.state.snapshots.append(snap)
        self.state.snapshots = self.state.snapshots[-25:]
        self._save_state()
        return snap

    def current_snapshot(self) -> Snapshot | None:
        return self.snapshot("current state")

    def mark_current(self, status: str) -> None:
        try:
            snap = self.current_snapshot()
        except DependencyError:
            return
        if snap and snap.status != status:
            snap.status = status
            self._save_state()

    # ------------------------------------------------------------------ actions
    def install(self, package: str | None = None, version: str | None = None, log=print) -> dict:
        """Install/upgrade one managed package (or all) to a version (or latest)."""
        self.ensure_venv(log)
        self.snapshot("before update")
        if package:
            if package not in ALL_PACKAGES:
                raise DependencyError(f"{package} is not a managed package")
            spec = f"{package}=={version}" if version else package
            self.pip("install", "--upgrade", spec, log=log)
        else:
            installed = [p for p, v in self.installed_versions().items() if v and p in OPTIONAL_PACKAGES]
            self.pip("install", "--upgrade", *MANAGED_PACKAGES, *installed, log=log)
        snap = self.snapshot("after update")
        return snap.versions if snap else {}

    @property
    def bin_dir(self) -> Path:
        """Folder with the environment's programs (python, deno, ...)."""
        return self.python.parent

    def ensure_managed(self, log=print) -> None:
        """Install managed packages that are missing, e.g. Deno for installs made before it was needed."""
        if not self.is_installed():
            return
        missing = [p for p, v in self.installed_versions().items() if p in MANAGED_PACKAGES and not v]
        if missing:
            log(f"Installing missing components: {', '.join(missing)}")
            self.install_packages(missing, log)

    def install_packages(self, packages: list[str], log=print) -> None:
        """Add optional packages (with a snapshot before, so the change can be undone)."""
        self.ensure_venv(log)
        self.snapshot("before installing " + ", ".join(packages))
        self.pip("install", *packages, log=log)
        self.snapshot("after installing " + ", ".join(packages))

    def restore(self, snapshot_id: str, log=print) -> dict:
        snap = next((s for s in self.state.snapshots if s.id == snapshot_id), None)
        if snap is None:
            raise DependencyError("Unknown snapshot")
        self.ensure_venv(log)
        self.snapshot("before restore")
        req = self.base / "restore-requirements.txt"
        req.write_text("\n".join(snap.freeze) + "\n", encoding="utf-8")
        log(f"Restoring {', '.join(f'{k} {v}' for k, v in snap.versions.items())}")
        self.pip("install", "--force-reinstall", "--no-deps", "-r", str(req), log=log)
        # Remove packages that were added after that snapshot.
        current = {line.split("==")[0].lower() for line in self.pip("freeze", "--all", log=None).splitlines() if "==" in line}
        wanted = {line.split("==")[0].lower() for line in snap.freeze if "==" in line}
        extra = sorted(current - wanted - {"pip", "setuptools", "wheel"})
        if extra:
            self.pip("uninstall", "-y", *extra, log=log)
        return snap.versions

    def reinstall_venv(self, log=print) -> None:
        """Last resort: throw the environment away and install fresh."""
        last_good = next((s for s in reversed(self.state.snapshots) if s.status == "good"), None)
        shutil.rmtree(self.venv, ignore_errors=True)
        self.ensure_venv(log)
        if last_good:
            req = self.base / "restore-requirements.txt"
            req.write_text("\n".join(last_good.freeze) + "\n", encoding="utf-8")
            self.pip("install", "-r", str(req), log=log)
        else:
            self.pip("install", *MANAGED_PACKAGES, log=log)

    def download_ffmpeg(self, log=print) -> None:
        self.ensure_venv(log)
        self._run([str(self.python), "-m", "spotdl", "--download-ffmpeg"], log)
