# DJ Manager

Single source of truth for a genre-based music collection. Spotify playlists (via
[spotdl](https://github.com/spotDL/spotify-downloader)) are the discovery tool, folders on
disk store every song once, and Traktor gets generated playlists for every genre layer.

The spec is in `requirements.txt`.

## Install

Download the file for your system from the latest GitHub release:

| File | System | Notes |
|---|---|---|
| `DJManager-<version>-setup.exe` | Windows 10/11 (x64) | **Recommended.** Installs per user (no admin rights), adds Start menu entries, updates itself |
| `DJManager-<version>-portable.exe` | Windows 10/11 (x64) | Single file, no installation. Keeps its data in `DJManager-data\` next to the exe |
| `dj-manager-<version>-linux-x86_64` | Linux x86_64 (glibc 2.35+, e.g. Ubuntu 22.04+) | Single file. `chmod +x` it and run; the UI opens in your browser |

Nothing else needs to be installed. The first time you install spotdl from the
Dependencies page, the packaged app downloads its own Python runtime (about 30 MB) and
creates spotdl's environment from it.

The Windows app shows its UI in a native window. That needs the Microsoft Edge WebView2
runtime, which Windows 10/11 normally include. Without it, the app falls back to the browser.

### Run from source

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

The portable Windows exe keeps settings and data in `DJManager-data\` next to the exe.
Set `DJMANAGER_HOME` to keep settings and data in a single folder of your choice.
Windowed builds write their log to `<data>/logs/dj-manager.log`.

## Dependency updates

spotdl and yt-dlp run in their own virtual environment and can be updated from the UI
(latest or any version from PyPI). Before every change the full `pip freeze` is
snapshotted, and any snapshot can be restored. After a successful playlist update, the
current snapshot is marked as known good.

## App updates

DJ Manager checks the latest GitHub release when it starts (Settings → Updates can turn
that off or check manually). When a newer version exists, an **UPDATE TO x.y.z** button
appears in the top bar. Every download is checked against the release's `SHA256SUMS.txt`
before anything is replaced.

| Installation | How the update is applied |
|---|---|
| Installer | Downloads the new `setup.exe` and runs it silently over the existing installation, then DJ Manager starts again |
| Portable exe | Renames the running exe to `.old.exe`, puts the new one in its place and restarts (the old file is removed on the next start) |
| Linux binary | Replaces the binary in place and restarts |
| Source checkout | Shows the new version; update with `git pull` |

Your library, settings, backups and spotdl environment are not touched by updates.
The release source is the GitHub repository the build came from. To use a different one,
set Settings → Updates → *Release source* (`owner/repo`).

## Building and releasing

Build for the platform you are on (PyInstaller cannot cross-compile):

```bash
pip install -e ".[build]"            # Windows: add ,window → ".[window,build]"
python packaging/build.py --repo owner/repo
```

| Platform | Output in `dist/` | Requirements |
|---|---|---|
| Linux | `dj-manager-<v>-linux-x86_64` | build on the oldest distro you want to support (glibc) |
| Windows | `DJManager-<v>-portable.exe`, `DJManager-<v>-setup.exe` | [Inno Setup 6](https://jrsoftware.org/isinfo.php) for the installer; `--skip-installer` builds only the portable exe |

`--repo` sets which GitHub repository the updater checks. The installer script is
`packaging/windows/installer.iss`. Never change its `AppId`: it is what lets new versions
install over old ones.

**Release:** bump `__version__` in `djmanager/__init__.py`, commit, then

```bash
git tag v0.2.0 && git push origin main v0.2.0
```

`.github/workflows/release.yml` runs the tests, checks that the tag matches the version,
builds all three files on GitHub's Windows and Ubuntu 22.04 runners, and publishes them
with `SHA256SUMS.txt` as a release. Installed copies pick the release up on their next start.

## Development

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

Layout: `djmanager/` contains `genres` (naming), `library` (model and persistence), `scanner`
(initial import), `audio` (tags), `spotdl_client`, `deps` (managed environment), `traktor`
(NML), `backup`, `updater` (self-update), `service` (operations as background jobs), `api` (FastAPI)
and `static/` (UI). `packaging/` holds the build script, PyInstaller launcher, icon and installer.
