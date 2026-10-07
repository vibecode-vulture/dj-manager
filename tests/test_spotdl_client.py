"""SpotdlClient against a stub 'spotdl' package, run in a real subprocess."""

import os
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
    bin_dir = os.path.dirname(sys.executable)

    def is_installed(self):
        return True

    def installed_versions(self):
        return {"spotdl": "stub"}

    def ensure_managed(self, log=print):
        pass


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


def test_download_lands_in_staging_even_below_dot_folders(tmp_path, monkeypatch):
    """Regression: spotdl drops leading dots of output path parts (~/.local -> ~/local)."""
    from djmanager import spotdl_client
    from djmanager.spotdl_client import RemoteSong

    home = tmp_path / ".local" / "share"  # like the real data dir
    monkeypatch.setenv("DJMANAGER_HOME", str(home))
    stub = tmp_path / "stub" / "spotdl"
    stub.mkdir(parents=True)
    (stub / "__init__.py").write_text("")
    (stub / "__main__.py").write_text(textwrap.dedent(r"""
        import json, os, sys
        args = sys.argv[1:]
        out = args[args.index("--output") + 1]
        songs = json.load(open(args[1]))
        for song in songs:
            path = out.replace("{track-id}", song["song_id"]).replace("{output-ext}", "mp3")
            # what spotdl does: sanitise every path part, which drops leading dots
            parts = path.split(os.sep)
            path = os.sep.join(p.lstrip(".") if p not in ("", ".", "..") else p for p in parts)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            open(path, "wb").write(b"x")
            print(f'Downloaded "{song["name"]}"')
    """))
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "stub"))
    monkeypatch.setattr(spotdl_client.SpotdlClient, "_complete", staticmethod(lambda f, s: True))
    song = RemoteSong.from_dict(SONGS[0])
    files = SpotdlClient(Deps(), Settings()).download([song], lambda line: None)
    assert set(files) == {song.spotify_id}
    assert ".local" in str(files[song.spotify_id])  # inside the real staging folder


def test_not_found_messages_are_matched_to_songs():
    from djmanager.spotdl_client import RemoteSong

    songs = [RemoteSong.from_dict({**SONGS[0], "artists": ["Blura"], "name": "Exes - Speed Garage"}),
             RemoteSong.from_dict({**SONGS[1], "artists": ["Phrva"], "name": "Is It All"})]
    lines = ["Blura - Exes - Speed Garage: Searching for song",
             "LookupError: No results found for song: Blura - Exes - Speed Garage",
             "AudioProviderError: YT-DLP download error - https://music.youtube.com/watch?v=-g8PQ6o1NmI"]
    found = SpotdlClient._not_found(songs, lines)
    assert list(found) == [songs[0].spotify_id]  # the yt-dlp error is a technical failure, not "not found"


def test_missing_components_like_deno_are_installed(home, monkeypatch):
    from djmanager.deps import MANAGED_PACKAGES, DependencyManager

    deps = DependencyManager()
    installed = []
    monkeypatch.setattr(deps, "is_installed", lambda: True)
    monkeypatch.setattr(deps, "installed_versions", lambda: {"spotdl": "4.5.2", "yt-dlp": "2026.8.19", "yt-dlp-ejs": "0.8.0", "deno": None, "librosa": None})
    monkeypatch.setattr(deps, "install_packages", lambda pkgs, log=print: installed.extend(pkgs))
    deps.ensure_managed(lambda line: None)
    assert installed == ["deno"]  # optional analysis packages are not forced
    assert "deno" in MANAGED_PACKAGES and "yt-dlp-ejs" in MANAGED_PACKAGES
    installed.clear()
    monkeypatch.setattr(deps, "installed_versions", lambda: {p: "1" for p in MANAGED_PACKAGES})
    deps.ensure_managed(lambda line: None)
    assert installed == []
