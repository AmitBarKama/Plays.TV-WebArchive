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
import os
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
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


def _retry_delay(attempt: int, exc: Exception) -> float:
    """How long to wait before the next retry.

    Honour the archive's `Retry-After` header on 429/503 if present; otherwise
    exponential backoff. A little jitter avoids a thundering herd of retries
    lining up after a rate-limit.
    """
    resp = getattr(exc, "response", None)
    if resp is not None:
        ra = resp.headers.get("Retry-After")
        if ra:
            secs = _retry_after_seconds(ra)
            if secs is not None:
                # Honour the archive's ask, but cap it so we never hold a
                # concurrency slot for an unbounded stretch.
                return min(config.MAX_RETRY_AFTER, secs) + random.uniform(0, 0.3)
    return config.BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 0.3)


def _retry_after_seconds(ra: str) -> float | None:
    """Parse a Retry-After value (delta-seconds or HTTP-date) to seconds, or None."""
    try:
        return max(0.0, float(ra))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(ra)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError):
        return None


async def _request(method: str, url: str, **kwargs) -> httpx.Response:
    """Issue a request with bounded concurrency + polite delay + retry/backoff."""
    client = await get_client()
    last_exc: Exception | None = None
    async with _sema():
        for attempt in range(config.MAX_RETRIES + 1):
            if config.REQUEST_DELAY:
                await asyncio.sleep(config.REQUEST_DELAY)
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
                await asyncio.sleep(_retry_delay(attempt, exc))
    assert last_exc is not None
    raise last_exc


class CdxUnavailable(Exception):
    """The CDX API could not be queried (throttled / empty-200 / repeated 5xx).

    Deliberately distinct from a query that *succeeded* and returned no rows.
    Under load, web.archive.org's CDX often answers with an HTTP 200 and an
    empty body instead of a 429 — which looks exactly like "no captures". If
    callers can't tell those apart, the stream resolver caches a throttled probe
    as a permanent "video not archived" miss, and a recoverable clip is lost.
    """


async def cdx(url_pattern: str, **params: Any) -> list[list[str]]:
    """Query the CDX API. Returns rows (first row is the header).

    Returns ``[]`` only for a *definitive* empty result — the query ran and the
    archive genuinely has no matching captures. Raises :class:`CdxUnavailable`
    when the archive could not be queried (throttled, empty 200 body, or
    repeated 5xx), so callers can avoid recording a false miss.

    Example: cdx("plays.tv/u/MLdini", fl="timestamp,original",
                 filter="statuscode:200", collapse="timestamp:8")
    """
    q = {"url": url_pattern, "output": "json"}
    q.update({k: v for k, v in params.items() if v is not None})
    last: Exception | None = None
    for attempt in range(config.MAX_RETRIES + 1):
        try:
            resp = await _request("GET", config.CDX_API, params=q)
        except httpx.HTTPError as exc:          # persistent 429/5xx after inner retries
            last = exc
        else:
            if resp.status_code == 200:
                text = resp.text.strip()
                if text:
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError as exc:
                        last = exc              # garbage/partial body -> treat as throttle
                else:
                    last = CdxUnavailable("empty 200 body (throttled)")
            elif resp.status_code in (400, 403, 404):
                return []                       # query ran, nothing matched -> definitive
            else:
                last = CdxUnavailable(f"cdx status {resp.status_code}")
        if attempt < config.MAX_RETRIES:
            await asyncio.sleep(
                config.BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 0.4)
            )
    raise CdxUnavailable(f"cdx unavailable for {url_pattern!r}: {last}")


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


async def find_capture(original_url: str) -> tuple[str, str] | None:
    """Return (timestamp, original) of an existing 200 capture for a URL, or None."""
    rows = await cdx(
        original_url, fl="timestamp,original", filter="statuscode:200", limit="1"
    )
    if len(rows) > 1:
        return rows[1][0], rows[1][1]
    return None


async def download(url: str, dest: Path) -> tuple[bool, str]:
    """Stream a URL to ``dest`` atomically, under the same politeness contract as
    every other archive request: bounded by the concurrency semaphore, polite
    delay, and retry/backoff (honouring Retry-After) on 429/503/5xx.

    Writes to ``<dest>.part`` and renames on success, so a partial download is
    never mistaken for a complete file. Returns ``(ok, path-or-reason)``.
    """
    client = await get_client()
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.parent / (dest.name + ".part")
    last_exc: Exception | None = None
    # The semaphore (and any backoff sleep) is held for the whole attempt on
    # purpose: when the archive rate-limits us, pausing the slot throttles the
    # overall request rate, which is the polite thing to do.
    async with _sema():
        for attempt in range(config.MAX_RETRIES + 1):
            if config.REQUEST_DELAY:
                await asyncio.sleep(config.REQUEST_DELAY)
            try:
                async with client.stream("GET", url) as resp:
                    if resp.status_code in (429, 503) or 500 <= resp.status_code < 600:
                        raise httpx.HTTPStatusError(
                            f"retryable {resp.status_code}",
                            request=resp.request, response=resp,
                        )
                    if resp.status_code not in (200, 206):
                        return False, f"http {resp.status_code}"
                    expected = resp.headers.get("Content-Length")
                    with open(part, "wb") as f:
                        async for chunk in resp.aiter_bytes(256 * 1024):
                            f.write(chunk)
                size = part.stat().st_size
                if size == 0:
                    part.unlink(missing_ok=True)
                    return False, "empty download"
                # Guard against a truncated stream being cached as a good file.
                if expected and expected.isdigit() and size != int(expected):
                    part.unlink(missing_ok=True)
                    raise httpx.HTTPError(f"truncated download {size}/{expected} bytes")
                os.replace(part, dest)
                return True, str(dest)
            except httpx.HTTPError as exc:
                last_exc = exc
                part.unlink(missing_ok=True)
                if attempt >= config.MAX_RETRIES:
                    break
                await asyncio.sleep(_retry_delay(attempt, exc))
    return False, f"download error: {last_exc}"
