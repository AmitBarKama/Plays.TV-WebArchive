"""Parse archived Plays.tv markup into structured data.

Two complementary sources inside a single profile page give us everything,
so there is no need to visit each video page (the slow part of the old script):

  1. A JSON-LD `<script class="linked_data">` block — exact title, upload date,
     duration and thumbnail (which embeds the CDN video id) for recent videos.
  2. The server-rendered `<li class="video-item" data-feed-id=...>` list — covers
     all videos rendered on the page, with thumbnail (CDN id), title and game.

We key videos on `feed_id` (the plays.tv/video/<feed_id> page id) and resolve the
CDN id from the thumbnail URL, e.g.
    //d1playscdntv-a.akamaihd.net/video/G0vXuaZbBWG/processed/144.jpg
            host ^^^^^^^^^^^^^^^^^^         cdn id ^^^^^^^^^^^
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# --- regexes -----------------------------------------------------------------

_JSONLD_RE = re.compile(
    r'<script[^>]*class="linked_data"[^>]*>(.*?)</script>', re.S
)
_THUMB_RE = re.compile(
    r'(?P<host>d\d+playscdntv-a\.akamaihd\.net)/video/(?P<cdn>[A-Za-z0-9_-]+)/processed'
)
# A single video list item, scoped to its title link.
_ITEM_RE = re.compile(
    r'<li class="video-item" data-feed-id="(?P<feed>[a-f0-9]+)">'
    r'(?P<body>.*?)'
    r'class="title">(?P<title>.*?)</a>',
    re.S,
)
_GAME_RE = re.compile(r'class="hashtag[^"]*"[^>]*>(?P<game>[^<]+)</a>')
# Restrict to the user's own videos module (avoids "featured"/related pollution).
_USERVIDS_RE = re.compile(r'data-module-id="UserVideosMod".*', re.S)
_MONTHBLOCK_RE = re.compile(
    r'video-list-container"\s+id="(?P<month>\d{4}-\d{2})">(?P<block>.*?)'
    r'(?=video-list-container"\s+id="\d{4}-\d{2}"|$)',
    re.S,
)

_AVATAR_RE = re.compile(
    r'og:image"\s+content="(?P<u>[^"]*avatars/[^"]+)"'
)
_AVATAR_RE2 = re.compile(r'data-lazyload="(?P<u>[^"]*avatars/[^"]+)"')
_TITLE_RE = re.compile(r"<title>(?P<n>.*?)\s*-\s*Plays\.tv</title>", re.S)
_VIDCOUNT_RE = re.compile(
    r'VIDEOS</span><span class="section-value">(?P<c>\d+)</span>'
)


@dataclass
class Video:
    feed_id: str
    cdn_id: str = ""
    cdn_host: str = ""
    title: str = ""
    game: str = ""
    upload_date: str = ""   # ISO date if known
    month: str = ""         # YYYY-MM fallback bucket
    duration: str = ""      # ISO 8601 duration, e.g. PT37S


@dataclass
class Profile:
    username: str
    display_name: str = ""
    avatar_url: str = ""
    live_video_count: int | None = None
    videos: dict[str, Video] = field(default_factory=dict)  # feed_id -> Video
    live_feed_ids: set[str] = field(default_factory=set)    # videos on newest snapshot


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _abs(url: str) -> str:
    if url.startswith("//"):
        return "http:" + url
    return url


def parse_jsonld(html: str, profile: Profile) -> None:
    """Enrich/seed videos from the JSON-LD ProfilePage block (exact metadata)."""
    m = _JSONLD_RE.search(html)
    if not m:
        return
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return
    for vo in data.get("video", []) or []:
        embed = vo.get("embedURL", "")
        fm = re.search(r"/embeds/([a-f0-9]+)", embed)
        if not fm:
            continue
        feed_id = fm.group(1)
        tm = _THUMB_RE.search(vo.get("thumbnailURL", "") or "")
        v = profile.videos.get(feed_id) or Video(feed_id=feed_id)
        if tm:
            v.cdn_id = v.cdn_id or tm.group("cdn")
            v.cdn_host = v.cdn_host or tm.group("host")
        v.title = v.title or _clean(vo.get("description") or vo.get("name", ""))
        v.duration = v.duration or vo.get("duration", "")
        if vo.get("uploadDate"):
            v.upload_date = v.upload_date or vo["uploadDate"][:10]
        profile.videos[feed_id] = v


def parse_video_items(html: str, profile: Profile) -> None:
    """Parse the server-rendered video list (the user's own videos module)."""
    scoped = _USERVIDS_RE.search(html)
    region = scoped.group(0) if scoped else html
    for mb in _MONTHBLOCK_RE.finditer(region):
        month = mb.group("month")
        block = mb.group("block")
        for im in _ITEM_RE.finditer(block):
            feed_id = im.group("feed")
            body = im.group("body")
            tm = _THUMB_RE.search(body)
            gm = _GAME_RE.search(body)
            v = profile.videos.get(feed_id) or Video(feed_id=feed_id)
            if tm:
                v.cdn_id = v.cdn_id or tm.group("cdn")
                v.cdn_host = v.cdn_host or tm.group("host")
            v.title = v.title or _clean(im.group("title"))
            if gm:
                v.game = v.game or _clean(gm.group("game"))
            v.month = v.month or month
            profile.videos[feed_id] = v


def parse_profile_meta(html: str, profile: Profile) -> None:
    if not profile.display_name:
        tm = _TITLE_RE.search(html)
        if tm:
            profile.display_name = _clean(tm.group("n"))
    if not profile.avatar_url:
        am = _AVATAR_RE.search(html) or _AVATAR_RE2.search(html)
        if am:
            profile.avatar_url = _abs(am.group("u"))
    if profile.live_video_count is None:
        cm = _VIDCOUNT_RE.search(html)
        if cm:
            profile.live_video_count = int(cm.group("c"))


def parse_profile_page(html: str, profile: Profile) -> None:
    """Fold one archived profile snapshot into the accumulating Profile."""
    parse_profile_meta(html, profile)
    parse_jsonld(html, profile)
    parse_video_items(html, profile)


# --- username extraction (autocomplete) --------------------------------------

# Sub-paths under /u/<name> that are not usernames.
_RESERVED = {"followers", "following", "videos", "featuring", "liked", "about"}
_VALID_NAME = re.compile(r"^[A-Za-z0-9_.\-]{1,40}$")


def extract_usernames(cdx_rows: list[list[str]]) -> list[str]:
    """From CDX `original` rows, return clean, de-duplicated usernames.

    Preserves the first-seen casing (URLs are case-insensitive but display
    casing like 'MLdini' matters to users).
    """
    seen: dict[str, str] = {}  # lower -> original casing
    for row in cdx_rows[1:] if cdx_rows and cdx_rows[0] == ["original"] else cdx_rows:
        original = row[0] if isinstance(row, list) else row
        m = re.search(r"/u/([^/?#]+)", original)
        if not m:
            continue
        try:
            name = re.sub(r"%[0-9A-Fa-f]{2}", "", m.group(1))  # drop encoded junk
        except Exception:
            continue
        if not name or name.lower() in _RESERVED:
            continue
        if not _VALID_NAME.match(name):
            continue
        seen.setdefault(name.lower(), name)
    return sorted(seen.values(), key=str.lower)


# --- followers / following list ----------------------------------------------

# Each follower/followee is a `user-item` container; the display name + handle
# live in its `name-link` anchor, and the avatar in an `op_avatar.php` lazyload.
_NAMELINK_RE = re.compile(
    r'href="/u/(?P<user>[^/?"#]+)[^"]*"\s+class="name-link[^"]*"\s*>(?P<disp>[^<]*)</a>'
)
# Avatars come either as op_avatar.php or an /avatars/ CDN path; the banner
# (a video thumbnail) is neither, so this won't pick it up by mistake.
_OPAVATAR_RE = re.compile(
    r'data-lazyload="(?P<u>[^"]*(?:op_avatar\.php|/avatars/)[^"]*)"'
)
_SECTION_RE = re.compile(
    r'class="section-title">(?P<t>[A-Za-z]+)</span>'
    r'<span class="section-value[^"]*">(?P<v>[\d,]+)</span>'
)


def parse_section_counts(html: str) -> dict[str, int]:
    """Header counts on a profile/followers page, e.g. {'FOLLOWERS': 8}."""
    out: dict[str, int] = {}
    for m in _SECTION_RE.finditer(html):
        out[m.group("t").upper()] = int(m.group("v").replace(",", ""))
    return out


def parse_user_list(html: str) -> list[dict]:
    """Parse a followers/following page into [{username, display_name, avatar_url}]."""
    starts = [m.start() for m in re.finditer(r'class="user-item\s*"', html)]
    users: list[dict] = []
    seen: set[str] = set()
    for i, s in enumerate(starts):
        block = html[s: starts[i + 1] if i + 1 < len(starts) else len(html)]
        nm = _NAMELINK_RE.search(block)
        if not nm:
            continue
        user = re.sub(r"%[0-9A-Fa-f]{2}", "", nm.group("user"))
        low = user.lower()
        if not user or low in _RESERVED or not _VALID_NAME.match(user) or low in seen:
            continue
        seen.add(low)
        av = _OPAVATAR_RE.search(block)
        users.append({
            "username": user,
            "display_name": _clean(nm.group("disp")) or user,
            "avatar_url": _abs(av.group("u").replace("&amp;", "&")) if av else "",
        })
    return users
