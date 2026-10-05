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
  $("#console-status").textContent = `${job.title}: ${job.status}`;
  updateJobIndicator(job.status === "running" || job.status === "queued" ? job : null);
  if (job.status === "done" || job.status === "failed") {
    if (job.status === "done") { log([`✔ ${job.result || job.title}`], "ok"); toast(job.result || `${job.title} done`); }
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
  const up = $("#btn-app-update");
  up.hidden = !(a.update && a.update.available && a.update.asset_url);
  if (!up.hidden) up.textContent = `UPDATE TO ${a.update.latest}`;
  const st = a.stats || {};
  $("#cnt-collection").textContent = st.tracks ?? "";
  $("#cnt-removed").textContent = st.removed || "";
  $("#cnt-duplicates").textContent = st.duplicates || "";
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
    return `<a class="node ${active ? "active" : ""} ${n.has_playlist ? "" : "implicit"}" data-key="${esc(n.key)}"
              title="${esc(n.key)}${n.spotify_url ? "\n" + esc(n.spotify_url) : "\nno Spotify link"}">
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
async function render() {
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
      case "collection": return await renderList(deck, view, "/api/collection", "Track Collection", "All tracks managed by DJ Manager, each stored once on disk.");
      case "removed": return await renderList(deck, view, "/api/removed", "Removed", "Tracks that are in no genre anymore. They stay on disk (moved to _removed/ when their playlist was removed) - delete the files yourself and they disappear from this list.");
      case "duplicates": return await renderList(deck, view, "/api/duplicates", "Duplicates", "Songs found more than once on disk. DJ Manager uses the first file; the extra copies listed here can be deleted manually.");
      case "settings": return renderSettings(view);
      case "deps": return await renderDeps(view);
      case "backups": return await renderBackups(view);
      default: setView({ type: "collection" });
    }
  } catch (e) {
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
  S.rows = await api("GET", `/api/genre/${encodeURIComponent(key)}/tracks`);
  const tools = node.has_playlist ? `
      <button class="btn" ${node.spotify_url ? "" : "disabled"} onclick="syncPlaylist(${js(key)})">⟳ UPDATE</button>
      <button class="btn" onclick="editLink(${js(key)})">🔗 LINK</button>
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
  renderTable(view, { showPlaylist: node.children.length > 0, selectable: node.has_playlist, playlistKey: key });
}

async function renderList(deck, view, url, title, help) {
  S.rows = await api("GET", url);
  deck.innerHTML = deckHtml({ letter: title[0], orange: title !== "Track Collection", title: esc(title), sub: esc(help), meters: [[S.rows.length, "TRACKS"]] });
  renderTable(view, { showPlaylist: true, showDuplicates: S.view.type === "duplicates", showPath: S.view.type !== "collection" });
}

const COLUMNS = {
  title: (r) => r.title, artists: (r) => r.artists, album: (r) => r.album, duration: (r) => r.duration,
  status: (r) => r.status, playlists: (r) => r.playlists.join(" "), path: (r) => r.path,
};

