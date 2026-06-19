# Backend container — runs the FastAPI recovery engine + serves the frontend.
# Works on Railway, Fly.io, Render, or any container host.
FROM python:3.12-slim

WORKDIR /app

# Deps first for layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App + frontend (backend serves the static UI too, so it runs standalone).
COPY app ./app
COPY frontend ./frontend

# Persistent state goes on a mounted volume in production. On Railway, attach a
# Volume at /data in the dashboard (the Docker VOLUME instruction is unsupported
# there); the app creates /data itself if it's missing. See DEPLOY.md.
ENV MEMORYTV_DATA_DIR=/data

# Hosts inject $PORT; default to 8000 for local `docker run`.
ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
