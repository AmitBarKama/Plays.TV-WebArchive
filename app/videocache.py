"""On-demand video cache.

Clips are downloaded to a temp folder on first play and served locally from
then on, so seeking and re-watching are instant even when the upstream source
is slow. The folder is wiped manually from the site settings ("delete folder").

The source URL is whatever ``service.resolve_stream`` hands us — this module
only handles the download → temp → serve lifecycle, so it works unchanged the
moment any real source becomes available.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from . import config, wayback

# One lock per feed_id so concurrent plays of the same clip download it once.
_locks: dict[str, asyncio.Lock] = {}


def _dir() -> Path:
    config.VIDEO_CACHE_DIR.mkdir(exist_ok=True)
    return config.VIDEO_CACHE_DIR


def path_for(feed_id: str) -> Path:
    return _dir() / f"{feed_id}.mp4"


def is_cached(feed_id: str) -> bool:
    p = path_for(feed_id)
    return p.exists() and p.stat().st_size > 0


def _lock(feed_id: str) -> asyncio.Lock:
    lock = _locks.get(feed_id)
    if lock is None:
        lock = _locks[feed_id] = asyncio.Lock()
    return lock


async def ensure_cached(feed_id: str, url: str) -> Path | None:
    """Download ``url`` to the cache as ``<feed_id>.mp4`` if not already present.

    Writes to a ``.part`` file and atomically renames on success so a partial
    download (crash, disconnect, error) never looks like a complete clip.
    Returns the cached path, or ``None`` if the download failed.
    """
    final = path_for(feed_id)
    if is_cached(feed_id):
        return final

    async with _lock(feed_id):
        if is_cached(feed_id):           # filled in while we waited for the lock
            return final
        part = final.parent / (final.name + ".part")  # <feed_id>.mp4.part
        client = await wayback.get_client()
        try:
            async with client.stream("GET", url) as resp:
                if resp.status_code not in (200, 206):
                    return None
                with open(part, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=256 * 1024):
                        f.write(chunk)
            if part.stat().st_size == 0:
                part.unlink(missing_ok=True)
                return None
            os.replace(part, final)      # atomic publish
            return final
        except Exception:
            # Drop any partial so we don't serve a truncated file.
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass
            return None


def stats() -> dict:
    """Counts + total size of the cache (finished clips and in-flight ones)."""
    d = _dir()
    count = 0
    total = 0
    downloading = 0
    for p in d.iterdir():
        if p.name.endswith(".mp4.part"):
            downloading += 1
        elif p.suffix == ".mp4":
            count += 1
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return {"count": count, "bytes": total, "downloading": downloading}


def clear() -> dict:
    """Delete every cached clip (and any in-flight ``.part`` files)."""
    d = _dir()
    removed = 0
    freed = 0
    for p in d.iterdir():
        if p.suffix == ".mp4" or p.name.endswith(".mp4.part"):
            try:
                freed += p.stat().st_size
            except OSError:
                pass
            try:
                p.unlink()
                if p.suffix == ".mp4":
                    removed += 1
            except OSError:
                pass
    return {"removed": removed, "freed_bytes": freed}
