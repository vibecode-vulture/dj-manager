import json
import os
import sys
import time
from pathlib import Path

import pytest

from conftest import wait
from djmanager.analysis import WORKER_SCRIPT, Analyzer, format_key, key_sort
from djmanager.settings import Settings
from test_service import env  # noqa: F401 - fixture reuse

# A stand-in for the analysis worker: same protocol, fake results.
# Files named "*crash*" kill the worker, "*bad*" return an error.
FAKE_WORKER = r'''
import json, sys, time, os
print("noise from a library", flush=True)
print("DJM:" + json.dumps({"ready": True, "engine": "fake"}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    time.sleep(float(os.environ.get("FAKE_DELAY", "0")))
    name = os.path.basename(req["path"])
    if "crash" in name:
        os._exit(1)
    if "bad" in name:
        print("DJM:" + json.dumps({"id": req["id"], "error": "RuntimeError: no audio"}), flush=True)
        continue
    print("DJM:" + json.dumps({"id": req["id"], "bpm": 128.0, "key": "A minor", "engine": "fake"}), flush=True)
'''


@pytest.mark.parametrize("key,openkey,camelot,musical", [
    ("C major", "1d", "8B", "C"), ("A minor", "1m", "8A", "Am"), ("G major", "2d", "9B", "G"),
    ("E minor", "2m", "9A", "Em"), ("F major", "12d", "7B", "F"), ("Bb minor", "8m", "3A", "Bbm"),
    ("F# major", "7d", "2B", "F#"), ("C# minor", "5m", "12A", "Dbm"),
])
def test_key_notations(key, openkey, camelot, musical):
    assert format_key(key, "openkey") == openkey
    assert format_key(key, "camelot") == camelot
    assert format_key(key, "musical") == musical


def test_key_edge_cases():
    assert format_key(None) == "" and format_key("nonsense") == ""
    assert key_sort("A minor") < key_sort("C major") < key_sort("E minor")


class Deps:
    python = sys.executable


def test_workers_handle_crash_and_errors(tmp_path):
    settings = Settings(analysis_workers=2)
    items = [(f"t{i}", str(tmp_path / name)) for i, name in enumerate(["a.mp3", "crash.mp3", "bad.mp3", "b.mp3", "c.mp3"])]
    results = []
    Analyzer(Deps(), settings).run(items, results.append, lambda line: None,
                                   command=[sys.executable, "-c", FAKE_WORKER])
    by_id = {r.track_id: r for r in results}
    assert set(by_id) == {"t0", "t1", "t2", "t3", "t4"}  # every song got an answer
    assert by_id["t1"].error == "analysis crashed on this file"
    assert by_id["t2"].error.startswith("RuntimeError")
    assert by_id["t0"].bpm == 128.0 and by_id["t4"].key == "A minor"


@pytest.fixture
def analysis_env(env, monkeypatch):  # noqa: F811
    svc, fake, music, nml = env
    monkeypatch.setattr(svc.analyzer, "ensure_tools", lambda log: "fake")
    monkeypatch.setattr(svc.analyzer, "worker_command", lambda: [sys.executable, "-c", FAKE_WORKER])
    return svc


def test_stop_pauses_and_resume_continues(analysis_env, monkeypatch):
    svc = analysis_env
    lib = svc.library
    monkeypatch.setenv("FAKE_DELAY", "0.4")
    svc.settings.analysis_workers = 1
    job = svc.submit_analysis(manual=True)
    while sum(t.analysis == "done" for t in lib.tracks.values()) < 1:
        time.sleep(0.02)
    svc.cancel_job(job.id)  # user presses Stop
    wait(job)
    assert job.status == "cancelled" and "Paused" in job.result
    assert svc.settings.analysis_paused  # no automatic restart ...
    assert svc.auto_analyze() is None
    status = svc.analysis_status()
    assert status["done"] >= 1 and status["pending"] >= 1
    stored = json.loads(lib.file.read_text())  # progress is saved
    assert any(t["analysis"] == "done" for t in stored["tracks"])

    monkeypatch.setenv("FAKE_DELAY", "0")
    wait(svc.submit_analysis(manual=True))  # ... until Resume
    assert not svc.settings.analysis_paused
    assert svc.analysis_status()["pending"] == 0
    assert all(t.bpm == 128.0 and t.key == "A minor" for t in svc._analysable(lib))


