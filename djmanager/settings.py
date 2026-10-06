"""User settings, persisted as JSON in the config directory."""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, fields

from . import paths
from .util import atomic_write_text


@dataclass
class Settings:
    music_folder: str = ""
    # Path to Traktor's collection.nml. Empty = auto-detect.
    traktor_nml: str = ""
    # "auto" (native on Windows, Wine on Linux), "native" or "wine"
    traktor_path_mode: str = "auto"
    # Wine prefix used to translate Linux paths; empty = derive from NML path or ~/.wine
    wine_prefix: str = ""
    # Name of the Traktor playlist folder owned by DJ Manager
    traktor_root_folder: str = "DJ Manager"

    # Client ID of the user's own Spotify app: the one Spotify login (spotify_api.py)
    spotify_client_id: str = ""
    # No longer used (spotdl reads playlists without credentials); kept so old settings load
    spotify_auth_mode: str = "default"
    spotify_client_secret: str = ""
    spotify_user_auth: bool = False
    spotify_user_name: str = ""
    # Name of playlists DJ Manager creates on Spotify: prefix + genre key
    spotify_playlist_prefix: str = "DJM · "

    audio_format: str = "mp3"  # mp3 | m4a
    bitrate: str = "320k"  # e.g. 320k, 256k, auto, disable
    download_threads: int = 4
    cookie_file: str = ""  # optional YouTube Music cookies (premium quality)

    update_on_start: bool = True
    scan_on_start: bool = True  # find songs added to the music folder outside DJ Manager

    # BPM / key analysis
    analysis_auto: bool = True
    analysis_paused: bool = False  # set when the user stops an analysis; cleared by Resume
    analysis_workers: int = 0      # 0 = automatic (CPU cores - 1, at most 4)
    bpm_min: int = 70
    bpm_max: int = 185
    key_notation: str = "openkey"  # openkey | camelot | musical

    # Split recommendations (optional, off by default)
    rec_enabled: bool = False
    rec_bpm: bool = True
    rec_energy: bool = True
    rec_timbre: bool = True
    rec_styles: bool = False
    features_auto: bool = True     # energy/sound analysed together with BPM/key for new songs
    styles_auto: bool = False      # AI styles only when requested
    rec_min_group: int = 6         # smallest group worth suggesting
    # GitHub "owner/repo" with DJ Manager releases; empty = the repo the build came from
    update_repo: str = ""
    check_app_updates: bool = True
    backups_to_keep: int = 30

    def public(self) -> dict:
        data = asdict(self)
        if data["spotify_client_secret"]:
            data["spotify_client_secret"] = "********"
        return data


class SettingsStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.path = paths.config_dir() / "settings.json"
        self.settings = self._load()

    def _load(self) -> Settings:
        if not self.path.exists():
            return Settings()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return Settings()
        known = {f.name for f in fields(Settings)}
        return Settings(**{k: v for k, v in raw.items() if k in known})

    def save(self) -> None:
        with self._lock:
            atomic_write_text(self.path, json.dumps(asdict(self.settings), indent=2))

    def update(self, values: dict) -> Settings:
        known = {f.name: f for f in fields(Settings)}
        for key, value in values.items():
            if key not in known:
                continue
            if key == "spotify_client_secret" and value == "********":
                continue  # masked value sent back unchanged by the UI
            current = getattr(self.settings, key)
            if isinstance(current, bool):
                value = bool(value)
            elif isinstance(current, int):
                value = int(value)
            elif isinstance(current, str):
                value = "" if value is None else str(value)
                if key != "spotify_playlist_prefix":
                    value = value.strip()
            setattr(self.settings, key, value)
        self.save()
        return self.settings
