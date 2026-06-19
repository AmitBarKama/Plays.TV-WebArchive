"""SQLite cache for scraped results.

Keeps the site fast and polite to web.archive.org: a username is scraped once,
then served from here. Schema is intentionally simple and append/upsert based.
The same tables are what a future ArchiveTeam bulk-ingest would populate, so the
rest of the app never needs to know where the data came from.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Iterable

from . import config

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                username      TEXT PRIMARY KEY,
                username_low  TEXT,
                display_name  TEXT,
                avatar_url    TEXT,
                live_count    INTEGER,
                recovered     INTEGER,
                last_indexed  REAL,
                indexed_at    REAL
            );
            CREATE INDEX IF NOT EXISTS idx_users_low ON users(username_low);

            CREATE TABLE IF NOT EXISTS videos (
                feed_id      TEXT PRIMARY KEY,
                username     TEXT,
                cdn_id       TEXT,
                cdn_host     TEXT,
                title        TEXT,
                game         TEXT,
                upload_date  TEXT,
                month        TEXT,
                duration     TEXT,
                deleted      INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_videos_user ON videos(username);

            CREATE TABLE IF NOT EXISTS streams (
                feed_id      TEXT PRIMARY KEY,
                quality      TEXT,
                archived_url TEXT,
                resolved_at  REAL
            );

            CREATE TABLE IF NOT EXISTS connections (
                username    TEXT,
                kind        TEXT,          -- 'followers' | 'following'
                total       INTEGER,
                truncated   INTEGER DEFAULT 0,
                users_json  TEXT,
                fetched_at  REAL,
                PRIMARY KEY (username, kind)
            );

            -- Phase 0 instrumentation: one row per recovery attempt, so a low
            -- hit-rate can later be split into true-loss vs. pipeline blind-spot.
            CREATE TABLE IF NOT EXISTS attempt_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                clip_id         TEXT,          -- canonical CDN id (when known)
                feed_id         TEXT,          -- app-internal alias
                url_pattern     TEXT NOT NULL, -- pattern NAME (e.g. 'legacy_profile')
                url_tried       TEXT,
                timestamp_tried TEXT,          -- Wayback 14-digit ts, if any
                outcome         TEXT NOT NULL, -- hit_*|miss_*|error_*|circuit_open
                http_status     INTEGER,
                bytes_len       INTEGER,
                latency_ms      INTEGER,
                created_at      REAL
            );
            CREATE INDEX IF NOT EXISTS idx_attempt_clip    ON attempt_log(clip_id);
            CREATE INDEX IF NOT EXISTS idx_attempt_outcome ON attempt_log(outcome);
            CREATE INDEX IF NOT EXISTS idx_attempt_pattern ON attempt_log(url_pattern);
            """
        )
        # Migration for DBs created before `indexed_at` existed. A header-only
        # autocomplete enrichment must never look like a finished video index,
        # so the scrape is gated on indexed_at; NULL on legacy rows means
        # "re-scrape", which auto-heals profiles wrongly cached as "0 clips".
        try:
            _conn.execute("ALTER TABLE users ADD COLUMN indexed_at REAL")
        except sqlite3.OperationalError:
            pass  # column already present
        _conn.commit()
    return _conn


def get_user(username: str) -> sqlite3.Row | None:
    with _lock:
        c = _connect()
        return c.execute(
            "SELECT * FROM users WHERE username_low = ?", (username.lower(),)
        ).fetchone()


def upsert_user(
    username: str,
    display_name: str,
    avatar_url: str,
    live_count: int | None,
    recovered: int,
    indexed: bool = False,
) -> None:
    """Insert/update a user row.

    ``indexed=True`` marks a *completed video index* (stamps ``indexed_at``).
    The header-only enrichment path leaves ``indexed_at`` untouched so a hovered
    autocomplete suggestion never looks like a finished scrape — ``get_user_videos``
    gates the cache on ``indexed_at``, so an un-indexed row always re-scrapes.
    """
    indexed_at = time.time() if indexed else None
    with _lock:
        c = _connect()
        c.execute(
            """INSERT INTO users
               (username, username_low, display_name, avatar_url, live_count,
                recovered, last_indexed, indexed_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(username) DO UPDATE SET
                 display_name=excluded.display_name,
                 avatar_url=excluded.avatar_url,
                 live_count=excluded.live_count,
                 recovered=excluded.recovered,
                 last_indexed=excluded.last_indexed,
                 indexed_at=CASE WHEN excluded.indexed_at IS NOT NULL
                                 THEN excluded.indexed_at
                                 ELSE users.indexed_at END""",
            (username, username.lower(), display_name, avatar_url, live_count,
             recovered, time.time(), indexed_at),
        )
        c.commit()


