"""Resumable command-line recovery scraper for Plays.tv clips.

Plays.tv shut down in 2019, so everything is recovered through the Internet
Archive's Wayback Machine — never the live site. This tool drives the same
discovery + media-resolution machinery the web app uses, but in batch: give it
one or more usernames (or a file of them) and it will, for each user,

  1. discover their archived clips (profile-snapshot union, cached in SQLite),
  2. probe the Wayback Machine for each clip's archived .mp4, and
  3. download the ones that survive — logging the (many) misses, never crashing.

It is polite (low concurrency, optional inter-request delay, exponential backoff
honoured by the shared client) and RESUMABLE: progress is checkpointed to disk
so a re-run skips users/clips already handled and doesn't re-download.

Run it:  ./scrape.sh <username> [more usernames…] [options]
   or:   python -m app.scrape --users-file handles.txt --out-dir data/recovered

Note: most clips' video files were never captured by the archive, so a low
recovery rate is expected and normal — the misses are logged, not errors.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import time
from pathlib import Path

from . import config, service, wayback

log = logging.getLogger("scrape")


# --- logging -----------------------------------------------------------------

def setup_logging(level: str, log_file: Path) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)
    fileh = logging.FileHandler(log_file, encoding="utf-8")
    fileh.setFormatter(fmt)
    root.addHandler(fileh)
    logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx is chatty at INFO


# --- checkpoint (resumability) ----------------------------------------------

def load_checkpoint(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _atomic_write_text(path: Path, text: str) -> None:
    """Write via a temp file + rename so a crash mid-write can't corrupt the target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def save_checkpoint(path: Path, data: dict) -> None:
    _atomic_write_text(path, json.dumps(data, indent=2))


# --- helpers -----------------------------------------------------------------

def _safe_name(s: str, limit: int = 80) -> str:
    """Filesystem-safe slug for a clip title / username (no path traversal)."""
    name = re.sub(r"[^\w.-]+", "_", s or "").strip("_.")
    if set(name) <= {"."}:  # "", ".", ".." → unusable, never escape the out-dir
        return ""
    return name[:limit]


def _entry_counts(entry: dict, errors: int = 0, available: int = 0) -> dict:
    """Per-user counts derived from the checkpoint entry (errors are per-run)."""
    return {
        "found": entry.get("found", 0),
        "recovered": len(entry.get("recovered_feed_ids", [])),
        "missing": len(entry.get("missing_feed_ids", [])),
        "errors": errors,
        "available": available,
    }


def read_user_list(args) -> list[str]:
    users: list[str] = list(args.users)
    if args.users_file:
        for line in Path(args.users_file).read_text().splitlines():
            line = line.split("#", 1)[0].strip()  # allow "# comment" lines
            if line:
                users.append(line)
    seen, out = set(), []
    for u in users:  # de-dupe, preserve order
        if u.lower() not in seen:
            seen.add(u.lower())
            out.append(u)
    return out


async def download_media(url: str, dest: Path) -> tuple[bool, str]:
    """Download an archived .mp4 to dest (dedupe + polite/retrying stream)."""
    if dest.exists() and dest.stat().st_size > 0:
        return True, "already downloaded"  # dedupe
    return await wayback.download(url, dest)


# --- per-user processing -----------------------------------------------------

