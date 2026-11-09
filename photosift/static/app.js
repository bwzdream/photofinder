/* PhotoSift front end — plain JS, no build step. */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const quoteVal = (v) => (/\s/.test(v) ? `"${v}"` : v);
const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

const state = {
  q: "", sort: "auto", items: [], total: 0, loading: false, done: false,
  selected: new Set(), lastClicked: null, albums: [], tags: [], sources: [],
  albumCtx: null, lbIndex: -1, jobsRunning: false, gen: 0,
};

/* ------------------------------------------------------------------ utils */
async function api(path, opts = {}) {
  const init = { ...opts };
  if (opts.body && typeof opts.body !== "string") {
    init.body = JSON.stringify(opts.body);
    init.headers = { "Content-Type": "application/json" };
  }
  const r = await fetch(path, init);
  if (!r.ok) {
    let msg = r.statusText;
    try { const j = await r.json(); msg = j.detail || msg; } catch {}
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  const ct = r.headers.get("content-type") || "";
  return ct.includes("json") ? r.json() : r;
}

let toastTimer;
function toast(msg, ms = 3000) {
  const t = $("#toast");
  t.textContent = msg; t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), ms);
}
const fail = (e) => toast("⚠ " + (e.message || e), 5000);

function fmtBytes(n) {
  if (!n) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n < 10 && i ? 1 : 0)} ${u[i]}`;
}
function fmtDate(iso, withTime = true) {
  if (!iso) return "Unknown date";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }) +
    (withTime ? " " + d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }) : "");
}
function monthLabel(ym) {
  if (!ym) return "Unknown date";
  const [y, m] = ym.split("-");
  return `${MONTHS[+m - 1] || ""} ${y}`.trim();
}

/* ------------------------------------------------------------------ dialog */
function openDialog(html, { onOpen, submit } = {}) {
  const dlg = $("#dlg"), form = $("#dlgForm");
  form.innerHTML = html;
  return new Promise((resolve) => {
    const onSubmit = async (ev) => {
      const btn = ev.submitter;
      if (btn && btn.value === "cancel") return;
      ev.preventDefault();
      const data = Object.fromEntries(new FormData(form).entries());
      data._button = btn ? btn.value : "";
      try {
        const res = submit ? await submit(data) : data;
        if (res === false) return;
        dlg.close(); resolve(res ?? data);
      } catch (e) { fail(e); }
    };
    form.onsubmit = onSubmit;
    dlg.onclose = () => resolve(null);
    dlg.showModal();
    onOpen && onOpen(form);
    const first = form.querySelector("input:not([type=hidden]), select, textarea");
    first && first.focus();
  });
}
const dlgButtons = (ok = "OK") =>
  `<div class="buttons"><button value="cancel" formnovalidate>Cancel</button><button class="primary" value="ok">${esc(ok)}</button></div>`;

/* ------------------------------------------------------------------ sidebar */
async function loadSidebar() {
  const [albums, tags, sources] = await Promise.all([api("/api/albums"), api("/api/tags"), api("/api/sources")]);
  state.albums = albums; state.tags = tags; state.sources = sources;

  $("#albumList").innerHTML = albums.length
    ? albums.map((a) => `<a href="#/album/${a.id}" data-album="${a.id}"><span class="name">${a.query ? "✦ " : ""}${esc(a.name)}</span><span class="n">${a.photo_count}</span></a>`).join("")
    : `<div class="empty">No albums yet</div>`;

  $("#tagCloud").innerHTML = tags.slice(0, 40)
    .map((t) => `<a class="chip${t.manual ? " manual" : ""}" href="#/search?q=${encodeURIComponent("tag:" + quoteVal(t.tag))}">${esc(t.tag)} <span class="n">${t.n}</span></a>`).join("")
    || `<div class="empty">Tags appear after indexing</div>`;

  $("#sourceList").innerHTML = sources.length
    ? sources.map((s) => `<a href="#/search?q=${encodeURIComponent("source:" + s.kind)}" title="${esc(s.location)}" data-src="${s.id}">
        <span class="name">${s.kind === "gdrive" ? "☁ " : "🗀 "}${esc(s.label)}</span><span class="n">${s.photo_count}</span></a>`).join("")
    : `<div class="empty">Add a folder or Google Drive to begin</div>`;
  markActiveNav();
}

$("#sourceList").addEventListener("contextmenu", async (e) => {
  const a = e.target.closest("[data-src]"); if (!a) return;
  e.preventDefault();
  sourceMenu(+a.dataset.src);
});
async function sourceMenu(id) {
  const s = state.sources.find((x) => x.id === id); if (!s) return;
  const res = await openDialog(`<h2>${esc(s.label)}</h2>
    <div class="hint">${esc(s.location)}<br>${s.photo_count} photos · last scan ${s.last_scan ? fmtDate(new Date(s.last_scan * 1000).toISOString()) : "never"}</div>
    <div class="buttons"><button class="danger" value="remove">Remove from library</button><button value="full">Re-index everything</button><button class="primary" value="scan">Scan for changes</button></div>`);
  if (!res) return;
  if (res._button === "scan") await api(`/api/sources/${id}/scan`, { method: "POST" }).catch(fail);
  if (res._button === "full") await api(`/api/sources/${id}/scan?full=true`, { method: "POST" }).catch(fail);
  if (res._button === "remove") {
    const ok = await openDialog(`<h2>Remove “${esc(s.label)}”?</h2><p>Its photos leave the PhotoSift library. The files themselves are not touched.</p>${dlgButtons("Remove")}`);
    if (ok) { await api(`/api/sources/${id}`, { method: "DELETE" }).catch(fail); loadSidebar(); route(); }
  }
  pollJobs(true);
}

/* ------------------------------------------------------------------ jobs */
let jobTimer;
async function pollJobs(soon = false) {
  clearTimeout(jobTimer);
  if (soon) { jobTimer = setTimeout(pollJobs, 400); return; }
  let jobs = [];
  try { jobs = await api("/api/jobs"); } catch {}
  const running = jobs.filter((j) => j.state === "running" || j.state === "queued");
  $("#jobBox").innerHTML = running.map((j) => {
    const pct = j.total ? Math.round((100 * j.done) / j.total) : 0;
    return `<div class="job"><b>${esc(j.title)}</b> <button class="icon-btn" data-cancel="${j.id}" title="Cancel">&times;</button>
      <div class="bar"><span style="width:${pct}%"></span></div>
      <div>${j.state === "queued" ? "Waiting…" : j.total ? `${j.done} / ${j.total}` : "Listing files…"}${j.skipped ? ` · ${j.skipped} unchanged` : ""}${j.error_count ? ` · ${j.error_count} errors` : ""}</div>
      <div class="cur" title="${esc(j.current)}">${esc(j.current)}</div></div>`;
  }).join("");
  const wasRunning = state.jobsRunning;
  state.jobsRunning = running.length > 0;
  if (wasRunning && !state.jobsRunning) {
    const last = jobs[0];
    if (last) toast(`${last.title}: ${last.state}${last.added ? ` · ${last.added} added` : ""}${last.error_count ? ` · ${last.error_count} errors` : ""}`, 5000);
    loadSidebar();
    if (!state.selected.size && $("#lightbox").hidden) route();
  } else if (state.jobsRunning && Math.random() < 0.25) {
    loadSidebar();
  }
  jobTimer = setTimeout(pollJobs, state.jobsRunning ? 1500 : 8000);
}
$("#jobBox").addEventListener("click", (e) => {
  const b = e.target.closest("[data-cancel]");
  if (b) api(`/api/jobs/${b.dataset.cancel}/cancel`, { method: "POST" }).then(() => pollJobs(true));
});

/* ------------------------------------------------------------------ routing */
function parseHash() {
  const h = location.hash.slice(1) || "/";
  const [path, qs] = h.split("?");
  return { path, params: new URLSearchParams(qs || "") };
}
function go(hash) { if (location.hash === hash) route(); else location.hash = hash; }
function searchHash(q) { return q ? `#/search?q=${encodeURIComponent(q)}` : "#/"; }

