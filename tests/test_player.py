
def test_audio_endpoint_serves_library_files_with_ranges(env):
    """The player streams files from the library only, with seeking (range requests)."""
    import threading
    import time
    import urllib.error
    import urllib.request

    import uvicorn

    from djmanager.api import create_app

    svc, fake, music, nml = env
    track = next(t for t in svc.library.tracks.values() if t.title == "Klonk")
    (music / track.path).write_bytes(bytes(range(256)) * 40)  # 10240 bytes
    server = uvicorn.Server(uvicorn.Config(create_app(svc), host="127.0.0.1", port=8931, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.02)
    try:
        base = "http://127.0.0.1:8931/api/tracks"
        with urllib.request.urlopen(f"{base}/{track.id}/audio") as r:
            assert r.headers["content-type"] == "audio/mpeg" and len(r.read()) == 10240
        req = urllib.request.Request(f"{base}/{track.id}/audio", headers={"Range": "bytes=100-199"})
        with urllib.request.urlopen(req) as r:
            assert r.status == 206 and r.read() == (bytes(range(256)) * 40)[100:200]
        for bad in ("unknown-id", "..%2F..%2Fetc%2Fpasswd"):
            try:
                urllib.request.urlopen(f"{base}/{bad}/audio")
                raise AssertionError("served a file that is not in the library")
            except urllib.error.HTTPError as exc:
                assert exc.code == 404
    finally:
        server.should_exit = True
