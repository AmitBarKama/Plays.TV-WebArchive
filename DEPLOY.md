# Deploying memoryTV

memoryTV deploys as a **split**:

- **Backend** (FastAPI recovery engine + cache + video) → a small **always-on host**
  (Railway recommended; Fly.io / Render work too). This is where the real work happens.
- **Frontend** (static `frontend/`) → **Vercel** (free CDN), proxying `/api/*` to the backend.

> Why not all-on-Vercel? Vercel is serverless: `/tmp` is wiped between requests, background
> downloads don't run, and proxying video burns function time/bandwidth. The recovery engine
> needs a persistent cache + background work, so it lives on an always-on host. See
> `_bmad-output/planning-artifacts/recovery-server-spec.md`.

The backend **also serves the frontend on its own**, so you can ship just the backend first and
have a fully working site at the backend URL — Vercel is an optional nicety on top.

---

## 1. Backend on Railway (recommended)

1. Push this repo to GitHub.
2. [railway.app](https://railway.app) → **New Project → Deploy from GitHub repo** → pick this repo.
   Railway detects `Dockerfile`/`railway.json` and builds automatically.
3. **Add a Volume** (Railway → service → *Variables/Volumes* → New Volume), mount path **`/data`**.
   This persists the SQLite cache + downloaded clips across deploys.
4. **Set env vars** (Railway → Variables):
   - `MEMORYTV_DATA_DIR=/data`
   - (optional) `MEMORYTV_ALLOWED_ORIGINS=https://your-frontend.vercel.app`
   - `PORT` is provided by Railway automatically.
5. Deploy. Health check is `/healthz`. Your backend is now live at
   `https://<name>.up.railway.app` — open it; the full app works there already.

### Fly.io (alternative)
`fly.toml` is included. `fly launch --no-deploy` (creates app + the `/data` volume), then `fly deploy`.

### Render (alternative)
New Web Service from the repo, Docker runtime; add a Disk mounted at `/data`; set `MEMORYTV_DATA_DIR=/data`.

---

## 2. Frontend on Vercel (optional)

1. Edit **`vercel.json`** → replace `REPLACE-WITH-YOUR-BACKEND.up.railway.app` with your real
   backend host (from step 1.5).
2. [vercel.com](https://vercel.com) → **Add New → Project** → import this repo.
   - Framework preset: **Other**. `vercel.json` already sets `outputDirectory: frontend` and the
     `/api/*` rewrite, so no build step is needed.
3. Deploy. The static UI is served from Vercel's CDN; all `/api/*` calls (and video) are proxied
   to your backend. No CORS or code changes required.

> Note: with the rewrite, video streams *through* Vercel (counts against Vercel bandwidth). Fine for
> low traffic; the documented next step (Cloudflare R2 + CDN) moves bytes off both Vercel and the
> archive — see the planning artifacts.

---

## 3. Local development

```bash
cd playstv-recovery
./run.sh                 # creates .venv, installs, serves on http://localhost:8765
# or: PORT=8765 uvicorn app.main:app --reload
```
The backend serves the frontend at `/`, so local dev is single-origin (no CORS needed).

---

## 4. Run it as a container locally (sanity-check the image)

```bash
docker build -t memorytv .
docker run -p 8000:8000 -v "$PWD/data:/data" memorytv
# open http://localhost:8000
```

---

## Operational notes
- **`/healthz`** → liveness probe + the recovery canary target.
- **`/robots.txt`** + `X-Robots-Tag: noindex` are set everywhere on purpose: memoryTV re-serves
  third-party clips and must not become a name-searchable index (consent guardrail). Keep them.
- **Removal/takedown:** publish a contact and honor removals fast (see the consent guardrails in
  `recovery-server-spec.md`). A first-class takedown endpoint is on the roadmap.
- **Scaling cost:** today the cache is the mounted volume + live archive backfill. The planned
  upgrade (Cloudflare R2 zero-egress + Turso) keeps it ~free while removing the archive from the
  hot path — see `_bmad-output/planning-artifacts/`.
