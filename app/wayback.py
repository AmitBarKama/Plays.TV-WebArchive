"""Async client for the Internet Archive Wayback Machine.

Replaces the old Selenium scraper entirely: discovery happens through the
structured CDX API and pages are fetched over plain HTTP (raw `id_` mode so we
get the original Plays.tv markup, not the rewritten replay view).

Built to be a polite, resilient citizen of web.archive.org: bounded
concurrency, exponential backoff on rate-limits (HTTP 429) and 5xx.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from . import config

_semaphore: asyncio.Semaphore | None = None
_client: httpx.AsyncClient | None = None


def _sema() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY)
    return _semaphore


async def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            headers={"User-Agent": config.USER_AGENT},
            timeout=config.REQUEST_TIMEOUT,
            follow_redirects=True,
        )
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def _request(method: str, url: str, **kwargs) -> httpx.Response:
    """Issue a request with bounded concurrency + retry/backoff."""
    client = await get_client()
    last_exc: Exception | None = None
    async with _sema():
        for attempt in range(config.MAX_RETRIES + 1):
            try:
                resp = await client.request(method, url, **kwargs)
                if resp.status_code in (429, 503) or 500 <= resp.status_code < 600:
                    raise httpx.HTTPStatusError(
                        f"retryable {resp.status_code}", request=resp.request, response=resp
                    )
                return resp
            except (httpx.HTTPError,) as exc:
                last_exc = exc
                if attempt >= config.MAX_RETRIES:
                    break
                await asyncio.sleep(config.BACKOFF_BASE * (2 ** attempt))
    assert last_exc is not None
    raise last_exc


async def cdx(url_pattern: str, **params: Any) -> list[list[str]]:
    """Query the CDX API. Returns rows (first row is the header).

    Example: cdx("plays.tv/u/MLdini", fl="timestamp,original",
                 filter="statuscode:200", collapse="timestamp:8")
    """
    q = {"url": url_pattern, "output": "json"}
    q.update({k: v for k, v in params.items() if v is not None})
    resp = await _request("GET", config.CDX_API, params=q)
    if resp.status_code != 200 or not resp.text.strip():
        return []
    try:
        return json.loads(resp.text)
    except json.JSONDecodeError:
        return []


async def fetch_raw(timestamp: str, original_url: str) -> str | None:
    """Fetch the original archived bytes of a page (id_ = no Wayback rewriting)."""
    url = f"{config.WAYBACK}/{timestamp}id_/{original_url}"
    try:
        resp = await _request("GET", url)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    return resp.text


async def find_capture(original_url: str, qualities_done: bool = False) -> tuple[str, str] | None:
    """Return (timestamp, original) of an existing 200 capture for a URL, or None."""
    rows = await cdx(
        original_url, fl="timestamp,original", filter="statuscode:200", limit="1"
    )
    if len(rows) > 1:
        return rows[1][0], rows[1][1]
    return None
