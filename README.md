# memoryTV — Plays.tv clip recovery

Plays.tv shut down in December 2019. This site lets you type a username and
recover their gameplay clips — **including ones that were deleted** — straight
from the Internet Archive. No Selenium, no browser automation: discovery runs
through the Wayback **CDX API** and pages are parsed over plain HTTP, with
results cached in SQLite.

## Run it

```bash
cd playstv-recovery
./run.sh
```

First launch creates a virtualenv and installs dependencies (FastAPI, httpx,
uvicorn), then starts the server. When it's up, open:

```
http://localhost:8765
```

To use a different port: `PORT=9000 ./run.sh`

### Manual start (if you prefer)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload --port 8765
```

## How to use

1. Start typing a username (e.g. `MLdini`). Matching/similar handles appear with
   their avatars — handy if you don't remember the exact spelling.
2. Pick one. The first lookup for a user scrapes the archive (a few seconds);
   after that it's cached and instant.
3. Browse the recovered clips — **filter** by game or deleted-only and **sort**
   by date or length. Click a card to open the player; use **‹ ›** or the arrow
   keys to move between clips.
4. Open a profile's **Followers / Following** and click any name to jump to
   that person's clips.

## Features

- ✅ Username search with similar-name suggestions and avatars
- ✅ Video recovery across all archived snapshots (recovers deleted clips)
- ✅ Thumbnails, titles, games, dates, durations
- ✅ Filter (game / deleted-only) and sort (newest, oldest, longest, shortest)
- ✅ In-player previous/next navigation (buttons + arrow keys)
- ✅ Shareable deep links — `#user` and `#user/<feed_id>` open straight to a clip
- ✅ Followers / following lists, clickable through to each user's profile
- ✅ On-demand video cache: clips are downloaded to a temp folder on first play
  and served locally afterwards (instant seeking + replay); wipe it any time
  from the in-app **Settings** (⚙)

## Bulk recovery (CLI)

To recover clips for specific users in batch — resumable, polite, logged — use
the scraper:

```bash
./scrape.sh <username> [more usernames…] [options]
./scrape.sh --users-file handles.txt --out-dir data/recovered
```

For each user it discovers the archived clips, probes the Wayback Machine for
each clip's `.mp4`, and downloads the ones that survive — logging the (many)
misses, never crashing. Progress is **checkpointed to disk**, so a re-run skips
work already done and doesn't re-download.

Useful flags: `--concurrency N` (default 3 — keep it low and polite), `--delay S`
(pause before each archive request, default 0.5s), `--limit N` (cap clips/user),
`--metadata-only` (record media URLs without downloading), `--refresh` (ignore
cache freshness), `--retry-misses` (re-probe clips previously found to have no
media). Recovered files land in `--out-dir` (default `data/recovered/`) with a
`manifest.json` summary; logs go to `--log-file` (default `data/scrape.log`).

## Status / what survives

- ✅ Profile pages, metadata, thumbnails, and follower/following lists are
  reliably archived.
- ⚠️ **Video files: a minority survive.** Most clips' `.mp4`s were never captured,
  but some *are* archived and fetchable from the Wayback Machine (verified — real,
  playable MP4s recovered). The app probes `…/processed/{quality}.mp4` across
  qualities and both CDN shards; recovered clips play and download, while the rest
  show a "thumbnail preserved, original video not archived" notice. **Expect a low
  hit rate — mostly gaps.**
- ArchiveTeam's `archiveteam_playstv` WARCs may hold more, but those items are
  access-restricted (HTTP 401), so the public Wayback Machine is the source.

## Layout

```
app/
  config.py      # tunables (concurrency, cache TTL, qualities, cache dirs)
  wayback.py     # async CDX + page-fetch client (retry/backoff, polite concurrency)
  parser.py      # JSON-LD + video-list parsing; username + follower/following lists
  cache.py       # SQLite cache (users, videos, streams, connections)
  videocache.py  # on-demand download-to-temp video cache (serve locally, clearable)
  service.py     # orchestration — the web app + CLI both talk to this
  scrape.py      # resumable bulk recovery CLI (python -m app.scrape)
  main.py        # FastAPI routes + streaming + static frontend
frontend/        # index.html, styles.css, app.js (vanilla JS, no build step)
run.sh           # start the web app          scrape.sh  # run the bulk scraper
data/            # cache.sqlite + video_cache/ + recovered/ (runtime; git-ignored)
```

## API

| Method | Route | Purpose |
|---|---|---|
| GET | `/api/search?q=` | Username suggestions (prefix + cached) |
| GET | `/api/user/{name}/header` | Lightweight avatar + display name |
| GET | `/api/user/{name}/videos` | Recovered clip list |
| GET | `/api/user/{name}/followers` | Followers list |
| GET | `/api/user/{name}/following` | Following list |
| GET | `/api/stream/{feed_id}` | Stream a clip (cache-first, Range support); `?dl=1` to download |
| GET | `/api/cache` | Video cache stats |
| DELETE | `/api/cache` | Wipe the video cache |
