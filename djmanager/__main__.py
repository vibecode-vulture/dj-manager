"""Start DJ Manager: local API server + native window (pywebview) or browser."""

from __future__ import annotations

import argparse
import atexit
import os
import signal
import socket
import sys
import threading
import time
import webbrowser

import uvicorn

from . import __version__
from .api import create_app
from .service import Service
from .updater import cleanup_after_update


def _wait_for_exit(pid: int, timeout: float = 20.0) -> None:
    """After a self-update: wait until the previous process released port and files."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            if os.name == "nt":
                import ctypes

                handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
                if not handle:
                    return
                ctypes.windll.kernel32.WaitForSingleObject(handle, int((end - time.time()) * 1000))
                ctypes.windll.kernel32.CloseHandle(handle)
                return
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.2)


def _free_port(preferred: int) -> int:
    with socket.socket() as sock:
        if os.name != "nt":
            # like uvicorn: a port in TIME_WAIT (just after a restart) is usable
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]


def main() -> None:
    parser = argparse.ArgumentParser(prog="dj-manager")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--browser", action="store_true", help="open in the web browser instead of a window")
    parser.add_argument("--no-open", action="store_true", help="only run the server")
    parser.add_argument("--version", action="version", version=f"DJ Manager {__version__}")
    parser.add_argument("--wait-pid", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.wait_pid:
        _wait_for_exit(args.wait_pid)
    cleanup_after_update()

    service = Service()
    # Never leave spotdl/ffmpeg running after DJ Manager is gone.
    atexit.register(service.shutdown)
    if hasattr(signal, "SIGHUP"):  # terminal closed
        signal.signal(signal.SIGHUP, lambda *_: sys.exit(0))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    app = create_app(service)
    port = _free_port(args.port)
    url = f"http://127.0.0.1:{port}/"
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    service.startup()
    print(f"DJ Manager running at {url}", flush=True)

    if args.no_open:
        thread.join()
        return
    if not args.browser:
        try:
            import webview  # pywebview

            # text_select: pywebview disables selecting text by default (log, paths, errors)
            from .window_style import CAPTION, install

            window = webview.create_window("DJ Manager", url, width=1400, height=880, min_size=(960, 600),
                                           background_color=CAPTION, text_select=True)
            install(window)  # dark title bar on Windows instead of the white default
            webview.start()
            server.should_exit = True
            return
        except Exception as exc:  # pywebview missing or no GUI backend
            print(f"Native window unavailable ({exc}); opening the browser")
    webbrowser.open(url)
    try:
        thread.join()
    except KeyboardInterrupt:
        server.should_exit = True


if __name__ == "__main__":
    main()