function markActiveNav() {
  const { path, params } = parseHash();
  $$("nav a, .list a").forEach((a) => a.classList.remove("active"));
  const q = params.get("q") || "";
  let sel = null;
  if (path === "/" && !q) sel = '[data-nav="all"]';
  else if (q === "is:favorite") sel = '[data-nav="fav"]';
  else if (path === "/timeline") sel = '[data-nav="timeline"]';
  else if (path === "/places") sel = '[data-nav="places"]';
  else if (path === "/duplicates") sel = '[data-nav="dups"]';
  else if (path === "/folders") sel = '[data-nav="folders"]';
  else if (path.startsWith("/album/")) sel = `[data-album="${path.split("/")[2]}"]`;
  if (sel) $(sel)?.classList.add("active");
}

async function route() {
  document.body.classList.remove("menu-open");
  const { path, params } = parseHash();
  markActiveNav();
  state.albumCtx = null;
  $("#unalbumBtn").hidden = true;
  if (path.startsWith("/album/")) {
    if (!state.albums.length) await loadSidebar().catch(() => {});
    const a = state.albums.find((x) => x.id === +path.split("/")[2]);
    if (!a) { $("#viewHead").innerHTML = ""; $("#view").innerHTML = emptyState("Album not found"); return; }
    state.albumCtx = a;
    $("#unalbumBtn").hidden = !!a.query;
    const q = a.query || `album:${quoteVal(a.name)}`;
    return runSearch(q, albumHead(a));
  }
  if (path === "/timeline") return showTimeline();
  if (path === "/places") return showPlaces();
  if (path === "/duplicates") return showDuplicates();
  if (path === "/folders") return showFolders();
  return runSearch(params.get("q") || "");
}
window.addEventListener("hashchange", route);

function emptyState(title, body = "") {
  return `<div class="empty-state"><h2>${esc(title)}</h2>${body}</div>`;
}

/* ------------------------------------------------------------------ search grid */
function albumHead(a) {
  return `<h1>${a.query ? "✦ " : ""}${esc(a.name)}
    <span class="actions">
      <button data-albumact="rename">Rename</button>
      ${a.query ? `<button data-albumact="query">Edit rule</button>` : ""}
      <button data-albumact="export">Copy to folder…</button>
      <button class="danger" data-albumact="delete">Delete album</button>
    </span></h1>
    <div class="sub">${a.query ? `Smart album · rule: <code>${esc(a.query)}</code>` : "Album"}</div>`;
}

function searchHead(res) {
  const q = res.query;
  let title = "All photos";
  if (q === "is:favorite") title = "Favorites";
  else if (q) title = `Results for “${q}”`;
  const saveBtn = q && q !== "is:favorite" ? `<span class="actions"><button data-saveq>Save as smart album</button></span>` : "";
  return `<h1>${esc(title)} ${saveBtn}</h1>
    <div class="sub">${res.total.toLocaleString()} photo${res.total === 1 ? "" : "s"}${res.text ? ` · ranked by AI similarity to “${esc(res.text)}”` : ""} · ${res.took_ms} ms</div>`;
}

async function runSearch(q, headHtml = null) {
  const gen = ++state.gen;
  state.q = q; state.items = []; state.total = 0; state.done = false; state.lastGroup = null;
  $("#q").value = state.albumCtx ? "" : q;
  $("#view").innerHTML = "";
  $("#view").scrollTop = 0;
  await loadMore(gen, headHtml);
}

