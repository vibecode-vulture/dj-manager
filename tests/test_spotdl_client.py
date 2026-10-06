"""SpotdlClient against a stub 'spotdl' package, run in a real subprocess."""

import sys
import textwrap

import pytest

from djmanager.settings import Settings
from djmanager.spotdl_client import SpotdlClient

SONGS = [
    {"song_id": "a" * 22, "name": "Track A", "artists": ["Artist"], "artist": "Artist", "duration": 200,
     "url": "https://open.spotify.com/track/" + "a" * 22, "isrc": ""},
    {"song_id": "b" * 22, "name": "Track B", "artists": ["Other"], "artist": "Other", "duration": 300,
     "url": "https://open.spotify.com/track/" + "b" * 22, "isrc": ""},
]


class Deps:
    python = sys.executable

    def is_installed(self):
        return True

    def installed_versions(self):
        return {"spotdl": "stub"}


def make_stub(root, fast: bool):
    pkg = root / "spotdl"
    (pkg / "utils").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "utils" / "__init__.py").write_text("")
    (pkg / "utils" / "config.py").write_text("DEFAULT_CONFIG = {'client_id': 'id', 'client_secret': 'secret'}\n")
    (pkg / "utils" / "spotify.py").write_text("class SpotifyClient:\n    @classmethod\n    def init(cls, **kw):\n        pass\n")
    if fast:
        (pkg / "utils" / "search.py").write_text(textwrap.dedent(f"""
            class S:
                def __init__(self, d): self.json = d
            def get_simple_songs(query):
                return [S(d) for d in {SONGS!r}]
        """))
    else:  # an older/newer spotdl without that function -> CLI fallback
        (pkg / "utils" / "search.py").write_text("")
        (pkg / "__main__.py").write_text(textwrap.dedent(f"""
            import json, sys
            out = sys.argv[sys.argv.index('--save-file') + 1]
            json.dump({SONGS!r} + {SONGS[:1]!r}, open(out, 'w'))
            print('saved via cli')
        """))


@pytest.mark.parametrize("fast", [True, False])
def test_fetch_playlist(home, tmp_path, monkeypatch, fast):
    make_stub(tmp_path / "stub", fast)
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "stub"))
    lines = []
    songs = SpotdlClient(Deps(), Settings()).fetch_playlist("https://open.spotify.com/playlist/x", lines.append)
    assert [s.title for s in songs] == ["Track A", "Track B"]  # duplicate removed
    assert songs[1].duration == 300 and songs[0].spotify_id == "a" * 22
    assert any("saved via cli" in line for line in lines) is (not fast)
