"""Song suggestions for a genre from Deezer's public API (no account, no key).

Spotify's API cannot tell which playlists contain a song, and Chosic's playlist search may
not be automated (its terms forbid it). Deezer's public API knows related artists and an
"artist radio" even for underground artists:
- each selected song is looked up on Deezer (by ISRC, else by artist + title),
- its artist's radio and the top songs of its related artists become suggestions,
- songs suggested for several of the selected artists rank first,
- songs already in the collection are left out.
Every Deezer song has a 30-second preview to listen to and an ISRC, which finds the very
same song on Spotify when it is added to a genre.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .util import normalize_text, urlopen

API = "https://api.deezer.com"
MAX_SEEDS = 25            # selected songs used per search
RADIO_LIMIT = 25          # songs of each artist radio
RELATED_PER_ARTIST = 10
RELATED_ARTISTS = 15      # related artists (shared by most selected artists) whose top songs count
TOP_PER_RELATED = 5
MAX_RESULTS = 150
RELATED_WEIGHT = 0.5      # a related artist's top song counts half as much as a radio song
WORKERS = 4               # Deezer allows 50 requests per 5 seconds
PREVIEWS_KEPT = 60


class DiscoverError(RuntimeError):
    pass


# url -> (status, body); replaceable in tests
Transport = Callable[[str], tuple[int, bytes]]


def urllib_transport(url: str) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": "dj-manager"})
    try:
        with urlopen(req, timeout=20) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise DiscoverError(f"Deezer is not reachable: {exc}") from exc


@dataclass
class Seed:
    artists: list[str]
    title: str
    isrc: str | None = None
    duration: float = 0.0

    @property
    def label(self) -> str:
        return f"{', '.join(self.artists)} - {self.title}"


def same_title(a: str, b: str) -> bool:
    x, y = normalize_text(a), normalize_text(b)
    return bool(x and y) and (x == y or x.startswith(y) or y.startswith(x))


def same_artist(names: list[str], artists: list[str]) -> bool:
    wanted = {normalize_text(a) for a in artists if a}
    return any(normalize_text(n) in wanted for n in names if n)


class Deezer:
    def __init__(self, transport: Transport = urllib_transport, pause: float = 1.0) -> None:
        self.transport = transport
        self.pause = pause  # back-off base when Deezer's quota is reached

    def get(self, path: str, **params) -> dict:
        url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
        for attempt in range(5):
            status, raw = self.transport(url)
            if status >= 500:
                time.sleep(self.pause * (attempt + 1))
                continue
            try:
                data = json.loads(raw or b"{}")
            except ValueError as exc:
                raise DiscoverError(f"Deezer answered with something unexpected (HTTP {status})") from exc
            error = data.get("error") if isinstance(data, dict) else None
            if not error:
                return data
            if error.get("code") == 4:  # quota: 50 requests per 5 seconds
                time.sleep(self.pause * (attempt + 1))
                continue
            if error.get("code") == 800:  # no data
                return {}
            raise DiscoverError(f"Deezer: {error.get('message') or error}")
        raise DiscoverError("Deezer is busy - try again in a minute")

    def track(self, deezer_id: int | str) -> dict:
        data = self.get(f"/track/{int(deezer_id)}")
        if not data.get("id"):
            raise DiscoverError(f"Song {deezer_id} not found on Deezer")
        return data

    def find(self, seed: Seed) -> dict | None:
        """The Deezer song for a song of the collection."""
        if seed.isrc:
            data = self.get(f"/track/isrc:{seed.isrc.upper()}")
            if data.get("id") and data.get("artist"):
                return data
        if not seed.artists or not seed.title:
            return None
        artist = seed.artists[0]
        for query in (f'artist:"{artist}" track:"{seed.title}"', f"{artist} {seed.title}"):
            hits = [h for h in self.get("/search", q=query, limit=10).get("data", []) if h.get("artist")]
            fitting = [h for h in hits if same_artist([h["artist"].get("name", "")], seed.artists)
                       and same_title(h.get("title_short") or h.get("title", ""), seed.title)]
            if seed.duration:
                fitting.sort(key=lambda h: abs((h.get("duration") or 0) - seed.duration))
            if fitting:
                return fitting[0]
        return None


def suggest(deezer: Deezer, seeds: list[Seed], is_known: Callable[[list[str], str, float], bool]) -> dict:
    """Suggestions for the seeds: {"songs": [...], "found": [labels], "missing": [labels]}."""
    seeds = seeds[:MAX_SEEDS]
    with ThreadPoolExecutor(WORKERS) as pool:
        hits = list(pool.map(deezer.find, seeds))
    found = [s.label for s, h in zip(seeds, hits) if h]
    missing = [s.label for s, h in zip(seeds, hits) if not h]
    artists: dict[int, str] = {}  # selected artists on Deezer: id -> name
    for hit in hits:
        if hit:
            artists.setdefault(hit["artist"]["id"], hit["artist"].get("name", ""))
    if not artists:
        return {"songs": [], "found": found, "missing": missing}

    ids = list(artists)
    with ThreadPoolExecutor(WORKERS) as pool:
        radios = list(pool.map(lambda a: deezer.get(f"/artist/{a}/radio", limit=RADIO_LIMIT).get("data", []), ids))
        relateds = list(pool.map(lambda a: deezer.get(f"/artist/{a}/related", limit=RELATED_PER_ARTIST).get("data", []), ids))

    candidates: dict[int, dict] = {}

    def offer(track: dict, weight: float, via: str) -> None:
        if not track.get("id") or not track.get("artist") or not track.get("readable", True):
            return
        c = candidates.setdefault(track["id"], {"track": track, "score": 0.0, "via": {}})
        if via not in c["via"]:  # every selected artist counts once per song
            c["via"][via] = weight
            c["score"] += weight

    for aid, radio in zip(ids, radios):
        for track in radio:
            offer(track, 1.0, artists[aid])
    shared: dict[int, dict] = {}  # related artist id -> {"name", "via": selected artist names}
    for aid, related in zip(ids, relateds):
        for rel in related:
            if rel.get("id") and rel["id"] not in artists:
                shared.setdefault(rel["id"], {"name": rel.get("name", ""), "via": [], "fans": rel.get("nb_fan", 0)})["via"].append(artists[aid])
    top = sorted(shared.items(), key=lambda kv: (-len(kv[1]["via"]), -kv[1]["fans"]))[:RELATED_ARTISTS]
    with ThreadPoolExecutor(WORKERS) as pool:
        tops = list(pool.map(lambda kv: deezer.get(f"/artist/{kv[0]}/top", limit=TOP_PER_RELATED).get("data", []), top))
    for (_, rel), songs in zip(top, tops):
        for track in songs:
            for via in rel["via"]:
                offer(track, RELATED_WEIGHT, via)

    songs, seen = [], set()
    ranked = sorted(candidates.values(), key=lambda c: (-c["score"], -(c["track"].get("rank") or 0)))
    for c in ranked:
        t = c["track"]
        artist = t["artist"].get("name", "")
        title = t.get("title", "")
        duration = float(t.get("duration") or 0)
        dedupe = f"{normalize_text(artist)}|{normalize_text(t.get('title_short') or title)}"
        if dedupe in seen or is_known([artist], title, duration) or is_known([artist], t.get("title_short") or title, duration):
            continue
        seen.add(dedupe)
        album = t.get("album") or {}
        songs.append({
            "id": str(t["id"]), "title": title, "artists": artist, "album": album.get("title", ""),
            "duration": duration, "cover": album.get("cover_small", ""), "link": t.get("link", ""),
            "score": round(c["score"], 2), "via": sorted(c["via"], key=lambda v: -c["via"][v]),
        })
        if len(songs) >= MAX_RESULTS:
            break
    return {"songs": songs, "found": found, "missing": missing}


def song_artists(track: dict) -> list[str]:
    """Main artist first, then the other contributors of a Deezer song."""
    names = [track.get("artist", {}).get("name", "")]
    for c in track.get("contributors") or []:
        if c.get("name") and c["name"] not in names:
            names.append(c["name"])
    return [n for n in names if n]


def pick_spotify(hits: list[dict], isrc: str | None, artists: list[str], title: str, duration: float) -> dict | None:
    """The Spotify song for a Deezer song: same ISRC, else same artist, title and length."""
    hits = [h for h in hits if h and h.get("id") and h.get("type", "track") == "track"]
    if isrc:
        exact = [h for h in hits if ((h.get("external_ids") or {}).get("isrc") or "").upper() == isrc.upper()]
        if exact:
            return max(exact, key=lambda h: h.get("popularity") or 0)
    for h in hits:
        names = [a.get("name", "") for a in h.get("artists") or []]
        length = (h.get("duration_ms") or 0) / 1000
        if same_artist(names, artists) and same_title(h.get("name", ""), title) \
                and (not duration or not length or abs(length - duration) <= 3):
            return h
    return None


def cached_preview(deezer: Deezer, deezer_id: str, folder: Path, fetch: Callable[[str], bytes] | None = None) -> Path:
    """The song's 30-second preview as a local file (Deezer's preview links expire)."""
    target = folder / f"{int(deezer_id)}.mp3"
    if target.exists():
        return target
    url = deezer.track(deezer_id).get("preview")
    if not url:
        raise DiscoverError("Deezer has no preview for this song")
    data = (fetch or _fetch_bytes)(url)
    folder.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(target)
    old = sorted(folder.glob("*.mp3"), key=lambda p: p.stat().st_mtime, reverse=True)[PREVIEWS_KEPT:]
    for path in old:
        path.unlink(missing_ok=True)
    return target


def _fetch_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "dj-manager"})
    try:
        with urlopen(req, timeout=30) as resp:
            return resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise DiscoverError(f"Preview not available: {exc}") from exc
