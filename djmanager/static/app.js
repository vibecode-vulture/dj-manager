"use strict";

// ------------------------------------------------------------------ helpers
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const js = (v) => esc(JSON.stringify(v));
const fmtTime = (s) => { s = Math.round(s || 0); return s ? `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}` : ""; };
const fmtDate = (iso) => iso ? new Date(iso).toLocaleString() : "never";
const store = {
  get(k, d) { try { return JSON.parse(localStorage.getItem("djm." + k)) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem("djm." + k, JSON.stringify(v)); } catch { /* ignore */ } },
};

async function api(method, url, body) {
  const res = await fetch(url, {
    method, headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) throw new Error((data && (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail))) || res.statusText);
  return data;
}

function toast(msg, isError = false) {
  const el = document.createElement("div");
  el.textContent = msg;
  if (isError) el.className = "e";
  $("#toast").appendChild(el);
  setTimeout(() => el.remove(), isError ? 8000 : 4000);
}

async function act(fn) {
  try { return await fn(); } catch (e) { toast(e.message, true); }
}

// ------------------------------------------------------------------ state
const S = {
  view: store.get("view", { type: "collection" }),
  app: null,
  tree: [],
  rows: [],
  selected: new Set(),
  lastClicked: null,
  filter: "",
  sort: store.get("sort", { col: null, dir: 1 }),
  expanded: new Set(store.get("expanded", [])),
  jobs: {}, // id -> log offset being followed
};

function setView(view) {
  if (view.type === "genre") {  // reveal the genre in the tree
    const parts = view.key.split("_");
    for (let i = 1; i < parts.length; i++) S.expanded.add(parts.slice(0, i).join("_"));
    store.set("expanded", [...S.expanded]);
  }
  S.view = view;
  S.selected.clear();
  S.filter = "";
  store.set("view", view);
  render();
}

// ------------------------------------------------------------------ jobs
function log(lines, cls = "") {
  const pre = $("#log");
  const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
  for (const line of lines) {
    const span = document.createElement("span");
    span.textContent = line + "\n";
    if (cls) span.className = cls;
    else if (/^(ERROR|FAILED)|Traceback/.test(line)) span.className = "e";
    pre.appendChild(span);
  }
  while (pre.childNodes.length > 4000) pre.removeChild(pre.firstChild);
  if (atBottom) pre.scrollTop = pre.scrollHeight;
}

function follow(job) {
  if (!job || S.jobs[job.id] !== undefined) return;
  S.jobs[job.id] = job.log_offset ?? 0;
  log([`▶ ${job.title}`], "ok");
  pollJob(job.id);
}

async function runJob(promise) {
  const res = await act(() => promise);
  if (res && res.job) follow(res.job);
  else if (res) refresh();
  return res;
}

async function pollJob(id) {
  let job;
  try { job = await api("GET", `/api/jobs/${id}?since=${S.jobs[id]}`); } catch { setTimeout(() => pollJob(id), 1500); return; }
  if (job.log.length) log(job.log);
  S.jobs[id] = job.log_size;
  $("#console-status").textContent = `${job.title}: ${job.cancel_requested && job.status === "running" ? "stopping" : job.status}`;
  if (job.lane === "analysis") updateAnalysisIndicator(job.status === "running" ? job : null);
  else if (job.status === "running") updateJobIndicator(job);
  else if (S.runningJob === id) updateJobIndicator(null);
  if (["done", "failed", "cancelled"].includes(job.status)) {
    if (job.status === "done") { log([`✔ ${job.result || job.title}`], "ok"); toast(job.result || `${job.title} done`); }
    else if (job.status === "cancelled") { log([`■ ${job.result || job.title + " stopped"}`], "ok"); toast(job.result || `${job.title} stopped`); }
    else toast(`${job.title} failed: ${job.error}`, true);
    refresh();
    return;
  }
  setTimeout(() => pollJob(id), 700);
}

function updateJobIndicator(job) {
  const ind = $("#ind-job");
  ind.className = "ind " + (job ? "busy" : "");
  $("#job-title").textContent = job ? job.title.toUpperCase() + (job.progress != null ? ` ${Math.round(job.progress * 100)}%` : "") : "IDLE";
  S.runningJob = job ? job.id : null;
  const stop = $("#btn-stop");
  stop.hidden = !job;
  stop.disabled = !!(job && job.cancel_requested);
  stop.textContent = job && job.cancel_requested ? "STOPPING…" : "■ STOP";
}

// While an analysis runs, fill in BPM/key of the visible table without re-rendering it
// (keeps selection, scroll position and the search field).
let lastCellRefresh = 0;
async function refreshAnalysisCells() {
  if (Date.now() - lastCellRefresh < 5000) return;
  lastCellRefresh = Date.now();
  const v = S.view;
  const url = v.type === "genre" ? `/api/genre/${encodeURIComponent(v.key)}/tracks`
    : { collection: "/api/collection", removed: "/api/removed", duplicates: "/api/duplicates" }[v.type];
  if (!url) return;
  let rows;
  try { rows = await api("GET", url); } catch { return; }
  const byId = new Map(rows.map((r) => [r.id, r]));
  S.rows = S.rows.map((r) => byId.get(r.id) || r);
  $$("tbody tr[data-id]").forEach((tr) => {
    const r = byId.get(tr.dataset.id);
    if (!r) return;
    const bpm = $("td.bpm", tr), key = $("td.key", tr);
    if (bpm) bpm.innerHTML = bpmCell(r);
    if (key) key.textContent = r.key;
  });
}

function updateAnalysisIndicator(job) {
  const a = S.app?.analysis || {};
  const ind = $("#ind-analysis"), btn = $("#btn-analysis");
  const left = a.pending || 0;
  S.analysisJob = job ? job.id : null;
  if (job) {
    refreshAnalysisCells();
    ind.className = "ind busy";
    $("#analysis-title").textContent = `ANALYSIS ${job.progress != null ? Math.round(job.progress * 100) + "%" : ""}`;
    btn.textContent = job.cancel_requested ? "STOPPING…" : "■ STOP";
    btn.className = "btn danger tiny";
    btn.disabled = !!job.cancel_requested;
  } else if (left && S.app?.library_loaded) {
    ind.className = "ind warn";
    $("#analysis-title").textContent = a.paused ? `ANALYSIS PAUSED · ${left} LEFT` : `${left} NOT ANALYSED`;
    btn.textContent = a.paused ? "▶ RESUME" : "▶ ANALYSE";
    btn.className = "btn accent tiny";
    btn.disabled = false;
  }
  const show = !!job || (left > 0 && S.app?.library_loaded);
  ind.hidden = btn.hidden = !show;
}

function analysisButton() {
  if (S.analysisJob) {
    const id = S.analysisJob;
    confirmBox("PAUSE ANALYSIS", "Pause the BPM/key analysis? Finished songs are kept. It continues when you press Resume (here or in Settings › Analysis).",
      "Pause", async () => { await act(() => api("POST", `/api/jobs/${id}/cancel`)); refreshState(); });
  } else {
    runJob(api("POST", "/api/analysis", { mode: "pending" }));
  }
}

function stopJob() {
  const id = S.runningJob;
  if (!id) return;
  confirmBox("STOP", "Stop the running task? Songs that finished downloading are kept; the rest are downloaded on the next update.",
    "Stop", async () => { const j = await act(() => api("POST", `/api/jobs/${id}/cancel`)); if (j) updateJobIndicator(j); });
}

// ------------------------------------------------------------------ data refresh
async function refreshState() {
  try { S.app = await api("GET", "/api/state"); } catch { return; }
  const a = S.app;
  const t = $("#ind-traktor");
  t.className = "ind " + (a.traktor_running ? "err" : a.nml_exists ? "ok" : "warn");
  t.title = a.traktor_running ? "Traktor is running - close it before DJ Manager writes the collection"
    : a.nml_path ? `Collection: ${a.nml_path}` : "No Traktor collection.nml found - set it in Settings";
  t.lastChild.textContent = a.traktor_running ? "TRAKTOR RUNNING" : a.nml_exists ? "TRAKTOR" : "TRAKTOR ?";
  const d = $("#ind-spotdl");
  d.className = "ind " + (a.deps_installed ? "ok" : "warn");
  d.lastChild.textContent = a.deps_installed ? "SPOTDL" : "SPOTDL MISSING";
  if (a.current_job) follow(a.current_job);
  else if (!Object.keys(S.jobs).length) updateJobIndicator(null);
  if (a.analysis?.job) follow(a.analysis.job);
  updateAnalysisIndicator(a.analysis?.job || null);
  const up = $("#btn-app-update");
  up.hidden = !(a.update && a.update.available && a.update.asset_url);
  if (!up.hidden) up.textContent = `UPDATE TO ${a.update.latest}`;
  const st = a.stats || {};
  $("#cnt-collection").textContent = st.tracks ?? "";
  $("#cnt-removed").textContent = st.removed || "";
  $("#cnt-duplicates").textContent = st.duplicate_songs || "";
  $("#cnt-duplicates").title = st.duplicates ? `${st.duplicate_songs} songs with ${st.duplicates} extra copies on disk` : "";
}

async function refresh() {
  await refreshState();
  S.tree = await api("GET", "/api/tree").catch(() => []);
  render();
}

function findNode(key, nodes = S.tree) {
  for (const n of nodes) {
    if (n.key.toLowerCase() === key.toLowerCase()) return n;
    const f = findNode(key, n.children);
    if (f) return f;
  }
  return null;
}