async function loadMore(gen = state.gen, headHtml = null) {
  if (state.loading || state.done) return;
  state.loading = true;
  try {
    const params = new URLSearchParams({ q: state.q, sort: state.sort, offset: state.items.length, limit: 200 });
    const res = await api(`/api/search?${params}`);
    if (gen !== state.gen) return;
    if (!state.items.length) {
      $("#viewHead").innerHTML = headHtml ?? searchHead(res);
      if (!res.total) {
        $("#view").innerHTML = state.sources.length || state.q
          ? emptyState("No photos found", state.q ? "<p>Try fewer filters or different words.</p>" : "<p>Indexing may still be running.</p>")
          : emptyState("Welcome to PhotoSift", `<p>Add a folder from your computer or connect Google Drive.<br>PhotoSift reads each photo with an AI model running on this computer so you can search by what is in it.</p><p><button class="primary" onclick="addSourceDialog()">Add photos</button></p>`);
      }
    }
    state.total = res.total;
    const start = state.items.length;
    state.items.push(...res.items);
    state.done = state.items.length >= res.total;
    appendTiles(res.items, start, res.sort);
  } catch (e) { fail(e); }
  finally { state.loading = false; }
}

function appendTiles(items, start, sort) {
  const view = $("#view");
  $(".sentinel", view)?.remove();
  const grouped = sort === "newest" || sort === "oldest";
  let grid = view.querySelector(".grid:last-of-type");
  const h = +$("#zoom").value;
  items.forEach((p, i) => {
    const group = grouped ? (p.taken_at || "").slice(0, 7) : "";
    if (!grid || (grouped && group !== state.lastGroup)) {
      if (grouped) {
        const t = document.createElement("div");
        t.className = "group-title"; t.textContent = monthLabel(group);
        view.appendChild(t);
      }
      grid = document.createElement("div"); grid.className = "grid";
      view.appendChild(grid);
      state.lastGroup = group;
    }
    const ratio = p.width && p.height ? Math.min(Math.max(p.width / p.height, 0.5), 2.5) : 1.33;
    const el = document.createElement("div");
    el.className = "tile" + (state.selected.has(p.id) ? " sel" : "");
    el.dataset.idx = start + i;
    el.dataset.id = p.id;
    el.style.flex = `${ratio * h}px ${ratio} 1`;
    el.style.setProperty("--h", h + "px");
    el.style.maxWidth = `${ratio * h * 1.6}px`;
    el.innerHTML = `<img loading="lazy" src="/api/photos/${p.id}/thumb" alt="${esc(p.filename)}">
      <span class="check" title="Select"></span>${p.favorite ? '<span class="fav">★</span>' : ""}
      ${p.kind === "gdrive" ? '<span class="badge">Drive</span>' : ""}`;
    grid.appendChild(el);
  });
  if (!state.done) {
    const s = document.createElement("div"); s.className = "sentinel";
    view.appendChild(s);
    sentinelObserver.observe(s);
  }
}
const sentinelObserver = new IntersectionObserver((entries) => {
  if (entries.some((e) => e.isIntersecting)) loadMore();
}, { root: $("#view"), rootMargin: "800px" });

$("#view").addEventListener("click", (e) => {
  const tile = e.target.closest(".tile");
  if (!tile) return;
  const idx = +tile.dataset.idx, id = +tile.dataset.id;
  if (e.target.closest(".check") || e.ctrlKey || e.metaKey || e.shiftKey || state.selected.size) {
    if (e.shiftKey && state.lastClicked !== null) {
      const [a, b] = [Math.min(state.lastClicked, idx), Math.max(state.lastClicked, idx)];
      for (let i = a; i <= b; i++) state.selected.add(state.items[i].id);
    } else if (state.selected.has(id)) state.selected.delete(id);
    else state.selected.add(id);
    state.lastClicked = idx;
    syncSelection();
    return;
  }
  openLightbox(idx);
});

function syncSelection() {
  $$(".tile").forEach((t) => t.classList.toggle("sel", state.selected.has(+t.dataset.id)));
  const n = state.selected.size;
  $("#selbar").hidden = n === 0;
  document.body.classList.toggle("selecting", n > 0);
  $("#selCount").textContent = `${n} selected`;
}

$("#zoom").addEventListener("input", () => {
  const h = +$("#zoom").value;
  $$(".tile").forEach((t) => {
    const p = state.items[+t.dataset.idx];
    const ratio = p.width && p.height ? Math.min(Math.max(p.width / p.height, 0.5), 2.5) : 1.33;
    t.style.flex = `${ratio * h}px ${ratio} 1`; t.style.maxWidth = `${ratio * h * 1.6}px`;
    t.style.setProperty("--h", h + "px");
  });
  try { localStorage.setItem("zoom", h); } catch {}
});
$("#sort").addEventListener("change", () => { state.sort = $("#sort").value; route(); });
$("#searchForm").addEventListener("submit", (e) => { e.preventDefault(); go(searchHash($("#q").value.trim())); });
$("#menuBtn").addEventListener("click", () => document.body.classList.toggle("menu-open"));

