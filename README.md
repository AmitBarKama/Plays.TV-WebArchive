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

## Status / known limitations

- ⚠️ **The original `.mp4` files are generally not publicly archived.** The
  Wayback Machine captured Plays.tv profile pages, metadata and thumbnails — but
  not the video binaries (CDX returns no `.mp4` captures). The ArchiveTeam
  `archiveteam_playstv` WARC collection may hold them, but those items are
  access-restricted (HTTP 401).
- As a result, most clips show a "the archive preserved this clip's details and
  thumbnail, but not the original video file" notice with a link to the original
  page. The **streaming/caching layer is complete** and kicks in automatically
  the moment any clip's video URL resolves (e.g. if ArchiveTeam access opens up).

## Layout

```
app/
  config.py      # tunables (concurrency, cache TTL, qualities, cache dirs)
  wayback.py     # async CDX + page-fetch client (retry/backoff, polite concurrency)
  parser.py      # JSON-LD + video-list parsing; username + follower/following lists
  cache.py       # SQLite cache (users, videos, streams, connections)
  videocache.py  # on-demand download-to-temp video cache (serve locally, clearable)
  service.py     # orchestration — the only module the API talks to
  main.py        # FastAPI routes + streaming + static frontend
frontend/        # index.html, styles.css, app.js (vanilla JS, no build step)
data/            # cache.sqlite + video_cache/ (created at runtime; git-ignored)
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