// ------------------------------------------------------------------ tree
function renderTree() {
  const root = $("#tree");
  if (!S.tree.length) {
    root.innerHTML = `<div class="node implicit"><span class="ico"></span><span class="name">no genres yet</span></div>`;
    return;
  }
  const build = (nodes) => nodes.map((n) => {
    const open = S.expanded.has(n.key);
    const active = S.view.type === "genre" && S.view.key === n.key;
    const twisty = n.children.length ? (open ? "▾" : "▸") : "";
    const icon = n.spotify_url ? "♫" : n.has_playlist ? "▪" : "▫";
    const warn = n.last_error ? `<span class="err" title="${esc(n.last_error)}">!</span>` : "";
    const kind = n.spotify_url ? `Linked to Spotify:\n${n.spotify_url}`
      : n.has_playlist ? "Songs from the folder only - no Spotify link (LINK adds one)"
      : "Gray: only sub genres - this folder has no songs of its own.\nIts Traktor playlist contains all songs of its sub genres.";
    const tip = `${n.key}\n${kind}\n${n.count} songs${n.children.length ? ` (incl. ${n.children.length} sub genres)` : ""}`;
    return `<a class="node ${active ? "active" : ""} ${n.has_playlist ? "" : "implicit"}" data-key="${esc(n.key)}"
              title="${esc(tip)}">
        <span class="twisty" data-toggle="${esc(n.key)}">${twisty}</span><span class="ico">${icon}</span>
        <span class="name">${esc(n.name)}</span>${warn}<span class="cnt">${n.count}</span></a>
      ${n.children.length && open ? `<div class="children">${build(n.children)}</div>` : ""}`;
  }).join("");
  root.innerHTML = build(S.tree);
}

document.addEventListener("click", (ev) => {
  const toggle = ev.target.closest("[data-toggle]");
  if (toggle && toggle.textContent) {
    const k = toggle.dataset.toggle;
    S.expanded.has(k) ? S.expanded.delete(k) : S.expanded.add(k);
    store.set("expanded", [...S.expanded]);
    renderTree();
    return;
  }
  const node = ev.target.closest(".browser .node");
  if (node && node.dataset.key) {
    S.expanded.add(node.dataset.key);
    store.set("expanded", [...S.expanded]);
    setView({ type: "genre", key: node.dataset.key });
  } else if (node && node.dataset.view) {
    setView({ type: node.dataset.view });
  }
});

// ------------------------------------------------------------------ render
// Views load their data asynchronously. When the user switches views (or a refresh starts a
// new render) before a slow request returns, e.g. /api/deps, the old view must not overwrite
// the new one - viewData() drops such results.
let renderSeq = 0;
const STALE = Symbol("stale render");

async function viewData(promise) {
  const seq = renderSeq;
  const data = await promise;
  if (seq !== renderSeq) throw STALE;
  return data;
}

async function render() {
  renderSeq++;
  renderTree();
  $$(".fixed-nodes .node").forEach((n) => n.classList.toggle("active", n.dataset.view === S.view.type));
  const deck = $("#deck");
  const view = $("#view");
  deck.innerHTML = "";
  if (S.app && !S.app.library_loaded && !["settings", "deps", "backups"].includes(S.view.type)) {
    view.innerHTML = welcome();
    return;
  }
  if (!view.dataset.type || view.dataset.type !== S.view.type + (S.view.key || "")) {
    view.innerHTML = `<div class="empty">Loading…</div>`;
    view.dataset.type = S.view.type + (S.view.key || "");
  }
  try {
    switch (S.view.type) {
      case "genre": return await renderGenre(deck, view);
      case "recommend": return await renderRecommend(deck, view);
      case "collection": return await renderList(deck, view, "/api/collection", "Track Collection", "All tracks managed by DJ Manager, each stored once on disk.");
      case "removed": return await renderList(deck, view, "/api/removed", "Removed", "Tracks that are in no genre anymore. They stay on disk (moved to _removed/ when their playlist was removed) - delete the files yourself and they disappear from this list.");
      case "duplicates": return await renderDuplicates(deck, view);
      case "settings": return renderSettings(view);
      case "deps": return await renderDeps(view);
      case "backups": return await renderBackups(view);
      default: setView({ type: "collection" });
    }
  } catch (e) {
    if (e === STALE) return;
    view.innerHTML = `<div class="empty"><h3>Error</h3>${esc(e.message)}</div>`;
  }
}

function welcome() {
  const a = S.app || {};
  return `<div class="empty">
    <h3>Welcome to DJ Manager</h3>
    <p>Select your main music folder. Existing sub folders are imported as genres and playlists.</p>
    <p><button class="btn accent" onclick="pickMusicFolder()">SELECT MUSIC FOLDER</button></p>
    ${a.deps_installed ? "" : `<p>spotdl is not installed yet: <button class="btn" onclick="setView({type:'deps'})">OPEN DEPENDENCIES</button></p>`}
  </div>`;
}

// ------------------------------------------------------------------ track lists
function deckHtml({ letter = "A", orange = false, title, sub, meters = [], tools = "" }) {
  return `<div class="letter ${orange ? "orange" : ""}">${letter}</div>
    <div class="info"><div class="title">${title}</div><div class="sub">${sub}</div></div>
    <div class="meter">${meters.map(([v, l]) => `<div><b>${v}</b><span>${l}</span></div>`).join("")}</div>
    <div class="tools">${tools}</div>`;
}

async function renderGenre(deck, view) {
  const key = S.view.key;
  const node = findNode(key);
  if (!node) { setView({ type: "collection" }); return; }
  const local = () => S.rows.filter((r) => r.status === "local").length;
  S.rows = await viewData(api("GET", `/api/genre/${encodeURIComponent(key)}/tracks`));
  const tools = node.has_playlist ? `
      <button class="btn" ${node.spotify_url ? "" : "disabled"} onclick="syncPlaylist(${js(key)})">⟳ UPDATE</button>
      ${node.spotify_url ? "" : `<button class="btn accent" onclick="createSpotifyPlaylist(${js(key)})" title="Create a Spotify playlist with the songs of this genre and link it">+ SPOTIFY PLAYLIST</button>`}
      <button class="btn" onclick="editLink(${js(key)})">🔗 LINK</button>
      <button class="btn" onclick="splitSelected(${js(key)})" title="Move the selected songs into a new sub genre">⑂ SPLIT</button>
      ${S.app?.settings.rec_enabled ? `<button class="btn" onclick="setView({type:'recommend', key:${js(key)}})" title="Suggested groups for a new sub genre">✦ RECOMMEND</button>` : ""}
      <button class="btn" onclick="showBlacklist(${js(key)})">⊘ BLACKLIST (${node.blacklist})</button>
      <button class="btn" id="btn-remove-tracks" onclick="removeSelected(${js(key)})">− REMOVE SELECTED</button>
      <button class="btn danger" onclick="removePlaylist(${js(key)})">✕ PLAYLIST</button>`
    : `<button class="btn accent" onclick="addPlaylist(${js(key)})">+ CREATE PLAYLIST FOR THIS GENRE</button>`;
  const sub = node.has_playlist
    ? `<code>${esc(key)}</code> · folder <code>${esc(node.folder)}</code><br>${node.spotify_url
        ? `<a href="${esc(node.spotify_url)}" target="_blank">${esc(node.spotify_url)}</a> · synced ${esc(fmtDate(node.last_synced))}`
        : "no Spotify link - add one to keep this playlist in sync"}${node.last_error ? ` · <span style="color:var(--orange)">${esc(node.last_error)}</span>` : ""}`
    : `<code>${esc(key)}</code> · genre without own playlist (contains sub genres only)`;
  deck.innerHTML = deckHtml({
    letter: node.spotify_url ? "S" : "L", orange: !node.spotify_url, title: esc(node.key.split("_").map((p) => p.replace(/-/g, " ")).join(" › ")), sub,
    meters: [[node.count, "IN GENRE"], [node.own_count, "OWN"], [local(), "LOCAL"]], tools,
  });
  const missing = S.rows.filter((r) => r.playlist === key && (r.status === "failed" || r.status === "unavailable")).length;
  if (missing && node.spotify_url) {
    $(".tools", deck).insertAdjacentHTML("afterbegin",
      `<button class="btn orange" onclick="retryDownloads(${js(key)})" title="Download the missing songs again, including those not found on YouTube before">↻ RETRY DOWNLOADS (${missing})</button>`);
  }
  renderTable(view, { showPlaylist: node.children.length > 0, selectable: node.has_playlist, playlistKey: key });
}

async function renderList(deck, view, url, title, help) {
  S.rows = await viewData(api("GET", url));
  deck.innerHTML = deckHtml({ letter: title[0], orange: title !== "Track Collection", title: esc(title), sub: esc(help), meters: [[S.rows.length, "TRACKS"]] });
  renderTable(view, { showPlaylist: true, showPath: S.view.type !== "collection" });
}

const COLUMNS = {
  title: (r) => r.title, artists: (r) => r.artists, album: (r) => r.album, duration: (r) => r.duration,
  status: (r) => r.status, playlists: (r) => r.playlists.join(" "), path: (r) => r.path,
  rating: (r) => r.rating ?? 0, bpm: (r) => r.bpm ?? 0, key: (r) => r.key_sort,
};

function stars(r) {
  if (!r.rating) return "";
  const src = r.rating_source === "traktor" ? "from Traktor" : "from the file's tags";
  return `<span class="stars" title="${r.rating} stars ${src}">${"★".repeat(r.rating)}<i>${"★".repeat(5 - r.rating)}</i></span>`;
}

function bpmCell(r) {
  if (r.bpm) return r.bpm.toFixed(1);
  if (r.analysis === "failed") return `<span class="ana-failed" title="${esc(r.analysis_error)}">!</span>`;
  return `<span class="ana-pending" title="not analysed yet">·</span>`;
}

