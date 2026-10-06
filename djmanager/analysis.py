"""BPM and key analysis.

Runs in the managed dependency environment like spotdl: Essentia where it is available
(Linux), librosa otherwise (Windows). Audio is decoded with spotdl's ffmpeg, so every
format spotdl handles works. Several worker processes analyse songs in parallel; each
result is stored right away, so a stopped analysis resumes with the songs that are left.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from dataclasses import dataclass
from typing import Callable

from . import paths
from .deps import DependencyManager, child_env
from .jobs import JobCancelled, current_job
from .procs import kill_tree, release, spawn

PITCHES = {"C": 0, "C#": 1, "Db": 1, "D": 2, "D#": 3, "Eb": 3, "E": 4, "F": 5, "F#": 6, "Gb": 6,
           "G": 7, "G#": 8, "Ab": 8, "A": 9, "A#": 10, "Bb": 10, "B": 11}
NAMES = ["C", "Db", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]


def parse_key(key: str | None) -> tuple[int, bool] | None:
    """'A minor' -> (9, True)."""
    if not key:
        return None
    parts = key.split()
    if len(parts) != 2 or parts[0] not in PITCHES:
        return None
    return PITCHES[parts[0]], parts[1].lower() == "minor"


def open_key_number(pitch: int, minor: bool) -> int:
    major_pitch = (pitch + 3) % 12 if minor else pitch  # relative major
    return (major_pitch * 7) % 12 + 1  # C = 1, G = 2, ... (circle of fifths)


def format_key(key: str | None, notation: str = "openkey") -> str:
    parsed = parse_key(key)
    if parsed is None:
        return ""
    pitch, minor = parsed
    number = open_key_number(pitch, minor)
    if notation == "camelot":
        return f"{(number + 6) % 12 + 1}{'A' if minor else 'B'}"
    if notation == "musical":
        return NAMES[pitch] + ("m" if minor else "")
    return f"{number}{'m' if minor else 'd'}"


def key_sort(key: str | None) -> float:
    """Sort keys like a DJ: by wheel position, minor before major."""
    parsed = parse_key(key)
    if parsed is None:
        return 99
    return open_key_number(*parsed) + (0.0 if parsed[1] else 0.5)


def analysis_packages() -> list[str]:
    """Analysis package for this platform (Essentia has no Windows build)."""
    return ["librosa"] if paths.IS_WINDOWS else ["essentia"]



# Runs inside the managed environment. Protocol: one JSON request per stdin line,
# one 'DJM:'-prefixed JSON answer per stdout line (libraries may print their own output).
WORKER_SCRIPT = r'''
import json, subprocess, sys
cfg = json.loads(sys.argv[1])
LO, HI = float(cfg["bpm_min"]), float(cfg["bpm_max"])
import numpy as np
es = None
if cfg.get("engine", "auto") in ("auto", "essentia"):
    try:
        import essentia
        essentia.log.infoActive = essentia.log.warningActive = False
        import essentia.standard as es
    except Exception:
        es = None
if es is None:
    import librosa

def answer(data):
    print("DJM:" + json.dumps(data), flush=True)

def decode(path, sr):
    raw = subprocess.run([cfg["ffmpeg"], "-v", "error", "-nostdin", "-i", path, "-t", "600",
                          "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"], capture_output=True)
    if raw.returncode != 0:
        raise RuntimeError("ffmpeg: " + raw.stderr.decode("utf-8", "replace").strip()[-200:])
    audio = np.frombuffer(raw.stdout, dtype=np.float32)
    if len(audio) < sr * 5:
        raise RuntimeError("too short or no audio")
    return audio

def fold(bpm):
    while bpm and bpm < LO:
        bpm *= 2
    while bpm > HI:
        bpm /= 2
    return bpm

KK_MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
KK_MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
NAMES = ["C", "Db", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]

def librosa_tempo(y, sr, hop=128):
    onset = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    # librosa's estimate with a prior at typical DJ tempo picks the right "octave" ...
    coarse = float(np.atleast_1d(librosa.feature.tempo(onset_envelope=onset, sr=sr, hop_length=hop, start_bpm=128))[0])
    # ... then refine it: autocorrelation peak near that tempo, interpolated between frames
    fps = sr / hop
    onset = onset - onset.mean()
    ac = librosa.autocorrelate(onset, max_size=int(fps * 60 / (coarse * 0.96)) + 2)
    lo_lag, hi_lag = int(fps * 60 / (coarse * 1.04)), int(fps * 60 / (coarse * 0.96)) + 1
    if hi_lag >= len(ac) - 1 or lo_lag < 1:
        return coarse
    k = lo_lag + int(np.argmax(ac[lo_lag:hi_lag + 1]))
    a, b, c = ac[k - 1], ac[k], ac[k + 1]
    denom = a - 2 * b + c
    k = k + (0.5 * (a - c) / denom if denom != 0 else 0.0)
    return 60 * fps / k

def analyze(path):
    if es is not None:
        audio = decode(path, 44100)
        bpm = es.RhythmExtractor2013(method="multifeature", minTempo=max(40, int(LO)), maxTempo=min(208, int(HI)))(audio)[0]
        key, scale, strength = es.KeyExtractor(profileType="edma")(audio)
        return {"bpm": round(fold(float(bpm)), 2), "key": f"{key} {scale}", "confidence": round(float(strength), 2),
                "engine": "essentia"}
    y = decode(path, 22050)
    bpm = librosa_tempo(y, 22050)
    chroma = librosa.feature.chroma_cqt(y=y, sr=22050).mean(axis=1)
    score, pitch, scale = max((float(np.corrcoef(chroma, np.roll(p, i))[0, 1]), i, s)
                              for p, s in ((KK_MAJOR, "major"), (KK_MINOR, "minor")) for i in range(12))
    return {"bpm": round(fold(float(bpm)), 2), "key": f"{NAMES[pitch]} {scale}", "confidence": round(score, 2),
            "engine": "librosa"}

answer({"ready": True, "engine": "essentia" if es is not None else "librosa"})
for line in sys.stdin:
    req = json.loads(line)
    try:
        res = analyze(req["path"])
    except Exception as exc:
        res = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    res["id"] = req["id"]
    answer(res)
'''


class AnalysisError(RuntimeError):
    pass


@dataclass
class Result:
    track_id: str
    path: str
    bpm: float | None = None
    key: str | None = None
    engine: str = ""
    error: str = ""


def default_workers() -> int:
    return max(1, min(4, (os.cpu_count() or 2) - 1))


class Analyzer:
    def __init__(self, deps: DependencyManager, settings) -> None:
        self.deps = deps
        self.settings = settings

    # ------------------------------------------------------------------ tools
    def engine(self) -> str | None:
        versions = self.deps.installed_versions()
        if versions.get("essentia") and not paths.IS_WINDOWS:
            return "essentia"
        if versions.get("librosa"):
            return "librosa"
        return None

    def ensure_tools(self, log) -> str:
        engine = self.engine()
        if engine:
            return engine
        self.deps.ensure_venv(log)
        log("Installing the analysis tools (once) ...")
        for package in analysis_packages() + ["librosa"]:
            try:
                self.deps.install_packages([package], log)
                break
            except Exception as exc:  # e.g. no Essentia build for this system -> librosa
                if isinstance(exc, JobCancelled):
                    raise
                log(f"{package} could not be installed ({str(exc).splitlines()[0]}), trying the alternative")
        engine = self.engine()
        if not engine:
            raise AnalysisError("No analysis tool could be installed - see the log")
        return engine

    def worker_command(self) -> list[str]:
        ffmpeg = self.deps.ffmpeg_path()
        if not ffmpeg:
            raise AnalysisError("ffmpeg not found - download it under Dependencies")
        s = self.settings
        cfg = {"ffmpeg": ffmpeg, "bpm_min": s.bpm_min, "bpm_max": s.bpm_max, "engine": "auto"}
        return [str(self.deps.python), "-c", WORKER_SCRIPT, json.dumps(cfg)]

    # ------------------------------------------------------------------ run
    def run(self, items: list[tuple[str, str]], on_result: Callable[[Result], None], log,
            command: list[str] | None = None) -> None:
        """Analyse (track id, file path) pairs with parallel workers; on_result is called per song."""
        if not items:
            return
        cmd = command or self.worker_command()
        workers = min(len(items), self.settings.analysis_workers or default_workers())
        todo: queue.Queue = queue.Queue()
        for item in items:
            todo.put(item)
        job = current_job.get()
        errors: list[BaseException] = []

        def start():
            proc = spawn(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                         text=True, encoding="utf-8", errors="replace", bufsize=1, env=child_env())
            if not read_answer(proc).get("ready"):
                raise AnalysisError("analysis worker did not start")
            return proc

        def read_answer(proc) -> dict:
            assert proc.stdout is not None
            for line in proc.stdout:
                if line.startswith("DJM:"):
                    return json.loads(line[4:])
            return {}  # process ended

        def work():
            token = current_job.set(job)  # register worker processes with the job
            proc = None
            try:
                while not (job and job.cancel_requested):
                    try:
                        track_id, path = todo.get_nowait()
                    except queue.Empty:
                        return
                    if proc is None or proc.poll() is not None:
                        proc = start()
                    try:
                        assert proc.stdin is not None
                        proc.stdin.write(json.dumps({"id": track_id, "path": path}) + "\n")
                        proc.stdin.flush()
                        answer = read_answer(proc)
                    except (BrokenPipeError, OSError):
                        answer = {}
                    if job and job.cancel_requested:
                        return
                    if not answer:  # worker crashed on this file - restart it for the next one
                        on_result(Result(track_id, path, error="analysis crashed on this file"))
                        release(proc)
                        proc = None
                        continue
                    on_result(Result(track_id, path, bpm=answer.get("bpm"), key=answer.get("key"),
                                     engine=answer.get("engine", ""), error=answer.get("error", "")))
            except BaseException as exc:  # noqa: BLE001 - reported by the job
                if not isinstance(exc, JobCancelled):
                    errors.append(exc)
            finally:
                if proc is not None:
                    if proc.poll() is None:
                        try:
                            proc.stdin.close()  # type: ignore[union-attr]
                            proc.wait(timeout=5)
                        except Exception:
                            kill_tree(proc)
                    release(proc)
                current_job.reset(token)

        log(f"Analysing {len(items)} songs with {workers} parallel workers")
        threads = [threading.Thread(target=work, daemon=True) for _ in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if job and job.cancel_requested:
            raise JobCancelled()
        if errors:
            raise errors[0]
