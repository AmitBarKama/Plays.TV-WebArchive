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

1. Start typing a username (e.g. `MLdini`). Matching users appear with their
   avatars — handy if you don't remember the exact handle.
2. Pick one. The first lookup for a user scrapes the archive (a few seconds);
   after that it's cached and instant.
3. Browse the recovered clips. Click any card to stream it, or hit **Download .mp4**.

## Status / known limitations

- ✅ Username autocomplete with avatars
- ✅ Video recovery across all archived snapshots (recovers deleted clips)
- ✅ Thumbnails, titles, games, dates, durations
- ⚠️ **Streaming/download depends on the actual `.mp4` being archived** — not
  every clip's video file was saved by the archive. Those play; others show a
  "not in the archive" notice. (Pulling the rest from the ArchiveTeam WARCs is
  a planned follow-up.)

## Layout

```
app/
  config.py     # tunables (concurrency, cache TTL, qualities)
  wayback.py    # async CDX + page-fetch client (retry/backoff, polite concurrency)
  parser.py     # JSON-LD + video-list parsing; username extraction
  cache.py      # SQLite cache (users, videos, resolved streams)
  service.py    # orchestration — the only module the API talks to
  main.py       # FastAPI routes + streaming proxy + static frontend
frontend/       # index.html, styles.css, app.js
data/           # cache.sqlite (created on first run)
```
