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
                last_indexed  REAL
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
            """
        )
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
) -> None:
    with _lock:
        c = _connect()
        c.execute(
            """INSERT INTO users
               (username, username_low, display_name, avatar_url, live_count,
                recovered, last_indexed)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(username) DO UPDATE SET
                 display_name=excluded.display_name,
                 avatar_url=excluded.avatar_url,
                 live_count=excluded.live_count,
                 recovered=excluded.recovered,
                 last_indexed=excluded.last_indexed""",
            (username, username.lower(), display_name, avatar_url, live_count,
             recovered, time.time()),
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


def is_fresh(last_indexed: float | None) -> bool:
    return last_indexed is not None and (time.time() - last_indexed) < config.INDEX_TTL