function renderTable(view, opts) {
  const f = S.filter.toLowerCase();
  let rows = S.rows.filter((r) => !f || `${r.title} ${r.artists} ${r.album} ${r.path} ${r.playlists.join(" ")} ${r.key}`.toLowerCase().includes(f));
  if (S.sort.col && COLUMNS[S.sort.col]) {
    const get = COLUMNS[S.sort.col];
    rows = [...rows].sort((a, b) => { const x = get(a), y = get(b); return (x > y ? 1 : x < y ? -1 : 0) * S.sort.dir; });
  }
  S.visible = rows;
  const th = (col, label, cls = "") => `<th class="${cls}" data-sort="${col}">${label}${S.sort.col === col ? (S.sort.dir > 0 ? " ▲" : " ▼") : ""}</th>`;
  const STATUS_LABEL = { failed: "DOWNLOAD FAILED", unavailable: "NOT ON YOUTUBE" };
  const elsewhere = (r) => r.stored_in ? ` <span class="badge elsewhere" title="Stored once on disk, in ${esc(r.stored_in)}/">IN OTHER GENRE</span>` : "";
  const badge = (r) => {
    if (r.status === "ok") return (r.source === "spotify" ? `<span class="badge spotify">SPOTIFY</span>` : "") + elsewhere(r);
    const label = `<span class="badge ${r.status}" title="${esc(r.download_error || "")}">${STATUS_LABEL[r.status] || r.status.toUpperCase()}</span>`;
    return STATUS_LABEL[r.status]
      ? `${label} <button class="btn tiny" data-link="${esc(r.id)}" title="Use a file you downloaded yourself">LINK FILE…</button>`
      : label;
  };
  view.innerHTML = `
    <div class="filterbar">
      <input type="text" id="filter" placeholder="Search title, artist, album, path…" value="${esc(S.filter)}">
      <button class="btn tiny" id="play-all" title="Play this list (in the shown order)">▶ PLAY</button>
      <span class="hint">${rows.length} tracks${opts.selectable ? " · click / ctrl / shift to select" : ""} · double-click to play</span>
    </div>
    ${rows.length ? `<table class="tracks"><thead><tr>
      <th class="num">#</th>${th("title", "TITLE")}${th("artists", "ARTIST")}${th("album", "ALBUM")}
      ${th("rating", "RATING", "rating")}${th("bpm", "BPM", "bpm")}${th("key", "KEY", "key")}
      ${th("duration", "TIME", "time")}${th("status", "STATUS", "st")}
      ${opts.showPlaylist ? th("playlists", "PLAYLISTS") : ""}${opts.showPath ? th("path", "FILE") : ""}
    </tr></thead><tbody>
    ${rows.map((r, i) => `<tr data-id="${esc(r.id)}" class="st-${r.status} ${S.selected.has(r.id) ? "sel" : ""}" title="${esc(r.path)}">
      <td class="num"><span class="row-no">${i + 1}</span>${r.has_file ? `<span class="play-row" data-play="${esc(r.id)}" title="Play from here">▶</span>` : ""}</td><td>${esc(r.title)}</td><td>${esc(r.artists)}</td><td>${esc(r.album)}</td>
      <td class="rating">${stars(r)}</td><td class="bpm">${bpmCell(r)}</td><td class="key">${esc(r.key)}</td>
      <td class="time">${fmtTime(r.duration)}</td><td class="st">${badge(r)}</td>
      ${opts.showPlaylist ? `<td>${r.playlists.map((p) => `<span class="pl-chip">${esc(p)}</span>`).join("")}</td>` : ""}
      ${opts.showPath ? `<td class="mono">${esc(r.path)}</td>` : ""}
    </tr>`).join("")}</tbody></table>`
    : `<div class="empty">No tracks${f ? " match the search" : ""}.</div>`}`;

  const input = $("#filter");
  input.addEventListener("input", () => {
    S.filter = input.value;
    const pos = input.selectionStart;
    renderTable(view, opts);
    const again = $("#filter"); again.focus(); again.setSelectionRange(pos, pos);
  });
  $$("th[data-sort]", view).forEach((h) => h.addEventListener("click", () => {
    S.sort = { col: h.dataset.sort, dir: S.sort.col === h.dataset.sort ? -S.sort.dir : 1 };
    store.set("sort", S.sort);
    renderTable(view, opts);
  }));
  const playFrom = (id) => Player.playList(S.visible, id, viewLabel());
  $("#play-all", view)?.addEventListener("click", () => playFrom(null));
  $$("tbody tr", view).forEach((tr) => tr.addEventListener("dblclick", () => {
    window.getSelection()?.removeAllRanges();  // a double-click would also select a word
    const r = S.rows.find((x) => x.id === tr.dataset.id);
    if (r && r.has_file) playFrom(r.id);
  }));
  $$("[data-play]", view).forEach((b) => b.addEventListener("click", (ev) => { ev.stopPropagation(); playFrom(b.dataset.play); }));
  Player.paint();
  if (opts.selectable) {
    $$("tbody tr", view).forEach((tr) => tr.addEventListener("click", (ev) => {
      const id = tr.dataset.id;
      const row = S.rows.find((r) => r.id === id);
      if (ev.target.closest("[data-link]")) return;  // the LINK FILE button, not a selection
      if (row && row.status === "deleted") return;
      if (ev.shiftKey && S.lastClicked) {
        const ids = S.visible.map((r) => r.id);
        const [a, b] = [ids.indexOf(S.lastClicked), ids.indexOf(id)].sort((x, y) => x - y);
        ids.slice(a, b + 1).forEach((x) => S.selected.add(x));
      } else if (ev.ctrlKey || ev.metaKey) {
        S.selected.has(id) ? S.selected.delete(id) : S.selected.add(id);
      } else {
        S.selected = new Set([id]);
      }
      S.lastClicked = id;
      $$("tbody tr", view).forEach((r) => r.classList.toggle("sel", S.selected.has(r.dataset.id)));
    }));
  }
}

// ------------------------------------------------------------------ modals
function modal(title, body, buttons) {
  const root = $("#modal-root");
  root.innerHTML = `<div class="modal-bg"><div class="modal"><h3>${esc(title)}</h3><div class="body">${body}</div>
    <div class="foot">${buttons.map((b, i) => `<button class="btn ${b.cls || ""}" data-i="${i}">${esc(b.label)}</button>`).join("")}</div></div></div>`;
  const close = () => { root.innerHTML = ""; };
  $$(".foot .btn", root).forEach((el) => el.addEventListener("click", async () => {
    const b = buttons[+el.dataset.i];
    if (!b.action) return close();
    const keep = await b.action(root);
    if (keep !== true) close();
  }));
  $(".modal-bg", root).addEventListener("mousedown", (ev) => { if (ev.target.classList.contains("modal-bg")) close(); });
  const first = $("input", root);
  if (first) {
    first.focus();
    // checkboxes throw on setSelectionRange
    if (first.type === "text") first.setSelectionRange(first.value.length, first.value.length);
  }
  return root;
}

function confirmBox(title, text, label, action, cls = "orange") {
  modal(title, `<p>${text}</p>`, [{ label: "Cancel" }, { label, cls, action }]);
}

function normalizeKey(name) {
  const parts = name.trim().split("_").map((p) => p.trim().replace(/\s+/g, "-").replace(/-{2,}/g, "-").replace(/^-|-$/g, ""));
  return parts.some((p) => !p) ? "" : parts.join("_");
}

function folderPreview(key) {
  const parts = key.split("_");
  let folder = [];
  for (let i = 1; i <= parts.length; i++) {
    const node = findNode(parts.slice(0, i).join("_"));
    if (node && node.has_playlist) folder = node.folder.split("/");
    else folder.push(parts[i - 1]);
  }
  return folder.join("/");
}

function addPlaylist(prefix = "") {
  const root = modal("ADD PLAYLIST", `
    <div class="field"><label>Name / genre</label><input type="text" id="pl-name" value="${esc(prefix ? prefix + "_" : "")}" placeholder="techno_hard-techno"></div>
    <div class="field"><label>Spotify link</label><input type="text" id="pl-url" placeholder="https://open.spotify.com/playlist/… (optional)"></div>
    <div class="field"><label></label><label><input type="checkbox" id="pl-create"> Create a new empty Spotify playlist instead
      ${S.app?.spotify?.connected ? "" : `<span class="help">(connect your Spotify account in Settings first)</span>`}</label></div>
    <div class="preview" id="pl-preview"></div>
    <p class="help">Use <b>_</b> for genre layers and <b>-</b> for spaces. Example: <span class="mono">house_deep-house</span>
      creates <span class="mono">house/deep-house/</span> and the Traktor playlists <span class="mono">house</span> and <span class="mono">house_deep-house</span>.</p>`,
  [{ label: "Cancel" }, {
    label: "Add & download", cls: "accent", action: async (r) => {
      const create = $("#pl-create", r).checked;
      const res = await runJob(api("POST", "/api/playlists", { name: $("#pl-name", r).value, url: create ? "" : $("#pl-url", r).value, create_on_spotify: create }));
      return res ? undefined : true;
    },
  }]);
  const upd = () => {
    const key = normalizeKey($("#pl-name", root).value);
    const lineage = key ? key.split("_").map((_, i, a) => a.slice(0, i + 1).join("_")) : [];
    $("#pl-preview", root).innerHTML = key
      ? `key <b>${esc(key)}</b><br>folder <b>${esc(folderPreview(key))}/</b><br>traktor ${lineage.map((l) => `<b>${esc(l)}</b>`).join(" › ")}${findNode(key)?.has_playlist ? "<br><span style='color:var(--red)'>already exists</span>" : ""}`
      : "enter a name";
  };
  $("#pl-name", root).addEventListener("input", upd);
  $("#pl-create", root).addEventListener("change", (ev) => { $("#pl-url", root).disabled = ev.target.checked; upd(); });
  upd();
}

function spotifyName(key) {
  return (S.app?.settings.spotify_playlist_prefix ?? "") + key;
}

function needSpotify() {
  if (S.app?.spotify?.connected) return false;
  confirmBox("SPOTIFY ACCOUNT", "Creating playlists on Spotify needs your Spotify account. Connect it in Settings › Spotify first.",
    "Open settings", () => setView({ type: "settings" }), "accent");
  return true;
}

function createSpotifyPlaylist(key) {
  if (needSpotify()) return;
  const node = findNode(key);
  confirmBox("CREATE SPOTIFY PLAYLIST", `Create the playlist <b>${esc(spotifyName(key))}</b> in your Spotify account with the ${node.own_count} songs of this genre and link it?<br><br>
    Songs without a Spotify id stay in the genre as <span class="badge local">LOCAL</span>.`,
    "Create", () => runJob(api("POST", `/api/playlists/${encodeURIComponent(key)}/create-spotify`)), "accent");
}

function splitSelected(key) {
  const ids = [...S.selected].filter((id) => S.rows.find((r) => r.id === id && r.playlist === key && r.status !== "deleted"));
  if (!ids.length) return toast("Select the songs of this genre that should move into the new sub genre (click / ctrl / shift)");
  openSplitDialog(key, ids);
}

