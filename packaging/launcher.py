"""PyInstaller entry point."""

import os
import sys


def _ensure_streams() -> None:
    # Windowed builds have no console: stdout/stderr are None, which breaks uvicorn's
    # logging. Send them to a log file in the data directory instead.
    if sys.stdout is not None and sys.stderr is not None:
        return
    from djmanager import paths

    log_dir = paths.data_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stream = open(log_dir / "dj-manager.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    sys.stdout = sys.stdout or stream
    sys.stderr = sys.stderr or stream


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    _ensure_streams()
    from djmanager.__main__ import main

    main()
