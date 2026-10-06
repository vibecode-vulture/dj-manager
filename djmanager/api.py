"""REST API consumed by the web UI."""

from __future__ import annotations

import os
import string
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, genres, paths
from .deps import ALL_PACKAGES, MANAGED_PACKAGES, DependencyError, DependencyManager
from .analysis import analysis_packages, format_key, key_sort
from .genres import GenreError
from .jobs import Job
from .library import SOURCE_LOCAL, Library, Track
from .util import AUDIO_EXTENSIONS
from .service import Service, ServiceError
from .traktor import TraktorError, find_collections, traktor_running
from .spotify_api import SpotifyAPIError
from .updater import UpdateError

STATIC = Path(__file__).parent / "static"


def track_row(lib: Library, track: Track, source: str | None = None, playlist_key: str | None = None, status: str = "",
              notation: str = "openkey") -> dict:
    exists = lib.has_file(track)
    in_playlists = [pl.key for pl in lib.playlists_of(track.id)]
    if not status:
        if not track.path and track.download_status:
            status = track.download_status  # "failed" (download error) or "unavailable" (not on YouTube)
        elif not exists:
            status = "missing"
        elif not in_playlists:
            status = "deleted"
        elif source == SOURCE_LOCAL and playlist_key and lib.playlists[playlist_key].spotify_url:
            status = "local"
        else:
            status = "ok"
    return {
        "id": track.id, "title": track.title, "artists": track.artist_line, "album": track.album,
        "duration": track.duration, "path": track.path, "spotify_id": track.spotify_id, "isrc": track.isrc,
        "playlists": in_playlists, "status": status, "source": source, "playlist": playlist_key,
        "duplicates": track.duplicates, "added_at": track.added_at,
        "rating": track.stars, "rating_source": "file" if track.rating is not None else "traktor" if track.rating_traktor else "",
        "bpm": track.bpm, "key": format_key(track.key, notation), "key_sort": key_sort(track.key),
        "analysis": track.analysis, "analysis_error": track.analysis_error,
        "download_error": track.download_error, "has_file": exists,
        "energy": track.energy, "styles": [[label.split("---")[-1], p] for label, p in track.styles[:3]],
    }