function openSplitDialog(key, ids, nameHint = "") {
  if (!ids.length) return toast("No songs selected");
  if (needSpotify()) return;
  const node = findNode(key);
  const rest = node.own_count - ids.length;
  const root = modal("SPLIT INTO SUB GENRE", `
    <div class="field"><label>Songs</label><span>${ids.length} selected · ${rest} stay in <span class="mono">${esc(key)}</span></span></div>
    <div class="field"><label>Sub genre name</label><input type="text" id="split-name" placeholder="speed garage" value="${esc(nameHint)}"></div>
    <div class="preview" id="split-preview"></div>
    <p class="help">${node.spotify_url ? "The genre is updated from Spotify first. " : ""}Two new playlists are created in your Spotify account:
      one for the new sub genre with the selected songs and one for <span class="mono">${esc(key)}</span> with the remaining songs.
      The previous Spotify playlist is <b>not changed or deleted</b>. Files in this genre's folder move into the new sub folder;
      songs stored in other genres' folders stay where they are.</p>`,
  [{ label: "Cancel" }, {
    label: "Split", cls: "accent", action: async (r) => {
      const name = $("#split-name", r).value.trim();
      if (!name) { toast("Enter a name for the sub genre", true); return true; }
      const res = await runJob(api("POST", `/api/playlists/${encodeURIComponent(key)}/split`, { track_ids: ids, name }));
      if (res) { S.selected.clear(); S.expanded.add(key); store.set("expanded", [...S.expanded]); }
      return res ? undefined : true;
    },
  }]);
  const upd = () => {
    const part = normalizeKey($("#split-name", root).value);
    const sub = part && !part.includes("_") ? `${key}_${part}` : "";
    $("#split-preview", root).innerHTML = sub
      ? `new genre <b>${esc(sub)}</b><br>folder <b>${esc(node.folder)}/${esc(part)}/</b><br>spotify <b>${esc(spotifyName(sub))}</b> + <b>${esc(spotifyName(key))}</b>${findNode(sub) ? "<br><span style='color:var(--red)'>already exists</span>" : ""}`
      : part.includes("_") ? "<span style='color:var(--red)'>one layer only - no _</span>" : "enter a name";
  };
  $("#split-name", root).addEventListener("input", upd);
  upd();
}

function retryDownloads(key) {
  runJob(api("POST", `/api/playlists/${encodeURIComponent(key)}/retry-downloads`));
}

function linkFile(trackId) {
  const r = S.rows.find((x) => x.id === trackId);
  browse(`LINK A FILE · ${r ? r.artists + " - " + r.title : ""}`, "", true, async (path) => {
    const res = await act(() => api("POST", `/api/tracks/${encodeURIComponent(trackId)}/link`,
      { path, playlist: S.view.type === "genre" ? S.view.key : null }));
    if (res && res.job) follow(res.job);
  }, "audio");
}

document.addEventListener("click", (ev) => {
  const b = ev.target.closest("[data-link]");
  if (b) { ev.stopPropagation(); linkFile(b.dataset.link); }
});

function syncPlaylist(key) {
  runJob(api("POST", `/api/playlists/${encodeURIComponent(key)}/sync`));
}

function editLink(key) {
  const node = findNode(key);
  modal("SPOTIFY LINK", `
    <div class="field"><label>Playlist</label><span class="mono">${esc(key)}</span></div>
    <div class="field"><label>Spotify link</label><input type="text" id="link-url" value="${esc(node.spotify_url)}" placeholder="https://open.spotify.com/playlist/…"></div>
    ${node.spotify_url && S.app?.spotify?.connected ? `<div class="field"><label>Name on Spotify</label><div id="link-name" class="row"><span class="help">loading…</span></div></div>` : ""}
    <p class="help">Changing the link never removes tracks: songs that are not part of the new Spotify playlist stay in this genre and are marked <span class="badge local">LOCAL</span>.</p>`,
  [{ label: "Cancel" }, { label: "Save & update", cls: "accent", action: async (r) => (await runJob(api("PUT", `/api/playlists/${encodeURIComponent(key)}/link`, { url: $("#link-url", r).value }))) ? undefined : true }]);
  const box = $("#link-name");
  if (box) loadSpotifyName(key, box);
}

// Name of the linked playlist; playlists of the user's own account without the DJM prefix
// can get it with one click (the name is only changed on Spotify).
async function loadSpotifyName(key, box, info) {
  const url = `/api/playlists/${encodeURIComponent(key)}/spotify`;
  try {
    info = info || await api("GET", url);
  } catch (e) {
    box.innerHTML = `<span class="help">${esc(e.message)}</span>`;
    return;
  }
  if (!box.isConnected) return;
  const name = `<span class="mono">${esc(info.name)}</span>`;
  if (info.prefixed) box.innerHTML = name;
  else if (!info.own) box.innerHTML = `${name}<span class="help">not your playlist, so DJ Manager cannot rename it</span>`;
  else {
    box.innerHTML = `${name}<button class="btn" id="link-prefix" title="Rename on Spotify to: ${esc(info.new_name)}">ADD PREFIX</button>`;
    $("#link-prefix", box).addEventListener("click", async (ev) => {
      ev.target.disabled = true;
      const res = await act(() => api("POST", `${url}/add-prefix`));
      if (res) { toast(`Renamed on Spotify to ${res.name}`); loadSpotifyName(key, box, res); } else ev.target.disabled = false;
    });
  }
}

function removeSelected(key) {
  const ids = [...S.selected];
  if (!ids.length) return toast("Select tracks first");
  confirmBox("REMOVE TRACKS", `Remove ${ids.length} track(s) from <b>${esc(key)}</b>? Songs from Spotify are added to this playlist's blacklist so the next update does not add them again. Files are never deleted.`,
    "Remove", () => { S.selected.clear(); return runJob(api("POST", `/api/playlists/${encodeURIComponent(key)}/remove-tracks`, { track_ids: ids })); });
}

function removePlaylist(key) {
  confirmBox("REMOVE PLAYLIST", `Remove <b>${esc(key)}</b>?<br><br>Its Traktor playlist is removed and the folder is deleted. Tracks that are also in other playlists are moved to one of their folders; all others are moved to <span class="mono">_removed/</span> and listed under Removed. No audio file is deleted.`,
    "Remove playlist", async () => { await runJob(api("DELETE", `/api/playlists/${encodeURIComponent(key)}`)); setView({ type: "collection" }); }, "danger");
}

async function showBlacklist(key) {
  const list = await act(() => api("GET", `/api/playlists/${encodeURIComponent(key)}/blacklist`));
  if (!list) return;
  modal(`BLACKLIST · ${key}`, list.length ? `<p class="help">These songs were removed manually and are skipped when the playlist is fetched. Unblock to add them again on the next update.</p>
    <table class="simple"><tr><th></th><th>TITLE</th><th>ARTIST</th><th>SINCE</th></tr>
    ${list.map((b) => `<tr><td><input type="checkbox" value="${esc(b.spotify_id)}"></td><td>${esc(b.title)}</td><td>${esc((b.artists || []).join(", "))}</td><td>${esc(fmtDate(b.added_at))}</td></tr>`).join("")}</table>`
    : `<p class="help">The blacklist is empty.</p>`,
  [{ label: "Close" }, ...(list.length ? [{
    label: "Unblock selected", cls: "accent", action: async (r) => {
      const ids = $$("input:checked", r).map((c) => c.value);
      if (ids.length) await runJob(api("POST", `/api/playlists/${encodeURIComponent(key)}/unblacklist`, { spotify_ids: ids }));
    },
  }] : [])]);
}

async function browse(title, startPath, wantFile, onPick, kind = "nml") {
  let current = startPath || "";
  const root = modal(title, `<div class="mono" id="fs-path"></div><div class="fs-list" id="fs-list"></div>`,
    [{ label: "Cancel" }, ...(wantFile ? [] : [{ label: "Use this folder", cls: "accent", action: () => onPick(current) }])]);
  const load = async (p) => {
    const res = await act(() => api("GET", `/api/browse?files=${wantFile}&kind=${kind}&path=${encodeURIComponent(p)}`));
    if (!res) return;
    current = res.path;
    $("#fs-path", root).textContent = res.path || "Computer";
    const items = [];
    if (res.parent !== null) items.push(`<div data-dir="${esc(res.parent)}">⬑ ..</div>`);
    const join = (n) => res.path ? res.path.replace(/[\\/]$/, "") + (res.path.includes("\\") ? "\\" : "/") + n : n;
    res.dirs.forEach((d) => items.push(`<div data-dir="${esc(join(d))}">▸ ${esc(d)}</div>`));
    res.files.forEach((f) => items.push(`<div class="file" data-file="${esc(join(f))}">♪ ${esc(f)}</div>`));
    $("#fs-list", root).innerHTML = items.join("");
    $$("[data-dir]", root).forEach((el) => el.addEventListener("click", () => load(el.dataset.dir)));
    $$("[data-file]", root).forEach((el) => el.addEventListener("click", () => { $("#modal-root").innerHTML = ""; onPick(el.dataset.file); }));
  };
  load(current);
}

function pickMusicFolder() {
  browse("SELECT MAIN MUSIC FOLDER", S.app?.settings.music_folder, false, async (path) => {
    const res = await act(() => api("POST", "/api/music-folder", { path }));
    if (res && res.job) follow(res.job);
    refresh();
  });
}

