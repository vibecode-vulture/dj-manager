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
from .util import download

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

# ---------------------------------------------------------------- energy, sound, styles
# Engine independent (numpy + ONNX), so results are identical on Linux and Windows.
# The mel spectrogram reproduces Essentia's TensorflowInputMusiCNN exactly (verified
# against essentia-tensorflow: identical Discogs-EffNet predictions).
SR16, N_FFT, HOP16, N_MELS = 16000, 512, 256, 96

def _slaney_hz_to_mel(f):
    f = np.asarray(f, dtype=float)
    return np.where(f >= 1000.0, 15.0 + np.log(np.maximum(f, 1e-9) / 1000.0) / (np.log(6.4) / 27.0), f / (200.0 / 3))

def _slaney_mel_to_hz(m):
    m = np.asarray(m, dtype=float)
    return np.where(m >= 15.0, 1000.0 * np.exp((m - 15.0) * (np.log(6.4) / 27.0)), m * (200.0 / 3))

_FILTERS = None
def mel_filters():
    global _FILTERS
    if _FILTERS is None:
        edges = _slaney_mel_to_hz(np.linspace(0.0, _slaney_hz_to_mel(SR16 / 2), N_MELS + 2))
        freqs = np.arange(N_FFT // 2 + 1) * SR16 / N_FFT
        fb = np.zeros((N_MELS, len(freqs)))
        for i in range(N_MELS):
            lo, c, hi = edges[i], edges[i + 1], edges[i + 2]
            fb[i] = np.maximum(0, np.minimum((freqs - lo) / (c - lo), (hi - freqs) / (hi - c))) * 2.0 / (hi - lo)
        _FILTERS = fb.T
    return _FILTERS

def power_frames(audio):
    padded = np.concatenate([np.zeros(N_FFT // 2), audio.astype(np.float64), np.zeros(N_FFT)])
    frames = np.lib.stride_tricks.sliding_window_view(padded, N_FFT)[::HOP16][:len(audio) // HOP16 + 2]
    window = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(N_FFT) / (N_FFT - 1))
    return np.abs(np.fft.rfft(frames * window, axis=1)) ** 2

def dct_ortho(x, n):
    k = np.arange(x.shape[1])
    basis = np.cos(np.pi / x.shape[1] * (k + 0.5)[None, :] * np.arange(n)[:, None])
    basis[0] *= 1 / np.sqrt(2)
    return x @ basis.T * np.sqrt(2 / x.shape[1])

def sound_features(power, mel):
    """Energy (1-10) and a sound fingerprint for similarity."""
    fps = SR16 / HOP16
    rms_db = 10 * np.log10(np.maximum(power.sum(axis=1) / (N_FFT ** 2 / 4), 1e-10))
    active = rms_db[rms_db > np.percentile(rms_db, 30)]
    loudness = float(np.mean(active))
    dynamics = float(np.percentile(rms_db, 95) - np.percentile(rms_db, 10))
    freqs = np.arange(power.shape[1]) * SR16 / N_FFT
    total = np.maximum(power.sum(axis=1), 1e-12)
    centroid = float(np.mean((power * freqs).sum(axis=1) / total))
    bass = float(np.mean(power[:, freqs < 150].sum(axis=1) / total))
    flux = np.maximum(0, np.diff(mel, axis=0)).sum(axis=1)
    thresh = flux.mean() + 0.5 * flux.std()
    peaks = (flux[1:-1] > thresh) & (flux[1:-1] >= flux[:-2]) & (flux[1:-1] >= flux[2:])
    onsets = float(peaks.sum() / (len(flux) / fps))
    clip = lambda v, lo, hi: min(1.0, max(0.0, (v - lo) / (hi - lo)))
    energy = 1 + 9 * (0.4 * clip(loudness, -24, -8) + 0.35 * clip(onsets, 1.5, 7) + 0.25 * clip(centroid, 300, 1800))
    mfcc = dct_ortho(mel, 20)
    timbre = np.r_[mfcc.mean(0), mfcc.std(0), centroid / 1000, bass, onsets, loudness, dynamics, flux.mean()]
    return round(float(energy), 1), timbre, {"brightness": centroid, "bass": bass, "onsets": onsets,
                                             "loudness": loudness, "dynamics": dynamics}

_SESSION = None
def styles(mel):
    global _SESSION
    import onnxruntime as ort
    if _SESSION is None:
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1  # parallelism comes from several workers
        _SESSION = ort.InferenceSession(cfg["style_model"], sess_options=opts, providers=["CPUExecutionProvider"])
    mel = mel.astype(np.float32)
    if len(mel) < 128:
        mel = np.pad(mel, ((0, 128 - len(mel)), (0, 0)))
    patches = np.stack([mel[i:i + 128] for i in range(0, len(mel) - 128 + 1, 62)])
    out = {o.name: v for o, v in zip(_SESSION.get_outputs(), _SESSION.run(None, {_SESSION.get_inputs()[0].name: patches}))}
    return out["activations"].mean(axis=0), out["embeddings"].mean(axis=0)

def save_vectors(path, **arrays):
    import os
    data = {}
    if os.path.exists(path):
        with np.load(path) as old:
            data = {k: old[k] for k in old.files}
    data.update({k: np.asarray(v, dtype=np.float16 if np.asarray(v).size > 64 else np.float32) for k, v in arrays.items()})
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp.npz"
    np.savez(tmp, **data)
    os.replace(tmp, path)

def extra(req):
    tasks, res, vectors = req.get("tasks", []), {}, {}
    audio = decode(req["path"], SR16)
    power = power_frames(audio)
    mel = np.log10(1 + 10000 * power @ mel_filters())
    if "features" in tasks:
        try:
            energy, timbre, desc = sound_features(power, mel)
            res["features"] = {"energy": energy, "desc": desc}
            vectors["timbre"] = timbre
        except Exception as exc:
            res["features"] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    if "styles" in tasks:
        try:
            act, emb = styles(mel)
            top = np.argsort(-act)[:5]
            res["styles"] = {"top": [[cfg["style_labels"][i], round(float(act[i]), 3)] for i in top]}
            vectors["styles"], vectors["embedding"] = act, emb
        except Exception as exc:
            res["styles"] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    if vectors and req.get("out"):
        save_vectors(req["out"], **vectors)
    return res

if cfg.get("style_labels_file"):
    cfg["style_labels"] = json.load(open(cfg["style_labels_file"], encoding="utf-8"))["classes"]

answer({"ready": True, "engine": "essentia" if es is not None else "librosa"})
for line in sys.stdin:
    req = json.loads(line)
    tasks = req.get("tasks", ["base"])
    res = {}
    if "base" in tasks:
        try:
            res = analyze(req["path"])
        except Exception as exc:
            res = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    if set(tasks) & {"features", "styles"}:
        try:
            res.update(extra(req))
        except Exception as exc:
            msg = f"{type(exc).__name__}: {exc}"[:300]
            for task in set(tasks) & {"features", "styles"}:
                res[task] = {"error": msg}
    res["id"] = req["id"]
    answer(res)
'''


class AnalysisError(RuntimeError):
    pass


# Discogs-EffNet (MTG, Essentia models; CC BY-NC-ND 4.0 - free for non-commercial use)
STYLE_MODEL_URL = "https://essentia.upf.edu/models/feature-extractors/discogs-effnet/"
STYLE_MODEL_FILES = ["discogs-effnet-bsdynamic-1.onnx", "discogs-effnet-bsdynamic-1.json"]
TASK_LABELS = {"base": "BPM and key", "features": "energy and sound", "styles": "styles (AI)"}


@dataclass
class Result:
    track_id: str
    path: str
    tasks: list[str]
    answer: dict  # raw worker answer: base fields at top level, "features"/"styles" sub-dicts
    error: str = ""  # the whole request failed (e.g. worker crashed)


def default_workers() -> int:
    return max(1, min(4, (os.cpu_count() or 2) - 1))


class Analyzer:
    def __init__(self, deps: DependencyManager, settings) -> None:
        self.deps = deps
        self.settings = settings

    # ------------------------------------------------------------------ tools
    @property
    def model_dir(self):
        return self.deps.base / "models"

    def engine(self) -> str | None:
        versions = self.deps.installed_versions()
        if versions.get("essentia") and not paths.IS_WINDOWS:
            return "essentia"
        if versions.get("librosa"):
            return "librosa"
        return None

    def styles_ready(self) -> bool:
        return bool(self.deps.installed_versions().get("onnxruntime")) and all(
            (self.model_dir / f).exists() for f in STYLE_MODEL_FILES)

    def ensure_tools(self, log, styles: bool = False) -> str:
        engine = self.engine()
        if not engine:
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
        if styles and not self.styles_ready():
            self.ensure_style_tools(log)
        return engine

    def ensure_style_tools(self, log) -> None:
        if not self.deps.installed_versions().get("onnxruntime"):
            log("Installing onnxruntime for the style model ...")
            self.deps.install_packages(["onnxruntime"], log)
        self.model_dir.mkdir(parents=True, exist_ok=True)
        for name in STYLE_MODEL_FILES:
            target = self.model_dir / name
            if target.exists():
                continue
            log(f"Downloading the style model {name} (Discogs-EffNet by MTG, CC BY-NC-ND 4.0) ...")
            download(STYLE_MODEL_URL + name, target)

    def worker_command(self) -> list[str]:
        ffmpeg = self.deps.ffmpeg_path()
        if not ffmpeg:
            raise AnalysisError("ffmpeg not found - download it under Dependencies")
        s = self.settings
        cfg = {"ffmpeg": ffmpeg, "bpm_min": s.bpm_min, "bpm_max": s.bpm_max, "engine": "auto"}
        if self.styles_ready():
            cfg["style_model"] = str(self.model_dir / STYLE_MODEL_FILES[0])
            cfg["style_labels_file"] = str(self.model_dir / STYLE_MODEL_FILES[1])
        return [str(self.deps.python), "-c", WORKER_SCRIPT, json.dumps(cfg)]

    # ------------------------------------------------------------------ run
    def run(self, items: list[tuple[str, str, list[str], str]], on_result: Callable[[Result], None], log,
            command: list[str] | None = None) -> None:
        """Analyse (track id, file, tasks, vector file) items with parallel workers."""
        if not items:
            return
        cmd = command or self.worker_command()
        workers = min(len(items), self.settings.analysis_workers or default_workers())
        todo: queue.Queue = queue.Queue()
        for item in items:
            todo.put((*item, 0))  # last field: crashes so far
        log_dir = paths.data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        worker_log = open(log_dir / "analysis-worker.log", "a", encoding="utf-8")  # noqa: SIM115
        job = current_job.get()
        errors: list[BaseException] = []

        def read_answer(proc) -> dict:
            assert proc.stdout is not None
            for line in proc.stdout:
                if line.startswith("DJM:"):
                    return json.loads(line[4:])
            return {}  # process ended

        def start():
            proc = spawn(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=worker_log,
                         text=True, encoding="utf-8", errors="replace", bufsize=1, env=child_env())
            if not read_answer(proc).get("ready"):
                raise AnalysisError("analysis worker did not start")
            return proc

        def work():
            token = current_job.set(job)  # register worker processes with the job
            proc = None
            try:
                while not (job and job.cancel_requested):
                    try:
                        track_id, path, tasks, out, crashes = todo.get_nowait()
                    except queue.Empty:
                        return
                    if proc is None or proc.poll() is not None:
                        proc = start()
                    try:
                        assert proc.stdin is not None
                        proc.stdin.write(json.dumps({"id": track_id, "path": path, "tasks": tasks, "out": out}) + "\n")
                        proc.stdin.flush()
                        answer = read_answer(proc)
                    except (BrokenPipeError, OSError):
                        answer = {}
                    if job and job.cancel_requested:
                        return
                    if not answer:  # worker crashed - restart it; retry the file once before failing it
                        release(proc)
                        proc = None
                        if crashes == 0:
                            todo.put((track_id, path, tasks, out, 1))
                        else:
                            on_result(Result(track_id, path, tasks, {}, error="analysis crashed on this file"))
                        continue
                    on_result(Result(track_id, path, tasks, answer))
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
        worker_log.close()
        if job and job.cancel_requested:
            raise JobCancelled()
        if errors:
            raise errors[0]
