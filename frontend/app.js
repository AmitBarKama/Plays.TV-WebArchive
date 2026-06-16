"use strict";

const $ = (s) => document.querySelector(s);
const el = (tag, cls, html) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (html != null) e.innerHTML = html;
  return e;
};

const searchInput = $("#search");
const suggestionsEl = $("#suggestions");
const searchSpinner = $("#search-spinner");
const resultsEl = $("#results");
const hero = $("#hero");

let activeIdx = -1;
let suggestions = [];
let searchSeq = 0;
let searchAbort = null;

// ---------- user view state ----------
let currentUser = null;   // full /videos payload for the loaded user
let currentList = [];     // videos currently shown in the grid (filtered + sorted)
let currentIndex = -1;    // index into currentList of the open clip
let openVideo = null;     // the clip currently shown in the modal

// ---------- autocomplete ----------

const debounce = (fn, ms) => {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
};

async function doSearch(q) {
  const query = q.trim();
  const seq = ++searchSeq;
  if (query.length < 2) { hideSuggestions(); return; }
  if (searchAbort) searchAbort.abort();
  searchAbort = new AbortController();
  searchSpinner.hidden = false;
  // Render immediately: the typed name is always openable right away (fast path),
  // similar-name suggestions stream in below and never block it.
  renderDropdown(query, null);
  try {
    const r = await fetch(`/api/search?q=${encodeURIComponent(query)}`,
                          { signal: searchAbort.signal });
    const data = await r.json();
    if (seq !== searchSeq) return; // stale
    suggestions = data.results || [];
    renderDropdown(query, suggestions);
    enrichAvatars(seq);
  } catch (e) {
    if (e.name === "AbortError") return;
    if (seq === searchSeq) renderDropdown(query, []);
  } finally {
    if (seq === searchSeq) searchSpinner.hidden = true;
  }
}

// results: null = still loading, [] = done/none, [..] = matches
function renderDropdown(query, results) {
  suggestionsEl.innerHTML = "";
  activeIdx = -1;

  // Always-present primary action: open exactly what was typed.
  const open = el("li", "open-exact");
  open.innerHTML =
    `<div class="sug-avatar go">↵</div>` +
    `<div><div class="sug-name">Open “${escapeHtml(query)}”</div>` +
    `<div class="sug-sub">go straight to this user</div></div>`;
  open.addEventListener("click", () => selectUser(query));
  suggestionsEl.appendChild(open);

  if (results === null) {
    suggestionsEl.appendChild(
      el("li", "searching", `<div class="spinner"></div>Finding similar names…`));
  } else if (!results.length) {
    suggestionsEl.appendChild(el("li", "searching", "No similar names found."));
  } else {
    results.forEach((s, i) => {
      const li = el("li");
      li.dataset.idx = i;
      const av = el("div", "sug-avatar");
      av.id = `av-${i}`;
      if (s.avatar_url) av.style.backgroundImage = `url("${s.avatar_url}")`;
      const txt = el("div");
      txt.appendChild(el("div", "sug-name", escapeHtml(s.username)));
      if (s.display_name && s.display_name !== s.username)
        txt.appendChild(el("div", "sug-sub", escapeHtml(s.display_name)));
      li.appendChild(av);
      li.appendChild(txt);
      if (s.recovered) li.appendChild(el("div", "sug-meta", `${s.recovered} clips`));
      li.addEventListener("click", () => selectUser(s.username));
      suggestionsEl.appendChild(li);
    });
  }
  suggestionsEl.hidden = false;
}

// Lazily fetch avatar + display name for the top few suggestions that lack one.
async function enrichAvatars(seq) {
  await Promise.all(suggestions.slice(0, 4).map(async (s, i) => {
    if (s.avatar_url) return;
    try {
      const r = await fetch(`/api/user/${encodeURIComponent(s.username)}/header`);
      const h = await r.json();
      if (seq !== searchSeq) return;
      s.avatar_url = h.avatar_url; s.display_name = h.display_name;
      const av = $(`#av-${i}`);
      if (av && h.avatar_url) av.style.backgroundImage = `url("${h.avatar_url}")`;
    } catch (e) { /* best effort */ }
  }));
}

function hideSuggestions() { suggestionsEl.hidden = true; suggestions = []; activeIdx = -1; }