// ------------------------------------------------------------------ settings
function renderSettings(view) {
  const s = S.app.settings;
  const sp = S.app.spotify || {};
  const field = (label, html, help = "") => `<div class="field"><label>${label}</label><div>${html}${help ? `<p class="help">${help}</p>` : ""}</div></div>`;
  const text = (k, ph = "", type = "text") => `<input type="${type}" data-k="${k}" value="${esc(s[k])}" placeholder="${esc(ph)}">`;
  const check = (k, label) => `<label><input type="checkbox" data-k="${k}" ${s[k] ? "checked" : ""}> ${label}</label>`;
  const select = (k, opts) => `<select data-k="${k}">${opts.map(([v, l]) => `<option value="${v}" ${s[k] === v ? "selected" : ""}>${l}</option>`).join("")}</select>`;
  view.innerHTML = `<div class="panel">
    <h2>MUSIC</h2>
    ${field("Main music folder", `<div class="row"><input type="text" readonly value="${esc(s.music_folder)}" placeholder="not set"><button class="btn" onclick="pickMusicFolder()">BROWSE</button></div>`,
      "Genres are folders in here, sub genres are folders inside them.")}
    ${field("Rescan", `<button class="btn" onclick="runJob(api('POST','/api/rescan'))">RESCAN FOLDER</button>`, "Imports new folders and files added outside of DJ Manager.")}

    <h2>TRAKTOR</h2>
    ${field("collection.nml", `<div class="row">${text("traktor_nml", S.app.nml_path ? "auto: " + S.app.nml_path : "auto-detect")}<button class="btn" onclick="pickNml()">BROWSE</button></div>`,
      "Leave empty to auto-detect (Documents/Native Instruments/Traktor x.y.z).")}
    ${field("Path mode", select("traktor_path_mode", [["auto", "Auto (Windows native / Linux Wine)"], ["native", "Windows native"], ["wine", "Wine (Linux)"]]))}
    ${field("Wine prefix", text("wine_prefix", "derived from the collection path or ~/.wine"))}
    ${field("Traktor folder", text("traktor_root_folder"), "Playlist folder owned by DJ Manager. Your other Traktor playlists are never edited.")}

    <h2>SPOTIFY</h2>
    ${field("Client ID", text("spotify_client_id", "from your app at developer.spotify.com"),
      "Needed to create playlists (splitting genres). Create an app at developer.spotify.com (the app owner needs Spotify Premium), "
      + "add the redirect URI <span class='mono'>http://127.0.0.1:9900/</span> and select the Web API.")}
    ${field("Spotify account", `<div class="row">${sp.connected
        ? `<span>connected as <b>${esc(sp.user)}</b></span> <button class="btn" onclick="disconnectSpotify()">DISCONNECT</button>`
        : `<button class="btn accent" onclick="connectSpotify()">CONNECT SPOTIFY ACCOUNT</button><span class="help">${sp.has_client_id ? "not connected" : "enter and save the Client ID first"}</span>`}</div>`,
      "The only login DJ Manager needs: your own playlists (also private ones) are read through it, and playlists are created with it. "
      + "Other people's public playlists are read by spotdl without any login. DJ Manager never deletes or changes your other playlists.")}
    ${field("Playlist name prefix", text("spotify_playlist_prefix"), "Playlists created by DJ Manager are named prefix + genre key, e.g. <span class='mono'>DJM · house_ukg-garage</span>.")}

    <h2>DOWNLOAD</h2>
    ${field("Format", select("audio_format", [["mp3", "MP3"], ["m4a", "M4A / AAC"]]))}
    ${field("Bitrate", text("bitrate", "320k"), "e.g. 320k, 256k. For M4A use <span class='mono'>disable</span> to keep the source quality without re-encoding.")}
    ${field("Parallel downloads", text("download_threads", "", "number"))}
    ${field("Retries", text("download_retries", "", "number"), "Extra attempts for songs that fail with a download error. Songs not found on YouTube are not retried automatically.")}
    ${field("YouTube cookie file", text("cookie_file", "optional, cookies.txt for YouTube Music Premium quality"))}

    <h2>UPDATES</h2>
    ${field("Version", `<span class="mono">${esc(S.app.version)}</span> · <span class="help">${esc({source: "running from source", installed: "installed (Windows installer)", portable: "portable exe", binary: "Linux binary"}[S.app.install_mode] || S.app.install_mode)}</span>`)}
    ${field("Release source", text("update_repo", "owner/repo on GitHub (empty = repository the build came from)"))}
    ${field("On start", check("check_app_updates", "Check for new DJ Manager versions when it starts"))}
    ${field("", `<div class="row"><button class="btn" id="check-update">CHECK FOR UPDATES</button><span class="help" id="update-result"></span></div>`)}

    <h2>ANALYSIS</h2>
    ${field("Status", `<div class="row" id="analysis-status">${analysisStatusHtml()}</div>`)}
    ${field("", `<div class="row">
        <button class="btn accent" onclick="runJob(api('POST','/api/analysis',{mode:'pending'}))">${S.app.analysis?.paused ? "▶ RESUME" : "▶ ANALYSE NEW SONGS"}</button>
        <button class="btn" onclick="runJob(api('POST','/api/analysis',{mode:'failed'}))">RETRY FAILED</button>
        <button class="btn" onclick="confirmBox('RE-ANALYSE','Analyse all songs again? Existing BPM and key values are replaced.','Re-analyse',()=>runJob(api('POST','/api/analysis',{mode:'all'})))">RE-ANALYSE ALL</button></div>`,
      "BPM and key are stored in DJ Manager only; Traktor keeps its own analysis. The tools (Essentia on Linux, librosa on Windows) are installed into the managed environment on first use.")}
    ${field("Automatic", check("analysis_auto", "Analyse new songs automatically (downloads, import, songs found in the music folder)"))}
    ${field("Key notation", select("key_notation", [["openkey", "Open Key (1m, 8d) - like Traktor"], ["camelot", "Camelot (8A, 3B)"], ["musical", "Musical (Am, C#)"]]))}
    ${field("BPM range", `<div class="row">${text("bpm_min", "", "number")}<span>to</span>${text("bpm_max", "", "number")}</div>`,
      "Results outside are halved or doubled, e.g. 87 BPM becomes 174 BPM with a range of 90-180.")}
    ${field("Parallel analyses", text("analysis_workers", "", "number"), "0 = automatic (CPU cores - 1, at most 4).")}

    ${recSettingsHtml(field, check, text)}

    <h2>GENERAL</h2>
    ${field("On start", check("update_playlists_on_start", "Update all playlists when DJ Manager starts"),
      "Off: playlists are only updated with UPDATE / UPDATE ALL.")}
    ${field("", check("scan_on_start", "Look for songs added to the music folder outside DJ Manager"))}
    ${field("Backups to keep", text("backups_to_keep", "", "number"), "The initial backup is always kept.")}
    <p><button class="btn accent" id="save-settings">SAVE SETTINGS</button></p>
  </div>`;
  $("#check-update").addEventListener("click", async () => {
    $("#update-result").textContent = "checking…";
    const info = await act(() => api("GET", "/api/update/check"));
    $("#update-result").innerHTML = !info ? "" : info.available && info.asset_url
      ? `${esc(info.latest)} available <button class="btn orange" onclick="applyUpdate()">INSTALL ${esc(info.latest)}</button>`
      : esc(info.message || (info.available ? `${info.latest} available` : "up to date"));
    refreshState();
  });
  const master = $("[data-k=rec_enabled]", view);
  if (master) master.addEventListener("change", async () => {
    if (await act(() => api("POST", "/api/settings", { rec_enabled: master.checked }))) refresh();
  });
  $("#save-settings").addEventListener("click", async () => {
    const values = {};
    $$("[data-k]", view).forEach((el) => { values[el.dataset.k] = el.type === "checkbox" ? el.checked : el.value; });
    if (await act(() => api("POST", "/api/settings", values))) { toast("Settings saved"); refresh(); }
  });
}

function applyUpdate() {
  const u = S.app?.update || {};
  confirmBox("UPDATE DJ MANAGER", `Download and install DJ Manager <b>${esc(u.latest || "")}</b>? The app closes and starts again with the new version. Your library, settings and spotdl environment are kept.`,
    "Update now", () => runJob(api("POST", "/api/update/apply")));
}

function analysisStatusHtml() {
  const a = S.app.analysis || {};
  const engine = a.engine ? `engine <b>${esc(a.engine)}</b>` : "tools not installed yet";
  return `<span>${a.done || 0} analysed · ${a.pending || 0} pending · ${a.failed || 0} failed · ${engine}${a.paused ? " · <b>paused</b>" : ""}${a.job ? " · running" : ""}</span>`;
}

async function connectSpotify() {
  const values = {};
  $$("[data-k]", $("#view")).forEach((el) => { values[el.dataset.k] = el.type === "checkbox" ? el.checked : el.value; });
  if (!(values.spotify_client_id || "").trim()) return toast("Enter the Client ID of your Spotify app first", true);
  await act(() => api("POST", "/api/settings", values));  // save the Client ID first
  runJob(api("POST", "/api/spotify/connect"));
  toast("Log in to Spotify in the browser window that opens");
}

async function disconnectSpotify() {
  if (await act(() => api("POST", "/api/spotify/disconnect"))) refresh();
}

function pickNml() {
  const start = S.app?.nml_path ? S.app.nml_path.replace(/[\\/][^\\/]*$/, "") : "";
  browse("SELECT collection.nml", start, true, async (path) => {
    if (await act(() => api("POST", "/api/settings", { traktor_nml: path }))) { toast("Collection set"); refresh(); }
  });
}

