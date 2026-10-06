"""High level operations. Every mutating operation runs as a background job."""

from __future__ import annotations

import threading
from pathlib import Path, PurePosixPath

from . import genres
from .analysis import Analyzer, Result, format_key
from .audio import popm_to_stars, read_info
from .backup import BackupManager
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
from .util import is_spotify_playlist_url, move_file, now_iso, safe_filename


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
        self.analysis_jobs = JobRunner()
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
                track.rating_traktor = popm_to_stars(collection.ranking(lib.abs_path(track.path)))
            for tid in lib.member_ids():
                track = lib.tracks.get(tid)
                if not track:
                    continue
                path = lib.abs_path(track.path)
                if path.exists() and collection.ensure_entry(TrackMeta(path, track.title, track.artist_line, track.album, track.duration)):
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
                path = lib.abs_path(lib.tracks[tid].path)
                if path.exists():
                    paths.append(path)
            nodes.append(PlaylistNode(name=key, track_paths=paths, children=self._playlist_nodes(lib, key)))
        return nodes

    def submit_write_traktor(self) -> Job:
        return self.jobs.submit("Write Traktor collection", lambda job: self.write_traktor(job, "manual write"))

    # ------------------------------------------------------------------ spotify sync
    def sync_playlist(self, job: Job, pl: Playlist) -> str:
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
        redownload: dict[str, Track] = {}
        with lib.lock:
            for song in allowed:
                track = lib.match(song.spotify_id, song.isrc, song.artists, song.title, song.duration)
                if track is None:
                    to_download.append(song)
                    continue
                lib.link_spotify(track, song.spotify_id, song.isrc)
                if not lib.abs_path(track.path).exists():
                    redownload[song.spotify_id] = track
                    to_download.append(song)
                matched[song.spotify_id] = track.id
        job.write(f"{len(songs)} songs on Spotify, {len(matched) - len(redownload)} already in the collection, "
                  f"{len(to_download)} to download, {skipped} blacklisted")

        folder = lib.abs_path(pl.folder)
        folder.mkdir(parents=True, exist_ok=True)
        failed: list[RemoteSong] = []
        if to_download:
            files = self.spotdl.download(to_download, job.write)
            try:
                with lib.lock:
                    for song in to_download:
                        src = files.get(song.spotify_id)
                        if src is None:
                            failed.append(song)
                            continue
                        name = safe_filename(f"{', '.join(song.artists)} - {song.title}") + src.suffix.lower()
                        dst = move_file(src.rename(src.with_name(name)), folder)
                        rel = lib.rel(dst)
                        if song.spotify_id in redownload:
                            redownload[song.spotify_id].path = rel
                            lib.reindex()
                        else:
                            # The playlist listing has no album/ISRC; spotdl tags the file with them.
                            tags = read_info(dst)
                            track = lib.add_track(Track(
                                id=lib.new_id(), path=rel, title=song.title, artists=song.artists,
                                album=song.album or tags.album, duration=song.duration or tags.duration,
                                spotify_id=song.spotify_id, isrc=song.isrc or tags.isrc,
                                rating=tags.rating, mtime=dst.stat().st_mtime,
                            ))
                            matched[song.spotify_id] = track.id
                        job.write(f"Downloaded: {rel}")
            finally:
                self.spotdl.cleanup_staging(files)

        with lib.lock:
            new_members: dict[str, str] = {}
            for song in allowed:
                tid = matched.get(song.spotify_id)  # failed new downloads have no track
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
                pl.last_error = f"{len(failed)} songs could not be downloaded" if failed else ""
            if not job.cancel_requested:
                for song in failed:
                    job.write(f"FAILED to download: {', '.join(song.artists)} - {song.title} ({song.url})")
            lib.save()
        added = len(to_download) - len(failed)
        if job.cancel_requested:
            return f"{pl.key}: {added} downloaded, {len(failed)} left for the next update"
        return f"{pl.key}: {added} downloaded, {len(removed)} removed, {len(failed)} failed"

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
        return self.jobs.get(job_id) or self.analysis_jobs.get(job_id)

    def cancel_job(self, job_id: str) -> Job | None:
        if self.analysis_jobs.get(job_id):
            # Stopped by the user: no automatic restart until Resume is pressed.
            self.settings_store.update({"analysis_paused": True})
            return self.analysis_jobs.cancel(job_id)
        return self.jobs.cancel(job_id)

    def shutdown(self) -> None:
        """Stop running work and every child process (called when DJ Manager exits)."""
        self.jobs.cancel_all()
        self.analysis_jobs.cancel_all()
        kill_all()

    # ------------------------------------------------------------------ analysis
    def _analysable(self, lib: Library) -> list[Track]:
        members = lib.member_ids()
        return [t for t in lib.tracks.values() if t.id in members]

    def analysis_status(self) -> dict:
        lib = self.library
        counts = {"done": 0, "pending": 0, "failed": 0}
        if lib:
            with lib.lock:
                for track in self._analysable(lib):
                    counts["done" if track.analysis == "done" else "failed" if track.analysis == "failed" else "pending"] += 1
        current = self.analysis_jobs.current
        return {**counts, "paused": self.settings.analysis_paused, "auto": self.settings.analysis_auto,
                "engine": self.analyzer.engine() if self.deps.is_installed() else None,
                "job": current.summary(len(current.log)) if current else None}

    def auto_analyze(self) -> Job | None:
        """Start analysing new songs unless the user switched it off or paused it."""
        lib = self.library
        if not lib or not self.settings.analysis_auto or self.settings.analysis_paused or not self.deps.is_installed():
            return None
        with lib.lock:
            pending = any(t.analysis == "" for t in self._analysable(lib))
        return self.submit_analysis() if pending else None

    def submit_analysis(self, mode: str = "pending", manual: bool = False) -> Job:
        """mode: 'pending' (resume), 'failed' (retry failed songs) or 'all' (analyse everything again)."""
        lib = self.require_library()
        if manual:
            self.settings_store.update({"analysis_paused": False})
        if mode in ("failed", "all"):
            with lib.lock:
                for track in self._analysable(lib):
                    if mode == "all" or track.analysis == "failed":
                        track.analysis, track.analysis_error = "", ""
                lib.save()
        return self.analysis_jobs.submit("Analyse BPM and key", self._job_analysis, dedupe=True)

    def _job_analysis(self, job: Job) -> str:
        lib = self.require_library()
        engine = self.analyzer.ensure_tools(job.write)
        with lib.lock:
            todo = [(t.id, str(lib.abs_path(t.path))) for t in self._analysable(lib)
                    if t.analysis == "" and lib.abs_path(t.path).exists()]
        total, done, failed = len(todo), 0, 0
        if not total:
            return "All songs are analysed"
        job.write(f"{total} songs to analyse ({engine})")
        notation = self.settings.key_notation
        unsaved = 0

        def on_result(res: Result) -> None:
            nonlocal done, failed, unsaved
            with lib.lock:
                track = lib.tracks.get(res.track_id)
                if track is None:
                    return
                if res.error and str(lib.abs_path(track.path)) != res.path:
                    pass  # moved while queued (e.g. split) - stays pending for the next run
                elif res.error:
                    track.analysis, track.analysis_error = "failed", res.error
                    failed += 1
                    job.write(f"FAILED {track.artist_line} - {track.title}: {res.error}")
                else:
                    track.bpm, track.key, track.analysis_engine = res.bpm, res.key, res.engine
                    track.analysis, track.analysis_error = "done", ""
                    job.write(f"{res.bpm:6.1f} BPM  {format_key(res.key, notation):>3}  {track.artist_line} - {track.title}")
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

    # ------------------------------------------------------------------ playlist management
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
        return self.jobs.submit("Connect Spotify account", run, dedupe=True)

    def submit_login(self) -> Job:
        def run(job: Job) -> str:
            job.write("A browser window opens for the Spotify login ...")
            name = self.spotdl.login(job.write)
            self.settings_store.update({"spotify_user_name": name, "spotify_user_auth": True})
            return f"Logged in as {name}"
        return self.jobs.submit("Spotify login", run)

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