async def process_user(username: str, args, out_dir: Path, ckpt: dict, items: list) -> dict:
    """Recover one user's clips. Updates the checkpoint entry; returns counts.

    Recovered (downloaded) and missing (no media) clips are tracked separately:
    recovered clips are always skipped on resume; missing clips are skipped too,
    *unless* --retry-misses asks to re-probe them. Errored clips are recorded in
    neither set, so they are retried next run; the error count is per-run (reset
    each pass) so a once-transient failure never poisons the user's status.
    """
    entry = ckpt.get(username.lower()) or {"username": username}
    recovered_set: set[str] = set(entry.get("recovered_feed_ids", []))
    missing_set: set[str] = set(entry.get("missing_feed_ids", []))

    log.info("→ %s: discovering archived clips…", username)
    data = await service.get_user_videos(username, refresh=args.refresh)
    if not data.get("found"):
        # Distinguish "no such handle" from a transient archive hiccup: never
        # discard progress we already have just because one scrape came back empty.
        if entry.get("recovered_feed_ids") or entry.get("missing_feed_ids"):
            log.warning("  %s: scrape returned nothing — keeping prior progress "
                        "(likely a transient archive hiccup)", username)
            ckpt[username.lower()] = entry
            return _entry_counts(entry)
        log.warning("  %s: nothing archived under this handle", username)
        entry.update(status="done", found=0, recovered_feed_ids=[], missing_feed_ids=[])
        ckpt[username.lower()] = entry
        return _entry_counts(entry)

    all_videos = data.get("videos", [])
    videos = all_videos[: args.limit] if args.limit else all_videos
    truncated = bool(args.limit) and args.limit < len(all_videos)
    log.info("  %s: %d clip(s) found (%d deleted)%s", username,
             len(all_videos), data.get("deleted_count", 0),
             f" — processing first {len(videos)}" if truncated else "")

    sem = asyncio.Semaphore(args.concurrency)
    user_dir = out_dir / (_safe_name(username) or "_user")
    errors = 0
    available = 0  # metadata-only matches (media exists but not downloaded)

    async def handle(v: dict) -> None:
        nonlocal errors, available
        feed_id = v["feed_id"]
        if feed_id in recovered_set:
            return  # already downloaded on a previous run
        if feed_id in missing_set and not args.retry_misses:
            return  # known to have no media; re-probe only when asked
        title = v.get("title") or feed_id
        async with sem:
            try:
                url = await service.resolve_stream(feed_id, retry_miss=args.retry_misses)
                if not url:
                    missing_set.add(feed_id)
                    items.append({"feed_id": feed_id, "username": username,
                                  "title": title, "status": "missing",
                                  "reason": "no archived media"})
                    log.info("  · no media: %s (%s)", feed_id, title)
                    return
                if args.metadata_only:
                    available += 1
                    missing_set.discard(feed_id)
                    items.append({"feed_id": feed_id, "username": username,
                                  "title": title, "status": "available", "url": url})
                    log.info("  ✓ available (metadata-only): %s", feed_id)
                    return  # NOT added to recovered_set → a real run still downloads it
                dest = user_dir / f"{feed_id}__{_safe_name(title)}.mp4"
                ok, info = await download_media(url, dest)
                if ok:
                    recovered_set.add(feed_id)
                    missing_set.discard(feed_id)
                    items.append({"feed_id": feed_id, "username": username,
                                  "title": title, "status": "recovered", "path": str(dest)})
                    log.info("  ✓ recovered: %s → %s", feed_id, dest.name)
                else:
                    errors += 1  # not recorded as done → retried next run
                    items.append({"feed_id": feed_id, "username": username,
                                  "title": title, "status": "error", "reason": info})
                    log.error("  ✗ %s: %s", feed_id, info)
            except Exception as e:  # noqa: BLE001 — log + continue, never crash the run
                errors += 1
                log.error("  ✗ %s: unexpected error: %s", feed_id, e)

    await asyncio.gather(*(handle(v) for v in videos))

    handled = recovered_set | missing_set
    all_handled = all(v["feed_id"] in handled for v in videos)
    # A user is only "done" when a full, error-free, real-download pass covered
    # every clip — so partial passes (truncated --limit / --metadata-only /
    # errors) stay resumable instead of being silently skipped later.
    complete = all_handled and errors == 0 and not args.metadata_only and not truncated
    entry.update(
        status="done" if complete else "partial",
        # `found` is the authoritative full-profile count; never let a --limit
        # slice shrink it below the accumulated recovered/missing sets.
        found=max(entry.get("found", 0), len(all_videos)),
        recovered_feed_ids=sorted(recovered_set),
        missing_feed_ids=sorted(missing_set),
    )
    ckpt[username.lower()] = entry
    return _entry_counts(entry, errors=errors, available=available)


# --- orchestration -----------------------------------------------------------

