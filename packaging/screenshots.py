"""Retake the README screenshots (docs/screenshots/) with the current version.

    python packaging/screenshots.py --analysis-python PATH [--ffmpeg PATH] [--models DIR]

Builds a demo library of generated, tagged tracks (several genres, a linked playlist with
LOCAL songs, a blacklist, a removed song, a duplicate, a song not found on YouTube), runs
DJ Manager from source on it including the real BPM/key, energy/sound and (with --models)
AI style analysis, and captures every view with headless Firefox.

Needs: firefox, selenium (pip install selenium), ffmpeg, and a Python environment with
spotdl, yt-dlp and the analysis tools (essentia or librosa, plus onnxruntime for styles),
e.g. DJ Manager's own dependency environment. Absolute paths in Settings/Dependencies are shown as the default
Linux locations so no machine-specific paths end up in the README.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "screenshots"
PORT = 8899
BASE = f"http://127.0.0.1:{PORT}/"

# genre folder -> (artist, title, album, bpm, character); character shapes the sound
LIBRARY = {
    "techno/hard-techno": [("I Hate Models", "Daydream", "Daydream EP", 146, "hard"), ("SPFDJ", "Bitch Mode", "Eat Me", 148, "hard"),
                           ("Dax J", "Offender", "Offender EP", 145, "hard"), ("Klangkuenstler", "Untergrund", "Untergrund", 150, "hard"),
                           ("Nico Moreno", "Hypnotic", "Hypnotic", 147, "hard"), ("Kobosil", "Aggressive Dancing", "We Grow", 145, "hard"),
                           ("Alarico", "Pressure", "Pressure EP", 149, "hard")],
    "techno/peak-time": [("Amelie Lens", "Feel It", "Hypnotized EP", 132, "dark"), ("Charlotte de Witte", "Selected", "Selected EP", 133, "dark"),
                         ("Adam Beyer", "Your Mind", "Your Mind", 131, "dark"), ("Enrico Sangiuliano", "Symbiosis", "Symbiosis", 132, "dark"),
                         ("ANNA", "Hidden Beauties", "Hidden Beauties", 133, "dark"), ("Reinier Zonneveld", "Things We Might Have Said", "Church of Matter", 131, "dark"),
                         ("Kölsch", "Grey", "1977", 132, "dark"), ("T78", "Inertia", "Inertia", 140, "bright"), ("Space 92", "Phoenix", "Phoenix", 141, "bright"),
                         ("Wehbba", "Straight Lines", "Straight Lines", 140, "bright"), ("Joyhauser", "Elevate", "Elevate", 141, "bright"),
                         ("Pig&Dan", "Sun Goes Down", "Sun Goes Down", 140, "bright"), ("Layton Giordani", "Order", "Order", 141, "bright"),
                         ("Kevin de Vries", "Dance With Me", "Dance With Me", 140, "bright")],
    "house/deep-house": [("Larry Heard", "Can You Feel It", "Can You Feel It", 120, "warm"), ("Moodymann", "Shades of Jae", "Mahogany Brown", 118, "warm"),
                         ("Kerri Chandler", "Rain", "Rain", 122, "warm"), ("Fred P", "Dimension", "Dimension", 121, "warm"),
                         ("Mood II Swing", "Closer", "Closer", 123, "warm")],
    "house/tech-house": [("Fisher", "Losing It", "Losing It", 126, "bright"), ("Michael Bibi", "Different Side", "Different Side", 125, "bright"),
                         ("Chris Lake", "Turn Off The Lights", "Turn Off The Lights", 126, "bright"), ("Phuture", "Acid Tracks", "Acid Tracks", 124, "warm")],
    "techno/acid": [("Phuture", "Acid Tracks", "Acid Tracks", 124, "warm"), ("DJ Pierre", "Box Energy", "Box Energy", 126, "warm"),
                    ("Hardfloor", "Acperience 1", "TB Resuscitation", 128, "warm"), ("Josh Wink", "Higher State of Consciousness", "Higher State", 130, "bright")],
    "drum-and-bass": [("Goldie", "Inner City Life", "Timeless", 172, "dark"), ("Noisia", "Diplodocus", "Split the Atom", 174, "hard"),
                      ("Calibre", "Mr Majestic", "Shelflife", 172, "warm"), ("Sub Focus", "Timewarp", "Torus", 174, "bright")],
}


def make_track(ffmpeg: str, path: Path, artist: str, title: str, album: str, bpm: int, kind: str, n: int) -> None:
    beat = 60 / bpm
    root = 110 * 2 ** ((n * 5 % 12) / 12)  # a different key per track
    layers = {  # kick + bass/pad + optional hats; the mix decides energy and brightness
        "hard": f"0.95*sin(2*PI*50*t)*exp(-22*mod(t,{beat})) + 0.25*sin(2*PI*{root}*t)",
        "dark": f"0.8*sin(2*PI*48*t)*exp(-28*mod(t,{beat})) + 0.12*sin(2*PI*{root}*t)*(0.6+0.4*sin(2*PI*0.05*t))",
        "warm": f"0.6*sin(2*PI*55*t)*exp(-30*mod(t,{beat})) + 0.2*sin(2*PI*{root}*t) + 0.12*sin(2*PI*{root * 1.5}*t)",
        "bright": f"0.85*sin(2*PI*55*t)*exp(-30*mod(t,{beat})) + 0.1*sin(2*PI*{root * 2}*t)",
    }
    inputs = ["-f", "lavfi", "-i", f"aevalsrc='{layers[kind]}':d=150"]
    graph = "[0]volume=0.8"
    if kind in ("bright", "hard"):
        inputs += ["-f", "lavfi", "-i", f"anoisesrc=d=150:a=0.5:seed={n}"]
        graph = (f"[1]highpass=f=7000,volume='if(lt(mod(t\\,{beat / 2})\\,0.03)\\,1\\,0)':eval=frame[h];"
                 f"[0][h]amix=inputs=2:normalize=0")
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([ffmpeg, "-loglevel", "error", "-y", *inputs, "-filter_complex", graph, "-b:a", "128k",
                    "-metadata", f"title={title}", "-metadata", f"artist={artist}", "-metadata", f"album={album}",
                    str(path)], check=True)


def post(path: str, data: dict | None = None) -> dict:
    req = urllib.request.Request(BASE + path.lstrip("/"), data=json.dumps(data or {}).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path.lstrip("/")) as resp:
        return json.load(resp)


def start(home: Path, env: dict) -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, "-m", "djmanager", "--no-open", "--port", str(PORT)], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in proc.stdout:  # type: ignore[union-attr]
        if "running at" in line:
            return proc
    raise RuntimeError("DJ Manager did not start")


def wait_for_analysis(timeout: int = 900) -> None:
    end = time.time() + timeout
    while time.time() < end:
        a = get("/api/state")["analysis"]
        if not a["job"] and a.get("pending_auto", a["pending"]) == 0:
            return
        time.sleep(2)
    raise TimeoutError("analysis did not finish")


def edit_library(music: Path) -> None:
    """Things the demo cannot produce without Spotify: links, LOCAL songs, blacklist, removed, not on YouTube."""
    path = music / ".djmanager" / "library.json"
    lib = json.loads(path.read_text())
    tracks = {t["id"]: t for t in lib["tracks"]}
    pls = {p["key"]: p for p in lib["playlists"]}
    now = datetime.now(timezone.utc).replace(microsecond=0)
    n = iter(range(1000))
    sid = lambda: f"3xDemo{next(n):016d}"  # noqa: E731
    for key, local in (("techno_hard-techno", {"Untergrund"}), ("techno_peak-time", set()), ("techno_acid", {"Higher State of Consciousness"}),
                       ("house_deep-house", set()), ("drum-and-bass", set())):
        pl = pls[key]
        pl["spotify_url"] = f"https://open.spotify.com/playlist/{sid()}"
        pl["last_synced"] = (now - timedelta(minutes=14)).isoformat()
        for tid in pl["members"]:
            tracks[tid]["spotify_id"] = tracks[tid]["spotify_id"] or sid()
            pl["members"][tid] = "local" if tracks[tid]["title"] in local else "spotify"
    hard = pls["techno_hard-techno"]
    pressure = next(t for t in tracks.values() if t["title"] == "Pressure")
    del hard["members"][pressure["id"]]  # removed by the user -> blacklist + "removed"
    hard["blacklist"] = {pressure["spotify_id"]: {"title": "Pressure", "artists": ["Alarico"], "added_at": (now - timedelta(days=2)).isoformat()},
                         sid(): {"title": "Gabber Wonderland", "artists": ["Some Artist"], "added_at": (now - timedelta(days=9)).isoformat()}}
    ghost = {"id": "demo-not-on-youtube", "path": "", "title": "Rave Machine (Bootleg)", "artists": ["Unknown Producer"],
             "album": "", "duration": 361.0, "spotify_id": sid(), "download_status": "unavailable",
             "download_error": "not found on YouTube / YouTube Music"}
    lib["tracks"].append(ghost)
    hard["members"][ghost["id"]] = "spotify"
    for t in lib["tracks"]:  # a few ratings, as Traktor would have written them
        if t["title"] in ("Daydream", "Offender", "Feel It", "Rain", "Inner City Life", "Selected"):
            t["rating"] = 5 if t["title"] in ("Daydream", "Rain") else 4
    path.write_text(json.dumps(lib, indent=1))


def capture(out: Path) -> None:
    from selenium import webdriver
    from selenium.webdriver.common.action_chains import ActionChains
    from selenium.webdriver.common.by import By

    o = webdriver.FirefoxOptions()
    o.add_argument("--headless")
    o.add_argument("--width=1440")
    o.add_argument("--height=900")
    o.set_preference("media.autoplay.default", 0)  # allow the player to start in headless Firefox
    d = webdriver.Firefox(options=o)
    ev = lambda js: d.execute_script("return window.eval(arguments[0])", js)  # noqa: E731
    shot = lambda name, wait=1.0: (time.sleep(wait), d.save_screenshot(str(out / f"{name}.png")))  # noqa: E731
    try:
        d.get(BASE)
        d.execute_script("localStorage.setItem('djm.console','true'); localStorage.setItem('djm.expanded', JSON.stringify(['techno','house']))")
        d.get(BASE)
        time.sleep(2)
        ev("S.app.spotify.connected = true")  # demo: the dialogs need a connected account
        ev("setView({type:'genre', key:'techno_hard-techno'})")
        time.sleep(1.2)
        d.find_elements(By.CSS_SELECTOR, "tbody tr td:nth-child(2)")[1].click()
        ev("Player.playList(S.visible, S.visible[1].id, viewLabel())")  # show the player bar in use
        time.sleep(2.5)
        ev("Player.toggle(false)")
        shot("genre-playlist")
        ev("S.selected.clear(); setView({type:'genre', key:'techno'})")
        shot("genre-aggregate")
        ev("addPlaylist('techno_acid')")
        time.sleep(0.3)
        d.find_element(By.ID, "pl-name").send_keys("chicago")
        d.find_element(By.ID, "pl-url").send_keys("https://open.spotify.com/playlist/3xDemoChicagoAcid00006")
        ev("document.getElementById('pl-name').dispatchEvent(new Event('input'))")
        shot("add-playlist", 0.4)
        ev("document.getElementById('modal-root').innerHTML=''; setView({type:'genre', key:'techno_hard-techno'})")
        time.sleep(1)
        ev("showBlacklist('techno_hard-techno')")
        shot("blacklist", 0.6)
        ev("document.getElementById('modal-root').innerHTML=''; setView({type:'genre', key:'techno_peak-time'})")
        time.sleep(1)
        d.find_element(By.XPATH, "//button[contains(., 'RECOMMEND')]").click()
        time.sleep(3)
        shot("recommendations")
        cards = d.find_elements(By.CSS_SELECTOR, ".rec-card")
        if cards:
            cards[0].find_element(By.XPATH, ".//button[contains(., 'SPLIT THESE')]").click()
            shot("split-dialog", 0.6)
            ev("document.getElementById('modal-root').innerHTML=''")
            cards[0].find_element(By.XPATH, ".//button[contains(., 'SHOW ON MAP')]").click()
        spaces = d.find_elements(By.XPATH, "//button[@data-space='bpm_energy']")
        if spaces:
            spaces[0].click()
            cv = d.find_element(By.ID, "rec-map")
            d.execute_script("arguments[0].scrollIntoView({block:'center'})", cv)
            time.sleep(0.4)
            w, h = cv.size["width"], cv.size["height"]
            a = ActionChains(d).move_to_element_with_offset(cv, int(w * 0.02), int(-h * 0.48)).click_and_hold()
            for fx, fy in ((0.49, -0.48), (0.49, 0.0), (0.02, 0.0), (0.02, -0.47)):
                a.move_to_element_with_offset(cv, int(w * fx), int(h * fy))
            a.release().perform()
            shot("recommendations-map", 0.6)
        ev("setView({type:'duplicates'})")
        shot("duplicates")
        ev("setView({type:'settings'})")
        time.sleep(1)
        ev("""const i = document.querySelectorAll('.panel input[type=text]'); i[0].value = '~/Music';
              const n = document.querySelector('[data-k=traktor_nml]'); n.value = '';
              n.placeholder = 'auto: ~/.wine/drive_c/users/dj/Documents/Native Instruments/Traktor 4.1.0/collection.nml';
              document.querySelector('[data-k=wine_prefix]').value = '';""")
        d.execute_script("arguments[0].scrollIntoView()", d.find_element(By.XPATH, "//h2[text()='SPOTIFY']"))
        shot("settings", 0.4)
        ev("setView({type:'deps'})")
        time.sleep(6)
        ev("""document.querySelectorAll('.panel .mono').forEach(e => {
                if (e.textContent.includes('/deps/venv')) e.textContent = '~/.local/share/dj-manager/deps/venv';
                if (e.textContent.endsWith('ffmpeg')) e.textContent = '~/.config/spotdl/ffmpeg'; });""")
        shot("dependencies", 0.3)
    finally:
        d.quit()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--analysis-python", required=True, help="Python with essentia or librosa (+ onnxruntime)")
    parser.add_argument("--ffmpeg", default=shutil.which("ffmpeg") or str(Path.home() / ".config/spotdl/ffmpeg"))
    parser.add_argument("--models", help="folder with discogs-effnet-bsdynamic-1.onnx/.json (enables AI styles)")
    parser.add_argument("--keep", action="store_true", help="keep the demo folder")
    args = parser.parse_args()

    work = Path(tempfile.mkdtemp(prefix="djm-screens-", dir=ROOT / ".bench" if (ROOT / ".bench").is_dir() else None))
    home, music = work / "home", work / "Music"
    venv = home / "data" / "deps" / "venv"
    venv.parent.mkdir(parents=True)
    # link the whole environment: a lone python symlink would not find its packages
    venv.symlink_to(Path(args.analysis_python).absolute().parent.parent, target_is_directory=True)
    if args.models:
        shutil.copytree(args.models, home / "data" / "deps" / "models", dirs_exist_ok=True)
    (home / "config").mkdir(parents=True)
    (home / "config" / "spotify_account.json").write_text(json.dumps(  # shown as connected; never used online
        {"client_id": "demo", "refresh_token": "demo", "user_id": "dj", "display_name": "DJ Demo", "access_token": "", "expires_at": 0}))
    n = 0
    for folder, tracks in LIBRARY.items():
        for artist, title, album, bpm, kind in tracks:
            n += 1
            make_track(args.ffmpeg, music / folder / f"{artist} - {title}.mp3", artist, title, album, bpm, kind, n)
    # certain duplicates (identical files in another genre) for the Duplicates view
    for src, dst in (("house/deep-house/Kerri Chandler - Rain.mp3", "house/tech-house"),
                     ("techno/hard-techno/Dax J - Offender.mp3", "techno/peak-time")):
        shutil.copy2(music / src, music / dst / Path(src).name)
    print(f"{n} demo tracks in {music}")

    env = {**os.environ, "DJMANAGER_HOME": str(home), "PATH": f"{Path(args.ffmpeg).parent}{os.pathsep}{os.environ['PATH']}"}
    settings = {"traktor_nml": str(work / "collection.nml"), "traktor_path_mode": "native", "update_on_start": False,
                "check_app_updates": False, "scan_on_start": False, "analysis_workers": 4, "spotify_client_id": "demo",
                "rec_enabled": True, "rec_styles": bool(args.models), "styles_auto": bool(args.models)}
    proc = start(home, env)
    try:
        post("/api/settings", settings)
        post("/api/music-folder", {"path": str(music)})
        time.sleep(3)
        wait_for_analysis()
    finally:
        proc.terminate()
        proc.wait(10)
    edit_library(music)
    proc = start(home, env)
    try:
        post("/api/deps", {"action": "good"})  # a snapshot, so the rollback list is not empty
        time.sleep(8)
        OUT.mkdir(parents=True, exist_ok=True)
        capture(OUT)
    finally:
        proc.terminate()
        proc.wait(10)
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    print("screenshots written to", OUT)


if __name__ == "__main__":
    main()