def upsert_videos(username: str, videos: Iterable[dict]) -> None:
    with _lock:
        c = _connect()
        c.executemany(
            """INSERT INTO videos
               (feed_id, username, cdn_id, cdn_host, title, game, upload_date,
                month, duration, deleted)
               VALUES (:feed_id,:username,:cdn_id,:cdn_host,:title,:game,
                       :upload_date,:month,:duration,:deleted)
               ON CONFLICT(feed_id) DO UPDATE SET
                 cdn_id=excluded.cdn_id, cdn_host=excluded.cdn_host,
                 title=excluded.title, game=excluded.game,
                 upload_date=excluded.upload_date, month=excluded.month,
                 duration=excluded.duration, deleted=excluded.deleted""",
            [{"username": username, **v} for v in videos],
        )
        c.commit()


def get_videos(username: str) -> list[sqlite3.Row]:
    with _lock:
        c = _connect()
        return c.execute(
            """SELECT * FROM videos WHERE username = ?
               ORDER BY COALESCE(upload_date, month) DESC""",
            (username,),
        ).fetchall()


def get_video(feed_id: str) -> sqlite3.Row | None:
    with _lock:
        c = _connect()
        return c.execute(
            "SELECT * FROM videos WHERE feed_id = ?", (feed_id,)
        ).fetchone()


def search_cached_users(prefix: str, limit: int = 10) -> list[sqlite3.Row]:
    with _lock:
        c = _connect()
        return c.execute(
            """SELECT * FROM users
               WHERE username_low LIKE ?
               ORDER BY length(username) LIMIT ?""",
            (prefix.lower() + "%", limit),
        ).fetchall()


def get_stream(feed_id: str) -> sqlite3.Row | None:
    with _lock:
        c = _connect()
        return c.execute(
            "SELECT * FROM streams WHERE feed_id = ?", (feed_id,)
        ).fetchone()


def put_stream(feed_id: str, quality: str, archived_url: str) -> None:
    with _lock:
        c = _connect()
        c.execute(
            """INSERT INTO streams (feed_id, quality, archived_url, resolved_at)
               VALUES (?,?,?,?)
               ON CONFLICT(feed_id) DO UPDATE SET
                 quality=excluded.quality, archived_url=excluded.archived_url,
                 resolved_at=excluded.resolved_at""",
            (feed_id, quality, archived_url, time.time()),
        )
        c.commit()


def get_stream_qualities(username: str) -> dict[str, str]:
    """feed_id -> cached stream quality for one user's clips, in a single query.

    A resolved hit maps to its quality ('preview', '720', ...); a cached miss
    maps to '' (empty string). Clips not yet resolved are absent from the map.
    Lets the grid label tiers without a resolve round-trip per clip.
    """
    with _lock:
        c = _connect()
        rows = c.execute(
            """SELECT s.feed_id AS fid, s.quality AS q FROM streams s
               JOIN videos v ON v.feed_id = s.feed_id
               WHERE v.username = ?""",
            (username,),
        ).fetchall()
    return {r["fid"]: (r["q"] or "") for r in rows}


def search_clips(
    query: str, game: str | None = None, limit: int = 240
) -> list[sqlite3.Row]:
    """Search every recovered clip by title / username / game (+ optional exact game).

    Pure local query over what's already been indexed — no archive round-trip.
    Each row carries the clip's cached stream quality (``stream_quality``) so the
    grid can tag tiers without re-resolving.
    """
    sql = [
        "SELECT v.*, s.quality AS stream_quality",
        "FROM videos v LEFT JOIN streams s ON s.feed_id = v.feed_id",
        "WHERE 1=1",
    ]
    args: list = []
    q = (query or "").strip()
    if q:
        like = f"%{q}%"
        sql.append("AND (v.title LIKE ? OR v.username LIKE ? OR v.game LIKE ?)")
        args += [like, like, like]
    if game:
        sql.append("AND v.game = ?")
        args.append(game)
    sql.append("ORDER BY COALESCE(v.upload_date, v.month) DESC LIMIT ?")
    args.append(limit)
    with _lock:
        c = _connect()
        return c.execute(" ".join(sql), args).fetchall()


