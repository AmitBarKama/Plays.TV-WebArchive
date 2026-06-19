"""Orchestration: tie together CDX discovery, page parsing and the cache.

This is the only module the API layer talks to. Swapping the data source
(live Wayback today, ArchiveTeam bulk index later) means changing this file
only — the routes and frontend stay the same.
"""
from __future__ import annotations

import asyncio
import re

from . import cache, config, parser, wayback
from .parser import Profile, Video

PROFILE_URL = "https://plays.tv/u/{user}"
VIDEOS_URL = "https://plays.tv/u/{user}/videos"   # the dedicated videos tab
CONN_URL = "https://plays.tv/u/{user}/{kind}"  # kind: followers | following
# Cap how many snapshots we union per user (most-recent + spread of older ones).
# Kept low so a lookup stays snappy; the early full snapshots already render ~60
# videos each, so a handful recovers the vast majority.
MAX_SNAPSHOTS = 6
# Extra captures of the /videos tab (base + ?page=N) to union for older clips the
# profile page no longer rendered. Capped so a lookup stays polite to the archive.
MAX_VIDEO_PAGE_CAPS = 4

# Tiny in-memory cache of autocomplete prefixes -> usernames (cleared on restart).
_search_cache: dict[str, list[str]] = {}


# --- username autocomplete ---------------------------------------------------

async def search_users(query: str, limit: int = 8) -> list[dict]:
    """Return username suggestions for the autocomplete box.

    Live prefix search via CDX, merged with anything we've already cached
    (so avatars/display names we know show up instantly).
    """
    query = query.strip()
    if len(query) < 2:
        return []

    key = query.lower()
    if key in _search_cache:
        names = _search_cache[key]
    else:
        # IMPORTANT: a trailing-wildcard URL (`plays.tv/u/<q>*`) makes CDX do a
        # slow scan (~13s for "MLdini*") that blows past SEARCH_DEADLINE, so the
        # box returned nothing. `matchType=prefix` (no `*`) hits the index and
        # returns the same rows in ~1s. The deadline still guards pathological
        # short prefixes — the UI lets the user open exactly what they typed.
        try:
            rows = await asyncio.wait_for(
                wayback.cdx(
                    f"plays.tv/u/{query}", matchType="prefix",
                    fl="original", limit="120",
                ),
                timeout=config.SEARCH_DEADLINE,
            )
        except (asyncio.TimeoutError, Exception):
            rows = []
        names = parser.extract_usernames(rows)
        _search_cache[key] = names

    # Always blend in usernames we've already cached (previously-viewed profiles)
    # so similar matches show instantly even if CDX is slow/unavailable.
    local = [r["username"] for r in cache.search_cached_users(query, limit=20)]
    merged = {n.lower(): n for n in names}
    for n in local:
        merged.setdefault(n.lower(), n)
    names = list(merged.values())

    # Rank: exact/startswith first, then by length, then alpha.
    ql = query.lower()
    names.sort(key=lambda n: (not n.lower().startswith(ql), len(n), n.lower()))
    names = names[:limit]

    out: list[dict] = []
    for n in names:
        cached = cache.get_user(n)
        out.append({
            "username": n,
            "display_name": cached["display_name"] if cached else None,
            "avatar_url": wb_image(cached["avatar_url"]) if cached else None,
            "recovered": cached["recovered"] if cached else None,
        })
    return out


async def enrich_user(username: str) -> dict:
    """Fetch just the lightweight profile header (avatar + display name).

    Used to fill in autocomplete photos without a full video scrape.
    """
    cached = cache.get_user(username)
    if cached and cached["avatar_url"]:
        return _user_dict(cached)

    cap = await _latest_ok_snapshot(PROFILE_URL.format(user=username))
    prof = Profile(username=username)
    if cap:
        html = await wayback.fetch_raw(cap, PROFILE_URL.format(user=username))
        if html:
            parser.parse_profile_meta(html, prof)
    cache.upsert_user(
        username, prof.display_name or username, prof.avatar_url,
        prof.live_video_count, cache.get_user(username)["recovered"] if cached else 0,
    )
    return {
        "username": username,
        "display_name": prof.display_name or username,
        "avatar_url": wb_image(prof.avatar_url),
        "recovered": cached["recovered"] if cached else None,
    }