def test_retry_failed_and_reanalyse_all(analysis_env):
    svc = analysis_env
    lib = svc.library
    wait(svc.submit_analysis(manual=True))
    track = svc._analysable(lib)[0]
    track.analysis, track.bpm = "failed", None
    wait(svc.submit_analysis("failed", manual=True))
    assert track.analysis == "done" and track.bpm == 128.0
    for t in lib.tracks.values():
        t.bpm = 1.0
    wait(svc.submit_analysis("all", manual=True))
    assert all(t.bpm == 128.0 for t in svc._analysable(lib))


# Real Essentia/librosa run - set DJM_ANALYSIS_PYTHON to a Python with essentia or librosa
# and DJM_FFMPEG to an ffmpeg binary.
@pytest.mark.skipif(not os.environ.get("DJM_ANALYSIS_PYTHON"), reason="real analysis tools not configured")
@pytest.mark.parametrize("engine", ["essentia", "librosa"])
def test_real_engine_on_synthetic_signal(tmp_path, engine):
    import subprocess

    ffmpeg = os.environ["DJM_FFMPEG"]
    wav = tmp_path / "128bpm-a-minor.wav"
    subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=220:d=40", "-f", "lavfi", "-i", "sine=f=261.63:d=40",
                    "-f", "lavfi", "-i", "sine=f=329.63:d=40", "-f", "lavfi",
                    "-i", "aevalsrc='0.9*sin(2*PI*55*t)*exp(-30*mod(t,60/128))':d=40",
                    "-filter_complex", "[0][1][2][3]amix=inputs=4:normalize=0,volume=0.5", str(wav)], check=True)
    cfg = {"ffmpeg": ffmpeg, "bpm_min": 70, "bpm_max": 185, "engine": engine}
    results = []
    Analyzer(Deps(), Settings(analysis_workers=1)).run(
        [("x", str(wav))], results.append, lambda line: None,
        command=[os.environ["DJM_ANALYSIS_PYTHON"], "-c", WORKER_SCRIPT, json.dumps(cfg)])
    assert not results[0].error, results[0].error
    assert results[0].engine == engine
    assert abs(results[0].bpm - 128) < 1.0
    assert results[0].key == "A minor"


def test_rating_scales():
    from djmanager.audio import popm_to_stars, scaled_to_stars

    assert [popm_to_stars(v) for v in (0, 51, 102, 153, 204, 255)] == [None, 1, 2, 3, 4, 5]  # Traktor
    assert [popm_to_stars(v) for v in (1, 64, 128, 196, 255)] == [1, 2, 3, 4, 5]  # Windows Media Player
    assert [scaled_to_stars(v) for v in ("0.6", "3", "80", "0", "x")] == [3, 3, 4, None, None]


def test_traktor_rating_is_the_fallback(env):  # noqa: F811
    import xml.etree.ElementTree as ET

    from djmanager.jobs import Job

    svc, fake, music, nml = env
    lib = svc.library
    klonk = next(t for t in lib.tracks.values() if t.title == "Klonk")
    acid = next(t for t in lib.tracks.values() if t.title == "Acid Tracks")
    tree = ET.parse(nml)
    for entry in tree.getroot().iter("ENTRY"):
        if entry.get("TITLE") in ("Klonk", "Acid Tracks"):
            entry.find("INFO").set("RANKING", "204")
    tree.write(nml)
    acid.rating = 2  # the file's own rating wins
    svc.write_traktor(Job(id="t", title="t"), "test")
    assert klonk.rating_traktor == 4 and klonk.stars == 4
    assert acid.stars == 2