$("#viewHead").addEventListener("click", async (e) => {
  if (e.target.closest("[data-saveq]")) {
    const res = await openDialog(`<h2>Save as smart album</h2>
      <label>Name</label><input name="name" required value="${esc(state.q.replace(/\w+:/g, "").replace(/"/g, "").trim() || state.q)}">
      <label>Rule</label><input name="query" value="${esc(state.q)}">
      <div class="hint">Smart albums update automatically as new photos are indexed.</div>${dlgButtons("Save")}`,
      { submit: (d) => api("/api/albums", { method: "POST", body: { name: d.name, query: d.query } }) });
    if (res) { await loadSidebar(); go(`#/album/${res.id}`); }
    return;
  }
  const act = e.target.closest("[data-albumact]")?.dataset.albumact;
  const a = state.albumCtx;
  if (!act || !a) return;
  if (act === "rename" || act === "query") {
    const res = await openDialog(`<h2>${act === "rename" ? "Rename album" : "Edit smart album rule"}</h2>
      <label>Name</label><input name="name" required value="${esc(a.name)}">
      ${a.query ? `<label>Rule</label><input name="query" value="${esc(a.query)}">` : ""}${dlgButtons("Save")}`,
      { submit: (d) => api(`/api/albums/${a.id}`, { method: "PATCH", body: { name: d.name, query: a.query ? d.query : null } }) });
    if (res) { await loadSidebar(); route(); }
  } else if (act === "delete") {
    const ok = await openDialog(`<h2>Delete album “${esc(a.name)}”?</h2><p>Photos stay in your library; only the album is removed.</p>${dlgButtons("Delete")}`);
    if (ok) { await api(`/api/albums/${a.id}`, { method: "DELETE" }); await loadSidebar(); go("#/"); }
  } else if (act === "export") {
    exportDialog({ query: a.query || `album:${quoteVal(a.name)}`, count: state.total, defaultPattern: "" });
  }
});

/* ------------------------------------------------------------------ bulk actions */
$("#selbar").addEventListener("click", async (e) => {
  const act = e.target.closest("[data-bulk]")?.dataset.bulk;
  if (!act) return;
  const ids = [...state.selected];
  const bulk = (action, value) => api("/api/photos/bulk", { method: "POST", body: { ids, action, value } });
  try {
    if (act === "clear") { state.selected.clear(); syncSelection(); return; }
    if (act === "selectall") {
      while (!state.done) await loadMore();
      state.items.forEach((p) => state.selected.add(p.id)); syncSelection(); return;
    }
    if (act === "favorite" || act === "unfavorite") {
      await bulk(act); toast(`${ids.length} photos ${act === "favorite" ? "added to" : "removed from"} favorites`);
      state.items.forEach((p) => { if (state.selected.has(p.id)) p.favorite = act === "favorite" ? 1 : 0; });
      $$(".tile").forEach((t) => {
        if (!state.selected.has(+t.dataset.id)) return;
        $(".fav", t)?.remove();
        if (act === "favorite") t.insertAdjacentHTML("beforeend", '<span class="fav">★</span>');
      });
    }
    if (act === "tag") {
      const d = await openDialog(`<h2>Tag ${ids.length} photos</h2><label>Tag</label>
        <input name="tag" list="tagOptions" required placeholder="e.g. family, paris trip, receipts">
        <datalist id="tagOptions">${state.tags.map((t) => `<option value="${esc(t.tag)}">`).join("")}</datalist>${dlgButtons("Add tag")}`);
      if (!d) return;
      await bulk("add_tag", d.tag); toast(`Tagged ${ids.length} photos “${d.tag}”`); loadSidebar();
    }
    if (act === "album") {
      const d = await openDialog(`<h2>Add ${ids.length} photos to album</h2>
        <label>Album</label><select name="album">${state.albums.filter((a) => !a.query).map((a) => `<option value="${a.id}">${esc(a.name)}</option>`).join("")}<option value="new">+ New album…</option></select>
        <label>New album name</label><input name="name" placeholder="Only if creating a new album">${dlgButtons("Add")}`);
      if (!d) return;
      let id = d.album;
      if (id === "new" || !id) {
        if (!d.name.trim()) return toast("Enter a name for the new album");
        id = (await api("/api/albums", { method: "POST", body: { name: d.name.trim() } })).id;
      }
      await bulk("add_to_album", +id); toast(`Added ${ids.length} photos to album`); loadSidebar();
    }
    if (act === "unalbum" && state.albumCtx) {
      await bulk("remove_from_album", state.albumCtx.id);
      state.selected.clear(); syncSelection(); await loadSidebar(); route();
    }
    if (act === "download") {
      toast("Preparing zip…");
      const r = await api("/api/download", { method: "POST", body: { ids } });
      const blob = await r.blob();
      const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: "photos.zip" });
      a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 10000);
    }
    if (act === "export") exportDialog({ ids, count: ids.length });
  } catch (err) { fail(err); }
});

async function exportDialog({ ids = [], query = null, count = 0, defaultPattern = "{year}/{month}" }) {
  const patterns = [["{year}/{month}", "Year / Month (2024/07)"], ["{year}", "Year"], ["{year}/{month}/{day}", "Year / Month / Day"],
    ["{camera}", "Camera"], ["{tag}", "First tag"], ["{album}", "Album"], ["", "All in one folder"]];
  const d = await openDialog(`<h2>Copy ${count} photos to a folder</h2>
    <label>Destination folder</label><input name="dest" required placeholder="/Users/me/Pictures/Organized">
    <label>Organize into subfolders by</label>
    <select name="pattern">${patterns.map(([v, l]) => `<option value="${esc(v)}"${v === defaultPattern ? " selected" : ""}>${esc(l)}</option>`).join("")}</select>
    <div class="hint">Files are copied. Your originals are never moved or deleted. Google Drive photos are downloaded at full quality.</div>${dlgButtons("Copy")}`,
    { submit: (data) => api("/api/export", { method: "POST", body: { ids, query, dest: data.dest, pattern: data.pattern } }) });
  if (d) { toast("Copy started"); pollJobs(true); }
}

