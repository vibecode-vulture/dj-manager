# DJ Manager

Single source of truth for a genre-based music collection. Spotify playlists (via
[spotdl](https://github.com/spotDL/spotify-downloader)) are the discovery tool, folders on
disk store every song once, and Traktor gets generated playlists for every genre layer.

The spec is in `requirements.txt`.

## Run

Requires Python 3.10+ (Windows or Linux).

```bash
python -m venv .venv
.venv/bin/pip install -e ".[window]"      # Windows: .venv\Scripts\pip install -e ".[window]"
.venv/bin/dj-manager                     # native window (pywebview)
.venv/bin/dj-manager --browser           # or in the browser
```

On Linux pywebview needs a GUI backend (`pip install "pywebview[qt]"` or the GTK packages of
your distribution). Without one, DJ Manager opens the browser instead.

First start:
1. **Dependencies** → *Install spotdl + yt-dlp*, then *Download ffmpeg via spotdl*.
2. **Settings** → select the main music folder. Existing folders are imported as genres.
   The Traktor `collection.nml` is auto-detected (Windows `Documents/Native Instruments/Traktor x.y.z`,
   Linux: Wine prefixes `~/.wine`, `~/.local/share/wineprefixes/*`, `~/Games/*`) or can be selected.
3. Optional: own Spotify app credentials and user login (redirect URI `http://127.0.0.1:9900/`).

Close Traktor whenever DJ Manager writes the collection. DJ Manager checks for a running
Traktor and refuses to write while it is open.

## Concepts

| Term | Meaning |
|---|---|
| Genre key | `techno_hard-techno`: `_` separates layers, `-` stands for a space. Folder: `techno/hard-techno/` |
| Playlist | One genre node, optionally linked to a Spotify playlist. 1 playlist = 1 genre |
| Traktor | Folder `DJ Manager` with nested genre folders. Every genre has a playlist that includes all its sub genres |
| `LOCAL` | Track in a linked playlist that is not part of the Spotify playlist (imported or kept after a link change) |
| Blacklist | Songs removed manually from a playlist. They are not added again by the next update |
| Removed | Tracks that are in no genre anymore. They are never deleted; delete the file yourself and they disappear |
| Duplicates | Copies of the same song found during import. The first file found is used and the others are listed |

Songs are identified by the Spotify URL that spotdl embeds, then by ISRC, then by
artist + title (± 3 s duration).

**Removing a playlist** deletes its Traktor playlist and folder. Files still used by other
playlists move into one of their folders, and the rest move to `<music>/_removed/<key>/`.
Moves are applied to the Traktor collection entries, so cue points and beat grids survive.

## Data locations

| What | Linux | Windows |
|---|---|---|
| Library state | `<music>/.djmanager/library.json` | same |
| Settings | `~/.config/dj-manager/settings.json` | `%APPDATA%\DJManager` |
| spotdl environment, snapshots, Traktor backups | `~/.local/share/dj-manager/` | `%LOCALAPPDATA%\DJManager` |

Set `DJMANAGER_HOME` to keep settings and data in a single portable folder.

## Dependency updates

spotdl and yt-dlp run in their own virtual environment and can be updated from the UI
(latest or any version from PyPI). Before every change the full `pip freeze` is
snapshotted, and any snapshot can be restored. After a successful playlist update, the
current snapshot is marked as known good.

## Development

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

Layout: `djmanager/` contains `genres` (naming), `library` (model and persistence), `scanner`
(initial import), `audio` (tags), `spotdl_client`, `deps` (managed environment), `traktor`
(NML), `backup`, `service` (operations as background jobs), `api` (FastAPI) and `static/` (UI).