async def run(args) -> int:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(args.log_level, Path(args.log_file))

    try:
        users = read_user_list(args)
    except OSError as e:
        log.error("Couldn't read --users-file: %s", e)
        return 2
    if not users:
        log.error("No usernames given. Pass handles as arguments or --users-file.")
        return 2
    if args.concurrency < 1:
        log.error("--concurrency must be >= 1 (got %d)", args.concurrency)
        return 2
    if args.limit < 0:
        log.error("--limit must be >= 0 (got %d)", args.limit)
        return 2
    args.delay = max(0.0, args.delay)

    # Apply politeness / thoroughness overrides BEFORE any archive request.
    config.MAX_CONCURRENCY = args.concurrency
    config.REQUEST_DELAY = args.delay
    config.STREAM_RESOLVE_DEADLINE = args.resolve_timeout  # avoid false misses at low concurrency

    ckpt_path = Path(args.checkpoint)
    ckpt = load_checkpoint(ckpt_path)
    items: list = []
    started = time.time()
    log.info("Recovering %d user(s) → %s  (concurrency=%d, delay=%.2gs)",
             len(users), out_dir, args.concurrency, args.delay)

    totals = {"users_processed": 0, "users_skipped": 0, "users_failed": 0,
              "found": 0, "recovered": 0, "missing": 0, "errors": 0, "available": 0}

    def roll(counts: dict) -> None:
        for k in ("found", "recovered", "missing", "errors", "available"):
            totals[k] += counts.get(k, 0)

    try:
        for u in users:
            prev = ckpt.get(u.lower())
            if prev and prev.get("status") == "done" and not (args.refresh or args.retry_misses):
                log.info("⏭  %s: already done (%d recovered) — skipping",
                         u, len(prev.get("recovered_feed_ids", [])))
                totals["users_skipped"] += 1
                roll(_entry_counts(prev))
                continue
            try:
                roll(await process_user(u, args, out_dir, ckpt, items))
                totals["users_processed"] += 1
            except Exception as e:  # noqa: BLE001 — one bad user can't kill the run
                totals["users_failed"] += 1
                log.error("✗ %s: failed — %s", u, e)
            finally:
                save_checkpoint(ckpt_path, ckpt)  # checkpoint after every user
    finally:
        await wayback.close()

    elapsed = time.time() - started
    manifest = {
        "generated_at_unix": time.time(),
        "elapsed_seconds": round(elapsed, 1),
        "totals": totals,
        "users": {k: ckpt[k] for k in (u.lower() for u in users) if k in ckpt},
        "items": items,
    }
    manifest_path = out_dir / "manifest.json"
    _atomic_write_text(manifest_path, json.dumps(manifest, indent=2))

    log.info("")
    log.info("========== Recovery summary ==========")
    log.info("users processed : %d (skipped: %d, failed: %d)",
             totals["users_processed"], totals["users_skipped"], totals["users_failed"])
    log.info("clips found     : %d", totals["found"])
    log.info("recovered       : %d", totals["recovered"])
    if totals["available"]:
        log.info("available (meta): %d", totals["available"])
    log.info("missing (no media): %d", totals["missing"])
    log.info("errors          : %d", totals["errors"])
    log.info("elapsed         : %.1fs", elapsed)
    log.info("output          : %s", out_dir)
    log.info("manifest        : %s", manifest_path)
    log.info("checkpoint      : %s", ckpt_path)
    log.info("======================================")
    # Exit: 1 if a whole user failed, 3 if some clips errored (downloads), else 0.
    if totals["users_failed"]:
        return 1
    return 3 if totals["errors"] else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m app.scrape",
        description="Recover archived Plays.tv clips for one or more users from the Wayback Machine.",
    )
    p.add_argument("users", nargs="*", help="Plays.tv username(s) to recover")
    p.add_argument("--users-file", help="file with one username per line ('#' comments allowed)")
    p.add_argument("--out-dir", default=str(config.RECOVERED_DIR), help="where recovered .mp4s land")
    p.add_argument("--concurrency", type=int, default=3, help="max parallel archive requests (be polite: 1-3)")
    p.add_argument("--delay", type=float, default=0.5, help="polite pause (s) before each archive request")
    p.add_argument("--resolve-timeout", type=float, default=30.0,
                   help="per-clip media-probe deadline (s); higher avoids false misses at low concurrency")
    p.add_argument("--refresh", action="store_true", help="re-scrape user metadata, ignore cache freshness")
    p.add_argument("--retry-misses", action="store_true", help="re-probe clips previously found to have no media")
    p.add_argument("--metadata-only", action="store_true", help="discover + record media URLs, but don't download")
    p.add_argument("--limit", type=int, default=0, help="cap clips per user (0 = all; handy for testing)")
    p.add_argument("--checkpoint", default=str(config.SCRAPE_CHECKPOINT), help="resumable progress file (JSON)")
    p.add_argument("--log-file", default=str(config.SCRAPE_LOG), help="log file path")
    p.add_argument("--log-level", default="INFO", help="DEBUG | INFO | WARNING | ERROR")
    return p


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