searchInput.addEventListener("input", debounce((e) => doSearch(e.target.value), 350));
searchInput.addEventListener("keydown", (e) => {
  if (suggestionsEl.hidden) {
    if (e.key === "Enter" && searchInput.value.trim()) selectUser(searchInput.value.trim());
    return;
  }
  const items = [...suggestionsEl.children];
  if (e.key === "ArrowDown") { e.preventDefault(); activeIdx = Math.min(activeIdx + 1, items.length - 1); }
  else if (e.key === "ArrowUp") { e.preventDefault(); activeIdx = Math.max(activeIdx - 1, 0); }
  else if (e.key === "Enter") {
    e.preventDefault();
    if (activeIdx >= 0) selectUser(suggestions[activeIdx].username);
    else if (searchInput.value.trim()) selectUser(searchInput.value.trim());
    return;
  } else if (e.key === "Escape") { hideSuggestions(); return; }
  items.forEach((it, i) => it.classList.toggle("active", i === activeIdx));
});
document.addEventListener("click", (e) => {
  if (!e.target.closest(".search-wrap")) hideSuggestions();
});

// ---------- load a user's videos ----------

async function selectUser(username, feedId) {
  hideSuggestions();
  searchInput.value = username;
  hero.classList.add("compact");
  resultsEl.hidden = false;
  resultsEl.scrollIntoView({ behavior: "smooth", block: "start" });
  resultsEl.innerHTML = `
    <div class="state">
      <div class="spinner-lg"></div>
      <h3>Searching the archive for “${escapeHtml(username)}”…</h3>
      <p>Unioning every saved snapshot to recover deleted clips. This can take a few seconds.</p>
    </div>`;
  setHash(username);

  try {
    const r = await fetch(`/api/user/${encodeURIComponent(username)}/videos`);
    if (r.status === 404) { renderEmpty(username); return; }
    if (!r.ok) throw new Error(`the archive responded with ${r.status}`);
    const data = await r.json();
    if (!data.found) { renderEmpty(username); return; }
    renderUser(data);
    if (feedId) {
      const v = currentUser.videos.find((x) => x.feed_id === feedId);
      if (v) openModal(v);
    }
  } catch (e) {
    renderError(username, e);
  }
}

// Shared "centered message + action button" state card.
function renderState(html, btnLabel, onClick) {
  resultsEl.innerHTML = "";
  const state = el("div", "state", html);
  if (btnLabel) {
    const btn = el("button", "btn", escapeHtml(btnLabel));
    btn.addEventListener("click", onClick);
    state.appendChild(btn);
  }
  resultsEl.appendChild(state);
}

function renderEmpty(username) {
  renderState(
    `<h3>No archived clips found for “${escapeHtml(username)}”</h3>
     <p>The handle may be spelled differently, or it was never captured by the archive.
        Try the autocomplete suggestions for close matches.</p>`,
    "Search again", () => selectUser(username));
}

function renderError(username, e) {
  const msg = e && e.message ? e.message : String(e);
  renderState(
    `<h3>Couldn’t reach the archive</h3>
     <p>Something went wrong loading “${escapeHtml(username)}” — ${escapeHtml(msg)}.
        The Internet Archive may be slow or temporarily unavailable.</p>`,
    "Retry", () => selectUser(username));
}

function renderUser(data) {
  currentUser = data;
  resultsEl.innerHTML = "";
  const back = el("button", "back-btn", "← New search");
  back.addEventListener("click", goHome);
  resultsEl.appendChild(back);
  const head = el("div", "user-head");
  const av = el("div", "avatar");
  av.style.backgroundImage = data.avatar_url ? `url("${data.avatar_url}")` : "";
  const info = el("div");
  info.appendChild(el("h2", null, escapeHtml(data.display_name || data.username)));
  const stats = el("div", "stat-row");
  stats.appendChild(el("div", null, `<b>${data.recovered}</b> clips recovered`));
  if (data.deleted_count)
    stats.appendChild(el("div", "deleted-stat", `<b>${data.deleted_count}</b> were deleted`));
  if (data.live_count != null)
    stats.appendChild(el("div", null, `<b>${data.live_count}</b> on profile at shutdown`));
  info.appendChild(stats);

  const conn = el("div", "conn-row");
  const followersBtn = el("button", "conn-btn", "Followers");
  followersBtn.addEventListener("click", () => openConnections(data.username, "followers"));
  const followingBtn = el("button", "conn-btn", "Following");
  followingBtn.addEventListener("click", () => openConnections(data.username, "following"));
  conn.appendChild(followersBtn);
  conn.appendChild(followingBtn);
  info.appendChild(conn);

  head.appendChild(av);
  head.appendChild(info);
  resultsEl.appendChild(head);

  resultsEl.appendChild(buildToolbar(data));
  const grid = el("div", "grid");
  grid.id = "grid";
  resultsEl.appendChild(grid);
  renderGrid();
}