// ------------------------------------------------------------------ dependencies
async function renderDeps(view) {
  const d = await viewData(api("GET", "/api/deps"));
  view.innerHTML = `<div class="panel">
    <h2>MANAGED DEPENDENCIES</h2>
    <p class="help">spotdl and yt-dlp change often when Spotify or YouTube change. They run in their own environment
      (<span class="mono">${esc(d.venv)}</span>) and can be updated here. Every change is snapshotted, so a broken update can be rolled back.</p>
    <table class="simple"><tr><th>PACKAGE</th><th>INSTALLED</th><th>INSTALL VERSION</th><th></th></tr>
    ${d.packages.map((p) => `<tr><td><b>${p}</b></td><td class="mono">${esc(d.versions[p] || "—")}</td>
      <td><select data-pkg="${p}"><option value="">latest</option></select></td>
      <td><button class="btn" data-install="${p}">${d.versions[p] ? "INSTALL" : "INSTALL"}</button></td></tr>`).join("")}
    </table>
    <p style="margin-top:10px"><button class="btn accent" id="deps-update-all">${d.installed ? "UPDATE ALL TO LATEST" : "INSTALL SPOTDL + YT-DLP"}</button>
      <button class="btn" onclick="runJob(api('POST','/api/deps',{action:'good'}))">MARK CURRENT AS WORKING</button>
      <button class="btn danger" onclick="confirmBox('REINSTALL','Delete the dependency environment and reinstall the last working versions?','Reinstall',()=>runJob(api('POST','/api/deps',{action:'reinstall'})))">REINSTALL ENVIRONMENT</button></p>

    <h2>FFMPEG</h2>
    <p class="help">${d.ffmpeg ? `Found: <span class="mono">${esc(d.ffmpeg)}</span>` : "ffmpeg not found - spotdl needs it to convert downloads."}</p>
    <button class="btn" onclick="runJob(api('POST','/api/deps',{action:'ffmpeg'}))">DOWNLOAD FFMPEG VIA SPOTDL</button>

    <h2>SNAPSHOTS</h2>
    ${d.snapshots.length ? `<table class="simple"><tr><th>DATE</th><th>REASON</th><th>VERSIONS</th><th>STATUS</th><th></th></tr>
      ${d.snapshots.map((s) => `<tr><td>${esc(fmtDate(s.created_at))}</td><td>${esc(s.reason)}</td>
        <td class="mono">${Object.entries(s.versions).map(([k, v]) => `${esc(k)} ${esc(v)}`).join(", ")}</td>
        <td><span class="pill ${s.status}">${s.status.toUpperCase()}</span></td>
        <td><button class="btn" data-restore="${esc(s.id)}">RESTORE</button></td></tr>`).join("")}</table>`
      : `<p class="help">No snapshots yet.</p>`}
  </div>`;
  $("#deps-update-all").addEventListener("click", () => runJob(api("POST", "/api/deps", { action: "install" })));
  $$("[data-install]", view).forEach((b) => b.addEventListener("click", () => {
    const pkg = b.dataset.install;
    runJob(api("POST", "/api/deps", { action: "install", package: pkg, version: $(`select[data-pkg="${pkg}"]`, view).value }));
  }));
  $$("[data-restore]", view).forEach((b) => b.addEventListener("click", () =>
    confirmBox("RESTORE DEPENDENCIES", "Reinstall exactly the package versions of this snapshot?", "Restore",
      () => runJob(api("POST", "/api/deps", { action: "restore", snapshot_id: b.dataset.restore })))));
  for (const p of d.packages) {
    api("GET", `/api/deps/versions/${p}`).then((versions) => {
      const sel = $(`select[data-pkg="${p}"]`, view);
      if (sel) sel.innerHTML += versions.map((v) => `<option value="${esc(v)}">${esc(v)}</option>`).join("");
    }).catch(() => {});
  }
}

// ------------------------------------------------------------------ backups
async function renderBackups(view) {
  const list = await viewData(api("GET", "/api/backups"));
  view.innerHTML = `<div class="panel">
    <h2>TRAKTOR BACKUPS</h2>
    <p class="help">A backup of the Traktor collection is created before DJ Manager changes it the first time and before every update.
      Restore only while Traktor is closed.</p>
    <p><button class="btn" id="backup-now">BACKUP NOW</button></p>
    ${list.length ? `<table class="simple"><tr><th>DATE</th><th>REASON</th><th>COLLECTION</th><th></th></tr>
      ${list.map((b) => `<tr><td>${esc(fmtDate(b.created_at))} ${b.initial ? `<span class="pill initial">INITIAL</span>` : ""}</td>
        <td>${esc(b.reason)}</td><td class="mono">${esc(b.nml_path)}</td>
        <td><button class="btn" data-restore="${esc(b.id)}" data-lib="${b.has_library}">RESTORE</button></td></tr>`).join("")}</table>`
      : `<p class="help">No backups yet.</p>`}
  </div>`;
  $("#backup-now").addEventListener("click", async () => { if (await act(() => api("POST", "/api/backups"))) { toast("Backup created"); render(); } });
  $$("[data-restore]", view).forEach((b) => b.addEventListener("click", () => {
    modal("RESTORE BACKUP", `<p>Restore the Traktor collection from this backup?</p>
      ${b.dataset.lib === "true" ? `<label><input type="checkbox" id="restore-lib"> Also restore DJ Manager's library state</label>
      <p class="help">Only needed if DJ Manager's own data is broken. Files moved since the backup are not moved back.</p>` : ""}`,
    [{ label: "Cancel" }, { label: "Restore", cls: "orange", action: (r) => runJob(api("POST", `/api/backups/${b.dataset.restore}/restore`, { restore_library: !!$("#restore-lib", r)?.checked })) }]);
  }));
}

// ------------------------------------------------------------------ audio player
const Player = {
  audio: null, queue: [], order: [], pos: -1, label: "",
  shuffle: store.get("player.shuffle", false), loop: store.get("player.loop", "off"),  // off | all | one

  init() {
    this.audio = $("#audio");
    const a = this.audio;
    a.volume = store.get("player.volume", 0.8);
    $("#pl-vol").value = Math.round(a.volume * 100);
    $("#pl-vol").addEventListener("input", (ev) => { a.volume = ev.target.value / 100; store.set("player.volume", a.volume); });
    $("#pl-play").addEventListener("click", () => this.toggle());
    $("#pl-next").addEventListener("click", () => this.next(true));
    $("#pl-prev").addEventListener("click", () => this.prev());
    $("#pl-shuffle").addEventListener("click", () => this.setShuffle(!this.shuffle));
    $("#pl-loop").addEventListener("click", () => this.setLoop({ off: "all", all: "one", one: "off" }[this.loop]));
    const pos = $("#pl-pos");
    let seeking = false;
    pos.addEventListener("input", () => { seeking = true; $("#pl-time").textContent = fmtTime((pos.value / 1000) * (a.duration || 0)); });
    pos.addEventListener("change", () => { if (a.duration) a.currentTime = (pos.value / 1000) * a.duration; seeking = false; });
    a.addEventListener("timeupdate", () => {
      if (!seeking && a.duration) pos.value = Math.round((a.currentTime / a.duration) * 1000);
      if (!seeking) $("#pl-time").textContent = fmtTime(a.currentTime);
    });
    a.addEventListener("loadedmetadata", () => { $("#pl-dur").textContent = fmtTime(a.duration); pos.disabled = false; });
    a.addEventListener("play", () => this.paint());
    a.addEventListener("pause", () => this.paint());
    a.addEventListener("ended", () => this.next(false));
    a.addEventListener("error", () => {
      if (this.pos < 0) return;
      const t = this.current();
      toast(`Cannot play ${t ? t.title : "this song"} here (format not supported by this window) - skipped`, true);
      this.next(false);
    });
    document.addEventListener("keydown", (ev) => {  // space = play/pause, unless typing
      if (ev.code !== "Space" || ev.target.closest("input, textarea, select, [contenteditable]") || $("#modal-root").innerHTML) return;
      if (this.pos < 0) return;
      ev.preventDefault();
      this.toggle();
    });
    if ("mediaSession" in navigator) {  // keyboard media keys / OS controls
      const ms = navigator.mediaSession;
      ms.setActionHandler("play", () => this.toggle(true));
      ms.setActionHandler("pause", () => this.toggle(false));
      ms.setActionHandler("previoustrack", () => this.prev());
      ms.setActionHandler("nexttrack", () => this.next(true));
      try { ms.setActionHandler("seekto", (d) => { a.currentTime = d.seekTime; }); } catch { /* not supported */ }
    }
    this.paint();
  },

  current() { return this.pos >= 0 ? this.queue[this.order[this.pos]] : null; },

  playList(rows, startId, label) {
    const playable = rows.filter((r) => r.has_file);  // songs without a file are skipped
    if (!playable.length) return toast("Nothing playable in this list", true);
    this.queue = playable.map((r) => ({ id: r.id, title: r.title, artists: r.artists, bpm: r.bpm, key: r.key }));
    this.label = label || "";
    const start = Math.max(0, this.queue.findIndex((t) => t.id === startId));
    this.order = this.queue.map((_, i) => i);
    if (this.shuffle) this.shuffleOrder(startId ? start : null);
    this.pos = this.shuffle ? 0 : start;
    this.load(true);
  },

  shuffleOrder(first) {
    const rest = this.queue.map((_, i) => i).filter((i) => i !== first);
    for (let i = rest.length - 1; i > 0; i--) { const j = Math.floor(Math.random() * (i + 1)); [rest[i], rest[j]] = [rest[j], rest[i]]; }
    this.order = first == null ? rest : [first, ...rest];
  },

  load(autoplay) {
    const t = this.current();
    if (!t) return;
    this.audio.src = `/api/tracks/${encodeURIComponent(t.id)}/audio`;
    $("#pl-pos").value = 0;
    $("#pl-time").textContent = "0:00";
    $("#pl-dur").textContent = "";
    if (autoplay) this.audio.play().catch(() => {});
    if ("mediaSession" in navigator && window.MediaMetadata) {
      navigator.mediaSession.metadata = new MediaMetadata({ title: t.title, artist: t.artists, album: this.label });
    }
    this.paint();
  },

  toggle(force) {
    if (this.pos < 0) return;
    const play = force ?? this.audio.paused;
    if (play) this.audio.play().catch(() => {}); else this.audio.pause();
  },

  next(manual) {
    if (this.pos < 0) return;
    if (!manual && this.loop === "one") { this.audio.currentTime = 0; this.audio.play().catch(() => {}); return; }
    if (this.pos + 1 < this.order.length) { this.pos += 1; return this.load(true); }
    if (this.loop === "all" || (manual && this.loop === "one")) {
      if (this.shuffle) this.shuffleOrder(null);
      this.pos = 0;
      return this.load(true);
    }
    this.audio.pause();  // end of the list
    this.audio.currentTime = 0;
    this.paint();
  },

  prev() {
    if (this.pos < 0) return;
    if (this.audio.currentTime > 3 || (this.pos === 0 && this.loop !== "all")) { this.audio.currentTime = 0; return; }
    this.pos = this.pos > 0 ? this.pos - 1 : this.order.length - 1;
    this.load(true);
  },

  setShuffle(on) {
    this.shuffle = on;
    store.set("player.shuffle", on);
    if (this.pos >= 0) {  // keep the current song, reorder the rest
      const cur = this.order[this.pos];
      if (on) this.shuffleOrder(cur); else this.order = this.queue.map((_, i) => i);
      this.pos = this.order.indexOf(cur);
    }
    this.paint();
  },

  setLoop(mode) { this.loop = mode; store.set("player.loop", mode); this.paint(); },

  paint() {
    const t = this.current();
    const playing = t && !this.audio.paused;
    $("#pl-play").textContent = playing ? "⏸" : "▶";
    $("#pl-shuffle").classList.toggle("on", this.shuffle);
    $("#pl-loop").classList.toggle("on", this.loop !== "off");
    $("#pl-loop").textContent = this.loop === "one" ? "⟳1" : "⟳";
    $("#pl-loop").title = { off: "Loop: off", all: "Loop: whole list", one: "Loop: this song" }[this.loop];
    $("#pl-title").textContent = t ? t.title : "Nothing playing";
    $("#pl-sub").textContent = t
      ? [t.artists, t.bpm ? `${t.bpm.toFixed(1)} BPM` : "", t.key, `${this.label} · ${this.pos + 1}/${this.order.length}`].filter(Boolean).join(" · ")
      : "Double-click a song or press ▶ PLAY in a list";
    $$("tbody tr[data-id]").forEach((tr) => tr.classList.toggle("playing", !!t && tr.dataset.id === t.id));
  },
};

