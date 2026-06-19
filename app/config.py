"""Central configuration for the Plays.tv recovery service."""
import os
from pathlib import Path

# Where persistent state lives. Override with MEMORYTV_DATA_DIR in production
# (e.g. a mounted Railway/Fly volume at /data) so the SQLite cache + downloaded
# clips survive restarts and redeploys.
DATA_DIR = Path(os.environ.get("MEMORYTV_DATA_DIR")
                or Path(__file__).resolve().parent.parent / "data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "cache.sqlite"

# On-demand video cache: clips are downloaded here on first play, then served
# locally (instant seeking + replay). Wiped manually from the site settings.
VIDEO_CACHE_DIR = DATA_DIR / "video_cache"
VIDEO_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# Bulk CLI recovery scraper (app/scrape.py): where recovered .mp4s land, plus
# its resumable checkpoint and log. All overridable via CLI flags.
RECOVERED_DIR = DATA_DIR / "recovered"
SCRAPE_CHECKPOINT = DATA_DIR / "scrape_checkpoint.json"
SCRAPE_LOG = DATA_DIR / "scrape.log"

# Wayback Machine endpoints.
CDX_API = "https://web.archive.org/cdx/search/cdx"
WAYBACK = "https://web.archive.org/web"

# Politeness / reliability knobs for hitting the archive.
MAX_CONCURRENCY = 8          # simultaneous requests to web.archive.org
REQUEST_TIMEOUT = 20.0       # seconds per request (so a slow capture can't hang us)
MAX_RETRIES = 2              # retries on 429/5xx (kept low so failures surface fast)
BACKOFF_BASE = 0.8           # exponential backoff base (seconds)
MAX_RETRY_AFTER = 60.0       # cap on an honoured Retry-After (don't hold a slot forever)
REQUEST_DELAY = 0.0          # optional polite pause before each request; the web
                             # app leaves this at 0, the bulk CLI raises it.

# Hard ceiling on how long we'll hunt for a video file before giving up.
# (Most Plays.tv video files were never archived, so keep this short.)
STREAM_RESOLVE_DEADLINE = 8.0   # seconds

# Autocomplete is best-effort; if CDX is slow for a common prefix, give up and
# let the user just open exactly what they typed. CDX latency swings a lot, so
# allow headroom — the UI streams similar names in when they arrive and always
# offers "open exactly what you typed" immediately, so the wait never blocks.
SEARCH_DEADLINE = 10.0   # seconds

USER_AGENT = (
    "Mozilla/5.0 (compatible; PlaysTV-Recovery/1.0; "
    "+archival recovery of public Plays.tv gameplay clips)"
)

# How long a cached user video-index stays fresh before we re-scrape (seconds).
INDEX_TTL = 60 * 60 * 24 * 7   # 1 week

# CORS: comma-separated allowed origins for when the frontend is hosted
# separately (e.g. on Vercel). Defaults to "*" — the API serves only public,
# already-archived data and uses no cookies/credentials.
ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get("MEMORYTV_ALLOWED_ORIGINS", "*").split(",")
    if o.strip()
] or ["*"]

# Video qualities to try, best first.
QUALITIES = ["1080", "720", "480", "360", "240"]