// Filter + sort controls above the grid.
function buildToolbar(data) {
  const bar = el("div", "toolbar");
  const count = el("span", "tool-count");
  count.id = "tool-count";

  const games = [...new Set(data.videos.map((v) => v.game).filter(Boolean))].sort();

  const filter = el("select");
  filter.id = "filter-select";
  filter.appendChild(new Option("All clips", "all"));
  if (data.deleted_count) filter.appendChild(new Option("Deleted only", "deleted"));
  games.forEach((g) => filter.appendChild(new Option(g, `game:${g}`)));
  const filterLabel = el("label", null, "Show");
  filterLabel.appendChild(filter);

  const sort = el("select");
  sort.id = "sort-select";
  [["newest", "Newest first"], ["oldest", "Oldest first"],
   ["longest", "Longest"], ["shortest", "Shortest"]]
    .forEach(([val, txt]) => sort.appendChild(new Option(txt, val)));
  const sortLabel = el("label", null, "Sort");
  sortLabel.appendChild(sort);

  filter.addEventListener("change", renderGrid);
  sort.addEventListener("change", renderGrid);

  bar.appendChild(count);
  bar.appendChild(filterLabel);
  bar.appendChild(sortLabel);
  return bar;
}

// Apply the active filter + sort to currentUser.videos and (re)build the grid.
function renderGrid() {
  if (!currentUser) return;
  const grid = $("#grid");
  if (!grid) return;
  const filterVal = ($("#filter-select") || {}).value || "all";
  const sortVal = ($("#sort-select") || {}).value || "newest";

  let list = currentUser.videos.slice();
  if (filterVal === "deleted") list = list.filter((v) => v.deleted);
  else if (filterVal.startsWith("game:")) {
    const g = filterVal.slice(5);
    list = list.filter((v) => v.game === g);
  }
  list.sort((a, b) => {
    switch (sortVal) {
      case "oldest":   return (a.date || "").localeCompare(b.date || "");
      case "longest":  return durSeconds(b.duration) - durSeconds(a.duration);
      case "shortest": return durSeconds(a.duration) - durSeconds(b.duration);
      default:         return (b.date || "").localeCompare(a.date || ""); // newest
    }
  });

  currentList = list;
  grid.innerHTML = "";
  list.forEach((v) => grid.appendChild(card(v)));

  const count = $("#tool-count");
  if (count) count.textContent = `${list.length} clip${list.length === 1 ? "" : "s"}`;

  // If a clip is open, keep nav in sync with the (possibly re-filtered) list.
  if (!modal.hidden && openVideo) {
    currentIndex = currentList.findIndex((v) => v.feed_id === openVideo.feed_id);
    updateNav();
  }
}

function card(v) {
  const c = el("div", "card");
  const thumb = el("div", "thumb");
  if (v.thumb) thumb.style.backgroundImage = `url("${v.thumb}")`;
  if (v.deleted) thumb.appendChild(el("div", "badge", "DELETED"));
  if (v.duration) {
    const d = isoDur(v.duration);
    if (d) thumb.appendChild(el("div", "dur", d));
  }
  thumb.appendChild(el("div", "play",
    '<svg viewBox="0 0 24 24" fill="#fff"><path d="M8 5v14l11-7z"/></svg>'));
  c.appendChild(thumb);

  const body = el("div", "card-body");
  body.appendChild(el("div", "card-title", escapeHtml(v.title)));
  const sub = el("div", "card-sub");
  sub.appendChild(el("span", "card-game", escapeHtml(v.game || "")));
  sub.appendChild(el("span", null, v.date ? v.date.slice(0, 7) : ""));
  body.appendChild(sub);
  c.appendChild(body);

  c.addEventListener("click", () => openModal(v));
  return c;
}

// ---------- modal player ----------

const modal = $("#modal");
const player = $("#player");
const modalLoading = $("#modal-loading");
const modalPrev = $("#modal-prev");
const modalNext = $("#modal-next");

let modalTimer = null;

