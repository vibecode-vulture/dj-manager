"""Start DJ Manager: local API server + native window (pywebview) or browser."""

from __future__ import annotations

import argparse
import socket
import threading
import time
import webbrowser

import uvicorn

from .api import create_app
from .service import Service


def _free_port(preferred: int) -> int:
    with socket.socket() as sock:
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
    args = parser.parse_args()

    service = Service()
    app = create_app(service)
    port = _free_port(args.port)
    url = f"http://127.0.0.1:{port}/"
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    service.startup()
    print(f"DJ Manager running at {url}")

    if args.no_open:
        thread.join()
        return
    if not args.browser:
        try:
            import webview  # pywebview

            webview.create_window("DJ Manager", url, width=1400, height=880, min_size=(960, 600),
                                  background_color="#161616")
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