/* ------------------------------------------------------------------ lightbox */
async function openLightbox(idx) {
  if (idx < 0 || idx >= state.items.length) return;
  state.lbIndex = idx;
  const item = state.items[idx];
  const lb = $("#lightbox");
  lb.hidden = false;
  const img = $("#lbImg");
  img.src = `/api/photos/${item.id}/thumb`;
  const full = new Image();
  full.onload = () => { if (state.lbIndex === idx) img.src = full.src; };
  full.src = `/api/photos/${item.id}/full`;
  $("#lbPrev").style.visibility = idx > 0 ? "visible" : "hidden";
  $("#lbNext").style.visibility = idx < state.total - 1 ? "visible" : "hidden";
  if (idx >= state.items.length - 5) loadMore();
  renderInfo(await api(`/api/photos/${item.id}`));
}
function closeLightbox() { $("#lightbox").hidden = true; state.lbIndex = -1; }

function renderInfo(p) {
  const item = state.items[state.lbIndex];
  if (!item || item.id !== p.id) return;
  const stars = [1, 2, 3, 4, 5].map((n) => `<span data-star="${n}" class="${n <= p.rating ? "on" : ""}">★</span>`).join("");
  const tags = p.tags.map((t) => `<span class="chip ${t.kind}" title="${t.kind === "auto" ? `AI tag · ${Math.round(t.score * 100)}%` : "Your tag"}">
      <a href="${searchHash("tag:" + quoteVal(t.tag))}" style="color:inherit;text-decoration:none">${esc(t.tag)}</a>
      <span class="x" data-rmtag="${esc(t.tag)}" title="Remove tag">×</span></span>`).join("");
  const albums = p.albums.map((a) => `<a class="chip" href="#/album/${a.id}">${esc(a.name)}</a>`).join("");
  const where = p.lat != null
    ? `<a href="https://www.openstreetmap.org/?mlat=${p.lat}&mlon=${p.lon}#map=15/${p.lat}/${p.lon}" target="_blank" rel="noopener">${p.lat.toFixed(4)}, ${p.lon.toFixed(4)}</a>` : "—";
  $("#lbInfo").innerHTML = `
    <h3>${esc(p.filename)}</h3>
    <div class="meta">${fmtDate(p.taken_at)}</div>
    <div class="block row">
      <button class="favbtn ${p.favorite ? "on" : ""}" data-fav>${p.favorite ? "★ Favorite" : "☆ Favorite"}</button>
      <button data-similar>Find similar</button>
      <span class="stars" title="Rating">${stars}</span>
    </div>
    <div class="block"><div class="label">Tags</div>
      <div class="chips">${tags || '<span class="meta">No tags</span>'}</div>
      <form class="row" data-addtag style="margin-top:8px"><input name="tag" list="lbTags" placeholder="Add a tag" style="flex:1">
        <datalist id="lbTags">${state.tags.map((t) => `<option value="${esc(t.tag)}">`).join("")}</datalist><button>Add</button></form>
    </div>
    <div class="block"><div class="label">Albums</div>
      <div class="chips">${albums || '<span class="meta">Not in an album</span>'}</div>
      <form class="row" data-addalbum style="margin-top:8px"><select name="album" style="flex:1">
        ${state.albums.filter((a) => !a.query && !p.albums.some((x) => x.id === a.id)).map((a) => `<option value="${a.id}">${esc(a.name)}</option>`).join("")}
        <option value="new">+ New album…</option></select><button>Add</button></form>
    </div>
    <div class="block"><div class="label">Note</div><textarea data-note placeholder="Add a note (searchable)">${esc(p.note)}</textarea></div>
    <dl>
      <dt>Size</dt><dd>${p.width}×${p.height} · ${fmtBytes(p.size)}</dd>
      <dt>Camera</dt><dd>${p.camera ? `<a href="${searchHash("camera:" + quoteVal(p.camera))}">${esc(p.camera)}</a>` : "—"}</dd>
      <dt>Location</dt><dd>${where}</dd>
      <dt>Source</dt><dd>${p.kind === "gdrive" ? `Google Drive${p.web_url ? ` · <a href="${esc(p.web_url)}" target="_blank" rel="noopener">open</a>` : ""}` : "Local"}</dd>
      <dt>Folder</dt><dd><a href="${searchHash("folder:" + quoteVal(p.folder))}">${esc(p.folder)}</a></dd>
      <dt>Path</dt><dd>${esc(p.path)} <a href="#" data-copy="${esc(p.path)}">copy</a></dd>
    </dl>`;
}

