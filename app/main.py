"""FastAPI application: API routes + static frontend + streaming proxy."""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import service, videocache, wayback

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(title="Plays.tv Recovery", version="1.0")


@app.on_event("shutdown")
async def _shutdown() -> None:
    await wayback.close()


@app.middleware("http")
async def no_cache(request: Request, call_next):
    """Never let the browser cache the frontend — always serve the latest code."""
    resp = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".js", ".css", ".html")):
        resp.headers["Cache-Control"] = "no-store, max-age=0, must-revalidate"
    return resp


# --- API ---------------------------------------------------------------------

@app.get("/api/search")
async def api_search(q: str = Query(..., min_length=1)):
    return {"results": await service.search_users(q)}


@app.get("/api/clips/search")
async def api_clips_search(q: str = "", game: str | None = None, limit: int = 240):
    """Search across every recovered clip (title / user / game). Local index only."""
    return service.search_clips(q, game=game, limit=limit)


@app.get("/api/user/{username}/header")
async def api_user_header(username: str):
    """Lightweight profile header (avatar + name) for autocomplete enrichment."""
    return await service.enrich_user(username)


@app.get("/api/user/{username}/videos")
async def api_user_videos(username: str, refresh: bool = False):
    data = await service.get_user_videos(username, refresh=refresh)
    if not data.get("found"):
        return JSONResponse(data, status_code=404)
    return data


@app.get("/api/user/{username}/followers")
async def api_user_followers(username: str):
    """Users following this account (clickable through to their profiles)."""
    return await service.get_connections(username, "followers")


@app.get("/api/user/{username}/following")
async def api_user_following(username: str):
    """Accounts this user follows (clickable through to their profiles)."""
    return await service.get_connections(username, "following")


def _download_name(feed_id: str) -> str:
    """Filesystem-safe download filename for a clip."""
    v = service.cache.get_video(feed_id)
    base = re.sub(r"[^\w.-]+", "_", (v["title"] if v and v["title"] else feed_id))
    return f"{base}.mp4"


@app.get("/api/stream/{feed_id}")
async def api_stream(feed_id: str, request: Request, dl: bool = False):
    """Serve a clip's .mp4 with HTTP Range support (seeking + download).

    Cache-first: a clip already downloaded to the temp cache is served straight
    off disk (instant seeking + replay). On a miss we kick off a background
    download for next time and live-proxy the archive so playback starts now.
    """
    if not re.fullmatch(r"[a-f0-9]+", feed_id):
        raise HTTPException(400, "bad id")

    # Fast path: already cached -> let FileResponse handle Range/206 natively.
    if videocache.is_cached(feed_id):
        headers = (
            {"Content-Disposition": f'attachment; filename="{_download_name(feed_id)}"'}
            if dl else None
        )
        return FileResponse(
            videocache.path_for(feed_id), media_type="video/mp4", headers=headers
        )

    url = await service.resolve_stream(feed_id)
    if not url:
        raise HTTPException(404, "No archived video file found")

    # Cache this clip in the background so the next play is instant; the per-feed
    # lock in videocache makes repeated clicks safe.
    asyncio.create_task(videocache.ensure_cached(feed_id, url))

    client = await wayback.get_client()
    fwd = {}
    if (rng := request.headers.get("range")):
        fwd["Range"] = rng

    upstream = await client.send(
        client.build_request("GET", url, headers=fwd), stream=True
    )
    if upstream.status_code not in (200, 206):
        await upstream.aclose()
        raise HTTPException(502, f"archive returned {upstream.status_code}")

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": "video/mp4",
    }
    for h in ("content-length", "content-range"):
        if h in upstream.headers:
            headers[h] = upstream.headers[h]
    if dl:
        headers["Content-Disposition"] = f'attachment; filename="{_download_name(feed_id)}"'

    async def body():
        try:
            async for chunk in upstream.aiter_bytes(chunk_size=256 * 1024):
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(body(), status_code=upstream.status_code, headers=headers)


@app.get("/api/resolve/{feed_id}")
async def api_resolve(feed_id: str):
    """Resolve a clip's best archived source and report its tier — no video bytes.

    The grid calls this lazily as cards scroll into view: a 'preview' tier
    auto-loops muted on the card, 'full' opens in the player, 'none' stays a
    thumbnail. The result is cached, so the subsequent /api/stream is instant.
    """
    if not re.fullmatch(r"[a-f0-9]+", feed_id):
        raise HTTPException(400, "bad id")
    url = await service.resolve_stream(feed_id)
    st = service.cache.get_stream(feed_id)
    quality = st["quality"] if st else None
    if not url:
        return {"feed_id": feed_id, "tier": "none"}
    tier = "preview" if quality == service.PREVIEW_QUALITY else "full"
    return {"feed_id": feed_id, "tier": tier, "quality": quality,
            "stream": f"/api/stream/{feed_id}"}


# --- video cache (site settings) ---------------------------------------------

@app.get("/api/cache")
async def api_cache_stats():
    """Stats for the on-demand video cache (clip count + bytes on disk)."""
    return videocache.stats()


@app.delete("/api/cache")
async def api_cache_clear():
    """Wipe the on-demand video cache ("delete folder")."""
    return videocache.clear()


# --- static frontend ---------------------------------------------------------

@app.get("/")
async def index():
    return FileResponse(FRONTEND / "index.html")


app.mount("/", StaticFiles(directory=FRONTEND), name="static")
