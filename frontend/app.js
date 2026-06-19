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

  // Secondary action: search across every clip already recovered.
  const sc = el("li", "open-exact search-clips");
  sc.innerHTML =
    `<div class="sug-avatar go">⌕</div>` +
    `<div><div class="sug-name">Search all clips for “${escapeHtml(query)}”</div>` +
    `<div class="sug-sub">title, game or user across everything recovered</div></div>`;
  sc.addEventListener("click", () => searchClips(query));
  suggestionsEl.appendChild(sc);

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
  twStop(); resizeAnswer();
  resultsEl.hidden = false;
  resultsEl.scrollIntoView({ behavior: "smooth", block: "start" });
  resultsEl.innerHTML = `
    <div class="state">
      <div class="spinner-lg"></div>
      <h3>Searching the archive for “${escapeHtml(username)}”…</h3>
      <p>Unioning every saved snapshot to recover deleted clips. This can take a few seconds.</p>
    </div>`;
  setHash(username);

  // Bound the wait: the Internet Archive can hang for minutes when it's slow/down.
  // Fail into a clear "couldn't reach the archive" message instead of spinning forever.
  const ctrl = new AbortController();
  const to = setTimeout(() => ctrl.abort(), 20000);
  try {
    const r = await fetch(`/api/user/${encodeURIComponent(username)}/videos`, { signal: ctrl.signal });
    clearTimeout(to);
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
    clearTimeout(to);
    renderError(username, e.name === "AbortError"
      ? new Error("the archive didn’t respond in time (it may be down)")
      : e);
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

// ---------- search across all recovered clips ----------

async function searchClips(query) {
  query = (query || "").trim();
  if (!query) return;
  hideSuggestions();
  searchInput.value = query;
  hero.classList.add("compact");
  twStop(); resizeAnswer();
  resultsEl.hidden = false;
  resultsEl.scrollIntoView({ behavior: "smooth", block: "start" });
  resultsEl.innerHTML =
    `<div class="state"><div class="spinner-lg"></div>
       <h3>Searching recovered clips for “${escapeHtml(query)}”…</h3></div>`;
  history.replaceState(null, "", `#search/${encodeURIComponent(query)}`);
  try {
    const r = await fetch(`/api/clips/search?q=${encodeURIComponent(query)}`);
    if (!r.ok) throw new Error(`search failed (${r.status})`);
    renderClips(await r.json(), query);
  } catch (e) { renderError(query, e); }
}

function renderClips(data, query) {
  // Reuse the grid + toolbar by presenting the search as a pseudo-"user".
  currentUser = {
    username: null, isSearch: true, query,
    videos: data.videos, recovered: data.total, deleted_count: data.deleted_count,
  };
  resultsEl.innerHTML = "";
  const back = el("button", "back-btn", "← New search");
  back.addEventListener("click", goHome);
  resultsEl.appendChild(back);

  const head = el("div", "user-head search-head");
  const info = el("div");
  info.appendChild(el("h2", null, `Results for “${escapeHtml(query)}”`));
  const users = new Set(data.videos.map((v) => v.username).filter(Boolean)).size;
  const stats = el("div", "stat-row");
  stats.appendChild(el("div", null,
    `<b>${data.total}</b> clip${data.total === 1 ? "" : "s"} from ` +
    `<b>${users}</b> user${users === 1 ? "" : "s"}`));
  if (data.deleted_count)
    stats.appendChild(el("div", "deleted-stat", `<b>${data.deleted_count}</b> deleted`));
  info.appendChild(stats);
  head.appendChild(info);
  resultsEl.appendChild(head);

  if (!data.total) {
    resultsEl.appendChild(el("div", "state",
      `<h3>No recovered clips match “${escapeHtml(query)}”</h3>
       <p>Search only covers clips already pulled from the archive. Recover a
          username first, then search again.</p>`));
    return;
  }
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
  c.dataset.feedId = v.feed_id;
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
  // In a cross-user clip search, attribute (and link to) whoever made it.
  if (currentUser && currentUser.isSearch && v.username) {
    const u = el("div", "card-user", "by " + escapeHtml(v.username));
    u.addEventListener("click", (e) => { e.stopPropagation(); selectUser(v.username); });
    body.appendChild(u);
  }
  c.appendChild(body);

  c.addEventListener("click", () => openModal(v));

  // Tier: known from the payload -> light it up now; unknown -> resolve lazily
  // as the card scrolls into view (keeps the archive load bounded and polite).
  if (v.tier) {
    setCardTier(c, v.tier, `/api/stream/${v.feed_id}`);
  } else {
    c.dataset.tier = "unknown";
    if (cardObserver) cardObserver.observe(c);
    else { resolveQueue.push(() => resolveCard(c)); pumpResolveQueue(); }
  }
  return c;
}

// ---------- lazy tier resolution + auto-looping preview cards ----------
// As each card enters the viewport we ask the server for its archived source.
// Preview-only clips auto-loop muted on the card (like Plays.tv's old hover);
// full clips get a badge and open in the player; thumbnail-only stay as-is.

const cardObserver = ("IntersectionObserver" in window)
  ? new IntersectionObserver(onCardsVisible, { rootMargin: "300px" })
  : null;

let resolveActive = 0;
const resolveQueue = [];

function pumpResolveQueue() {
  while (resolveActive < 4 && resolveQueue.length) {
    const job = resolveQueue.shift();
    resolveActive++;
    Promise.resolve(job()).finally(() => { resolveActive--; pumpResolveQueue(); });
  }
}

function onCardsVisible(entries, obs) {
  let added = false;
  entries.forEach((e) => {
    if (!e.isIntersecting) return;
    obs.unobserve(e.target);
    resolveQueue.push(() => resolveCard(e.target));
    added = true;
  });
  if (added) pumpResolveQueue();
}

async function resolveCard(cardEl) {
  const feedId = cardEl.dataset.feedId;
  if (!feedId || (cardEl.dataset.tier && cardEl.dataset.tier !== "unknown")) return;
  if (!cardEl.isConnected) return; // grid was re-rendered (filter/sort) — skip
  try {
    const r = await fetch(`/api/resolve/${feedId}`);
    const data = await r.json();
    setCardTier(cardEl, data.tier, data.stream || `/api/stream/${feedId}`);
  } catch (e) { cardEl.dataset.tier = "none"; }
}

function setCardTier(cardEl, tier, streamUrl) {
  cardEl.dataset.tier = tier || "none";
  const thumb = cardEl.querySelector(".thumb");
  if (!thumb) return;
  thumb.querySelectorAll(".tier-tag").forEach((n) => n.remove());

  if (tier === "preview") {
    if (!thumb.querySelector("video.preview-loop")) {
      const vid = el("video", "preview-loop");
      vid.muted = true; vid.loop = true; vid.autoplay = true;
      vid.playsInline = true; vid.preload = "metadata";
      vid.src = streamUrl;
      vid.addEventListener("error", () => { vid.remove(); cardEl.dataset.tier = "none"; });
      thumb.insertBefore(vid, thumb.firstChild);
      vid.play().catch(() => {});
    }
    thumb.appendChild(el("div", "tier-tag preview", "PREVIEW"));
  } else if (tier === "full") {
    thumb.appendChild(el("div", "tier-tag full", "FULL"));
  } else {
    thumb.appendChild(el("div", "tier-tag none", "THUMBNAIL"));
  }
}

// ---------- modal player ----------

const modal = $("#modal");
const player = $("#player");
const modalLoading = $("#modal-loading");
const modalPrev = $("#modal-prev");
const modalNext = $("#modal-next");

let modalTimer = null;

async function openModal(v) {
  modal.hidden = false;
  document.body.style.overflow = "hidden";
  document.body.classList.add("modal-open");   // drops full-screen GPU layers (see CSS)
  // Pause the grid's auto-looping preview clips: multiple simultaneous decodes
  // behind the modal compete for the GPU and make playback stutter.
  document.querySelectorAll("video.preview-loop").forEach((vid) => vid.pause());
  openVideo = v;
  currentIndex = currentList.indexOf(v);
  if (currentIndex < 0) currentIndex = currentList.findIndex((x) => x.feed_id === v.feed_id);
  if (currentUser && currentUser.username) setHash(currentUser.username, v.feed_id);
  updateNav();
  $("#modal-title").textContent = v.title;
  $("#modal-sub").textContent = [v.game, v.date].filter(Boolean).join(" · ");
  player.poster = v.thumb || "";
  player.loop = false;
  player.muted = false;
  setModalBadge(null);
  modalLoading.hidden = false;
  modalLoading.classList.remove("err");
  modalLoading.textContent = "Locating the original video in the archive…";

  // Tier may already be known (from the payload or the card's lazy resolve);
  // otherwise resolve it now so we show the right player + label.
  let tier = v.tier;
  if (!tier) {
    try {
      const r = await fetch(`/api/resolve/${v.feed_id}`);
      tier = (await r.json()).tier;
      v.tier = tier;
    } catch (e) { tier = null; }
  }
  if (openVideo !== v) return; // user navigated to another clip while resolving

  if (tier === "none") { showUnarchived(v); return; }

  const isPreview = tier === "preview";
  player.loop = isPreview;     // previews are short silent loops
  player.muted = isPreview;
  setModalBadge(isPreview ? "preview" : "full");
  const dl = $("#modal-dl");
  dl.style.display = "";
  dl.href = `/api/stream/${v.feed_id}?dl=1`;
  dl.textContent = isPreview ? "Download preview .mp4" : "Download .mp4";
  player.src = `/api/stream/${v.feed_id}`;
  player.play().catch(() => {});

  player.oncanplay = () => { clearTimeout(modalTimer); modalLoading.hidden = true; };
  player.onerror = () => showUnarchived(v);
  // Safety net: if neither canplay nor error fires (very slow archive), give up.
  clearTimeout(modalTimer);
  modalTimer = setTimeout(() => showUnarchived(v), 25000);
}

// The full video for this clip wasn't archived — keep the page honest.
function showUnarchived(v) {
  clearTimeout(modalTimer);
  player.pause();
  player.removeAttribute("src");
  player.load();
  setModalBadge(null);
  modalLoading.hidden = false;
  modalLoading.classList.add("err");
  modalLoading.innerHTML =
    `<div>The archive preserved this clip's details and thumbnail, but not the original video file.` +
    `&nbsp;<a href="${v.page_url}" target="_blank" rel="noopener">View original page ↗</a></div>`;
  $("#modal-dl").style.display = "none";
}

function setModalBadge(tier) {
  const b = $("#modal-badge");
  if (!b) return;
  if (tier === "preview") {
    b.hidden = false;
    b.className = "modal-badge preview";
    b.textContent = "PREVIEW · silent loop · full video not archived";
  } else if (tier === "full") {
    b.hidden = false;
    b.className = "modal-badge full";
    b.textContent = "FULL VIDEO";
  } else {
    b.hidden = true;
  }
}

function closeModal() {
  modal.hidden = true;
  clearTimeout(modalTimer);
  player.pause();
  player.removeAttribute("src");
  player.load();
  document.body.style.overflow = "";
  document.body.classList.remove("modal-open");
  document.querySelectorAll("video.preview-loop").forEach((vid) => vid.play().catch(() => {}));
  openVideo = null;
  if (currentUser && currentUser.username) setHash(currentUser.username);
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
  landingReset();
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

// ---------- theme toggle (one button: dark <-> light) ----------
// First visit follows the OS (applied pre-paint by the <head> script); after
// that the button sets an explicit choice. The icon reflects the current theme.
const THEME_KEY = "mtv-theme";
const themeToggle = $("#theme-toggle");
const THEME_ICON = {
  sun: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="5"></circle><line x1="12" y1="1" x2="12" y2="3"></line><line x1="12" y1="21" x2="12" y2="23"></line><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"></line><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"></line><line x1="1" y1="12" x2="3" y2="12"></line><line x1="21" y1="12" x2="23" y2="12"></line><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"></line><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"></line></svg>',
  moon: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"></path></svg>',
};

function currentTheme() {
  return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
}
function renderThemeToggle() {
  if (!themeToggle) return;
  const t = currentTheme();
  themeToggle.innerHTML = t === "dark" ? THEME_ICON.moon : THEME_ICON.sun;
  themeToggle.title = t === "dark" ? "Switch to light mode" : "Switch to dark mode";
}
function setTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  try { localStorage.setItem(THEME_KEY, theme); } catch (e) { /* private mode */ }
  renderThemeToggle();
}
if (themeToggle)
  themeToggle.addEventListener("click", () =>
    setTheme(currentTheme() === "dark" ? "light" : "dark"));
// Until the user makes an explicit choice, keep following the OS live.
matchMedia("(prefers-color-scheme: light)").addEventListener("change", (e) => {
  if (!localStorage.getItem(THEME_KEY)) {
    document.documentElement.setAttribute("data-theme", e.matches ? "light" : "dark");
    renderThemeToggle();
  }
});
renderThemeToggle();

// ---------- channel-tuning landing: self-typing example + idle resume ----------
// The username is typed straight into the page (no search box). When idle, an
// example types/deletes through a roster so it's obvious where to type; it stands
// down the moment the user focuses/types, and resumes after 20s of stillness.
const answerLine = $("#answer-line");
const TW_NAMES = ["Smaf", "MaxTotal", "Tikwikman", "Slappy", "RonBotic", "Mldini"];
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

// Hidden sizer so the inline input hugs its text — the cursor sits right after it.
const answerSizer = document.createElement("span");
answerSizer.style.cssText = "position:absolute;visibility:hidden;white-space:pre;left:-9999px;top:0;";
document.body.appendChild(answerSizer);
function resizeAnswer() {
  const cs = getComputedStyle(searchInput);
  answerSizer.style.fontFamily = cs.fontFamily;
  answerSizer.style.fontSize = cs.fontSize;
  answerSizer.style.fontWeight = cs.fontWeight;
  answerSizer.style.letterSpacing = cs.letterSpacing;
  answerSizer.textContent = searchInput.value || searchInput.placeholder || "";
  searchInput.style.width = (answerSizer.offsetWidth + 2) + "px";
}

let twName = 0, twChar = 0, twDel = false, twTimer = null, idleTimer = null;
function twStop() { if (twTimer) { clearTimeout(twTimer); twTimer = null; } }
function twTick() {
  if (searchInput.value) { twStop(); return; }       // user typed — stand down
  const w = TW_NAMES[twName];
  searchInput.placeholder = w.slice(0, twChar);
  resizeAnswer();
  let delay;
  if (!twDel) { if (twChar < w.length) { twChar++; delay = 95; } else { twDel = true; delay = 1400; } }
  else { if (twChar > 0) { twChar--; delay = 45; } else { twDel = false; twName = (twName + 1) % TW_NAMES.length; delay = 450; } }
  twTimer = setTimeout(twTick, delay);
}
function twStart() {
  if (reduceMotion || twTimer || hero.classList.contains("compact") || searchInput.value) return;
  twDel = false; twChar = 0; twTick();
}
function resetIdle() {
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => {
    if (!searchInput.value && document.activeElement === searchInput) twStart();
  }, 20000);
}
function landingReset() {            // return to the landing: clear + play the demo
  twStop(); searchInput.value = ""; searchInput.placeholder = ""; resizeAnswer();
  if (reduceMotion) { searchInput.placeholder = "e.g. Smaf, MaxTotal, Tikwikman, Slappy, RonBotic, Mldini"; resizeAnswer(); }
  else twStart();
}

searchInput.addEventListener("focus", () => { twStop(); searchInput.placeholder = ""; resizeAnswer(); resetIdle(); });
searchInput.addEventListener("blur", () => { clearTimeout(idleTimer); if (!searchInput.value) { searchInput.placeholder = ""; twStart(); } });
searchInput.addEventListener("input", () => { twStop(); resetIdle(); resizeAnswer(); });
searchInput.addEventListener("keydown", resetIdle);
if (answerLine) answerLine.addEventListener("click", () => searchInput.focus());

// On load, open a shared link (#user or #user/feed_id) if present;
// otherwise start on the clean search screen.
window.addEventListener("load", () => {
  const raw = location.hash.replace(/^#/, "");
  if (raw.startsWith("search/")) { searchClips(decodeURIComponent(raw.slice(7))); return; }
  const h = parseHash();
  if (h && h.username) selectUser(h.username, h.feedId);
  else landingReset();
});
