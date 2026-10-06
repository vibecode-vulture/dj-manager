"""High level operations. Every mutating operation runs as a background job."""

from __future__ import annotations

import shutil
import threading
from pathlib import Path, PurePosixPath

from . import genres
from .analysis import STYLE_MODEL_FILES, TASK_LABELS, Analyzer, Result, format_key
from .recommend import RecommendError, run_recommendations
from .audio import popm_to_stars, read_info
from .backup import BackupManager
from . import dedupe
from .deps import DependencyManager
from .jobs import Job, JobCancelled, JobRunner
from .procs import kill_all
from .library import REMOVED_FOLDER, SOURCE_LOCAL, SOURCE_SPOTIFY, Library, Playlist, Track
from .scanner import scan
from .settings import SettingsStore
from .spotdl_client import RemoteSong, SpotdlClient, unique_songs
from .spotify_api import SpotifyAPI, SpotifyAPIError, playlist_id
from .updater import Updater
from .traktor import Collection, PlaylistNode, TrackMeta, TraktorError, find_collections, mapper_for, traktor_running
from .util import AUDIO_EXTENSIONS, is_spotify_playlist_url, move_file, now_iso, safe_filename, unique_path


class ServiceError(RuntimeError):
    pass


class Service:
    def __init__(self, settings: SettingsStore | None = None, deps: DependencyManager | None = None,
                 backups: BackupManager | None = None, spotdl: SpotdlClient | None = None,
                 spotify: SpotifyAPI | None = None) -> None:
        self.settings_store = settings or SettingsStore()
        self.deps = deps or DependencyManager()
        self.backups = backups or BackupManager()
        self.spotdl = spotdl or SpotdlClient(self.deps, self.settings_store.settings)
        self.spotify = spotify or SpotifyAPI(self.settings_store.settings)
        self.jobs = JobRunner()
        # Analysis runs in its own lane so a long first analysis never blocks downloads.
        self.analysis_jobs = JobRunner(lane="analysis")
        # Logging in must not wait behind a long download in the main lane.
        self.account_jobs = JobRunner(lane="account")
        self.analyzer = Analyzer(self.deps, self.settings_store.settings)
        self.updater = Updater(self.settings_store.settings)
        self.library: Library | None = None
        self.open_library()

    @property
    def settings(self):
        return self.settings_store.settings

    # ------------------------------------------------------------------ setup
    def open_library(self) -> Library | None:
        folder = self.settings.music_folder
        if not folder or not Path(folder).is_dir():
            self.library = None
            return None
        self.library = Library.load(Path(folder))
        if self.library.purge_missing():
            self.library.save()
        return self.library

    def require_library(self) -> Library:
        if self.library is None:
            raise ServiceError("Select your main music folder in the settings first")
        return self.library

    def nml_path(self) -> Path | None:
        if self.settings.traktor_nml:
            return Path(self.settings.traktor_nml).expanduser()
        found = find_collections(self.settings.wine_prefix)
        return Path(found[0]) if found else None

    def set_music_folder(self, folder: str) -> Job | None:
        path = Path(folder).expanduser()
        if not path.is_dir():
            raise ServiceError(f"{folder} is not a folder")
        self.settings_store.update({"music_folder": str(path.resolve())})
        lib = self.open_library()
        if lib and not lib.initialized:
            return self.jobs.submit("Import existing music folder", self._job_initial_import)
        return None

    def _job_initial_import(self, job: Job) -> str:
        lib = self.require_library()
        job.write(f"Scanning {lib.root} ...")
        with lib.lock:
            result = scan(lib, job.write)
            lib.save()
        job.write(result.summary())
        self.write_traktor(job, "initial import")
        self.auto_analyze()
        return result.summary()

    def submit_rescan(self) -> Job:
        def run(job: Job) -> str:
            lib = self.require_library()
            with lib.lock:
                lib.purge_missing()
                result = scan(lib, job.write)
                lib.save()
            self.write_traktor(job, "rescan")
            self.auto_analyze()
            return result.summary()
        return self.jobs.submit("Rescan music folder", run, dedupe=True)

    # ------------------------------------------------------------------ traktor
    def write_traktor(self, job: Job, reason: str) -> str:
        lib = self.require_library()
        nml = self.nml_path()
        if nml is None:
            job.write("No Traktor collection.nml configured/found - skipping Traktor update")
            return ""
        if traktor_running():
            raise TraktorError("Traktor is running. Close Traktor and retry (changes are kept and written next time).")
        keep = self.settings.backups_to_keep
        if not self.backups.has_initial():
            self.backups.create(nml, lib.file, "initial backup before DJ Manager changed anything", initial=True, keep=keep)
            job.write("Initial backup of the Traktor collection created")
        self.backups.create(nml, lib.file, f"before {reason}", keep=keep)

        collection = Collection(nml, mapper_for(self.settings, nml))
        moved = 0
        with lib.lock:
            for move in lib.pending_moves:
                if collection.move(lib.abs_path(move.old), lib.abs_path(move.new)):
                    moved += 1
            added = 0
            for track in lib.tracks.values():  # Traktor's ratings are the fallback for files without one
                if track.path:
                    track.rating_traktor = popm_to_stars(collection.ranking(lib.abs_path(track.path)))
            for tid in lib.member_ids():
                track = lib.tracks.get(tid)
                if not track:
                    continue
                path = lib.file_of(track)  # songs that are not downloaded yet stay out of Traktor
                if path and collection.ensure_entry(TrackMeta(path, track.title, track.artist_line, track.album, track.duration)):
                    added += 1
            nodes = self._playlist_nodes(lib, None)
            count = collection.set_managed_tree(self.settings.traktor_root_folder, nodes)
            collection.save()
            lib.pending_moves.clear()
            lib.save()
        msg = f"Traktor: {added} tracks added, {moved} relocated, {count} playlists written ({nml})"
        job.write(msg)
        return msg

    def _playlist_nodes(self, lib: Library, parent_key: str | None) -> list[PlaylistNode]:
        nodes = []
        for key in lib.genre_children(parent_key):
            paths = []
            for tid in lib.genre_track_ids(key):
                path = lib.file_of(lib.tracks[tid])
                if path:
                    paths.append(path)
            nodes.append(PlaylistNode(name=key, track_paths=paths, children=self._playlist_nodes(lib, key)))
        return nodes

    def submit_write_traktor(self) -> Job:
        return self.jobs.submit("Write Traktor collection", lambda job: self.write_traktor(job, "manual write"))

    # ------------------------------------------------------------------ spotify sync
    def sync_playlist(self, job: Job, pl: Playlist, retry_unavailable: bool = False) -> str:
        lib = self.require_library()
        if not pl.spotify_url:
            return f"{pl.key}: no Spotify link"
        job.write(f"--- {pl.key}: fetching {pl.spotify_url}")
        try:
            songs = self._fetch(job, pl)
        except JobCancelled:
            raise
        except Exception as exc:
            pl.last_error = str(exc)
            lib.save()
            raise
        allowed = [s for s in songs if s.spotify_id not in pl.blacklist]
        skipped = len(songs) - len(allowed)

        matched: dict[str, str] = {}  # spotify id -> track id
        to_download: list[RemoteSong] = []
        known: dict[str, Track] = {}  # tracks that exist but have no file yet
        kept_unavailable = 0
        with lib.lock:
            for song in allowed:
                track = lib.match(song.spotify_id, song.isrc, song.artists, song.title, song.duration)
                if track is None:
                    to_download.append(song)
                    continue
                lib.link_spotify(track, song.spotify_id, song.isrc)
                matched[song.spotify_id] = track.id
                if lib.has_file(track):
                    continue
                if track.download_status == "unavailable" and not retry_unavailable:
                    kept_unavailable += 1  # not searched again on every update - use Retry
                    continue
                known[song.spotify_id] = track
                to_download.append(song)
        job.write(f"{len(songs)} songs on Spotify, {len(matched) - len(known) - kept_unavailable} already in the collection, "
                  f"{len(to_download)} to download, {skipped} blacklisted"
                  + (f", {kept_unavailable} not on YouTube (use Retry to search again)" if kept_unavailable else ""))

        folder = lib.abs_path(pl.folder)
        folder.mkdir(parents=True, exist_ok=True)
        failed: list[RemoteSong] = []      # technical errors (after all retries)
        unavailable: dict[str, str] = {}  # not found on YouTube
        pending, downloaded = list(to_download), 0
        for attempt in range(1 + max(0, self.settings.download_retries)):
            if not pending or job.cancel_requested:
                break
            if attempt:
                job.write(f"Retrying {len(pending)} failed downloads (attempt {attempt + 1}) ...")
            files = self.spotdl.download(pending, job.write)
            not_found = dict(getattr(self.spotdl, "last_unavailable", {}) or {})
            try:
                with lib.lock:
                    for song in pending:
                        src = files.get(song.spotify_id)
                        if src is None:
                            continue
                        name = safe_filename(f"{', '.join(song.artists)} - {song.title}") + src.suffix.lower()
                        dst = move_file(src.rename(src.with_name(name)), folder)
                        rel = lib.rel(dst)
                        # The playlist listing has no album/ISRC; spotdl tags the file with them.
                        tags = read_info(dst)
                        track = known.get(song.spotify_id)
                        if track is None:
                            track = lib.add_track(Track(
                                id=lib.new_id(), path=rel, title=song.title, artists=song.artists,
                                album=song.album or tags.album, duration=song.duration or tags.duration,
                                spotify_id=song.spotify_id, isrc=song.isrc or tags.isrc,
                                rating=tags.rating, mtime=dst.stat().st_mtime,
                            ))
                            matched[song.spotify_id] = track.id
                        else:
                            track.path, track.mtime = rel, dst.stat().st_mtime
                            track.rating = track.rating if track.rating is not None else tags.rating
                        track.download_status, track.download_error = "", ""
                        lib.reindex()
                        downloaded += 1
                        job.write(f"Downloaded: {rel}")
            finally:
                self.spotdl.cleanup_staging(files)
            missing = [s for s in pending if s.spotify_id not in files]
            unavailable.update({s.spotify_id: not_found[s.spotify_id] for s in missing if s.spotify_id in not_found})
            pending = [s for s in missing if s.spotify_id not in not_found]  # only technical errors are retried
        failed = pending

        with lib.lock:
            # Songs without a file stay in the genre, marked, so they can be retried or linked to a file.
            for song in failed + [s for s in to_download if s.spotify_id in unavailable]:
                if job.cancel_requested and song.spotify_id not in unavailable:
                    status, error = "failed", "stopped before it was downloaded"
                elif song.spotify_id in unavailable:
                    status, error = "unavailable", unavailable[song.spotify_id]
                else:
                    status, error = "failed", "download error (yt-dlp) - see the log; retried on the next update"
                track = known.get(song.spotify_id) or (lib.tracks.get(matched[song.spotify_id]) if song.spotify_id in matched else None)
                if track is None:
                    track = lib.add_track(Track(
                        id=lib.new_id(), path="", title=song.title, artists=song.artists, album=song.album,
                        duration=song.duration, spotify_id=song.spotify_id, isrc=song.isrc))
                    matched[song.spotify_id] = track.id
                track.download_status, track.download_error = status, error
            lib.reindex()

        with lib.lock:
            new_members: dict[str, str] = {}
            for song in allowed:
                tid = matched.get(song.spotify_id)
                if tid:
                    new_members.setdefault(tid, SOURCE_SPOTIFY)
            removed = [tid for tid, src in pl.members.items() if src == SOURCE_SPOTIFY and tid not in new_members]
            for tid, src in pl.members.items():
                if src == SOURCE_LOCAL and tid not in new_members:
                    new_members[tid] = SOURCE_LOCAL
            for tid in removed:
                t = lib.tracks.get(tid)
                job.write(f"Removed from Spotify playlist: {t.artist_line + ' - ' + t.title if t else tid}")
            pl.members = new_members
            pl.last_synced = now_iso()
            if job.cancel_requested and failed:
                pl.last_error = f"Stopped - {len(failed)} songs not downloaded yet"
            else:
                problems = [f"{len(failed)} download errors"] if failed else []
                problems += [f"{len(unavailable)} not on YouTube"] if unavailable else []
                pl.last_error = ", ".join(problems)
            if not job.cancel_requested:
                for song in failed:
                    job.write(f"FAILED to download: {', '.join(song.artists)} - {song.title} ({song.url})")
                for song in (s for s in to_download if s.spotify_id in unavailable):
                    job.write(f"NOT ON YOUTUBE: {', '.join(song.artists)} - {song.title} ({song.url}) - "
                              f"download it yourself and link the file")
            lib.save()
        added = downloaded
        if job.cancel_requested:
            return f"{pl.key}: {added} downloaded, {len(failed)} left for the next update"
        return (f"{pl.key}: {added} downloaded, {len(removed)} removed, {len(failed)} failed"
                + (f", {len(unavailable)} not on YouTube" if unavailable else ""))

    def _fetch(self, job: Job, pl: Playlist) -> list[RemoteSong]:
        """Own playlists through the Web API (private ones too), all others through spotdl."""
        pid = playlist_id(pl.spotify_url)
        if pid and self.spotify.connected:
            try:
                if not pl.spotify_owner:
                    pl.spotify_owner = self.spotify.owner_of(pid)
                if pl.spotify_owner == self.spotify.account.user_id:
                    songs = unique_songs(self.spotify.playlist_tracks(pid))
                    job.write(f"Found {len(songs)} songs (read from your Spotify account)")
                    return songs
            except SpotifyAPIError as exc:
                job.write(f"Spotify API not usable for this playlist ({exc}) - using spotdl")
        return self.spotdl.fetch_playlist(pl.spotify_url, job.write)

    def _spotify_name(self, key: str) -> str:
        return f"{self.settings.spotify_playlist_prefix}{key}"

    def _create_spotify_playlist(self, job: Job, key: str, track_ids: list[str]) -> str:
        """Create a private playlist for a genre with the given tracks; returns its URL."""
        lib = self.require_library()
        sids = [lib.tracks[t].spotify_id for t in track_ids if t in lib.tracks and lib.tracks[t].spotify_id]
        created = self.spotify.create_playlist(self._spotify_name(key), f"Genre {key} - managed by DJ Manager")
        job.write(f"Created Spotify playlist '{self._spotify_name(key)}': {created['url']}")
        self.spotify.add_tracks(created["id"], sids)
        job.write(f"  added {len(sids)} songs" + (f" ({len(track_ids) - len(sids)} local songs have no Spotify id)"
                                                  if len(sids) < len(track_ids) else ""))
        return created["url"]

    def submit_update_all(self) -> Job:
        def run(job: Job) -> str:
            lib = self.require_library()
            results, errors = [], 0
            linked = [pl for pl in lib.playlists.values() if pl.spotify_url]
            for i, pl in enumerate(linked):
                if job.cancel_requested:
                    break
                job.write(f"=== Playlist {i + 1}/{len(linked)}")
                try:
                    results.append(self.sync_playlist(job, pl))
                except JobCancelled:
                    break
                except Exception as exc:  # keep going with the other playlists
                    errors += 1
                    job.write(f"ERROR {pl.key}: {exc}")
            job.progress = None
            if linked and errors < len(linked):
                self.deps.mark_current("good")
            self.write_traktor(job, "playlist update")
            self.auto_analyze()
            for line in results:
                job.write(line)
            if job.cancel_requested:
                return f"Stopped after {len(results)} of {len(linked)} playlists - finished downloads were kept"
            return f"{len(linked)} playlists updated" + (f", {errors} failed" if errors else "")
        return self.jobs.submit("Update all playlists", run, dedupe=True)

    def submit_sync(self, key: str) -> Job:
        def run(job: Job) -> str:
            pl = self._playlist(key)
            result = self.sync_playlist(job, pl)
            if not job.cancel_requested:
                self.deps.mark_current("good")
            self.write_traktor(job, f"update of {key}")
            self.auto_analyze()
            return f"Stopped - {result}" if job.cancel_requested else result
        return self.jobs.submit(f"Update {key}", run, dedupe=True)

    def find_job(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id) or self.analysis_jobs.get(job_id) or self.account_jobs.get(job_id)

    def cancel_job(self, job_id: str) -> Job | None:
        if self.analysis_jobs.get(job_id):
            # Stopped by the user: no automatic restart until Resume is pressed.
            self.settings_store.update({"analysis_paused": True})
            return self.analysis_jobs.cancel(job_id)
        if self.account_jobs.get(job_id):
            return self.account_jobs.cancel(job_id)
        return self.jobs.cancel(job_id)

    def shutdown(self) -> None:
        """Stop running work and every child process (called when DJ Manager exits)."""
        self.jobs.cancel_all()
        self.analysis_jobs.cancel_all()
        self.account_jobs.cancel_all()
        kill_all()

    # ------------------------------------------------------------------ analysis
    def _analysable(self, lib: Library) -> list[Track]:
        members = lib.member_ids()
        return [t for t in lib.tracks.values() if t.id in members and t.path]  # not: songs without file

    STATUS_FIELD = {"base": "analysis", "features": "features_status", "styles": "styles_status"}

    def _enabled_tasks(self) -> set[str]:
        """Analysis tasks that are switched on at all."""
        s = self.settings
        tasks = {"base"}
        if s.rec_enabled and (s.rec_energy or s.rec_timbre):
            tasks.add("features")
        if s.rec_enabled and s.rec_styles:
            tasks.add("styles")
        return tasks

    def _auto_tasks(self) -> set[str]:
        """Tasks that run by themselves for new songs (the others only on request)."""
        s = self.settings
        enabled = self._enabled_tasks()
        return {t for t in enabled if t == "base" or (t == "features" and s.features_auto)
                or (t == "styles" and s.styles_auto)}

    def analysis_status(self) -> dict:
        lib = self.library
        counts = {task: {"done": 0, "pending": 0, "failed": 0} for task in self.STATUS_FIELD}
        if lib:
            with lib.lock:
                for track in self._analysable(lib):
                    for task, attr in self.STATUS_FIELD.items():
                        value = getattr(track, attr)
                        counts[task]["done" if value == "done" else "failed" if value == "failed" else "pending"] += 1
        current = self.analysis_jobs.current
        enabled, auto = self._enabled_tasks(), self._auto_tasks()
        return {**counts["base"], "tasks": {t: {**counts[t], "enabled": t in enabled, "auto": t in auto}
                                            for t in counts},
                "pending_auto": sum(counts[t]["pending"] for t in auto),
                "paused": self.settings.analysis_paused, "auto": self.settings.analysis_auto,
                "engine": self.analyzer.engine() if self.deps.is_installed() else None,
                "styles_ready": self.analyzer.styles_ready() if self.deps.is_installed() else False,
                "job": current.summary(len(current.log)) if current else None}

    def _pending_tasks(self, track: Track, wanted: set[str]) -> list[str]:
        return [t for t in ("base", "features", "styles") if t in wanted and getattr(track, self.STATUS_FIELD[t]) == ""]

    def auto_analyze(self) -> Job | None:
        """Start analysing new songs unless the user switched it off or paused it."""
        lib = self.library
        if not lib or not self.settings.analysis_auto or self.settings.analysis_paused or not self.deps.is_installed():
            return None
        wanted = self._auto_tasks()
        with lib.lock:
            pending = any(self._pending_tasks(t, wanted) for t in self._analysable(lib))
        return self.submit_analysis() if pending else None

    def submit_analysis(self, mode: str = "pending", manual: bool = False, tasks: list[str] | None = None) -> Job:
        """mode: 'pending' (resume), 'failed' (retry) or 'all' (analyse again).

        Without tasks, everything that runs automatically is done; with tasks (e.g.
        ['styles']) exactly those are run for the whole collection, on request.
        """
        lib = self.require_library()
        requested = set(tasks or [])
        if requested - set(self.STATUS_FIELD):
            raise ServiceError("Unknown analysis")
        if requested - self._enabled_tasks():
            raise ServiceError("Enable this analysis under Settings > Split recommendations first")
        if manual:
            self.settings_store.update({"analysis_paused": False})
        wanted = requested or self._auto_tasks()
        if mode in ("failed", "all"):
            with lib.lock:
                for track in self._analysable(lib):
                    for task in (requested or {"base"}):
                        attr = self.STATUS_FIELD[task]
                        if mode == "all" or getattr(track, attr) == "failed":
                            setattr(track, attr, "")
                lib.save()
        title = "Analyse " + " + ".join(TASK_LABELS[t] for t in ("base", "features", "styles") if t in wanted)
        return self.analysis_jobs.submit(title, lambda job: self._job_analysis(job, wanted), dedupe=True)

    def _job_analysis(self, job: Job, wanted: set[str]) -> str:
        lib = self.require_library()
        engine = self.analyzer.ensure_tools(job.write, styles="styles" in wanted)
        with lib.lock:
            todo = []
            for t in self._analysable(lib):
                tasks = self._pending_tasks(t, wanted)
                if tasks and lib.has_file(t):
                    todo.append((t.id, str(lib.file_of(t)), tasks, str(lib.vector_file(t.id))))
        total, done, failed = len(todo), 0, 0
        if not total:
            return "All songs are analysed"
        job.write(f"{total} songs to analyse ({engine}): " + ", ".join(TASK_LABELS[t] for t in sorted(wanted)))
        notation = self.settings.key_notation
        unsaved = 0

        def on_result(res: Result) -> None:
            nonlocal done, failed, unsaved
            with lib.lock:
                track = lib.tracks.get(res.track_id)
                if track is None:
                    return
                moved = str(lib.abs_path(track.path)) != res.path  # e.g. split while queued
                ans, problems, parts = res.answer, [], []
                for task in res.tasks:
                    attr = self.STATUS_FIELD[task]
                    error = res.error or (ans.get("error") if task == "base" else (ans.get(task) or {}).get("error", ""))
                    if task != "base" and not res.error and task not in ans:
                        error = "no result"
                    if error:
                        if not moved:  # moved files stay pending for the next run
                            setattr(track, attr, "failed")
                            problems.append(f"{TASK_LABELS[task]}: {error}")
                        continue
                    setattr(track, attr, "done")
                    if task == "base":
                        track.bpm, track.key, track.analysis_engine = ans.get("bpm"), ans.get("key"), ans.get("engine", "")
                        track.analysis_error = ""
                        parts.append(f"{track.bpm:6.1f} BPM  {format_key(track.key, notation):>3}")
                    elif task == "features":
                        track.energy = ans["features"].get("energy")
                        parts.append(f"energy {track.energy}")
                    elif task == "styles":
                        track.styles = ans["styles"].get("top", [])
                        if track.styles:
                            parts.append(track.styles[0][0].split("---")[-1])
                if problems:
                    failed += 1
                    track.extra_error = "; ".join(problems)
                    if "base" in res.tasks and track.analysis == "failed":
                        track.analysis_error = track.extra_error
                    job.write(f"FAILED {track.artist_line} - {track.title}: {track.extra_error}")
                else:
                    job.write(f"{'  '.join(parts)}  {track.artist_line} - {track.title}")
                done += 1
                unsaved += 1
                job.progress = done / total
                if unsaved >= 10:
                    lib.save()
                    unsaved = 0

        try:
            self.analyzer.run(todo, on_result, job.write)
        except JobCancelled:
            pass  # results so far are kept; the message below says how to resume
        finally:
            with lib.lock:
                lib.save()
        if job.cancel_requested:
            return f"Paused - {done} of {total} analysed, {total - done} left (Resume in Settings > Analysis)"
        return f"{done - failed} songs analysed" + (f", {failed} failed" if failed else "")

    # ------------------------------------------------------------------ split recommendations
    def recommendations(self, key: str) -> dict:
        """Suggested groups of the genre's own songs for a new sub genre (nothing is changed)."""
        s = self.settings
        if not s.rec_enabled:
            raise ServiceError("Split recommendations are switched off (Settings > Split recommendations)")
        lib = self.require_library()
        pl = self._playlist(key)
        with lib.lock:
            tracks = [lib.tracks[t] for t in pl.members if t in lib.tracks and lib.has_file(lib.tracks[t])]
            payload_tracks = [{"id": t.id, "bpm": t.bpm, "energy": t.energy, "file": str(lib.vector_file(t.id))}
                              for t in tracks]
            coverage = {"songs": len(tracks), "bpm": sum(t.analysis == "done" for t in tracks),
                        "features": sum(t.features_status == "done" for t in tracks),
                        "styles": sum(t.styles_status == "done" for t in tracks)}
        signals = {"bpm": s.rec_bpm, "energy": s.rec_energy, "timbre": s.rec_timbre, "styles": s.rec_styles}
        labels = self.analyzer.model_dir / STYLE_MODEL_FILES[1]
        result = {"suggestions": [], "maps": {}}
        if len(tracks) >= 2 * s.rec_min_group:
            try:
                result = run_recommendations(self.deps, {
                    "tracks": payload_tracks, "signals": signals, "min_group": s.rec_min_group,
                    "labels_file": str(labels) if labels.exists() else None})
            except RecommendError as exc:
                raise ServiceError(str(exc)) from exc
        return {**result, "coverage": coverage, "signals": signals, "min_group": s.rec_min_group,
                "tasks": self.analysis_status()["tasks"]}

    def _playlist(self, key: str) -> Playlist:
        pl = self.require_library().find_playlist(key)
        if pl is None:
            raise ServiceError(f"Playlist '{key}' not found")
        return pl

    def submit_add_playlist(self, name: str, url: str, create_on_spotify: bool = False) -> Job:
        lib = self.require_library()
        key = genres.normalize_key(name)
        if lib.find_playlist(key):
            raise ServiceError(f"A playlist '{key}' already exists")
        if url and not is_spotify_playlist_url(url):
            raise ServiceError("That does not look like a Spotify playlist link")
        if create_on_spotify:
            self.spotify.require()

        def run(job: Job) -> str:
            link, owner = url.strip(), ""
            if create_on_spotify:
                link = self._create_spotify_playlist(job, key, [])
                owner = self.spotify.account.user_id
            with lib.lock:
                pl = Playlist(key=key, folder=lib.folder_for_key(key), spotify_url=link, spotify_owner=owner)
                lib.playlists[key] = pl
                lib.abs_path(pl.folder).mkdir(parents=True, exist_ok=True)
                lib.save()
            job.write(f"Created {key} -> {pl.folder}")
            result = self.sync_playlist(job, pl) if url else f"{key} created"
            self.write_traktor(job, f"adding {key}")
            self.auto_analyze()
            return f"Stopped - {result}" if job.cancel_requested else result
        return self.jobs.submit(f"Add playlist {key}", run)

    def submit_edit_link(self, key: str, url: str) -> Job:
        pl = self._playlist(key)
        url = (url or "").strip()
        if url and not is_spotify_playlist_url(url):
            raise ServiceError("That does not look like a Spotify playlist link")

        def run(job: Job) -> str:
            lib = self.require_library()
            with lib.lock:
                # Changing the link never drops songs: existing ones stay as 'local'
                # until the new Spotify playlist contains them.
                if url != pl.spotify_url:
                    pl.members = {tid: SOURCE_LOCAL for tid in pl.members}
                pl.spotify_url = url
                lib.save()
            result = self.sync_playlist(job, pl) if url else f"{key}: link removed"
            self.write_traktor(job, f"link change of {key}")
            return result
        return self.jobs.submit(f"Change link of {key}", run)

    def submit_remove_tracks(self, key: str, track_ids: list[str]) -> Job:
        pl = self._playlist(key)

        def run(job: Job) -> str:
            lib = self.require_library()
            with lib.lock:
                count = 0
                for tid in track_ids:
                    if pl.members.pop(tid, None) is None:
                        continue
                    count += 1
                    track = lib.tracks.get(tid)
                    if track and track.spotify_id:
                        pl.blacklist[track.spotify_id] = {"title": track.title, "artists": track.artists, "added_at": now_iso()}
                    if track and not lib.playlists_of(tid):
                        job.write(f"Not in any genre anymore (kept on disk, see Removed): {track.path}")
                lib.save()
            self.write_traktor(job, f"removing tracks from {key}")
            return f"{count} tracks removed from {key}"
        return self.jobs.submit(f"Remove tracks from {key}", run)

    def submit_unblacklist(self, key: str, spotify_ids: list[str]) -> Job:
        pl = self._playlist(key)

        def run(job: Job) -> str:
            lib = self.require_library()
            with lib.lock:
                for sid in spotify_ids:
                    pl.blacklist.pop(sid, None)
                lib.save()
            return f"{len(spotify_ids)} songs removed from the blacklist of {key} (added again on next update)"
        return self.jobs.submit(f"Edit blacklist of {key}", run)

    def submit_remove_playlist(self, key: str) -> Job:
        self._playlist(key)

        def run(job: Job) -> str:
            lib = self.require_library()
            with lib.lock:
                pl = lib.find_playlist(key)
                assert pl is not None
                del lib.playlists[pl.key]
                folder_rel = pl.folder.lower()
                removed_dir = lib.root / REMOVED_FOLDER / pl.key
                moved_other = moved_removed = 0

                def in_folder(rel: str) -> bool:
                    return PurePosixPath(rel).parent.as_posix().lower() == folder_rel

                for track in list(lib.tracks.values()):
                    for i, dup in enumerate(track.duplicates):
                        if in_folder(dup) and lib.abs_path(dup).exists():
                            new = move_file(lib.abs_path(dup), removed_dir)
                            track.duplicates[i] = lib.rel(new)
                            lib.record_move(dup, track.duplicates[i])
                    if not in_folder(track.path):
                        continue
                    src = lib.abs_path(track.path)
                    if not src.exists():
                        continue
                    others = sorted(lib.playlists_of(track.id), key=lambda p: p.key.lower())
                    if others:
                        dst_dir = lib.abs_path(others[0].folder)
                        moved_other += 1
                    else:
                        dst_dir = removed_dir
                        moved_removed += 1
                    new_rel = lib.rel(move_file(src, dst_dir))
                    lib.record_move(track.path, new_rel)
                    job.write(f"Moved {track.path} -> {new_rel}")
                    track.path = new_rel
                lib.reindex()
                lib.save()

                folder = lib.abs_path(pl.folder)
                leftovers = []
                if folder.is_dir():
                    leftovers = [p.name for p in folder.iterdir()]
                    if not leftovers:
                        folder.rmdir()
                        job.write(f"Deleted folder {pl.folder}")
                    else:
                        job.write(f"Folder {pl.folder} kept, it still contains: {', '.join(leftovers[:10])}")
            self.write_traktor(job, f"removing {key}")
            return f"{key} removed: {moved_other} tracks moved to other playlists, {moved_removed} moved to {REMOVED_FOLDER}"
        return self.jobs.submit(f"Remove playlist {key}", run)

    # ------------------------------------------------------------------ backups / deps / auth
    def submit_restore_backup(self, backup_id: str, restore_library: bool) -> Job:
        def run(job: Job) -> str:
            if traktor_running():
                raise TraktorError("Close Traktor before restoring a backup")
            lib = self.library
            nml = self.nml_path()
            if nml:
                self.backups.create(nml, lib.file if lib else None, "before restoring a backup", keep=self.settings.backups_to_keep)
            info = self.backups.restore(backup_id, lib.file if lib else None, restore_library)
            if restore_library:
                self.open_library()
            return f"Restored backup from {info.created_at}"
        return self.jobs.submit("Restore backup", run)

    def submit_deps(self, action: str, package: str | None = None, version: str | None = None,
                    snapshot_id: str | None = None) -> Job:
        def run(job: Job) -> str:
            if action == "install":
                versions = self.deps.install(package, version, job.write)
                return "Installed " + ", ".join(f"{k} {v}" for k, v in versions.items())
            if action == "restore":
                versions = self.deps.restore(snapshot_id or "", job.write)
                return "Restored " + ", ".join(f"{k} {v}" for k, v in versions.items())
            if action == "reinstall":
                self.deps.reinstall_venv(job.write)
                return "Dependency environment reinstalled"
            if action == "ffmpeg":
                self.deps.download_ffmpeg(job.write)
                return "ffmpeg downloaded"
            if action in ("good", "broken"):
                self.deps.mark_current(action)
                return f"Current dependency versions marked as {action}"
            raise ServiceError(f"Unknown action {action}")
        return self.jobs.submit(f"Dependencies: {action}", run)

    # ------------------------------------------------------------------ duplicates
    def _cue_data(self):
        """Traktor cue data per file, to keep the copy the user worked with."""
        nml = self.nml_path()
        if not nml or not nml.exists():
            return None
        try:
            return Collection(nml, mapper_for(self.settings, nml)).cue_data
        except TraktorError:
            return None

    def duplicate_report(self) -> dict:
        lib = self.require_library()
        with lib.lock:
            groups = dedupe.plan(lib, cue_data=self._cue_data())

        def copy(c):
            return {"path": c.path, "size": c.size, "duration": round(c.duration), "bitrate": c.bitrate,
                    "lossless": c.lossless, "cues": c.cues, "evidence": c.evidence}
        return {
            "groups": [{"track_id": g.track.id, "title": g.track.title, "artists": g.track.artist_line,
                        "playlists": [p.key for p in lib.playlists_of(g.track.id)],
                        "keep": copy(g.keep), "reason": g.reason,
                        "remove": [copy(c) for c in g.remove], "uncertain": [copy(c) for c in g.uncertain]}
                       for g in groups],
            "songs": len(groups),
            "certain_files": sum(len(g.remove) for g in groups),
            "certain_bytes": sum(c.size for g in groups for c in g.remove),
            "uncertain_files": sum(len(g.uncertain) for g in groups),
        }

    def submit_clean_duplicates(self, track_ids: list[str] | None = None, uncertain: list[str] | None = None) -> Job:
        """Move duplicate copies to the trash (certain ones, plus the selected uncertain ones)."""
        lib = self.require_library()
        nml = self.nml_path()
        if nml and nml.exists() and traktor_running():
            raise TraktorError("Close Traktor first - its references to the removed copies are updated right after")

        def run(job: Job) -> str:
            ids = set(track_ids) if track_ids else None
            with lib.lock:
                groups = dedupe.plan(lib, ids, cue_data=self._cue_data())
                job.write(f"Cleaning up {len(groups)} songs - copies go to the trash, one file per song is kept")
                try:
                    result = dedupe.clean(lib, groups, job.write, set(uncertain or []))
                finally:
                    lib.save()
            self.write_traktor(job, "cleaning up duplicates")
            mb = result["freed"] / 1e6
            return (f"{result['removed']} duplicate files moved to the trash ({mb:.0f} MB)"
                    + (f", {result['skipped']} skipped - see the log" if result["skipped"] else ""))
        return self.jobs.submit("Clean up duplicates", run, dedupe=True)

    def submit_retry_downloads(self, key: str) -> Job:
        """Download the genre's missing songs again, including those not found on YouTube before."""
        pl = self._playlist(key)
        if not pl.spotify_url:
            raise ServiceError(f"{key} has no Spotify link")

        def run(job: Job) -> str:
            result = self.sync_playlist(job, pl, retry_unavailable=True)
            self.write_traktor(job, f"retrying downloads of {key}")
            self.auto_analyze()
            return result
        return self.jobs.submit(f"Retry downloads of {key}", run, dedupe=True)

    def submit_link_file(self, track_id: str, file: str, key: str | None = None) -> Job:
        """Use a file the user downloaded for a song that has none (e.g. not on YouTube)."""
        lib = self.require_library()
        track = lib.tracks.get(track_id)
        src = Path(file).expanduser()
        if track is None:
            raise ServiceError("Song not found")
        if lib.has_file(track):
            raise ServiceError("This song already has a file")
        if not src.is_file() or src.suffix.lower() not in AUDIO_EXTENSIONS:
            raise ServiceError("Choose an audio file (mp3, m4a, flac, wav, aiff, ...)")
        playlists = lib.playlists_of(track_id)
        target = next((p for p in playlists if key and p.key == key), playlists[0] if playlists else None)
        if target is None:
            raise ServiceError("The song is in no genre")

        def run(job: Job) -> str:
            with lib.lock:
                folder = lib.abs_path(target.folder)
                folder.mkdir(parents=True, exist_ok=True)
                wanted = folder / (safe_filename(f"{track.artist_line} - {track.title}") + src.suffix.lower())
                dst = wanted if src.resolve() == wanted.resolve() else unique_path(wanted)
                if dst != src:
                    shutil.move(str(src), str(dst))  # into the genre's folder, named like downloads
                tags = read_info(dst)
                track.path, track.mtime = lib.rel(dst), dst.stat().st_mtime
                track.rating = tags.rating if tags.rating is not None else track.rating
                track.duration = track.duration or tags.duration
                track.download_status, track.download_error = "", ""
                lib.reindex()
                lib.save()
            job.write(f"Linked {track.artist_line} - {track.title} -> {track.path}")
            self.write_traktor(job, "linking a file")
            self.auto_analyze()
            return f"File linked: {track.path}"
        return self.jobs.submit(f"Link file for {track.title}", run)

    def submit_create_spotify_playlist(self, key: str) -> Job:
        """Give a genre without link (e.g. an imported folder) its own Spotify playlist."""
        pl = self._playlist(key)
        if pl.spotify_url:
            raise ServiceError(f"{key} already has a Spotify playlist")
        self.spotify.require()

        def run(job: Job) -> str:
            lib = self.require_library()
            url = self._create_spotify_playlist(job, pl.key, list(pl.members))
            with lib.lock:
                pl.spotify_url, pl.spotify_owner = url, self.spotify.account.user_id
                for tid in pl.members:
                    if lib.tracks.get(tid) and lib.tracks[tid].spotify_id:
                        pl.members[tid] = SOURCE_SPOTIFY
                pl.last_synced = now_iso()
                lib.save()
            return f"{pl.key} is now linked to {url}"
        return self.jobs.submit(f"Create Spotify playlist for {key}", run)

    def submit_split(self, key: str, track_ids: list[str], sub_name: str) -> Job:
        """Move selected songs of a genre into a new sub genre.

        On Spotify two new playlists are created - one for the sub genre with the selected
        songs and one for the genre with the remaining songs. The previous playlist is left
        untouched (DJ Manager never deletes playlists). Files that live in the genre's folder
        move into the sub genre's folder; songs stored elsewhere stay where they are.
        """
        lib = self.require_library()
        pl = self._playlist(key)
        part = genres.normalize_part(sub_name)
        sub_key = genres.normalize_key(f"{pl.key}{genres.SEPARATOR}{part}") if part else ""
        if not sub_key:
            raise ServiceError("Enter a name for the new sub genre")
        if lib.find_playlist(sub_key):
            raise ServiceError(f"The genre '{sub_key}' already exists")
        selected = [t for t in track_ids if t in pl.members]
        if not selected:
            raise ServiceError("Select the songs for the new sub genre first")
        self.spotify.require()  # fail before anything changes

        def run(job: Job) -> str:
            if pl.spotify_url:
                job.write(f"Updating {pl.key} from Spotify first ...")
                self.sync_playlist(job, pl)
            job.check_cancelled()
            chosen = set(selected)
            moving = [t for t in pl.members if t in chosen]  # keeps playlist order
            staying = [t for t in pl.members if t not in chosen]
            gone = len(chosen) - len(moving)
            if gone:
                job.write(f"{gone} selected songs are no longer in {pl.key} on Spotify and are skipped")
            if not moving:
                raise ServiceError("None of the selected songs is in the playlist anymore")

            # From here on nothing is cancelled half-way: Spotify and the library stay consistent.
            sub_url = self._create_spotify_playlist(job, sub_key, moving)
            new_url = self._create_spotify_playlist(job, pl.key, staying)
            owner = self.spotify.account.user_id
            moved_files = 0
            with lib.lock:
                sources = dict(pl.members)
                sub = Playlist(key=sub_key, folder=lib.folder_for_key(sub_key), spotify_url=sub_url,
                               spotify_owner=owner, last_synced=now_iso())
                for tid in moving:
                    track = lib.tracks[tid]
                    sub.members[tid] = SOURCE_SPOTIFY if track.spotify_id else sources[tid]
                pl.members = {tid: (SOURCE_SPOTIFY if lib.tracks[tid].spotify_id else sources[tid]) for tid in staying}
                previous = pl.spotify_url
                pl.spotify_url, pl.spotify_owner, pl.last_synced = new_url, owner, now_iso()
                lib.playlists[sub_key] = sub
                sub_dir = lib.abs_path(sub.folder)
                sub_dir.mkdir(parents=True, exist_ok=True)
                folder = pl.folder.lower()
                for tid in moving:
                    track = lib.tracks[tid]
                    src = lib.abs_path(track.path)
                    if PurePosixPath(track.path).parent.as_posix().lower() != folder or not src.exists():
                        continue  # stored in another genre's folder - stays there
                    new_rel = lib.rel(move_file(src, sub_dir))
                    lib.record_move(track.path, new_rel)
                    track.path = new_rel
                    moved_files += 1
                lib.reindex()
                lib.save()
            job.write(f"Previous playlist of {pl.key} is unchanged on Spotify: {previous or '(none)'}")
            job.write(f"{moved_files} files moved to {sub.folder}/")
            self.write_traktor(job, f"splitting {pl.key}")
            return f"{sub_key} created with {len(moving)} songs, {len(staying)} stay in {pl.key}"
        return self.jobs.submit(f"Split {key}", run)

    def submit_spotify_connect(self) -> Job:
        def run(job: Job) -> str:
            name = self.spotify.login(job.write)
            return f"Spotify account connected: {name}"
        return self.account_jobs.submit("Connect Spotify account", run, dedupe=True)

    def submit_app_update(self) -> Job:
        return self.jobs.submit("Update DJ Manager", lambda job: self.updater.apply(job.write))

    def check_app_update_background(self) -> None:
        def run() -> None:
            try:
                self.updater.check()
            except Exception:  # offline, rate limited, ... - just try again next start
                pass
        threading.Thread(target=run, daemon=True).start()

    def startup(self) -> None:
        if self.settings.check_app_updates:
            self.check_app_update_background()
        if self.library and self.library.initialized and self.settings.scan_on_start:
            self.submit_rescan()  # finds songs added outside DJ Manager, then analyses them
        if self.library and self.settings.update_on_start and self.deps.is_installed() \
                and any(pl.spotify_url for pl in self.library.playlists.values()):
            self.submit_update_all()
        self.auto_analyze()  # resumes an interrupted analysis
