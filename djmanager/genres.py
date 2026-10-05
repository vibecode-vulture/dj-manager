"""Genre keys.

A genre key encodes the hierarchy in one name: '_' separates layers and '-'
stands for a space, e.g. 'techno_hard-techno' is 'techno > hard techno'.
On disk every layer is a folder named like the layer: techno/hard-techno/.
"""

from __future__ import annotations

import re

SEPARATOR = "_"
_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class GenreError(ValueError):
    pass


def normalize_part(part: str) -> str:
    part = part.strip()
    part = re.sub(r"\s+", "-", part)
    part = part.replace(SEPARATOR, "-")
    part = re.sub(r"-{2,}", "-", part).strip("-")
    return part


def parse_key(key: str) -> list[str]:
    """Validate a genre key and return its layers."""
    if not key or not key.strip():
        raise GenreError("Genre name must not be empty")
    raw_parts = key.strip().split(SEPARATOR)
    parts = [normalize_part(p) for p in raw_parts]
    if any(not p for p in parts):
        raise GenreError(f"'{key}' contains an empty genre layer")
    for part in parts:
        if _INVALID.search(part):
            raise GenreError(f"'{part}' contains characters not allowed in folder names")
        if part in {".", ".."} or part.startswith("."):
            raise GenreError(f"'{part}' is not a valid genre name")
        if part.lower() in {"_removed", "con", "prn", "aux", "nul"}:
            raise GenreError(f"'{part}' is a reserved name")
    return parts


def make_key(parts: list[str]) -> str:
    return SEPARATOR.join(normalize_part(p) for p in parts)


def normalize_key(key: str) -> str:
    return make_key(parse_key(key))


def ancestors(key: str) -> list[str]:
    """'a_b_c' -> ['a', 'a_b'] (excluding the key itself)."""
    parts = key.split(SEPARATOR)
    return [SEPARATOR.join(parts[:i]) for i in range(1, len(parts))]


def lineage(key: str) -> list[str]:
    """'a_b_c' -> ['a', 'a_b', 'a_b_c']."""
    return ancestors(key) + [key]


def parent(key: str) -> str | None:
    parts = key.split(SEPARATOR)
    return SEPARATOR.join(parts[:-1]) if len(parts) > 1 else None


def is_descendant_or_self(key: str, root: str) -> bool:
    return key.lower() == root.lower() or key.lower().startswith(root.lower() + SEPARATOR)


def display_name(key: str) -> str:
    """Last layer, human readable."""
    return key.split(SEPARATOR)[-1].replace("-", " ")