$("#lbInfo").addEventListener("click", async (e) => {
  const item = state.items[state.lbIndex]; if (!item) return;
  const id = item.id;
  const refresh = async () => renderInfo(await api(`/api/photos/${id}`));
  try {
    if (e.target.closest("[data-fav]")) {
      const p = await api(`/api/photos/${id}`, { method: "PATCH", body: { favorite: !item.favorite } });
      item.favorite = p.favorite; renderInfo(p);
      const tile = $(`.tile[data-id="${id}"]`);
      if (tile) { $(".fav", tile)?.remove(); if (p.favorite) tile.insertAdjacentHTML("beforeend", '<span class="fav">★</span>'); }
    }
    const star = e.target.closest("[data-star]");
    if (star) {
      const n = +star.dataset.star;
      renderInfo(await api(`/api/photos/${id}`, { method: "PATCH", body: { rating: item.rating === n ? 0 : n } }));
      item.rating = item.rating === n ? 0 : n;
    }
    if (e.target.closest("[data-similar]")) { closeLightbox(); go(searchHash(`similar:${id}`)); }
    const rm = e.target.closest("[data-rmtag]");
    if (rm) { await api("/api/photos/bulk", { method: "POST", body: { ids: [id], action: "remove_tag", value: rm.dataset.rmtag } }); refresh(); loadSidebar(); }
    const cp = e.target.closest("[data-copy]");
    if (cp) { e.preventDefault(); navigator.clipboard?.writeText(cp.dataset.copy); toast("Path copied"); }
    if (e.target.closest("a[href^='#']") && !cp) closeLightbox();
  } catch (err) { fail(err); }
});
$("#lbInfo").addEventListener("submit", async (e) => {
  e.preventDefault();
  const item = state.items[state.lbIndex]; if (!item) return;
  const f = e.target, id = item.id;
  try {
    if (f.matches("[data-addtag]")) {
      const tag = f.tag.value.trim(); if (!tag) return;
      await api("/api/photos/bulk", { method: "POST", body: { ids: [id], action: "add_tag", value: tag } });
    } else if (f.matches("[data-addalbum]")) {
      let aid = f.album.value;
      if (aid === "new") {
        const d = await openDialog(`<h2>New album</h2><label>Name</label><input name="name" required>${dlgButtons("Create")}`);
        if (!d) return;
        aid = (await api("/api/albums", { method: "POST", body: { name: d.name.trim() } })).id;
      }
      await api("/api/photos/bulk", { method: "POST", body: { ids: [id], action: "add_to_album", value: +aid } });
    }
    renderInfo(await api(`/api/photos/${id}`)); loadSidebar();
  } catch (err) { fail(err); }
});
$("#lbInfo").addEventListener("change", async (e) => {
  if (!e.target.matches("[data-note]")) return;
  const item = state.items[state.lbIndex]; if (!item) return;
  await api(`/api/photos/${item.id}`, { method: "PATCH", body: { note: e.target.value } }).catch(fail);
  toast("Note saved");
});
$("#lbPrev").onclick = () => openLightbox(state.lbIndex - 1);
$("#lbNext").onclick = () => openLightbox(state.lbIndex + 1);
$("#lbClose").onclick = closeLightbox;
document.addEventListener("keydown", (e) => {
  if ($("#lightbox").hidden || $("#dlg").open) {
    if (e.key === "Escape" && state.selected.size && !$("#dlg").open) { state.selected.clear(); syncSelection(); }
    if (e.key === "/" && document.activeElement.tagName !== "INPUT" && document.activeElement.tagName !== "TEXTAREA") { e.preventDefault(); $("#q").focus(); }
    return;
  }
  if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) return;
  if (e.key === "Escape") closeLightbox();
  if (e.key === "ArrowLeft") openLightbox(state.lbIndex - 1);
  if (e.key === "ArrowRight") openLightbox(state.lbIndex + 1);
  if (e.key === "f") $("[data-fav]")?.click();
  if (/^[0-5]$/.test(e.key)) $(`[data-star="${e.key}"]`)?.click();
});

/* ------------------------------------------------------------------ other views */
async function showTimeline() {
  $("#viewHead").innerHTML = `<h1>Timeline</h1><div class="sub">Photos by month. Click a month to open it.</div>`;
  const months = await api("/api/timeline");
  if (!months.length) { $("#view").innerHTML = emptyState("No dated photos yet"); return; }
  const byYear = {};
  months.forEach((m) => (byYear[m.month.slice(0, 4)] ||= []).push(m));
  $("#view").innerHTML = Object.entries(byYear).sort((a, b) => b[0].localeCompare(a[0])).map(([y, ms]) =>
    `<div class="year"><h2><a href="${searchHash("year:" + y)}">${y}</a> <span class="sub">${ms.reduce((s, m) => s + m.n, 0)} photos</span></h2>
      <div class="months">${ms.map((m) => `<a class="month-card" href="${searchHash("date:" + m.month)}"><div>${MONTHS[+m.month.slice(5, 7) - 1] || m.month}</div><div class="n">${m.n} photos</div></a>`).join("")}</div></div>`
  ).join("");
}

async function showFolders() {
  $("#viewHead").innerHTML = `<h1>Folders</h1><div class="sub">Browse by the folder each photo lives in.</div>`;
  const rows = await api("/api/folders");
  $("#view").innerHTML = rows.length
    ? `<div class="rows">${rows.map((r) => `<a href="${searchHash("folder:" + quoteVal(r.folder))}"><span>${r.kind === "gdrive" ? "☁ " : "🗀 "}${esc(r.folder)}</span><span class="sub">${r.n}</span></a>`).join("")}</div>`
    : emptyState("No folders yet");
}

let map;
async function showPlaces() {
  $("#viewHead").innerHTML = `<h1>Places</h1><div class="sub">Photos with GPS location.</div>`;
  const pts = await api("/api/places");
  if (!pts.length) { $("#view").innerHTML = emptyState("No photos with location yet", "<p>Phones usually store GPS in photos when location is enabled for the camera.</p>"); return; }
  if (!window.L) { $("#view").innerHTML = emptyState("Map unavailable", "<p>The map library could not load (no internet?).</p>"); return; }
  $("#view").innerHTML = `<div id="map"></div>`;
  if (map) map.remove();
  map = L.map("map");
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", { maxZoom: 19, attribution: "© OpenStreetMap" }).addTo(map);
  // Simple grid clustering so thousands of photos stay fast.
  const layer = L.layerGroup().addTo(map);
  const draw = () => {
    layer.clearLayers();
    const z = map.getZoom(), cell = 360 / Math.pow(2, z + 2), cells = {};
    pts.forEach((p) => { const k = `${Math.floor(p.lat / cell)}:${Math.floor(p.lon / cell)}`; (cells[k] ||= []).push(p); });
    Object.values(cells).forEach((g) => {
      const lat = g.reduce((s, p) => s + p.lat, 0) / g.length, lon = g.reduce((s, p) => s + p.lon, 0) / g.length;
      const m = L.circleMarker([lat, lon], { radius: Math.min(8 + Math.sqrt(g.length) * 2, 30), color: "#3b6ef5", fillOpacity: 0.55, weight: 2 }).addTo(layer);
      m.bindPopup(`<div class="map-pop"><img src="/api/photos/${g[0].id}/thumb" data-open="${g[0].id}"><div>${g.length} photo${g.length > 1 ? "s" : ""}</div>
        ${g.length > 1 ? `<a href="#" data-ids="${g.slice(0, 500).map((p) => p.id).join(",")}">Show all</a>` : ""}</div>`);
    });
  };
  map.fitBounds(L.latLngBounds(pts.map((p) => [p.lat, p.lon])).pad(0.1), { maxZoom: 12 });
  map.on("zoomend", draw); draw();
  $("#map").addEventListener("click", (e) => {
    const one = e.target.closest("[data-open]");
    const many = e.target.closest("[data-ids]");
    if (one) showIdList([+one.dataset.open], "Place");
    if (many) { e.preventDefault(); showIdList(many.dataset.ids.split(",").map(Number), "Photos here"); }
  });
}