def get_connections(username: str, kind: str) -> dict | None:
    """Cached followers/following list for a user, or None if not cached."""
    with _lock:
        c = _connect()
        row = c.execute(
            "SELECT * FROM connections WHERE username = ? AND kind = ?",
            (username.lower(), kind),
        ).fetchone()
    if not row:
        return None
    return {
        "total": row["total"],
        "truncated": bool(row["truncated"]),
        "users": json.loads(row["users_json"] or "[]"),
        "fetched_at": row["fetched_at"],
    }


def put_connections(
    username: str, kind: str, total: int | None, truncated: bool, users: list[dict]
) -> None:
    with _lock:
        c = _connect()
        c.execute(
            """INSERT INTO connections
               (username, kind, total, truncated, users_json, fetched_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(username, kind) DO UPDATE SET
                 total=excluded.total, truncated=excluded.truncated,
                 users_json=excluded.users_json, fetched_at=excluded.fetched_at""",
            (username.lower(), kind, total, int(truncated),
             json.dumps(users), time.time()),
        )
        c.commit()


def is_fresh(last_indexed: float | None, ttl: float | None = None) -> bool:
    ttl = config.INDEX_TTL if ttl is None else ttl
    return last_indexed is not None and (time.time() - last_indexed) < ttl


# --- Phase 0: recovery instrumentation ---------------------------------------

def log_attempt(
    url_pattern: str,
    outcome: str,
    *,
    clip_id: str | None = None,
    feed_id: str | None = None,
    url_tried: str | None = None,
    timestamp_tried: str | None = None,
    http_status: int | None = None,
    bytes_len: int | None = None,
    latency_ms: int | None = None,
) -> None:
    """Fire-and-forget per-attempt recovery outcome.

    Instrumentation MUST NOT raise into the fetch path — measurement never
    breaks resolution. Lets ``loss_split()`` later separate clips that are
    genuinely not archived (true-loss) from clips our pipeline simply failed
    to find (blind-spot). See planning-artifacts/phase-0-1-plan.md.
    """
    try:
        with _lock:
            c = _connect()
            c.execute(
                """INSERT INTO attempt_log
                   (clip_id, feed_id, url_pattern, url_tried, timestamp_tried,
                    outcome, http_status, bytes_len, latency_ms, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (clip_id, feed_id, url_pattern, url_tried, timestamp_tried,
                 outcome, http_status, bytes_len, latency_ms, time.time()),
            )
            c.commit()
    except Exception:
        pass


def classify_outcome(
    status: int | None, body_len: int | None, content_type: str | None = None
) -> str:
    """Map an HTTP result to an attempt_log outcome (error/miss axis only).

    The caller decides hit *quality* (hit_full|hit_preview|hit_thumb) from the
    pattern it tried; this only classifies failures and bare hits.
    """
    if status == 404:
        return "miss_404"
    if status in (401, 403):
        return "miss_forbidden"
    if status == 429:
        return "circuit_open"
    if status and status >= 500:
        return f"error_http_{status}"
    if status == 200 and body_len:
        return "hit"
    return f"error_unknown_{status}"


def loss_split() -> dict:
    """The Phase-1 measurement: true-loss vs. blind-spot over attempt_log.

    true_loss            = clips that missed on EVERY pattern tried
    blind_spot_recovered = clips a non-'legacy_profile' pattern hit but the
                           old single-snapshot path ('legacy_profile') missed
    Caveat (see Murat's plan): exclude clips whose attempt set contains any
    unreachable outcome (error_*/circuit_open) before trusting true_loss.
    """
    with _lock:
        c = _connect()
        row = c.execute(
            """WITH per_clip AS (
                 SELECT clip_id,
                   MAX(outcome LIKE 'hit%') AS any_hit,
                   MAX(CASE WHEN url_pattern = 'legacy_profile'
                             AND outcome LIKE 'hit%' THEN 1 ELSE 0 END) AS legacy_hit,
                   MAX(CASE WHEN url_pattern <> 'legacy_profile'
                             AND outcome LIKE 'hit%' THEN 1 ELSE 0 END) AS tier1_hit
                 FROM attempt_log
                 WHERE clip_id IS NOT NULL
                 GROUP BY clip_id
               )
               SELECT
                 SUM(CASE WHEN any_hit = 0 THEN 1 ELSE 0 END) AS true_loss,
                 SUM(CASE WHEN tier1_hit = 1 AND legacy_hit = 0 THEN 1 ELSE 0 END)
                   AS blind_spot_recovered,
                 SUM(CASE WHEN any_hit = 1 THEN 1 ELSE 0 END) AS recoverable_total,
                 COUNT(*) AS clips_total
               FROM per_clip""",
        ).fetchone()
    return dict(row) if row else {}