function viewLabel() {
  const v = S.view;
  if (v.type === "genre") return v.key.split("_").map((p) => p.replace(/-/g, " ")).join(" › ");
  return { collection: "Track Collection", removed: "Removed" }[v.type] || "";
}

// ------------------------------------------------------------------ duplicates
const fmtMB = (b) => b >= 1e9 ? `${(b / 1e9).toFixed(1)} GB` : `${Math.round(b / 1e6)} MB`;

function copyLine(c, extra = "") {
  const q = c.lossless ? "lossless" : c.bitrate ? `${c.bitrate} kbit/s` : "";
  const facts = [q, c.duration ? fmtTime(c.duration) : "", c.cues ? `Traktor: ${c.cues} cues/grid` : "", fmtMB(c.size)].filter(Boolean).join(" · ");
  return `<div class="dup-file">${extra}<span class="mono">${esc(c.path)}</span><i>${esc(facts)}${c.evidence ? " · " + esc(c.evidence) : ""}</i></div>`;
}

async function renderDuplicates(deck, view) {
  const rep = await viewData(api("GET", "/api/duplicates"));
  if (S.view.type !== "duplicates") return;
  const uncertain = rep.groups.filter((g) => g.uncertain.length);
  deck.innerHTML = deckHtml({
    letter: "D", orange: true, title: "Duplicates",
    sub: "The same song stored more than once on disk. Cleaning up keeps one file per song (the one with Traktor cue points, else the best quality) "
      + "and moves the other copies to the system trash. Every genre keeps the song; Traktor references are updated.",
    meters: [[rep.songs, "SONGS"], [rep.certain_files + rep.uncertain_files, "EXTRA COPIES"], [fmtMB(rep.certain_bytes), "TO FREE"]],
    tools: rep.certain_files ? `<button class="btn orange" id="dup-clean">🗑 MOVE ${rep.certain_files} DUPLICATES TO TRASH</button>` : "",
  });
  const groupHtml = (g, mode) => `<div class="dup-group">
      <div class="dup-song"><b>${esc(g.title)}</b> <span class="help">${esc(g.artists)}</span>
        ${g.playlists.map((p) => `<span class="pl-chip">${esc(p)}</span>`).join("")}</div>
      ${copyLine(g.keep, `<span class="badge spotify" title="${esc(g.reason)}">KEEP</span>`)}
      <div class="help dup-reason">kept: ${esc(g.reason)}</div>
      ${mode === "certain"
        ? g.remove.map((c) => copyLine(c, `<span class="badge failed">TRASH</span>`)).join("")
        : g.uncertain.map((c) => copyLine(c, `<label class="dup-pick"><input type="checkbox" data-uncertain="${esc(c.path)}" data-track="${esc(g.track_id)}"> TRASH?</label>`)).join("")}
    </div>`;
  const certain = rep.groups.filter((g) => g.remove.length);
  view.innerHTML = `<div class="panel dup">
    ${rep.songs ? "" : `<div class="empty"><h3>No duplicates</h3>Every song is stored once.</div>`}
    ${certain.length ? `<h2>CERTAIN DUPLICATES · ${certain.length} SONGS</h2>
      <p class="help">Same Spotify id, same ISRC, or an identical file. These copies are moved to the trash with the button above.</p>
      ${certain.map((g) => groupHtml(g, "certain")).join("")}` : ""}
    ${uncertain.length ? `<h2>MAYBE DUPLICATES · ${uncertain.length} SONGS</h2>
      <p class="help">Only artist and title match - possibly another version (e.g. Extended Mix). Compare the durations and tick the copies that really are duplicates.</p>
      <p><button class="btn orange" id="dup-clean-selected" disabled>🗑 MOVE SELECTED TO TRASH</button></p>
      ${uncertain.map((g) => groupHtml(g, "uncertain")).join("")}` : ""}
  </div>`;
  const run = (body, text) => confirmBox("CLEAN UP DUPLICATES", text, "Move to trash",
    () => runJob(api("POST", "/api/duplicates/clean", body)), "orange");
  const btn = $("#dup-clean");
  if (btn) btn.addEventListener("click", () => run({},
    `Move <b>${rep.certain_files}</b> certain duplicate copies (${fmtMB(rep.certain_bytes)}) to the system trash?<br><br>
     One file per song is kept and checked first. Every genre keeps the song (shown as <span class="badge elsewhere">IN OTHER GENRE</span> where the file lives elsewhere),
     Traktor playlists - also your own - point to the kept file, and updates do not download the songs again.
     You can restore the files from the trash. Close Traktor first.`));
  const picks = $$("[data-uncertain]", view);
  const sel = $("#dup-clean-selected");
  picks.forEach((x) => x.addEventListener("change", () => { sel.disabled = !picks.some((p) => p.checked); }));
  if (sel) sel.addEventListener("click", () => {
    const chosen = picks.filter((p) => p.checked);
    run({ track_ids: [...new Set(chosen.map((p) => p.dataset.track))], uncertain: chosen.map((p) => p.dataset.uncertain) },
      `Move the <b>${chosen.length}</b> selected copies to the trash? Make sure they are the same recording - the kept file of each song is shown in green.`);
  });
}

// ------------------------------------------------------------------ split recommendations
const SIGNAL_LABEL = { tempo: "BPM", energy: "ENERGY", sound: "SOUND", styles: "AI STYLE" };
const MAP_LABEL = { bpm_energy: "BPM × Energy", sound: "Sound", style: "Style (AI)" };

async function renderRecommend(deck, view) {
  const key = S.view.key;
  const node = findNode(key);
  if (!node) { setView({ type: "collection" }); return; }
  deck.innerHTML = deckHtml({
    letter: "✦", title: `Recommendations · ${esc(node.key.split("_").map((p) => p.replace(/-/g, " ")).join(" › "))}`,
    sub: "Suggested groups for a new sub genre. Nothing changes until you confirm a split.",
    meters: [[node.own_count, "OWN SONGS"]],
    tools: `<button class="btn" onclick="setView({type:'genre', key:${js(key)}})">← BACK TO GENRE</button>`,
  });
  view.innerHTML = `<div class="empty">Looking for groups…</div>`;
  const [rec, rows] = await viewData(Promise.all([api("GET", `/api/genre/${encodeURIComponent(key)}/recommendations`),
    api("GET", `/api/genre/${encodeURIComponent(key)}/tracks`)]));
  const own = rows.filter((r) => r.playlist === key && r.status !== "deleted");
  S.rec = { key, rec, byId: new Map(own.map((r) => [r.id, r])), mapSel: new Set(), mapSpace: null, focus: null };
  const c = rec.coverage, t = rec.tasks;
  const missing = [];
  if (rec.signals.bpm || rec.signals.energy) {
    if (c.bpm < c.songs) missing.push(`${c.songs - c.bpm} songs without BPM`);
  }
  if ((rec.signals.energy || rec.signals.timbre) && c.features < c.songs)
    missing.push(`${c.songs - c.features} songs without energy/sound <button class="btn tiny" onclick="analyseFor(['features'])">ANALYSE</button>`);
  if (rec.signals.styles && c.styles < c.songs)
    missing.push(`${c.songs - c.styles} songs without AI style <button class="btn tiny" onclick="analyseFor(['styles'])">ANALYSE</button>`);
  const cards = rec.suggestions.map((sg, i) => `
    <div class="rec-card" data-i="${i}">
      <div class="rec-head">${sg.signals.map((x) => `<span class="badge spotify">${SIGNAL_LABEL[x] || x.toUpperCase()}</span>`).join("")}
        <b>${esc(sg.title)}</b>${sg.signals.length > 1 ? `<span class="help">${sg.signals.length} signals agree</span>` : ""}</div>
      <ul class="rec-reasons">${sg.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>
      <div class="rec-songs">${sg.track_ids.map((id) => songChip(id, true)).join("")}</div>
      <div class="row"><button class="btn accent" data-split="${i}">⑂ SPLIT THESE SONGS…</button>
        <button class="btn" data-focus="${i}">SHOW ON MAP</button></div>
    </div>`).join("");
  const spaces = Object.keys(rec.maps).filter((k) => rec.maps[k].length);
  view.innerHTML = `<div class="panel rec">
    ${missing.length ? `<p class="help rec-missing">Not all songs are analysed yet: ${missing.join(" · ")}</p>` : ""}
    <h2>SUGGESTIONS</h2>
    ${cards || `<p class="help">${c.songs < 2 * rec.min_group ? `At least ${2 * rec.min_group} songs are needed for suggestions.` : "No clear group found with the enabled signals - this genre looks consistent. You can still pick groups on the map."}</p>`}
    <h2>MAP</h2>
    ${spaces.length ? `<div class="row map-tools">${spaces.map((k) => `<button class="btn ${k === spaces[0] ? "accent" : ""}" data-space="${k}">${MAP_LABEL[k] || k}</button>`).join("")}
        <span class="help" id="map-info">Draw around songs to select them (shift adds, double-click clears).</span>
        <button class="btn accent" id="map-split" disabled>⑂ SPLIT SELECTION…</button></div>
      <canvas id="rec-map" class="rec-map" width="1200" height="520"></canvas>
      <div class="rec-songs" id="map-songs"></div>`
      : `<p class="help">The map needs analysed songs (BPM + energy, sound or AI style).</p>`}
  </div>`;
  $$("[data-split]", view).forEach((b) => b.addEventListener("click", () => {
    const card = b.closest(".rec-card");
    const ids = $$("input[type=checkbox]:checked", card).map((x) => x.value);
    openSplitDialog(key, ids, rec.suggestions[+b.dataset.split].name_hint);
  }));
  $$("[data-focus]", view).forEach((b) => b.addEventListener("click", () => {
    S.rec.focus = new Set(rec.suggestions[+b.dataset.focus].track_ids);
    drawMap();
    $("#rec-map").scrollIntoView({ behavior: "smooth", block: "center" });
  }));
  $$("[data-space]", view).forEach((b) => b.addEventListener("click", () => {
    $$("[data-space]", view).forEach((x) => x.classList.toggle("accent", x === b));
    S.rec.mapSpace = b.dataset.space;
    drawMap();
  }));
  if (spaces.length) {
    S.rec.mapSpace = spaces[0];
    setupMap();
    drawMap();
    $("#map-split").addEventListener("click", () => openSplitDialog(key, [...S.rec.mapSel]));
  }
}