async function showIdList(ids, title) {
  const items = (await Promise.all(ids.map((id) => api(`/api/photos/${id}`).catch(() => null)))).filter(Boolean);
  state.gen++; state.items = items; state.total = items.length; state.done = true; state.lastGroup = null;
  $("#viewHead").innerHTML = `<h1>${esc(title)}</h1><div class="sub">${items.length} photos · <a href="#/places">back to map</a></div>`;
  $("#view").innerHTML = "";
  appendTiles(items, 0, "relevance");
}

async function showDuplicates() {
  $("#viewHead").innerHTML = `<h1>Duplicates</h1><div class="sub">Finding identical and near-identical photos…</div>`;
  $("#view").innerHTML = "";
  const groups = await api("/api/duplicates");
  const extra = groups.reduce((s, g) => s + g.photos.length - 1, 0);
  $("#viewHead").innerHTML = `<h1>Duplicates <span class="actions">${groups.length ? `<button id="selExtras">Select all extra copies</button>` : ""}</span></h1>
    <div class="sub">${groups.length} groups · ${extra} extra copies. The best version (largest) is marked “keep”. PhotoSift never deletes files — select extras to tag, album or review them.</div>`;
  if (!groups.length) { $("#view").innerHTML = emptyState("No duplicates found 🎉"); return; }
  state.gen++; state.items = groups.flatMap((g) => g.photos); state.total = state.items.length; state.done = true;
  let idx = 0;
  $("#view").innerHTML = groups.map((g) => `<div class="dup-group"><div class="head"><span>${g.exact ? "Identical files" : "Look the same"} · ${g.photos.length} copies</span></div>
    <div class="dup-items">${g.photos.map((p, i) => `<div class="dup-item"><img src="/api/photos/${p.id}/thumb" data-idx="${idx++}" loading="lazy">
      <div>${i === 0 ? '<span class="keep">keep</span> · ' : ""}${p.width}×${p.height} · ${fmtBytes(p.size)}</div>
      <div class="path">${p.kind === "gdrive" ? "☁ " : ""}${esc(p.path)}</div></div>`).join("")}</div></div>`).join("");
  $("#selExtras").onclick = () => {
    groups.forEach((g) => g.photos.slice(1).forEach((p) => state.selected.add(p.id)));
    syncSelection(); toast(`${state.selected.size} extra copies selected`);
  };
  $$(".dup-item img").forEach((img) => (img.onclick = () => openLightbox(+img.dataset.idx)));
}

