"""Small shared helpers."""

from __future__ import annotations

import os
import re
import shutil
import ssl
import tempfile
import unicodedata
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".flac", ".wav", ".aiff", ".aif", ".ogg", ".opus", ".mp4"}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


_INVALID_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_filename(name: str, max_len: int = 180) -> str:
    name = _INVALID_FILENAME.sub("", name).strip().rstrip(".")
    return name[:max_len] or "track"


def unique_path(path: Path) -> Path:
    """Return path, or 'name (2).ext' etc. if it already exists."""
    if not path.exists():
        return path
    for i in range(2, 10_000):
        candidate = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"no free filename for {path}")


def move_file(src: Path, dst_dir: Path) -> Path:
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = unique_path(dst_dir / src.name)
    shutil.move(str(src), str(dst))
    return dst


def normalize_text(value: str) -> str:
    """Normalize for fuzzy matching: lowercase, no accents, no feat., no punctuation."""
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(c for c in value if not unicodedata.combining(c)).lower()
    value = re.sub(r"[\(\[]\s*(feat|ft|with)\.?\s[^\)\]]*[\)\]]", "", value)
    value = re.sub(r"\s(feat|ft)\.?\s.*$", "", value)
    value = re.sub(r"[^a-z0-9]+", "", value)
    return value


def spotify_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    match = re.search(r"open\.spotify\.com/(?:intl-[a-z]+/)?track/([A-Za-z0-9]{22})", url)
    if match:
        return match.group(1)
    match = re.fullmatch(r"spotify:track:([A-Za-z0-9]{22})", url.strip())
    return match.group(1) if match else None


def is_spotify_playlist_url(url: str) -> bool:
    return bool(
        re.search(r"open\.spotify\.com/(?:intl-[a-z]+/)?(playlist|album)/[A-Za-z0-9]+", url or "")
        or re.fullmatch(r"spotify:(playlist|album):[A-Za-z0-9]+", (url or "").strip())
    )


_SSL: ssl.SSLContext | None = None


def ssl_context() -> ssl.SSLContext:
    """System certificates plus the bundled certifi ones.

    Packaged builds cannot rely on the system alone: Python on Windows only knows the
    roots already in the Windows store, and a Linux binary looks for the CA bundle where
    the build machine kept it. certifi makes HTTPS work everywhere; the system store keeps
    e.g. company proxy certificates working.
    """
    global _SSL
    if _SSL is None:
        ctx = ssl.create_default_context()
        try:
            import certifi

            ctx.load_verify_locations(certifi.where())
        except (ImportError, OSError):
            pass
        _SSL = ctx
    return _SSL


def urlopen(url_or_request, timeout: float = 30):
    return urllib.request.urlopen(url_or_request, timeout=timeout, context=ssl_context())


def download(url: str, target, timeout: float = 120) -> None:
    """Download to target (via a temporary file, so an interrupted download leaves nothing)."""
    target = Path(target)
    tmp = target.with_name(target.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "dj-manager"})
    with urlopen(req, timeout=timeout) as resp, open(tmp, "wb") as out:
        shutil.copyfileobj(resp, out, 1 << 20)
    os.replace(tmp, target)
