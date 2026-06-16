#!/usr/bin/env bash
# Launch the Plays.tv Recovery site.
# First run sets up a virtualenv + installs deps; afterwards it just starts.
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "[*] Creating virtualenv…"
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip -q
  .venv/bin/pip install -r requirements.txt -q
fi

PORT="${PORT:-8765}"
echo "[*] Starting memoryTV on http://localhost:${PORT}"
echo "[*] Open that URL in your browser. Press Ctrl+C to stop."
exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port "$PORT"