def create_app(service: Service | None = None) -> FastAPI:
    svc = service or Service()
    app = FastAPI(title="DJ Manager", version=__version__)
    app.state.service = svc

    def row(*args, **kwargs) -> dict:
        return track_row(*args, notation=svc.settings.key_notation, **kwargs)

    @app.exception_handler(ServiceError)
    @app.exception_handler(GenreError)
    @app.exception_handler(TraktorError)
    @app.exception_handler(DependencyError)
    @app.exception_handler(UpdateError)
    @app.exception_handler(SpotifyAPIError)
    async def handle_error(_: Request, exc: Exception):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    def lib() -> Library:
        return svc.require_library()

    def job_ref(job: Job | None) -> dict:
        return {"job": job.summary(len(job.log)) if job else None}

    # ------------------------------------------------------------------ state
    @app.get("/api/state")
    def state():
        nml = svc.nml_path()
        current = svc.jobs.current
        library = svc.library
        return {
            "version": __version__,
            "install_mode": paths.install_mode(),
            "update": svc.updater.last.to_dict() if svc.updater.last else None,
            "spotify": svc.spotify.status(),
            "analysis": svc.analysis_status(),
            "settings": svc.settings.public(),
            "library_loaded": library is not None,
            "nml_path": str(nml) if nml else None,
            "nml_exists": bool(nml and nml.exists()),
            "traktor_running": traktor_running(),
            "deps_installed": svc.deps.is_installed(),
            "busy": svc.jobs.busy,
            "current_job": current.summary(len(current.log)) if current else None,
            "stats": {
                "tracks": len(library.tracks), "playlists": len(library.playlists),
                "removed": len(library.orphans()), "pending_moves": len(library.pending_moves),
                "duplicates": sum(len(t.duplicates) for t in library.tracks.values()),  # extra files
                "duplicate_songs": sum(1 for t in library.tracks.values() if t.duplicates),
            } if library else None,
        }

    @app.post("/api/settings")
    def save_settings(values: dict = Body(...)):
        values.pop("music_folder", None)  # changed via /api/music-folder
        svc.settings_store.update(values)
        return svc.settings.public()

    @app.post("/api/music-folder")
    def music_folder(data: dict = Body(...)):
        return job_ref(svc.set_music_folder(data.get("path", "")))

    @app.get("/api/traktor/candidates")
    def nml_candidates():
        return find_collections(svc.settings.wine_prefix)

    @app.get("/api/browse")
    def browse(path: str = "", files: bool = False, kind: str = "nml"):
        wanted = AUDIO_EXTENSIONS if kind == "audio" else {".nml"}
        if not path:
            if paths.IS_WINDOWS:
                drives = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
                return {"path": "", "parent": None, "dirs": drives, "files": []}
            path = str(Path.home())
        p = Path(path).expanduser()
        if not p.is_dir():
            raise HTTPException(400, f"{path} is not a folder")
        dirs, file_list = [], []
        try:
            for child in sorted(p.iterdir(), key=lambda c: c.name.lower()):
                if child.name.startswith("."):
                    continue
                if child.is_dir():
                    dirs.append(child.name)
                elif files and child.suffix.lower() in wanted:
                    file_list.append(child.name)
        except PermissionError:
            pass
        parent = str(p.parent) if p.parent != p else ("" if paths.IS_WINDOWS else None)
        return {"path": str(p), "parent": parent, "dirs": dirs, "files": file_list}

    # ------------------------------------------------------------------ library views
    @app.get("/api/tree")
    def tree():
        library = svc.library
        if library is None:
            return []
        with library.lock:
            def node(key: str) -> dict:
                pl = library.find_playlist(key)
                return {
                    "key": key, "name": genres.display_name(key),
                    "has_playlist": pl is not None,
                    "spotify_url": pl.spotify_url if pl else "",
                    "folder": pl.folder if pl else library.folder_for_key(key),
                    "own_count": len(pl.members) if pl else 0,
                    "count": len(library.genre_track_ids(key)),
                    "last_synced": pl.last_synced if pl else "",
                    "last_error": pl.last_error if pl else "",
                    "blacklist": len(pl.blacklist) if pl else 0,
                    "children": [node(c) for c in library.genre_children(key)],
                }
            return [node(k) for k in library.genre_children(None)]

    @app.get("/api/genre/{key}/tracks")
    def genre_tracks(key: str):
        library = lib()
        with library.lock:
            own = library.find_playlist(key)
            rows = []
            for tid in library.genre_track_ids(key):
                track = library.tracks[tid]
                if own and tid in own.members:
                    rows.append(row(library, track, own.members[tid], own.key))
                else:
                    holder = next((pl for pl in library.playlists_of(tid) if genres.is_descendant_or_self(pl.key, key)), None)
                    rows.append(row(library, track, holder.members[tid] if holder else None, holder.key if holder else None))
            if own:
                # Tracks removed from every genre whose file still lives in this folder
                for track in library.orphans():
                    if library.playlist_owning_folder(track.path) is own:
                        rows.append(row(library, track, None, own.key, status="deleted"))
            return rows

    @app.get("/api/collection")
    def collection():
        library = lib()
        with library.lock:
            return [row(library, t) for t in library.tracks.values()]

    @app.get("/api/removed")
    def removed():
        library = lib()
        with library.lock:
            library.purge_missing()
            return [row(library, t, status="deleted") for t in library.orphans()]

    @app.get("/api/duplicates")
    def duplicates():
        library = lib()
        with library.lock:
            return [row(library, t) for t in library.tracks.values() if t.duplicates]

    @app.get("/api/genre/{key}/recommendations")
    def recommendations(key: str):
        return svc.recommendations(key)

    @app.get("/api/playlists/{key}/blacklist")
    def blacklist(key: str):
        pl = lib().find_playlist(key)
        if pl is None:
            raise HTTPException(404, "Playlist not found")
        return [{"spotify_id": sid, **info} for sid, info in pl.blacklist.items()]

    # ------------------------------------------------------------------ mutations
    @app.post("/api/playlists")
    def add_playlist(data: dict = Body(...)):
        return job_ref(svc.submit_add_playlist(data.get("name", ""), data.get("url", ""),
                                               bool(data.get("create_on_spotify"))))

    @app.post("/api/playlists/{key}/split")
    def split(key: str, data: dict = Body(...)):
        return job_ref(svc.submit_split(key, list(data.get("track_ids", [])), data.get("name", "")))

    @app.post("/api/playlists/{key}/retry-downloads")
    def retry_downloads(key: str):
        return job_ref(svc.submit_retry_downloads(key))

    @app.post("/api/tracks/{track_id}/link")
    def link_file(track_id: str, data: dict = Body(...)):
        return job_ref(svc.submit_link_file(track_id, data.get("path", ""), data.get("playlist")))

    @app.post("/api/playlists/{key}/create-spotify")
    def create_spotify(key: str):
        return job_ref(svc.submit_create_spotify_playlist(key))

    @app.put("/api/playlists/{key}/link")
    def edit_link(key: str, data: dict = Body(...)):
        return job_ref(svc.submit_edit_link(key, data.get("url", "")))

    @app.post("/api/playlists/{key}/sync")
    def sync(key: str):
        return job_ref(svc.submit_sync(key))

    @app.delete("/api/playlists/{key}")
    def remove_playlist(key: str):
        return job_ref(svc.submit_remove_playlist(key))

    @app.post("/api/playlists/{key}/remove-tracks")
    def remove_tracks(key: str, data: dict = Body(...)):
        return job_ref(svc.submit_remove_tracks(key, list(data.get("track_ids", []))))

    @app.post("/api/playlists/{key}/unblacklist")
    def unblacklist(key: str, data: dict = Body(...)):
        return job_ref(svc.submit_unblacklist(key, list(data.get("spotify_ids", []))))

    @app.post("/api/update-all")
    def update_all():
        return job_ref(svc.submit_update_all())

    @app.post("/api/rescan")
    def rescan():
        return job_ref(svc.submit_rescan())

    @app.post("/api/traktor/write")
    def write_traktor():
        return job_ref(svc.submit_write_traktor())

    # ------------------------------------------------------------------ backups
    @app.get("/api/backups")
    def backups():
        return [b.__dict__ for b in svc.backups.list()]

    @app.post("/api/backups")
    def create_backup():
        nml = svc.nml_path()
        if not nml:
            raise HTTPException(400, "No Traktor collection configured")
        info = svc.backups.create(nml, svc.library.file if svc.library else None, "manual backup",
                                  initial=not svc.backups.has_initial(), keep=svc.settings.backups_to_keep)
        return info.__dict__ if info else None

    @app.post("/api/backups/{backup_id}/restore")
    def restore_backup(backup_id: str, data: dict = Body(default={})):
        return job_ref(svc.submit_restore_backup(backup_id, bool(data.get("restore_library"))))

    # ------------------------------------------------------------------ dependencies
    @app.get("/api/deps")
    def deps_status():
        status = svc.deps.status()
        optional = [p for p in analysis_packages() if p not in MANAGED_PACKAGES]
        optional += [p for p, v in status["versions"].items() if v and p not in MANAGED_PACKAGES + optional]
        return {**status, "packages": MANAGED_PACKAGES + optional}

    @app.get("/api/deps/versions/{package}")
    def deps_versions(package: str):
        if package not in ALL_PACKAGES:
            raise HTTPException(404, "Unknown package")
        try:
            return DependencyManager.available_versions(package)
        except OSError as exc:
            raise HTTPException(502, f"PyPI not reachable: {exc}") from exc

    @app.post("/api/deps")
    def deps_action(data: dict = Body(...)):
        return job_ref(svc.submit_deps(data.get("action", ""), data.get("package") or None,
                                       data.get("version") or None, data.get("snapshot_id")))

    @app.post("/api/spotify/connect")
    def spotify_connect():
        return job_ref(svc.submit_spotify_connect())

    @app.post("/api/spotify/disconnect")
    def spotify_disconnect():
        svc.spotify.disconnect()
        return svc.spotify.status()

    # ------------------------------------------------------------------ app updates
    @app.get("/api/update/check")
    def update_check():
        try:
            return svc.updater.check().to_dict()
        except OSError as exc:
            raise HTTPException(502, f"Update server not reachable: {exc}") from exc

    @app.post("/api/update/apply")
    def update_apply():
        return job_ref(svc.submit_app_update())

    # ------------------------------------------------------------------ jobs
    @app.get("/api/jobs")
    def jobs():
        return [j.summary(len(j.log)) for j in svc.jobs.recent() + svc.analysis_jobs.recent()]

    @app.post("/api/analysis")
    def analysis(data: dict = Body(default={})):
        return job_ref(svc.submit_analysis(data.get("mode", "pending"), manual=True, tasks=data.get("tasks")))

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str):
        j = svc.cancel_job(job_id)
        if j is None:
            raise HTTPException(404, "Job not found")
        return j.summary(len(j.log))

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str, since: int = 0):
        j = svc.find_job(job_id)
        if j is None:
            raise HTTPException(404, "Job not found")
        return j.summary(since)

    # ------------------------------------------------------------------ ui
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    return app