function openModal(v) {
  modal.hidden = false;
  document.body.style.overflow = "hidden";
  openVideo = v;
  currentIndex = currentList.indexOf(v);
  if (currentIndex < 0) currentIndex = currentList.findIndex((x) => x.feed_id === v.feed_id);
  if (currentUser) setHash(currentUser.username, v.feed_id);
  updateNav();
  $("#modal-title").textContent = v.title;
  $("#modal-sub").textContent = [v.game, v.date].filter(Boolean).join(" · ");
  modalLoading.hidden = false;
  modalLoading.classList.remove("err");
  modalLoading.textContent = "Locating the original video in the archive…";
  player.poster = v.thumb || "";
  $("#modal-dl").style.display = "";
  $("#modal-dl").href = `/api/stream/${v.feed_id}?dl=1`;
  player.src = `/api/stream/${v.feed_id}`;
  player.play().catch(() => {});

  const fail = () => {
    clearTimeout(modalTimer);
    modalLoading.hidden = false;
    modalLoading.classList.add("err");
    modalLoading.innerHTML =
      `<div>The archive preserved this clip's details and thumbnail, but not the original video file.` +
      `&nbsp;<a href="${v.page_url}" target="_blank" rel="noopener">View original page ↗</a></div>`;
    document.getElementById("modal-dl").style.display = "none";
  };
  player.oncanplay = () => { clearTimeout(modalTimer); modalLoading.hidden = true; };
  player.onerror = fail;
  // Safety net: if neither canplay nor error fires (very slow archive), give up.
  clearTimeout(modalTimer);
  modalTimer = setTimeout(fail, 25000);
}

function closeModal() {
  modal.hidden = true;
  clearTimeout(modalTimer);
  player.pause();
  player.removeAttribute("src");
  player.load();
  document.body.style.overflow = "";
  openVideo = null;
  if (currentUser) setHash(currentUser.username);
}

// Move to the previous/next clip in the currently displayed (filtered) list.
function step(delta) {
  if (currentIndex < 0) return;
  const i = currentIndex + delta;
  if (i < 0 || i >= currentList.length) return;
  openModal(currentList[i]);
}

function updateNav() {
  if (modalPrev) modalPrev.disabled = currentIndex <= 0;
  if (modalNext) modalNext.disabled = currentIndex < 0 || currentIndex >= currentList.length - 1;
}

[modalPrev, modalNext].forEach((b) => b && b.addEventListener("click", (e) => {
  e.stopPropagation();
  step(Number(b.dataset.nav));
}));

modal.addEventListener("click", (e) => { if (e.target.dataset.close !== undefined) closeModal(); });
document.addEventListener("keydown", (e) => {
  if (modal.hidden) return;
  if (e.key === "Escape") closeModal();
  else if (e.key === "ArrowRight") step(1);
  else if (e.key === "ArrowLeft") step(-1);
});

// ---------- helpers ----------

function escapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
function isoDur(d) {
  const m = /PT(?:(\d+)M)?(?:(\d+)S)?/.exec(d || "");
  if (!m) return "";
  const mins = +(m[1] || 0), secs = +(m[2] || 0);
  return `${mins}:${String(secs).padStart(2, "0")}`;
}
function durSeconds(d) {
  const m = /PT(?:(\d+)M)?(?:(\d+)S)?/.exec(d || "");
  if (!m) return 0;
  return (+(m[1] || 0)) * 60 + (+(m[2] || 0));
}