# --- video index -------------------------------------------------------------

async def get_user_videos(username: str, refresh: bool = False) -> dict:
    """Full recovered video list for a user (scrape-on-demand, then cached)."""
    cached_user = cache.get_user(username)
    if not refresh and cached_user and cached_user["indexed_at"] is not None:
        # Gate on indexed_at (a *completed* scrape), not last_indexed: a header-
        # only autocomplete enrichment leaves indexed_at NULL and so never
        # suppresses a real scrape. A 0-clip result is provisional (the archive
        # may have been throttling), so it gets a much shorter retry window.
        ttl = None if cached_user["recovered"] else config.EMPTY_INDEX_TTL
        if cache.is_fresh(cached_user["indexed_at"], ttl):
            return _assemble(username, cached_user)

    profile = await _scrape_user(username)
    if not profile.videos and not profile.display_name:
        # Nothing archived under this exact handle.
        return {"username": username, "found": False, "videos": []}

    # Flag deletions conservatively. A snapshot only renders the most-recent
    # ~60 videos, so "absent from the latest snapshot" alone over-counts (old
    # videos simply scrolled off). We only call a clip *deleted* when it falls
    # within the live era — between the oldest and newest video still on the
    # profile at shutdown — yet is gone. Clips outside that window (older gaps,
    # or newer ones recovered from the /videos tab that postdate the live
    # snapshot) are "unknown" and never flagged.
    live_ids = profile.live_feed_ids
    live_months = sorted(
        profile.videos[f].month for f in live_ids
        if profile.videos.get(f) and profile.videos[f].month
    )
    min_live_month = live_months[0] if live_months else None
    max_live_month = live_months[-1] if live_months else None

    def is_deleted(v: Video) -> int:
        if v.feed_id in live_ids:
            return 0
        if min_live_month and v.month and min_live_month <= v.month <= max_live_month:
            return 1
        return 0

    video_dicts = []
    for v in profile.videos.values():
        video_dicts.append({
            "feed_id": v.feed_id, "cdn_id": v.cdn_id, "cdn_host": v.cdn_host,
            "title": v.title, "game": v.game, "upload_date": v.upload_date,
            "month": v.month, "duration": v.duration,
            "deleted": is_deleted(v),
        })
    recovered = len(video_dicts)
    cache.upsert_user(
        username, profile.display_name or username, profile.avatar_url,
        profile.live_video_count, recovered, indexed=True,
    )
    cache.upsert_videos(username, video_dicts)
    return _assemble(username, cache.get_user(username))


async def _scrape_user(username: str) -> Profile:
    """Union archived profile + /videos-tab snapshots into one Profile.

    The main profile page renders the most-recent clips; the dedicated /videos
    tab (and its ?page=N captures) often preserves older ones the profile page
    no longer showed, so we union both for the widest recovery.
    """
    snaps = await _profile_snapshots(username)
    caps = await _videos_page_captures(username)
    profile = Profile(username=username)
    if not snaps and not caps:
        return profile

    # Main profile snapshots, newest-first: the newest defines the set of clips
    # still live on the profile at shutdown (drives the "deleted" flag).
    if snaps:
        htmls = await asyncio.gather(
            *[wayback.fetch_raw(ts, PROFILE_URL.format(user=username)) for ts in snaps]
        )
        for i, html in enumerate(htmls):
            if not html:
                continue
            parser.parse_profile_page(html, profile)
            if i == 0:
                # newest snapshot -> the videos still on the live profile
                profile.live_feed_ids = set(profile.videos)

    # Then union the /videos-tab captures — pure additive coverage.
    if caps:
        extra = await asyncio.gather(*[wayback.fetch_raw(ts, url) for ts, url in caps])
        for html in extra:
            if html:
                parser.parse_profile_page(html, profile)
    return profile