function songChip(id, checkbox) {
  const r = S.rec.byId.get(id);
  if (!r) return "";
  const style = r.styles && r.styles[0] ? ` · ${esc(r.styles[0][0])}` : "";
  const facts = `${r.bpm ? r.bpm.toFixed(0) + " BPM" : ""}${r.energy != null ? " · E" + r.energy : ""}${style}`;
  return `<label class="song-chip" title="${esc(r.artists)} - ${esc(r.title)}">${checkbox ? `<input type="checkbox" value="${esc(id)}" checked>` : ""}
    <span>${esc(r.title)}</span><i>${esc(r.artists)}${facts ? " · " + facts : ""}</i></label>`;
}

function analyseFor(tasks) {
  runJob(api("POST", "/api/analysis", { mode: "pending", tasks }));
  toast("Analysis started - come back to the recommendations when it is done");
}

function mapPoints() {
  const raw = (S.rec.rec.maps[S.rec.mapSpace] || []).filter((p) => S.rec.byId.has(p[0]));
  if (!raw.length) return [];
  const xs = raw.map((p) => p[1]), ys = raw.map((p) => p[2]);
  const [x0, x1, y0, y1] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const cv = $("#rec-map"), pad = 40;
  return raw.map(([id, x, y]) => ({
    id, x: pad + ((x - x0) / ((x1 - x0) || 1)) * (cv.width - 2 * pad),
    y: cv.height - pad - ((y - y0) / ((y1 - y0) || 1)) * (cv.height - 2 * pad),
  }));
}

function drawMap(lasso) {
  const cv = $("#rec-map");
  if (!cv) return;
  const ctx = cv.getContext("2d");
  const css = getComputedStyle(document.documentElement);
  const col = (n) => css.getPropertyValue(n).trim();
  ctx.clearRect(0, 0, cv.width, cv.height);
  ctx.fillStyle = col("--bg-2"); ctx.fillRect(0, 0, cv.width, cv.height);
  ctx.fillStyle = col("--muted"); ctx.font = "12px sans-serif";
  if (S.rec.mapSpace === "bpm_energy") {
    ctx.fillText("BPM →", cv.width - 60, cv.height - 12);
    ctx.save(); ctx.translate(14, 60); ctx.rotate(-Math.PI / 2); ctx.fillText("ENERGY →", -40, 0); ctx.restore();
  } else {
    ctx.fillText("similar songs are close together", 12, 18);
  }
  S.rec.points = mapPoints();
  for (const p of S.rec.points) {
    const sel = S.rec.mapSel.has(p.id), focus = S.rec.focus && S.rec.focus.has(p.id);
    ctx.beginPath(); ctx.arc(p.x, p.y, sel ? 6 : 4.5, 0, 2 * Math.PI);
    ctx.fillStyle = sel ? col("--yellow") : focus ? col("--orange") : col("--active");
    ctx.globalAlpha = sel || focus || !S.rec.focus ? 0.95 : 0.35;
    ctx.fill();
  }
  ctx.globalAlpha = 1;
  if (lasso && lasso.length > 1) {
    ctx.strokeStyle = col("--yellow"); ctx.setLineDash([4, 4]); ctx.beginPath();
    lasso.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    ctx.closePath(); ctx.stroke(); ctx.setLineDash([]);
  }
  const n = S.rec.mapSel.size;
  $("#map-split").disabled = !n;
  $("#map-info").textContent = n ? `${n} songs selected` : "Draw around songs to select them (shift adds, double-click clears).";
  $("#map-songs").innerHTML = [...S.rec.mapSel].map((id) => songChip(id, false)).join("");
}

function setupMap() {
  const cv = $("#rec-map");
  const pos = (ev) => { const r = cv.getBoundingClientRect(); return [(ev.clientX - r.left) * cv.width / r.width, (ev.clientY - r.top) * cv.height / r.height]; };
  let lasso = null, additive = false;
  cv.addEventListener("mousedown", (ev) => { lasso = [pos(ev)]; additive = ev.shiftKey; });
  cv.addEventListener("mousemove", (ev) => {
    if (lasso) { lasso.push(pos(ev)); drawMap(lasso); return; }
    const [x, y] = pos(ev);
    const hit = (S.rec.points || []).find((p) => Math.hypot(p.x - x, p.y - y) < 7);
    const r = hit && S.rec.byId.get(hit.id);
    cv.title = r ? `${r.artists} - ${r.title}${r.bpm ? " · " + r.bpm.toFixed(1) + " BPM" : ""}${r.energy != null ? " · energy " + r.energy : ""}` : "";
  });
  window.addEventListener("mouseup", () => {
    if (!lasso) return;
    const poly = lasso;
    lasso = null;
    if (poly.length < 3) { drawMap(); return; }
    const inside = ([x, y]) => {  // ray casting
      let hit = false;
      for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
        const [xi, yi] = poly[i], [xj, yj] = poly[j];
        if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) hit = !hit;
      }
      return hit;
    };
    if (!additive) S.rec.mapSel = new Set();
    for (const p of S.rec.points) if (inside([p.x, p.y])) S.rec.mapSel.add(p.id);
    drawMap();
  });
  cv.addEventListener("dblclick", () => { S.rec.mapSel = new Set(); S.rec.focus = null; drawMap(); });
}

function recSettingsHtml(field, check, text) {
  const t = S.app.analysis?.tasks || {};
  const st = (task) => t[task] ? `${t[task].done} done · ${t[task].pending} pending${t[task].failed ? " · " + t[task].failed + " failed" : ""}` : "";
  const s = S.app.settings;
  return `
    <h2>SPLIT RECOMMENDATIONS</h2>
    ${field("Recommendations", check("rec_enabled", "Enable split recommendations (optional)"),
      "Adds a ✦ RECOMMEND button to every genre: suggested groups of songs for a new sub genre and a map to pick groups yourself. Suggestions only pre-fill the split dialog.")}
    ${s.rec_enabled ? `
    ${field("BPM groups", check("rec_bpm", "Split by tempo"), "Uses the BPM analysis, nothing extra to analyse.")}
    ${field("Energy & sound", `${check("rec_energy", "Energy level (1-10)")} &nbsp; ${check("rec_timbre", "Sound similarity")}
        <div class="row" style="margin-top:6px">${check("features_auto", "Analyse new songs automatically")}
        <button class="btn" onclick="analyseFor(['features'])">ANALYSE COLLECTION NOW</button><span class="help">${st("features")}</span></div>`,
      "About 2 seconds extra per song, analysed together with BPM and key.")}
    ${field("AI styles", `${check("rec_styles", "Style recognition (400 Discogs styles, e.g. Speed Garage)")}
        <div class="row" style="margin-top:6px">${check("styles_auto", "Analyse new songs automatically")}
        <button class="btn" onclick="analyseFor(['styles'])">ANALYSE COLLECTION NOW</button><span class="help">${st("styles")}</span></div>`,
      "About 3-4 seconds per song. Downloads onnxruntime and the Discogs-EffNet model by MTG (about 35 MB, CC BY-NC-ND 4.0: free for non-commercial use). Suggestions get a style name.")}
    ${field("Smallest group", text("rec_min_group", "", "number"), "Groups with fewer songs are not suggested.")}` : ""}`;
}

// ------------------------------------------------------------------ boot
$("#btn-add").addEventListener("click", () => addPlaylist(S.view.type === "genre" ? S.view.key : ""));
$("#btn-update-all").addEventListener("click", () => runJob(api("POST", "/api/update-all")));
$("#btn-write").addEventListener("click", () => runJob(api("POST", "/api/traktor/write")));
$("#btn-app-update").addEventListener("click", applyUpdate);
$("#btn-stop").addEventListener("click", stopJob);
$("#btn-analysis").addEventListener("click", analysisButton);
$("#btn-copy-log").addEventListener("click", async () => {
  const pre = $("#log");
  const text = pre.innerText;
  try {
    await navigator.clipboard.writeText(text);
  } catch {  // older webviews: copy via a selection
    const range = document.createRange();
    range.selectNodeContents(pre);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    document.execCommand("copy");
    sel.removeAllRanges();
  }
  toast(`Log copied (${text.split("\n").length - 1} lines)`);
});
$("#btn-console").addEventListener("click", () => {
  const c = $("#console");
  c.classList.toggle("collapsed");
  $("#btn-console").textContent = c.classList.contains("collapsed") ? "▴" : "▾";
  store.set("console", c.classList.contains("collapsed"));
});
if (store.get("console", false)) { $("#console").classList.add("collapsed"); $("#btn-console").textContent = "▴"; }
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") $("#modal-root").innerHTML = ""; });

Player.init();
refresh();
setInterval(refreshState, 4000);
