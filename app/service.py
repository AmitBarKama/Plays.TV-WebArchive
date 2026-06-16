"""Orchestration: tie together CDX discovery, page parsing and the cache.

This is the only module the API layer talks to. Swapping the data source
(live Wayback today, ArchiveTeam bulk index later) means changing this file
only — the routes and frontend stay the same.
"""
from __future__ import annotations

import asyncio

from . import cache, config, parser, wayback
from .parser import Profile, Video

PROFILE_URL = "https://plays.tv/u/{user}"
CONN_URL = "https://plays.tv/u/{user}/{kind}"  # kind: followers | following
# Cap how many snapshots we union per user (most-recent + spread of older ones).
# Kept low so a lookup stays snappy; the early full snapshots already render ~60
# videos each, so a handful recovers the vast majority.
MAX_SNAPSHOTS = 6

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

    cap = await _latest_profile_snapshot(username)
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
    if not refresh and cached_user and cache.is_fresh(cached_user["last_indexed"]):
        return _assemble(username, cached_user)

    profile = await _scrape_user(username)
    if not profile.videos and not profile.display_name:
        # Nothing archived under this exact handle.
        return {"username": username, "found": False, "videos": []}

    # Flag deletions conservatively. A snapshot only renders the most-recent
    # ~60 videos, so "absent from the latest snapshot" alone over-counts (old
    # videos simply scrolled off). We only call a clip *deleted* when it's from
    # the same era as clips still on the profile at shutdown — i.e. newer than
    # the oldest video still present — yet it's gone. Older gaps are "unknown".
    live_ids = profile.live_feed_ids
    live_months = sorted(
        profile.videos[f].month for f in live_ids
        if profile.videos.get(f) and profile.videos[f].month
    )
    min_live_month = live_months[0] if live_months else None

    def is_deleted(v: Video) -> int:
        if v.feed_id in live_ids:
            return 0
        if min_live_month and v.month and v.month >= min_live_month:
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
        profile.live_video_count, recovered,
    )
    cache.upsert_videos(username, video_dicts)
    return _assemble(username, cache.get_user(username))


async def _scrape_user(username: str) -> Profile:
    """Union all archived profile snapshots into one Profile."""
    snaps = await _profile_snapshots(username)
    profile = Profile(username=username)
    if not snaps:
        return profile

    # Snapshots are newest-first; the first one defines the "still live" set.
    htmls = await asyncio.gather(
        *[wayback.fetch_raw(ts, PROFILE_URL.format(user=username)) for ts in snaps]
    )
    profile.live_feed_ids = set()
    for i, html in enumerate(htmls):
        if not html:
            continue
        before = set(profile.videos)
        parser.parse_profile_page(html, profile)
        if i == 0:
            # newest snapshot -> these are the videos still on the live profile
            profile.live_feed_ids = set(profile.videos)
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


async def _latest_profile_snapshot(username: str) -> str | None:
    rows = await wayback.cdx(
        PROFILE_URL.format(user=username),
        fl="timestamp", limit="-1",   # no statuscode filter -> fast
    )
    return rows[1][0] if len(rows) > 1 else None


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

async def resolve_stream(feed_id: str) -> str | None:
    """Return a working archived .mp4 URL for a video, trying qualities.

    Verified through CDX so we only ever hand the player a URL that exists.
    Cached after first resolution.
    """
    cached = cache.get_stream(feed_id)
    if cached:
        return cached["archived_url"] or None

    v = cache.get_video(feed_id)
    if not v or not v["cdn_id"]:
        return None

    hosts = [v["cdn_host"]] if v["cdn_host"] else []
    # CDN was sharded across d0/d1; try both in case capture used the other.
    for alt in ("d0playscdntv-a.akamaihd.net", "d1playscdntv-a.akamaihd.net"):
        if alt not in hosts:
            hosts.append(alt)

    async def probe(quality: str, host: str) -> tuple[int, str] | None:
        original = f"http://{host}/video/{v['cdn_id']}/processed/{quality}.mp4"
        cap = await wayback.find_capture(original)
        if cap:
            ts, orig = cap
            return config.QUALITIES.index(quality), f"{config.WAYBACK}/{ts}id_/{orig}"
        return None

    # Probe every quality/host combination in parallel, bounded by a deadline.
    tasks = [probe(q, h) for q in config.QUALITIES for h in hosts]
    best: tuple[int, str] | None = None
    try:
        for coro in asyncio.as_completed(tasks, timeout=config.STREAM_RESOLVE_DEADLINE):
            res = await coro
            if res and (best is None or res[0] < best[0]):
                best = res  # prefer higher quality (lower index)
    except asyncio.TimeoutError:
        pass

    if best:
        cache.put_stream(feed_id, config.QUALITIES[best[0]], best[1])
        return best[1]
    # Remember the miss so we fail instantly next time instead of re-hunting.
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


def _assemble(username: str, user_row) -> dict:
    rows = cache.get_videos(username)
    videos = [{
        "feed_id": r["feed_id"],
        "title": r["title"] or "(untitled)",
        "game": r["game"],
        "date": r["upload_date"] or (r["month"] + "-01" if r["month"] else None),
        "duration": r["duration"],
        "deleted": bool(r["deleted"]),
        "thumb": thumb_url(r["cdn_host"], r["cdn_id"]),
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