async def _profile_snapshots(username: str) -> list[str]:
    """Distinct profile-page snapshot timestamps, newest first, capped.

    NOTE: `filter=statuscode:200` makes CDX do a slow full scan. We instead ask
    for status inline and filter client-side, which is much faster.
    """
    rows = await wayback.cdx(
        PROFILE_URL.format(user=username),
        fl="timestamp,statuscode",
        collapse="timestamp:8",
    )
    tss = [r[0] for r in rows[1:] if len(r) < 2 or r[1] in ("200", "-")]
    tss.sort(reverse=True)
    if len(tss) <= MAX_SNAPSHOTS:
        return tss
    # Keep newest few + an even spread of older ones for max recovery.
    newest = tss[:3]
    rest = tss[3:]
    step = max(1, len(rest) // (MAX_SNAPSHOTS - 3))
    return newest + rest[::step][: MAX_SNAPSHOTS - 3]


async def _videos_page_captures(username: str) -> list[tuple[str, str]]:
    """`(timestamp, url)` captures of the user's /videos tab, incl. ?page=N.

    A CDX prefix query on `.../videos` surfaces the base tab and its paginated
    captures in one call. We keep the newest 200/revisit capture per distinct
    clean URL (dropping `?_t=` tracking dupes), order base-tab-then-pages and
    cap it, so a lookup stays polite. Best-effort: a CDX hiccup here never breaks
    a lookup whose profile snapshots already succeeded.
    """
    try:
        rows = await wayback.cdx(
            f"plays.tv/u/{username}/videos", matchType="prefix",
            fl="timestamp,original,statuscode", limit="400",
        )
    except wayback.CdxUnavailable:
        return []
    newest: dict[str, str] = {}  # clean url -> newest timestamp
    for r in rows[1:]:
        if len(r) < 3 or r[2] not in ("200", "-"):
            continue
        ts, original = r[0], r[1]
        if "/videos" not in original:
            continue
        tail = original.split("/videos", 1)[1]
        if tail != "" and not tail.startswith("?page="):
            continue  # skip ?_t= tracking dupes / deeper sub-paths
        if original not in newest or ts > newest[original]:
            newest[original] = ts
    caps = [(ts, url) for url, ts in newest.items()]
    caps.sort(key=lambda c: (len(c[1]), c[1]))  # base tab first, then ?page=1,2,…
    return caps[:MAX_VIDEO_PAGE_CAPS]


async def _latest_ok_snapshot(url: str) -> str | None:
    """Newest snapshot of `url` that was captured 200 (skips 301 redirects)."""
    rows = await wayback.cdx(url, fl="timestamp,statuscode", collapse="timestamp:8")
    good = [r[0] for r in rows[1:] if len(r) > 1 and r[1] == "200"]
    good.sort(reverse=True)
    return good[0] if good else None


# --- followers / following ---------------------------------------------------

async def get_connections(username: str, kind: str) -> dict:
    """Followers/following list for a user (scrape-on-demand, then cached).

    Clickable in the UI: each entry loads that user's own profile.
    """
    if kind not in ("followers", "following"):
        return {"username": username, "kind": kind, "total": 0,
                "truncated": False, "users": []}

    cached = cache.get_connections(username, kind)
    if cached and cache.is_fresh(cached["fetched_at"]):
        return {"username": username, "kind": kind, "total": cached["total"],
                "truncated": cached["truncated"], "users": cached["users"]}

    url = CONN_URL.format(user=username, kind=kind)
    ts = await _latest_ok_snapshot(url)
    users: list[dict] = []
    total: int | None = None
    truncated = False
    if ts:
        html = await wayback.fetch_raw(ts, url)
        if html:
            users = parser.parse_user_list(html)
            for u in users:
                u["avatar_url"] = wb_image(u["avatar_url"]) or None
            total = parser.parse_section_counts(html).get(kind.upper())
            truncated = "?page=2" in html
    if total is None:
        total = len(users)

    cache.put_connections(username, kind, total, truncated, users)
    return {"username": username, "kind": kind, "total": total,
            "truncated": truncated, "users": users}


# --- stream / download resolution -------------------------------------------

# The short, silent hover-preview loop. When the full clip wasn't archived this
# is often still there — a few seconds of real motion, far better than a frozen
# thumbnail. Resolved as a last resort and tagged so the UI can label it.
PREVIEW_NAME = "preview_144.mp4"
PREVIEW_QUALITY = "preview"

# Filename at the tail of a .../processed/<file> capture URL.
_PROCESSED_FILE_RE = re.compile(r"/processed/([^/?#]+)$")
_CDN_HOSTS = ("d0playscdntv-a.akamaihd.net", "d1playscdntv-a.akamaihd.net")


async def resolve_stream(feed_id: str, retry_miss: bool = False) -> str | None:
    """Return a working archived video URL for a clip, best quality first.

    A single CDX *prefix* query lists every file archived under the clip's
    ``/processed/`` directory — all qualities, the preview loop, the thumbnails —
    in one request. We then pick the best available: the full clip by quality,
    else the silent preview loop. This replaced a 12-probe-per-clip fan-out
    (5 qualities x 2 hosts + preview) whose request burst got us throttled by
    web.archive.org — the single biggest cause of the old ~2.5% hit rate.

    A miss is cached only when a query *succeeded* and the directory genuinely
    held no video. If every host came back throttled (``CdxUnavailable``), the
    clip is left unresolved so a later visit can re-probe — never frozen into the
    cache as a false gap. Pass ``retry_miss=True`` to re-probe a cached miss.
    """
    cached = cache.get_stream(feed_id)
    if cached and (cached["archived_url"] or not retry_miss):
        return cached["archived_url"] or None

    v = cache.get_video(feed_id)
    if not v or not v["cdn_id"]:
        return None
    cdn_id = v["cdn_id"]

    # The clip's files live on the host its thumbnail came from; query that first
    # and only fall back to the other shard if the query was throttled.
    primary = v["cdn_host"] or _CDN_HOSTS[1]
    order = [primary] + [h for h in _CDN_HOSTS if h != primary]

    async def list_dir(host: str) -> list[list[str]] | None:
        """Captures under this clip's /processed/ dir, or None if throttled."""
        try:
            return await wayback.cdx(
                f"http://{host}/video/{cdn_id}/processed/",
                matchType="prefix", collapse="urlkey",
                fl="timestamp,original,statuscode", limit="250",
            )
        except wayback.CdxUnavailable:
            return None

    rows: list[list[str]] | None = None
    any_listed = False
    for host in order:
        r = await list_dir(host)
        if r is None:
            continue            # throttled this host
        any_listed = True
        if r:                   # real listing (thumbnails are ~always present)
            rows = r
            break
    if rows is None:
        if not any_listed:
            return None         # every host throttled — don't poison the cache
        rows = []               # hosts answered, dir is empty — a real miss

    # filename -> (timestamp, original) for playable (200 / revisit) captures.
    avail: dict[str, tuple[str, str]] = {}
    for row in rows[1:]:        # row[0] is the CDX header
        if len(row) < 3:
            continue
        ts, original, status = row[0], row[1], row[2]
        if status not in ("200", "-"):
            continue
        m = _PROCESSED_FILE_RE.search(original)
        if m:
            avail.setdefault(m.group(1), (ts, original))

    chosen: tuple[str, tuple[str, str]] | None = None
    for q in config.QUALITIES:                    # full clip, best quality first
        if f"{q}.mp4" in avail:
            chosen = (q, avail[f"{q}.mp4"])
            break
    if chosen is None and PREVIEW_NAME in avail:  # else the silent preview loop
        chosen = (PREVIEW_QUALITY, avail[PREVIEW_NAME])

    if chosen:
        quality, (ts, original) = chosen
        url = f"{config.WAYBACK}/{ts}id_/{original}"
        cache.put_stream(feed_id, quality, url)
        return url

    # Directory listed fine but holds no video — safe to remember the miss.
    cache.put_stream(feed_id, "", "")
    return None


# --- helpers -----------------------------------------------------------------

def thumb_url(cdn_host: str, cdn_id: str) -> str | None:
    if not cdn_host or not cdn_id:
        return None
    original = f"http://{cdn_host}/video/{cdn_id}/processed/480.jpg"
    return f"{config.WAYBACK}/2019id_/{original}"  # Wayback redirects to nearest


def wb_image(raw_url: str | None) -> str | None:
    """Wrap a raw (dead) Plays.tv CDN image URL so it loads from the archive."""
    if not raw_url:
        return None
    if "web.archive.org" in raw_url:
        return raw_url
    if raw_url.startswith("//"):
        raw_url = "http:" + raw_url
    return f"{config.WAYBACK}/2019id_/{raw_url}"


def _user_dict(row) -> dict:
    return {
        "username": row["username"],
        "display_name": row["display_name"],
        "avatar_url": wb_image(row["avatar_url"]),
        "recovered": row["recovered"],
        "live_count": row["live_count"],
    }


def _tier(quality: str | None) -> str | None:
    """Map a cached stream quality to a UI tier. ``None`` = not resolved yet.

    '' (cached miss) -> 'none'; the preview marker -> 'preview'; anything else
    is a real quality -> 'full'.
    """
    if quality is None:
        return None
    if quality == "":
        return "none"
    return "preview" if quality == PREVIEW_QUALITY else "full"


def search_clips(query: str, game: str | None = None, limit: int = 240) -> dict:
    """Grid-shaped results for a search across every recovered clip (local index).

    Same per-clip shape the grid already renders, plus ``username`` so a result
    card can link back to whoever made it. No archive call — searches only what
    has already been recovered.
    """
    rows = cache.search_clips(query, game=game, limit=limit)
    videos = [{
        "feed_id": r["feed_id"],
        "username": r["username"],
        "title": r["title"] or "(untitled)",
        "game": r["game"],
        "date": r["upload_date"] or (r["month"] + "-01" if r["month"] else None),
        "duration": r["duration"],
        "deleted": bool(r["deleted"]),
        "thumb": thumb_url(r["cdn_host"], r["cdn_id"]),
        "tier": _tier(r["stream_quality"]),
        "page_url": f"https://plays.tv/video/{r['feed_id']}",
    } for r in rows]
    return {
        "query": query,
        "total": len(videos),
        "games": sorted({r["game"] for r in rows if r["game"]}),
        "deleted_count": sum(1 for v in videos if v["deleted"]),
        "videos": videos,
    }


def _assemble(username: str, user_row) -> dict:
    rows = cache.get_videos(username)
    tiers = cache.get_stream_qualities(username)
    videos = [{
        "feed_id": r["feed_id"],
        "title": r["title"] or "(untitled)",
        "game": r["game"],
        "date": r["upload_date"] or (r["month"] + "-01" if r["month"] else None),
        "duration": r["duration"],
        "deleted": bool(r["deleted"]),
        "thumb": thumb_url(r["cdn_host"], r["cdn_id"]),
        "tier": _tier(tiers.get(r["feed_id"])),
        "page_url": f"https://plays.tv/video/{r['feed_id']}",
    } for r in rows]
    return {
        "username": username,
        "found": True,
        "display_name": user_row["display_name"] if user_row else username,
        "avatar_url": wb_image(user_row["avatar_url"]) if user_row else None,
        "live_count": user_row["live_count"] if user_row else None,
        "recovered": len(videos),
        "deleted_count": sum(1 for v in videos if v["deleted"]),
        "videos": videos,
    }