function renderTable(view, opts) {
  const f = S.filter.toLowerCase();
  let rows = S.rows.filter((r) => !f || `${r.title} ${r.artists} ${r.album} ${r.path} ${r.playlists.join(" ")}`.toLowerCase().includes(f));
  if (S.sort.col && COLUMNS[S.sort.col]) {
    const get = COLUMNS[S.sort.col];
    rows = [...rows].sort((a, b) => { const x = get(a), y = get(b); return (x > y ? 1 : x < y ? -1 : 0) * S.sort.dir; });
  }
  S.visible = rows;
  const th = (col, label, cls = "") => `<th class="${cls}" data-sort="${col}">${label}${S.sort.col === col ? (S.sort.dir > 0 ? " ▲" : " ▼") : ""}</th>`;
  const badge = (r) => r.status === "ok" ? (r.source === "spotify" ? `<span class="badge spotify">SPOTIFY</span>` : "")
    : `<span class="badge ${r.status}">${r.status.toUpperCase()}</span>`;
  view.innerHTML = `
    <div class="filterbar">
      <input type="text" id="filter" placeholder="Search title, artist, album, path…" value="${esc(S.filter)}">
      <span class="hint">${rows.length} tracks${opts.selectable ? " · click / ctrl / shift to select" : ""}</span>
    </div>
    ${rows.length ? `<table class="tracks"><thead><tr>
      <th class="num">#</th>${th("title", "TITLE")}${th("artists", "ARTIST")}${th("album", "ALBUM")}
      ${th("duration", "TIME", "time")}${th("status", "STATUS", "st")}
      ${opts.showPlaylist ? th("playlists", "PLAYLISTS") : ""}${opts.showPath ? th("path", "FILE") : ""}
      ${opts.showDuplicates ? "<th>DUPLICATE FILES</th>" : ""}
    </tr></thead><tbody>
    ${rows.map((r, i) => `<tr data-id="${esc(r.id)}" class="st-${r.status} ${S.selected.has(r.id) ? "sel" : ""}" title="${esc(r.path)}">
      <td class="num">${i + 1}</td><td>${esc(r.title)}</td><td>${esc(r.artists)}</td><td>${esc(r.album)}</td>
      <td class="time">${fmtTime(r.duration)}</td><td class="st">${badge(r)}</td>
      ${opts.showPlaylist ? `<td>${r.playlists.map((p) => `<span class="pl-chip">${esc(p)}</span>`).join("")}</td>` : ""}
      ${opts.showPath ? `<td class="mono">${esc(r.path)}</td>` : ""}
      ${opts.showDuplicates ? `<td class="mono">${r.duplicates.map(esc).join("<br>")}</td>` : ""}
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
  if (opts.selectable) {
    $$("tbody tr", view).forEach((tr) => tr.addEventListener("click", (ev) => {
      const id = tr.dataset.id;
      const row = S.rows.find((r) => r.id === id);
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
    <div class="preview" id="pl-preview"></div>
    <p class="help">Use <b>_</b> for genre layers and <b>-</b> for spaces. Example: <span class="mono">house_deep-house</span>
      creates <span class="mono">house/deep-house/</span> and the Traktor playlists <span class="mono">house</span> and <span class="mono">house_deep-house</span>.</p>`,
  [{ label: "Cancel" }, {
    label: "Add & download", cls: "accent", action: async (r) => {
      const res = await runJob(api("POST", "/api/playlists", { name: $("#pl-name", r).value, url: $("#pl-url", r).value }));
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
  upd();
}

function syncPlaylist(key) {
  runJob(api("POST", `/api/playlists/${encodeURIComponent(key)}/sync`));
}

function editLink(key) {
  const node = findNode(key);
  modal("SPOTIFY LINK", `
    <div class="field"><label>Playlist</label><span class="mono">${esc(key)}</span></div>
    <div class="field"><label>Spotify link</label><input type="text" id="link-url" value="${esc(node.spotify_url)}" placeholder="https://open.spotify.com/playlist/…"></div>
    <p class="help">Changing the link never removes tracks: songs that are not part of the new Spotify playlist stay in this genre and are marked <span class="badge local">LOCAL</span>.</p>`,
  [{ label: "Cancel" }, { label: "Save & update", cls: "accent", action: async (r) => (await runJob(api("PUT", `/api/playlists/${encodeURIComponent(key)}/link`, { url: $("#link-url", r).value }))) ? undefined : true }]);
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

async function browse(title, startPath, wantFile, onPick) {
  let current = startPath || "";
  const root = modal(title, `<div class="mono" id="fs-path"></div><div class="fs-list" id="fs-list"></div>`,
    [{ label: "Cancel" }, ...(wantFile ? [] : [{ label: "Use this folder", cls: "accent", action: () => onPick(current) }])]);
  const load = async (p) => {
    const res = await act(() => api("GET", `/api/browse?files=${wantFile}&path=${encodeURIComponent(p)}`));
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
    ${field("Credentials", select("spotify_auth_mode", [["default", "spotdl built-in credentials"], ["custom", "Own Spotify app (client id / secret)"]]),
      "Own credentials avoid rate limits: create an app at developer.spotify.com and add the redirect URI <span class='mono'>http://127.0.0.1:9900/</span>.")}
    ${field("Client ID", text("spotify_client_id"))}
    ${field("Client secret", text("spotify_client_secret", "", "password"))}
    ${field("User login", `${check("spotify_user_auth", "Use my Spotify login (private playlists)")}
      <div class="row" style="margin-top:6px"><button class="btn" onclick="runJob(api('POST','/api/spotify/login'))">LOGIN WITH SPOTIFY</button>
      <span class="help">${s.spotify_user_name ? "logged in as " + esc(s.spotify_user_name) : "not logged in"}</span></div>`)}

    <h2>DOWNLOAD</h2>
    ${field("Format", select("audio_format", [["mp3", "MP3"], ["m4a", "M4A / AAC"]]))}
    ${field("Bitrate", text("bitrate", "320k"), "e.g. 320k, 256k. For M4A use <span class='mono'>disable</span> to keep the source quality without re-encoding.")}
    ${field("Parallel downloads", text("download_threads", "", "number"))}
    ${field("YouTube cookie file", text("cookie_file", "optional, cookies.txt for YouTube Music Premium quality"))}

    <h2>UPDATES</h2>
    ${field("Version", `<span class="mono">${esc(S.app.version)}</span> · <span class="help">${esc({source: "running from source", installed: "installed (Windows installer)", portable: "portable exe", binary: "Linux binary"}[S.app.install_mode] || S.app.install_mode)}</span>`)}
    ${field("Release source", text("update_repo", "owner/repo on GitHub (empty = repository the build came from)"))}
    ${field("On start", check("check_app_updates", "Check for new DJ Manager versions when it starts"))}
    ${field("", `<div class="row"><button class="btn" id="check-update">CHECK FOR UPDATES</button><span class="help" id="update-result"></span></div>`)}

    <h2>GENERAL</h2>
    ${field("On start", check("update_on_start", "Update all playlists when DJ Manager starts"))}
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

function pickNml() {
  const start = S.app?.nml_path ? S.app.nml_path.replace(/[\\/][^\\/]*$/, "") : "";
  browse("SELECT collection.nml", start, true, async (path) => {
    if (await act(() => api("POST", "/api/settings", { traktor_nml: path }))) { toast("Collection set"); refresh(); }
  });
}

// ------------------------------------------------------------------ dependencies
async function renderDeps(view) {
  const d = await api("GET", "/api/deps");
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
  const list = await api("GET", "/api/backups");
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

// ------------------------------------------------------------------ boot
$("#btn-add").addEventListener("click", () => addPlaylist(S.view.type === "genre" ? S.view.key : ""));
$("#btn-update-all").addEventListener("click", () => runJob(api("POST", "/api/update-all")));
$("#btn-write").addEventListener("click", () => runJob(api("POST", "/api/traktor/write")));
$("#btn-app-update").addEventListener("click", applyUpdate);
$("#btn-console").addEventListener("click", () => {
  const c = $("#console");
  c.classList.toggle("collapsed");
  $("#btn-console").textContent = c.classList.contains("collapsed") ? "▴" : "▾";
  store.set("console", c.classList.contains("collapsed"));
});
if (store.get("console", false)) { $("#console").classList.add("collapsed"); $("#btn-console").textContent = "▴"; }
document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") $("#modal-root").innerHTML = ""; });

refresh();
setInterval(refreshState, 4000);
