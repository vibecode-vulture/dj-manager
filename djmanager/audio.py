"""Read the metadata needed to identify a track (incl. the Spotify URL spotdl embeds)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .util import spotify_id_from_url

try:
    import mutagen
except ImportError:  # pragma: no cover - mutagen is a hard dependency
    mutagen = None


@dataclass
class AudioInfo:
    title: str = ""
    artists: list[str] = field(default_factory=list)
    album: str = ""
    duration: float = 0.0
    spotify_id: str | None = None
    isrc: str | None = None
    rating: int | None = None  # 1-5 stars, None = not rated


def popm_to_stars(value: int) -> int | None:
    """ID3 POPM rating (0-255) to stars. Fits Traktor (51/102/153/204/255) and WMP (1/64/128/196/255)."""
    if value <= 0:
        return None
    for stars, limit in ((1, 64), (2, 128), (3, 196), (4, 255)):
        if value < limit:
            return stars
    return 5


def scaled_to_stars(raw: str) -> int | None:
    """Vorbis/MP4 ratings come as 0-1, 1-5 or 0-100 depending on the program that wrote them."""
    try:
        value = float(str(raw).strip())
    except ValueError:
        return None
    if value <= 0:
        return None
    if value <= 1:
        value *= 5
    elif value > 5:
        value /= 20
    return max(1, min(5, int(round(value))))


def _first(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "ignore")
    if hasattr(value, "text"):  # ID3 frames
        return _first(value.text)
    if hasattr(value, "url"):  # ID3 url frames
        return str(value.url)
    return str(value)


def _split_artists(value: str) -> list[str]:
    parts = re.split(r"\s*(?:,|;|/|\s&\s|\sfeat\.?\s|\sft\.?\s)\s*", value or "")
    return [p for p in (s.strip() for s in parts) if p]


def read_info(path: Path) -> AudioInfo:
    info = AudioInfo()
    if mutagen is None:
        return info
    try:
        audio = mutagen.File(path)
    except Exception:
        audio = None
    if audio is None:
        return _from_filename(info, path)
    if audio.info is not None and getattr(audio.info, "length", None):
        info.duration = round(float(audio.info.length), 2)

    tags = audio.tags or {}
    url = ""
    try:
        keys = list(tags.keys())
    except Exception:
        keys = []

    def get(*names: str) -> str:
        for name in names:
            if name in keys:
                return _first(tags[name])
        return ""

    if any(k.startswith(("TIT2", "TPE1", "TALB", "WOAS", "TSRC", "POPM")) for k in keys):  # ID3
        popm = [tags[k] for k in keys if k.startswith("POPM")]
        # several programs can each store a rating - prefer Traktor's
        popm.sort(key=lambda f: "native-instruments" not in (getattr(f, "email", "") or ""))
        if popm:
            info.rating = popm_to_stars(int(getattr(popm[0], "rating", 0) or 0))
        info.title = get("TIT2")
        artists = tags["TPE1"].text if "TPE1" in keys else []
        info.artists = [a for a in (str(x).strip() for x in artists) if a]
        if len(info.artists) == 1:
            info.artists = _split_artists(info.artists[0])
        info.album = get("TALB")
        info.isrc = get("TSRC") or None
        url = get("WOAS") or next((_first(tags[k]) for k in keys if k.startswith("WOAS")), "")
        if not url:
            url = next((_first(tags[k]) for k in keys if k.startswith("COMM") and "spotify" in _first(tags[k])), "")
    elif "\xa9nam" in keys or "----:spotdl:WOAS" in keys:  # MP4 / M4A
        info.title = get("\xa9nam")
        info.artists = _split_artists(get("\xa9ART"))
        info.album = get("\xa9alb")
        info.isrc = get("----:spotdl:ISRC") or None
        url = get("----:spotdl:WOAS", "\xa9cmt")
        rating = get("rate", "----:com.apple.iTunes:RATING", "----:com.apple.iTunes:rating")
        info.rating = scaled_to_stars(rating) if rating else None
    else:  # Vorbis comments (flac, ogg, opus) and others
        lower = {k.lower(): k for k in keys}

        def vget(*names: str) -> str:
            for name in names:
                if name in lower:
                    return _first(tags[lower[name]])
            return ""

        info.title = vget("title")
        artists = tags[lower["artist"]] if "artist" in lower else []
        info.artists = [str(a) for a in artists] if isinstance(artists, list) else _split_artists(str(artists))
        if len(info.artists) == 1:
            info.artists = _split_artists(info.artists[0])
        info.album = vget("album")
        info.isrc = vget("isrc") or None
        rating = vget("fmps_rating", "rating")
        info.rating = scaled_to_stars(rating) if rating else None
        url = vget("url", "woas", "comment")

    info.spotify_id = spotify_id_from_url(url)
    if not info.title:
        return _from_filename(info, path)
    return info


def _from_filename(info: AudioInfo, path: Path) -> AudioInfo:
    """'Artist - Title.mp3' fallback for untagged files."""
    info.title = path.stem
    if not info.artists and " - " in path.stem:
        artist, title = path.stem.split(" - ", 1)
        info.artists, info.title = _split_artists(artist), title.strip()
    return info
