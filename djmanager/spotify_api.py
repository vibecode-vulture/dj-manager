"""Spotify Web API access with the user's own Spotify app (needed to create playlists).

spotdl can only read. Creating playlists and adding songs needs the official Web API with
a user login. Since February 2026 that requires a Development Mode app whose owner has
Spotify Premium. The login uses PKCE, so only the app's Client ID is needed (no secret).

DJ Manager never deletes Spotify playlists or removes songs from them. It creates new ones,
adds songs to the user's own playlists (split, DISCOVER) and, on request, adds the name
prefix to a playlist of the user's own.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import asdict, dataclass
from typing import Callable

from . import paths
from .jobs import JobCancelled, current_job
from .util import atomic_write_text, urlopen

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
REDIRECT_HOST, REDIRECT_PORT = "127.0.0.1", 9900
REDIRECT_URI = f"http://{REDIRECT_HOST}:{REDIRECT_PORT}/"
SCOPES = "playlist-read-private playlist-read-collaborative playlist-modify-private playlist-modify-public"
LOGIN_TIMEOUT = 300

# Fields of spotdl's Song dataclass (its .spotdl save format) - needed to download songs
# that were read through the Web API.
SONG_FIELDS = [
    "name", "artists", "artist", "genres", "disc_number", "disc_count", "album_name", "album_artist",
    "duration", "year", "date", "track_number", "tracks_count", "song_id", "explicit", "publisher",
    "url", "isrc", "cover_url", "copyright_text", "download_url", "lyrics", "popularity", "album_id",
    "list_name", "list_url", "list_position", "list_length", "artist_id", "album_type",
]


class SpotifyAPIError(RuntimeError):
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def playlist_id(url: str) -> str | None:
    match = re.search(r"playlist[/:]([A-Za-z0-9]+)", url or "")
    return match.group(1) if match else None


@dataclass
class Account:
    client_id: str = ""
    access_token: str = ""
    refresh_token: str = ""
    expires_at: float = 0.0
    user_id: str = ""
    display_name: str = ""


# (method, url, headers, body) -> (status, headers, body bytes); replaceable in tests
Transport = Callable[[str, str, dict, bytes | None], tuple[int, dict, bytes]]


def urllib_transport(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urlopen(req, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()


class SpotifyAPI:
    def __init__(self, settings, transport: Transport = urllib_transport) -> None:
        self.settings = settings
        self.transport = transport
        self.file = paths.config_dir() / "spotify_account.json"
        self.account = self._load()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ account
    def _load(self) -> Account:
        try:
            return Account(**json.loads(self.file.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return Account()

    def _save(self) -> None:
        atomic_write_text(self.file, json.dumps(asdict(self.account), indent=1))

    @property
    def client_id(self) -> str:
        return (self.settings.spotify_client_id or "").strip()

    @property
    def connected(self) -> bool:
        a = self.account
        return bool(a.refresh_token and a.client_id and a.client_id == self.client_id)

    def status(self) -> dict:
        return {"connected": self.connected, "user": self.account.display_name if self.connected else "",
                "user_id": self.account.user_id if self.connected else "", "redirect_uri": REDIRECT_URI,
                "has_client_id": bool(self.client_id)}

    def disconnect(self) -> None:
        self.account = Account()
        self.file.unlink(missing_ok=True)

    def require(self) -> None:
        if not self.client_id:
            raise SpotifyAPIError("Enter the Client ID of your Spotify app in Settings > Spotify first")
        if not self.connected:
            raise SpotifyAPIError("Connect your Spotify account in Settings > Spotify first")

    # ------------------------------------------------------------------ login (PKCE)
    def login(self, log=print, open_browser: bool = True) -> str:
        if not self.client_id:
            raise SpotifyAPIError("Enter the Client ID of your Spotify app in Settings > Spotify first")
        verifier = secrets.token_urlsafe(64)[:96]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        url = AUTH_URL + "?" + urllib.parse.urlencode({
            "client_id": self.client_id, "response_type": "code", "redirect_uri": REDIRECT_URI,
            "code_challenge_method": "S256", "code_challenge": challenge, "state": state, "scope": SCOPES,
        })
        result: dict = {}
        done = threading.Event()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server API
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if "code" not in query and "error" not in query:
                    self.send_response(404)
                    self.end_headers()
                    return
                result.update({k: v[0] for k, v in query.items()})
                ok = "code" in query and query.get("state", [""])[0] == state
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                text = "Spotify connected. You can close this window." if ok else "Spotify login failed."
                self.wfile.write(f"<html><body style='font-family:sans-serif;background:#0c0b10;color:#ecebf2;"
                                 f"padding:40px'><h2>DJ Manager</h2><p>{text}</p></body></html>".encode())
                done.set()

            def log_message(self, *args):  # silence
                pass

        try:
            server = http.server.HTTPServer((REDIRECT_HOST, REDIRECT_PORT), Handler)
        except OSError as exc:
            raise SpotifyAPIError(f"Port {REDIRECT_PORT} is in use - close other Spotify logins and retry ({exc})")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            log("Opening the Spotify login in your browser. If nothing opens, use this link:")
            log(url)
            if open_browser:
                webbrowser.open(url)
            job = current_job.get()
            end = time.time() + LOGIN_TIMEOUT
            while not done.wait(0.5):
                if job is not None and job.cancel_requested:
                    raise JobCancelled()
                if time.time() > end:
                    raise SpotifyAPIError("Spotify login timed out")
        finally:
            server.shutdown()
            server.server_close()
        if result.get("error") or result.get("state") != state or "code" not in result:
            raise SpotifyAPIError(f"Spotify login failed: {result.get('error', 'invalid response')}")
        self._token({"grant_type": "authorization_code", "code": result["code"],
                     "redirect_uri": REDIRECT_URI, "code_verifier": verifier})
        me = self.request("GET", "/me")
        self.account.user_id = me.get("id", "")
        self.account.display_name = me.get("display_name") or me.get("id", "")
        self._save()
        return self.account.display_name

    def _token(self, form: dict) -> None:
        body = urllib.parse.urlencode({**form, "client_id": self.client_id}).encode()
        status, _, raw = self.transport("POST", TOKEN_URL, {"Content-Type": "application/x-www-form-urlencoded"}, body)
        data = json.loads(raw or b"{}")
        if status != 200:
            raise SpotifyAPIError(f"Spotify token request failed: {data.get('error_description') or data.get('error') or status}", status)
        a = self.account
        a.client_id = self.client_id
        a.access_token = data["access_token"]
        a.refresh_token = data.get("refresh_token") or a.refresh_token
        a.expires_at = time.time() + int(data.get("expires_in", 3600)) - 60
        self._save()

    def _access_token(self) -> str:
        self.require()
        with self._lock:
            if time.time() >= self.account.expires_at:
                self._token({"grant_type": "refresh_token", "refresh_token": self.account.refresh_token})
            return self.account.access_token

    # ------------------------------------------------------------------ requests
    def request(self, method: str, path: str, params: dict | None = None, body: dict | None = None) -> dict:
        url = path if path.startswith("http") else API + path
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(5):
            headers = {"Authorization": f"Bearer {self._access_token()}"}
            if data is not None:
                headers["Content-Type"] = "application/json"
            status, resp_headers, raw = self.transport(method, url, headers, data)
            if status == 401 and attempt == 0:
                self.account.expires_at = 0  # token revoked/expired early -> refresh once
                continue
            if status == 429:
                wait = int({k.lower(): v for k, v in resp_headers.items()}.get("retry-after", "2"))
                if wait > 60:
                    raise SpotifyAPIError(f"Spotify rate limit - try again in {wait // 60} minutes", 429)
                time.sleep(wait)
                continue
            if status >= 400:
                try:
                    message = json.loads(raw).get("error", {}).get("message", "")
                except ValueError:
                    message = raw[:200].decode("utf-8", "replace")
                raise SpotifyAPIError(f"Spotify API {method} {path}: {status} {message}", status)
            return json.loads(raw) if raw else {}
        raise SpotifyAPIError(f"Spotify API {method} {path}: too many retries")

    # ------------------------------------------------------------------ playlists
    def create_playlist(self, name: str, description: str = "", public: bool = False) -> dict:
        """Create a playlist in the user's account. Returns {'id', 'url'}."""
        data = self.request("POST", "/me/playlists", body={"name": name, "description": description, "public": public})
        url = (data.get("external_urls") or {}).get("spotify") or f"https://open.spotify.com/playlist/{data['id']}"
        if not public and data.get("public"):
            # Spotify has long ignored "public": false on creation; asking again afterwards is the
            # documented way, but Spotify may ignore that too (the app's "private" toggle is not
            # available through the API at all).
            try:
                self.request("PUT", f"/playlists/{data['id']}", body={"public": False})
            except SpotifyAPIError:
                pass
        return {"id": data["id"], "url": url}

    def add_tracks(self, pid: str, spotify_ids: list[str]) -> None:
        uris = [f"spotify:track:{sid}" for sid in spotify_ids]
        for i in range(0, len(uris), 100):  # API limit per request
            self.request("POST", f"/playlists/{pid}/items", body={"uris": uris[i:i + 100]})

    def owner_of(self, pid: str) -> str:
        data = self.request("GET", f"/playlists/{pid}", params={"fields": "owner(id)"})
        return (data.get("owner") or {}).get("id", "")

    def playlist_info(self, pid: str) -> dict:
        data = self.request("GET", f"/playlists/{pid}", params={"fields": "name,owner(id)"})
        return {"name": data.get("name", ""), "owner": (data.get("owner") or {}).get("id", "")}

    def rename_playlist(self, pid: str, name: str) -> None:
        """Only works for playlists the user owns (Spotify answers 403 otherwise)."""
        self.request("PUT", f"/playlists/{pid}", body={"name": name})

    def search_tracks(self, query: str) -> list[dict]:
        """Songs matching a search (Development Mode apps get at most 10 results)."""
        data = self.request("GET", "/search", params={"q": query, "type": "track", "limit": 10})
        return [t for t in (data.get("tracks") or {}).get("items", []) if t]

    def playlist_tracks(self, pid: str) -> list[dict]:
        """Songs of a playlist the user owns, as dicts in spotdl's save format."""
        songs: list[dict] = []
        page = self.request("GET", f"/playlists/{pid}/items", params={"limit": 50})
        while True:
            for entry in page.get("items", []):
                # renamed from "track" to "item" in February 2026 - accept both
                track = entry.get("item") or entry.get("track")
                if not track or track.get("type", "track") != "track" or track.get("is_local") or not track.get("id"):
                    continue
                songs.append(to_spotdl_song(track))
            if not page.get("next"):
                return songs
            page = self.request("GET", page["next"])


def to_spotdl_song(track: dict) -> dict:
    artists = [a.get("name", "") for a in track.get("artists") or [] if a.get("name")]
    album = track.get("album") or {}
    song = {field: None for field in SONG_FIELDS}
    song.update({
        "name": track.get("name", ""), "artists": artists, "artist": artists[0] if artists else "",
        "genres": [], "song_id": track["id"],
        "url": (track.get("external_urls") or {}).get("spotify") or f"https://open.spotify.com/track/{track['id']}",
        "duration": int(round((track.get("duration_ms") or 0) / 1000)),
        "album_name": album.get("name"), "album_id": album.get("id"),
        "isrc": (track.get("external_ids") or {}).get("isrc"),
        "explicit": track.get("explicit"), "track_number": track.get("track_number"),
        "disc_number": track.get("disc_number"),
    })
    return song
