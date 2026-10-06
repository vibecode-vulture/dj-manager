"""Split recommendations: suggest groups of songs that could become a new sub genre.

Optional feature (Settings > Split recommendations). Nothing is changed automatically:
a suggestion only pre-fills the split dialog, which the user confirms.

The computation runs in the managed environment (numpy is there with the analysis
tools). Signals:
  * BPM / energy  - best two-way split along the value ("natural break"); suggested only
                    when the groups are clearly separated and big enough.
  * sound         - k-means on the sound fingerprint, 2-4 groups chosen by silhouette,
                    described in words ("brighter, busier").
  * styles (AI)   - k-means on the Discogs-EffNet embedding; the group is named after the
                    style that is most over-represented in it ("Speed Garage").
It also returns 2D coordinates for the interactive map.
"""

from __future__ import annotations

import json
import subprocess

from .deps import DependencyManager, _no_window, child_env

REC_SCRIPT = r'''
import json, sys
import numpy as np

data = json.load(sys.stdin)
tracks, signals, MIN = data["tracks"], data["signals"], int(data["min_group"])
labels = json.load(open(data["labels_file"], encoding="utf-8"))["classes"] if data.get("labels_file") else None
rng = np.random.default_rng(7)
ids = [t["id"] for t in tracks]
suggestions, maps = [], {}

def two_way(values):
    """Best split of 1-D values into a low and a high group (min within-group variance)."""
    order = np.argsort(values)
    v = np.asarray(values, dtype=float)[order]
    n = len(v)
    total = ((v - v.mean()) ** 2).sum()
    if n < 2 * MIN or total == 0:
        return None
    best = None
    for i in range(MIN, n - MIN + 1):
        lo, hi = v[:i], v[i:]
        within = ((lo - lo.mean()) ** 2).sum() + ((hi - hi.mean()) ** 2).sum()
        if best is None or within < best[0]:
            best = (within, i)
    within, i = best
    return order[:i], order[i:], 1 - within / total, v[:i], v[i:]

def noise_limit(n):
    """Separation that pure noise (normal distribution) reaches in only 1% of cases."""
    return 0.645 + 0.8 / np.sqrt(n)

def add_split(kind, values, idx, unit, fmt, min_gap, names, words=None, signal=None, strict=False):
    """Suggest the smaller side of the best two-way split.

    strict: only clearly separated groups (beyond what noise produces). Otherwise a wide
    range is enough (e.g. a genre spanning 124-145 BPM), as long as the groups differ by min_gap.
    """
    res = two_way(values)
    if res is None:
        return
    low, high, sep, lv, hv = res
    limit = noise_limit(len(values))
    if hv.mean() - lv.mean() < min_gap or sep < (limit if strict else 0.6):
        return
    small, big, side = (low, high, 0) if len(low) <= len(high) else (high, low, 1)
    sv, bv = (lv, hv) if side == 0 else (hv, lv)
    shape = "clear gap" if sep >= limit else "wide range"
    word = (words or ("lower " + kind, "higher " + kind))[side]
    suggestions.append({
        "signal": signal or kind, "score": round(float(sep), 3), "track_ids": [idx[k] for k in small],
        "title": f"{len(small)} songs at {fmt(sv.min())}-{fmt(sv.max())} {unit}",
        "description": f"{word} than the other {len(big)} songs ({fmt(bv.min())}-{fmt(bv.max())} {unit}); "
                       f"{shape}, separation {sep:.0%}",
        "short": f"{word} ({fmt(sv.min())}-{fmt(sv.max())} {unit})" if kind == "tempo" else word,
        "name_hint": names[side],
    })

def standardize(x):
    sd = x.std(axis=0)
    return (x - x.mean(axis=0)) / np.where(sd > 1e-9, sd, 1)

def pca(x, k):
    x = x - x.mean(axis=0)
    u, s, vt = np.linalg.svd(x, full_matrices=False)
    return x @ vt[:k].T

def signal_space(x, max_k):
    """Principal components that stand out from the noise floor (2..max_k of them)."""
    x = x - x.mean(axis=0)
    u, s, vt = np.linalg.svd(x, full_matrices=False)
    ev = s ** 2
    k = int(np.clip((ev > 2 * np.median(ev)).sum(), 2, max_k))
    return x @ vt[:k].T

def kmeans(x, k, restarts=8, iters=60):
    best = None
    for _ in range(restarts):
        centers = x[rng.choice(len(x), 1)]
        for _ in range(k - 1):  # k-means++ initialisation
            d = ((x[:, None] - centers[None]) ** 2).sum(-1).min(1)
            centers = np.vstack([centers, x[rng.choice(len(x), 1, p=d / d.sum())]])
        for _ in range(iters):
            lab = ((x[:, None] - centers[None]) ** 2).sum(-1).argmin(1)
            new = np.array([x[lab == j].mean(0) if (lab == j).any() else centers[j] for j in range(k)])
            if np.allclose(new, centers):
                break
            centers = new
        inertia = ((x - centers[lab]) ** 2).sum()
        if best is None or inertia < best[0]:
            best = (inertia, lab)
    return best[1]

def silhouette(x, lab):
    if len(x) > 1500:  # a random sample is accurate enough and keeps memory small
        pick = rng.choice(len(x), 1500, replace=False)
        x, lab = x[pick], lab[pick]
    sq = (x ** 2).sum(1)
    d = np.sqrt(np.maximum(sq[:, None] + sq[None] - 2 * x @ x.T, 0))
    scores = []
    for i in range(len(x)):
        same = lab == lab[i]
        if same.sum() < 2:
            scores.append(0.0)
            continue
        a = d[i, same].sum() / (same.sum() - 1)
        b = min(d[i, lab == j].mean() for j in set(lab.tolist()) if j != lab[i])
        scores.append((b - a) / max(a, b) if max(a, b) > 0 else 0.0)
    return float(np.mean(scores))

def clusters(x, min_sil, evaluate=None):
    """Best k-means grouping (k = 2..4) by silhouette, or None if there is no clear structure.

    evaluate: space in which the grouping is judged. Clustering in a reduced space is
    fine, but judging it there would see structure in pure noise (few songs, many dims).
    """
    evaluate = x if evaluate is None else evaluate
    if len(x) < 2 * MIN:
        return None
    best = None
    for k in range(2, 5):
        if len(x) < k * MIN:
            break
        lab = kmeans(x, k)
        sizes = np.bincount(lab, minlength=k)
        if sizes.min() < MIN:
            continue
        sil = silhouette(evaluate, lab)
        if best is None or sil > best[0]:
            best = (sil, lab)
    return best if best and best[0] >= min_sil else None

# ---------------------------------------------------------------- BPM / energy
if signals.get("bpm"):
    idx = [i for i, t in enumerate(tracks) if t.get("bpm")]
    add_split("tempo", [tracks[i]["bpm"] for i in idx], [ids[i] for i in idx], "BPM",
              lambda v: f"{v:.0f}", min_gap=4, names=("slow", "fast"), words=("slower", "faster"))
if signals.get("energy"):
    idx = [i for i, t in enumerate(tracks) if t.get("energy") is not None]
    add_split("energy", [tracks[i]["energy"] for i in idx], [ids[i] for i in idx], "energy",
              lambda v: f"{v:.1f}", min_gap=1.5, names=("warm-up", "peak-time"), words=("calmer", "more energetic"))

# ---------------------------------------------------------------- vectors
def load(name):
    rows, keep = [], []
    for i, t in enumerate(tracks):
        try:
            with np.load(t["file"]) as z:
                if name in z.files:
                    rows.append(z[name].astype(np.float64))
                    keep.append(i)
        except (OSError, ValueError, KeyError):
            pass
    return (np.array(rows), keep) if rows else (None, [])

DESC = [(40, "brighter", "darker"), (41, "more bass", "less bass"), (42, "busier, more percussive", "sparser"),
        (43, "louder", "quieter"), (44, "more dynamic", "flatter, more compressed")]
# per descriptor: unit, formatting and minimal difference worth mentioning
UNITS = {40: ("kHz brightness", lambda v: f"{v:.1f}", 0.3), 41: ("% bass", lambda v: f"{100 * v:.0f}", 0.05),
         42: ("onsets/s", lambda v: f"{v:.1f}", 0.8), 43: ("dB loudness", lambda v: f"{v:.0f}", 2.0),
         44: ("dB dynamic range", lambda v: f"{v:.0f}", 2.0)}

timbre, t_keep = load("timbre")
if timbre is not None and len(timbre) >= 3:
    z = standardize(timbre)
    maps["sound"] = [[ids[i], float(p[0]), float(p[1])] for i, p in zip(t_keep, pca(z, 2))]
    if signals.get("timbre"):
        # 1) one clearly different property (e.g. a much brighter group)
        for d, up, down in DESC:
            unit, fmt, gap = UNITS[d]
            add_split("sound", list(timbre[:, d]), [ids[i] for i in t_keep], unit, fmt, min_gap=gap,
                      names=("", ""), words=(down, up), strict=True)
        # 2) a combination of properties: groups in the space of the descriptors
        found = clusters(z[:, 40:46], min_sil=0.35)
        if found:
            sil, lab = found
            biggest = np.bincount(lab).argmax()
            for c in sorted(set(lab.tolist())):
                if c == biggest:
                    continue
                members = np.where(lab == c)[0]
                centre = z[members].mean(0)
                words = [(abs(centre[d]), up if centre[d] > 0 else down) for d, up, down in DESC if abs(centre[d]) >= 0.5]
                words = [w for _, w in sorted(words, reverse=True)[:3]] or ["a different sound"]
                suggestions.append({
                    "signal": "sound", "score": round(sil, 3), "track_ids": [ids[t_keep[m]] for m in members],
                    "title": f"{len(members)} songs with a similar sound",
                    "description": ", ".join(words) + f" than the rest; grouping quality {sil:.2f}",
                    "short": ", ".join(words), "name_hint": "",
                })

emb, e_keep = load("embedding")
if emb is not None and len(emb) >= 3:
    e = emb / np.maximum(np.linalg.norm(emb, axis=1, keepdims=True), 1e-9)
    maps["style"] = [[ids[i], float(p[0]), float(p[1])] for i, p in zip(e_keep, pca(e, 2))]
    if signals.get("styles"):
        act, _ = load("styles")
        found = clusters(signal_space(e, min(10, len(e) - 1)), min_sil=0.1, evaluate=e)
        if found:
            sil, lab = found
            biggest = np.bincount(lab).argmax()
            overall = act.mean(0) if act is not None and len(act) == len(e) else None
            for c in sorted(set(lab.tolist())):
                if c == biggest:
                    continue
                members = np.where(lab == c)[0]
                name, why = "", "similar style"
                if overall is not None and labels:
                    group = act[members].mean(0)
                    lift = np.where(group >= 0.05, group - overall, -1)
                    top = int(np.argmax(lift))
                    name = labels[top].split("---")[-1]
                    why = f"mostly {name} ({group[top]:.0%} vs {overall[top]:.0%} in the whole genre)"
                suggestions.append({
                    "signal": "styles", "score": round(sil, 3), "track_ids": [ids[e_keep[m]] for m in members],
                    "title": f"{len(members)} songs" + (f": {name}" if name else " with a similar style"),
                    "description": why + f"; grouping quality {sil:.2f}",
                    "short": f"mostly {name}" if name else "similar style",
                    "name_hint": name.lower().replace(" ", "-"),
                })

maps["bpm_energy"] = [[t["id"], t["bpm"], t["energy"]] for t in tracks if t.get("bpm") and t.get("energy") is not None]
# Several signals finding (almost) the same songs make one suggestion that lists all of
# them - agreement between signals is the strongest hint for a real sub genre.
unique = []
for s in sorted(suggestions, key=lambda s: -s["score"]):
    ids_s = set(s["track_ids"])
    same = next((u for u in unique if len(ids_s & set(u["track_ids"])) / len(ids_s | set(u["track_ids"])) >= 0.8), None)
    if same is None:
        unique.append({**s, "signals": [s["signal"]], "reasons": [s["description"]], "shorts": [s["short"]]})
    elif s["signal"] not in same["signals"]:
        same["signals"].append(s["signal"])
        same["reasons"].append(s["description"])
        same["shorts"].append(s["short"])
        same["name_hint"] = same["name_hint"] or s["name_hint"]
for u in unique:  # agreement first, then separation quality
    u["score"] = round(u["score"] + 0.25 * (len(u["signals"]) - 1), 3)
    u["title"] = f"{len(u['track_ids'])} songs: " + ", ".join(u["shorts"])
unique.sort(key=lambda u: -u["score"])
print(json.dumps({"suggestions": unique, "maps": maps}))
'''


class RecommendError(RuntimeError):
    pass


def run_recommendations(deps: DependencyManager, payload: dict, timeout: int = 180) -> dict:
    if not deps.is_installed():
        raise RecommendError("Install the analysis tools first (Settings > Analysis)")
    out = subprocess.run([str(deps.python), "-c", REC_SCRIPT], input=json.dumps(payload), capture_output=True,
                         text=True, encoding="utf-8", timeout=timeout, env=child_env(), **_no_window())
    if out.returncode != 0:
        lines = (out.stderr or "").strip().splitlines()
        if any("No module named 'numpy'" in line for line in lines):
            raise RecommendError("Install the analysis tools first (Settings > Analysis)")
        raise RecommendError("Recommendations failed: " + (lines[-1] if lines else f"exit {out.returncode}"))
    return json.loads(out.stdout.strip().splitlines()[-1])