/* ------------------------------------------------------------------ sources & albums */
async function addSourceDialog(tab = "local") {
  const g = await api("/api/gdrive").catch(() => ({}));
  const driveBody = !g.installed
    ? `<p>Google Drive support isn’t installed. Run <code>pip install 'photosift[gdrive]'</code> and restart.</p>`
    : !g.client_secret
      ? `<p>One-time setup:</p><ol style="padding-left:18px">
          <li>In <a href="https://console.cloud.google.com/apis/library/drive.googleapis.com" target="_blank" rel="noopener">Google Cloud Console</a>, enable the <b>Google Drive API</b>.</li>
          <li>Create an <b>OAuth client ID</b> → application type <b>Desktop app</b>, and download the JSON.</li>
          <li>Save it as <code>${esc(g.client_secret_path)}</code>, then reopen this dialog.</li></ol>`
      : !g.connected
        ? `<p>${g.auth_running ? "Waiting for you to approve access in the browser tab that opened…" : "Connect your account. PhotoSift only asks for <b>read-only</b> access."}</p>
           ${g.auth_error ? `<p style="color:var(--danger)">${esc(g.auth_error)}</p>` : ""}
           <button type="button" class="primary" id="gconnect">Connect Google Drive</button>`
        : `<p>✓ Google Drive connected. <a href="#" id="gdisc">Disconnect</a></p>
           <label>Folder to index</label><input name="gfolder" value="root" placeholder="root, a folder ID, or a folder link">
           <div class="hint"><code>root</code> indexes every image in your Drive. Or paste a folder link to index just that folder.</div>`;
  const res = await openDialog(`<h2>Add photos</h2>
    <div class="tabs"><button type="button" data-tab="local" class="${tab === "local" ? "on" : ""}">🗀 Folder on this computer</button>
      <button type="button" data-tab="gdrive" class="${tab === "gdrive" ? "on" : ""}">☁ Google Drive</button></div>
    <div data-pane="local" ${tab === "local" ? "" : "hidden"}>
      <label>Folder path</label><input name="path" placeholder="/Users/me/Pictures  or  D:\\Photos  or  /Volumes/External/Photos">
      <div class="hint">Include external drives by their mount path. Subfolders are included. Files are only read, never changed.</div>
    </div>
    <div data-pane="gdrive" ${tab === "gdrive" ? "" : "hidden"}>${driveBody}</div>
    <input type="hidden" name="tab" value="${tab}">${dlgButtons("Add & index")}`, {
    onOpen(form) {
      $$("[data-tab]", form).forEach((b) => (b.onclick = () => {
        $$("[data-tab]", form).forEach((x) => x.classList.toggle("on", x === b));
        $$("[data-pane]", form).forEach((p) => (p.hidden = p.dataset.pane !== b.dataset.tab));
        form.tab.value = b.dataset.tab;
      }));
      $("#gconnect", form)?.addEventListener("click", async () => {
        try {
          await api("/api/gdrive/connect", { method: "POST" });
          toast("Approve access in the browser tab that opened…", 6000);
          const wait = setInterval(async () => {
            const s = await api("/api/gdrive");
            if (!s.auth_running) { clearInterval(wait); $("#dlg").close(); addSourceDialog("gdrive"); if (s.auth_error) fail(s.auth_error); }
          }, 1500);
        } catch (e) { fail(e); }
      });
      $("#gdisc", form)?.addEventListener("click", async (e) => {
        e.preventDefault(); await api("/api/gdrive/disconnect", { method: "POST" }); $("#dlg").close(); addSourceDialog("gdrive");
      });
    },
    submit: async (d) => {
      if (d.tab === "local") {
        if (!d.path?.trim()) { toast("Enter a folder path"); return false; }
        return api("/api/sources", { method: "POST", body: { kind: "local", location: d.path.trim() } });
      }
      if (!g.connected) { toast("Connect Google Drive first"); return false; }
      return api("/api/sources", { method: "POST", body: { kind: "gdrive", location: d.gfolder || "root" } });
    },
  });
  if (res && res.id) { toast("Indexing started — photos appear as they are processed"); loadSidebar(); pollJobs(true); }
}
window.addSourceDialog = addSourceDialog;
$("#addSourceBtn").onclick = () => addSourceDialog();
$("#rescanBtn").onclick = async () => { await api("/api/scan-all", { method: "POST" }).catch(fail); toast("Rescanning sources…"); pollJobs(true); };
$("#sourceList").addEventListener("click", (e) => {
  const a = e.target.closest("[data-src]");
  if (a && e.altKey) { e.preventDefault(); sourceMenu(+a.dataset.src); }
});
$("#sourceList").addEventListener("dblclick", (e) => {
  const a = e.target.closest("[data-src]"); if (a) { e.preventDefault(); sourceMenu(+a.dataset.src); }
});

$("#newAlbumBtn").onclick = async () => {
  const res = await openDialog(`<h2>New album</h2>
    <label>Name</label><input name="name" required placeholder="Summer 2024">
    <label>Smart rule (optional)</label><input name="query" placeholder='e.g. beach after:2024-06 before:2024-09'>
    <div class="hint">Leave the rule empty for a normal album you fill by hand (select photos → “Add to album”).
      With a rule, the album fills itself with every matching photo.</div>${dlgButtons("Create")}`,
    { submit: (d) => api("/api/albums", { method: "POST", body: { name: d.name, query: d.query || null, photo_ids: [...state.selected] } }) });
  if (res) { await loadSidebar(); go(`#/album/${res.id}`); }
};

$("#helpBtn").onclick = () => openDialog(`<h2>Search tips</h2><div class="help">
  <p>Type what you are looking for in plain words — the AI understands content: <code>birthday cake</code>, <code>dog in snow</code>, <code>red car at night</code>, <code>whiteboard</code>.</p>
  <table>
    <tr><td><code>tag:beach</code></td><td>has a tag (AI or yours)</td></tr>
    <tr><td><code>album:"Paris 2024"</code></td><td>in an album</td></tr>
    <tr><td><code>after:2023-06 before:2024</code></td><td>date range (YYYY, YYYY-MM, YYYY-MM-DD)</td></tr>
    <tr><td><code>year:2022</code> · <code>date:2022-12-25</code></td><td>a year, month or day</td></tr>
    <tr><td><code>camera:iphone</code></td><td>camera model contains</td></tr>
    <tr><td><code>folder:vacation</code> · <code>file:IMG_</code></td><td>folder / file name contains</td></tr>
    <tr><td><code>source:gdrive</code> · <code>source:local</code></td><td>where the photo is stored</td></tr>
    <tr><td><code>is:favorite</code> · <code>rating:4</code></td><td>favorites · at least 4 stars</td></tr>
    <tr><td><code>has:gps</code> · <code>has:note</code> · <code>is:duplicate</code></td><td>other properties</td></tr>
    <tr><td><code>similar:123</code></td><td>looks like photo #123 (or use “Find similar”)</td></tr>
    <tr><td><code>-tag:screenshot</code></td><td>a minus sign excludes</td></tr>
  </table>
  <p>Combine freely: <code>sunset tag:beach -tag:person after:2023</code>. Save any search as a <b>smart album</b>.</p>
  <p>Keys: <code>/</code> search · <code>←</code> <code>→</code> browse · <code>f</code> favorite · <code>0</code>–<code>5</code> rate · <code>Esc</code> close. Shift-click selects a range; double-click a source for options.</p>
  </div><div class="buttons"><button class="primary" value="ok">Close</button></div>`);

/* ------------------------------------------------------------------ boot */
try { const z = localStorage.getItem("zoom"); if (z) $("#zoom").value = z; } catch {}
loadSidebar().catch(fail).finally(route);
pollJobs();
