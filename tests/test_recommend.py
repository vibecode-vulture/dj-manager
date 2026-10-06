"""Recommendation engine on synthetic data (needs a Python with numpy: DJM_ANALYSIS_PYTHON)."""

import os
import sys

import pytest

from djmanager.recommend import run_recommendations

PY = os.environ.get("DJM_ANALYSIS_PYTHON")
pytestmark = pytest.mark.skipif(not PY, reason="needs a Python with numpy (DJM_ANALYSIS_PYTHON)")


class Deps:
    python = PY

    def is_installed(self):
        return True


def make_tracks(tmp_path, n_a=20, n_b=10):
    import subprocess
    import json

    # two sound groups (group B brighter and busier), two tempo groups, two style groups
    script = r"""
import json, sys, numpy as np
out, n_a, n_b = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
rng = np.random.default_rng(1)
rows = []
for i in range(n_a + n_b):
    b = i >= n_a
    timbre = rng.normal(0, 1, 46)
    timbre[40] += 5 if b else 0   # brightness: clearly brighter group
    timbre[42] += 5 if b else 0   # onsets
    emb = rng.normal(0, 0.1, 1280); emb[:640] += 1 if b else 0; emb[640:] += 0 if b else 1
    act = np.full(400, 0.01); act[7] = 0.6 if b else 0.05; act[9] = 0.05 if b else 0.5
    f = f"{out}/{i}.npz"
    np.savez(f, timbre=timbre, embedding=emb.astype(np.float16), styles=act.astype(np.float16))
    rows.append({"id": f"t{i}", "bpm": float(140 + rng.normal(0, 1) if b else 128 + rng.normal(0, 1)),
                 "energy": float(8 + rng.normal(0, .3) if b else 5 + rng.normal(0, .3)), "file": f})
print(json.dumps(rows))
"""
    out = subprocess.run([PY, "-c", script, str(tmp_path), str(n_a), str(n_b)], capture_output=True, text=True, check=True)
    labels = tmp_path / "labels.json"
    labels.write_text(json.dumps({"classes": [f"Electronic---Style {i}" for i in range(400)]}))
    return json.loads(out.stdout), str(labels)


def test_finds_the_planted_groups(tmp_path):
    tracks, labels = make_tracks(tmp_path)
    group_b = {f"t{i}" for i in range(20, 30)}
    res = run_recommendations(Deps(), {"tracks": tracks, "min_group": 6, "labels_file": labels,
                                       "signals": {"bpm": True, "energy": True, "timbre": True, "styles": True}})
    by_signal = {}
    for s in res["suggestions"]:
        by_signal.setdefault(s["signal"], s)
    # BPM, energy, sound and style all point at the same 10 songs -> near-duplicates are merged,
    # so at least one suggestion must match the planted group exactly
    assert any(set(s["track_ids"]) == group_b for s in res["suggestions"])
    best = res["suggestions"][0]
    assert set(best["track_ids"]) == group_b
    # all four signals found the same group -> merged into one suggestion naming all of them
    assert set(best["signals"]) == {"tempo", "energy", "sound", "styles"} and len(best["reasons"]) == 4
    assert best["name_hint"]
    assert {"bpm_energy", "sound", "style"} <= set(res["maps"])
    assert len(res["maps"]["sound"]) == 30


def test_each_signal_alone(tmp_path):
    tracks, labels = make_tracks(tmp_path)
    group_b = {f"t{i}" for i in range(20, 30)}
    for signal in ("bpm", "energy", "timbre", "styles"):
        res = run_recommendations(Deps(), {"tracks": tracks, "min_group": 6, "labels_file": labels,
                                           "signals": {signal: True}})
        assert res["suggestions"], signal
        s = res["suggestions"][0]
        assert set(s["track_ids"]) == group_b, signal
    styled = run_recommendations(Deps(), {"tracks": tracks, "min_group": 6, "labels_file": labels,
                                          "signals": {"styles": True}})["suggestions"][0]
    assert styled["name_hint"] == "style-7" and "Style 7" in styled["title"]
    sound = run_recommendations(Deps(), {"tracks": tracks, "min_group": 6, "labels_file": labels,
                                         "signals": {"timbre": True}})["suggestions"][0]
    assert "brighter" in sound["description"] or "busier" in sound["description"]


def test_no_suggestion_without_structure(tmp_path):
    tracks, labels = make_tracks(tmp_path, n_a=30, n_b=0)
    res = run_recommendations(Deps(), {"tracks": tracks, "min_group": 6, "labels_file": labels,
                                       "signals": {"bpm": True, "energy": True, "timbre": True, "styles": True}})
    assert res["suggestions"] == []


def test_noise_rarely_produces_suggestions(tmp_path):
    """Random data without groups: at most an occasional false positive."""
    import json
    import subprocess

    script = r"""
import json, sys, numpy as np
out, seed = sys.argv[1], int(sys.argv[2])
rng = np.random.default_rng(seed)
rows = []
for i in range(30):
    f = f"{out}/{seed}-{i}.npz"
    np.savez(f, timbre=rng.normal(0, 1, 46), embedding=rng.normal(0, 1, 1280).astype(np.float16))
    rows.append({"id": f"t{i}", "bpm": float(128 + rng.normal(0, 1)), "energy": float(6 + rng.normal(0, .4)), "file": f})
print(json.dumps(rows))
"""
    hits = 0
    for seed in range(20):
        rows = json.loads(subprocess.run([PY, "-c", script, str(tmp_path), str(seed)], capture_output=True,
                                         text=True, check=True).stdout)
        res = run_recommendations(Deps(), {"tracks": rows, "min_group": 6, "labels_file": None,
                                           "signals": {"bpm": True, "energy": True, "timbre": True, "styles": True}})
        hits += bool(res["suggestions"])
    assert hits <= 2, f"{hits}/20 random genres got suggestions"