// ---------- URL hash (shareable links) ----------
// #<username>           → open a user's clips
// #<username>/<feed_id> → open a user and a specific clip in the player
function setHash(username, feedId) {
  const h = feedId
    ? `#${encodeURIComponent(username)}/${encodeURIComponent(feedId)}`
    : `#${encodeURIComponent(username)}`;
  history.replaceState(null, "", h);
}
function parseHash() {
  const raw = location.hash.replace(/^#/, "");
  if (!raw) return null;
  const slash = raw.indexOf("/"); // feed_id is hex, so the first slash splits cleanly
  if (slash === -1) return { username: decodeURIComponent(raw), feedId: null };
  return {
    username: decodeURIComponent(raw.slice(0, slash)),
    feedId: decodeURIComponent(raw.slice(slash + 1)),
  };
}

// ---------- home / reset ----------

function goHome() {
  hideSuggestions();
  closeModal();
  resultsEl.hidden = true;
  resultsEl.innerHTML = "";
  hero.classList.remove("compact");
  searchInput.value = "";
  currentUser = null;
  currentList = [];
  currentIndex = -1;
  history.replaceState(null, "", location.pathname);
  searchInput.focus();
}

const brandEl = $("#brand");
if (brandEl) brandEl.addEventListener("click", goHome);
const goBtn = $("#search-go");
if (goBtn) goBtn.addEventListener("click", () => {
  const v = searchInput.value.trim();
  if (v) selectUser(v);
});

// ---------- followers / following ----------

const connEl = $("#conn");
const connTitle = $("#conn-title");
const connList = $("#conn-list");

async function openConnections(username, kind) {
  connEl.hidden = false;
  connTitle.textContent = kind === "followers" ? "Followers" : "Following";
  connList.innerHTML = `<div class="state"><div class="spinner-lg"></div></div>`;
  try {
    const r = await fetch(`/api/user/${encodeURIComponent(username)}/${kind}`);
    renderConnections(await r.json(), kind);
  } catch (e) {
    connList.innerHTML = `<p class="muted">Couldn’t load ${kind}.</p>`;
  }
}

function renderConnections(data, kind) {
  const users = data.users || [];
  const label = kind === "followers" ? "followers" : "following";
  const n = data.total != null ? data.total : users.length;
  connTitle.textContent = `${n} ${label}${data.truncated ? " · first page" : ""}`;
  if (!users.length) {
    connList.innerHTML = `<p class="muted">No archived ${label} found for this user.</p>`;
    return;
  }
  connList.innerHTML = "";
  users.forEach((u) => {
    const item = el("div", "conn-item");
    const a = el("div", "sug-avatar");
    if (u.avatar_url) a.style.backgroundImage = `url("${u.avatar_url}")`;
    const txt = el("div");
    txt.appendChild(el("div", "sug-name", escapeHtml(u.username)));
    if (u.display_name && u.display_name !== u.username)
      txt.appendChild(el("div", "sug-sub", escapeHtml(u.display_name)));
    item.appendChild(a);
    item.appendChild(txt);
    item.addEventListener("click", () => { closeConnections(); selectUser(u.username); });
    connList.appendChild(item);
  });
}

function closeConnections() { connEl.hidden = true; }
connEl.addEventListener("click", (e) => { if (e.target.dataset.close !== undefined) closeConnections(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !connEl.hidden) closeConnections(); });

// ---------- settings / video cache ----------

const settingsEl = $("#settings");
const cacheStatsEl = $("#cache-stats");
const cacheClearBtn = $("#cache-clear");

function fmtBytes(b) {
  if (!b) return "0 MB";
  const mb = b / (1024 * 1024);
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${mb.toFixed(mb < 10 ? 1 : 0)} MB`;
}

function renderCacheStats(s) {
  if (!s || !s.count) {
    cacheStatsEl.textContent = "No videos cached yet";
    cacheClearBtn.disabled = true;
    return;
  }
  const dl = s.downloading ? ` (${s.downloading} downloading)` : "";
  cacheStatsEl.textContent =
    `${s.count} clip${s.count === 1 ? "" : "s"} · ${fmtBytes(s.bytes)}${dl}`;
  cacheClearBtn.disabled = false;
}

async function loadCacheStats() {
  cacheStatsEl.textContent = "Loading…";
  try {
    const r = await fetch("/api/cache");
    renderCacheStats(await r.json());
  } catch (e) {
    cacheStatsEl.textContent = "Couldn’t read cache info";
    cacheClearBtn.disabled = true;
  }
}

async function clearCache() {
  cacheClearBtn.disabled = true;
  cacheStatsEl.textContent = "Clearing…";
  try {
    await fetch("/api/cache", { method: "DELETE" });
    await loadCacheStats();
    cacheStatsEl.textContent = "Cache cleared · " + cacheStatsEl.textContent;
  } catch (e) {
    cacheStatsEl.textContent = "Couldn’t clear the cache";
  }
}

function openSettings() { settingsEl.hidden = false; loadCacheStats(); }
function closeSettings() { settingsEl.hidden = true; }

const settingsBtn = $("#settings-btn");
if (settingsBtn) settingsBtn.addEventListener("click", openSettings);
cacheClearBtn.addEventListener("click", clearCache);
settingsEl.addEventListener("click", (e) => { if (e.target.dataset.close !== undefined) closeSettings(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !settingsEl.hidden) closeSettings(); });

// On load, open a shared link (#user or #user/feed_id) if present;
// otherwise start on the clean search screen.
window.addEventListener("load", () => {
  const h = parseHash();
  if (h && h.username) selectUser(h.username, h.feedId);
  else searchInput.focus();
});
