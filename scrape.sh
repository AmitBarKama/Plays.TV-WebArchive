#!/usr/bin/env bash
# Run the bulk Plays.tv recovery scraper inside the project virtualenv.
# First run sets up the venv (shared with run.sh); afterwards it just runs.
#
# Examples:
#   ./scrape.sh MLdini
#   ./scrape.sh MLdini SomeOtherUser --concurrency 2 --delay 0.5
#   ./scrape.sh --users-file handles.txt --out-dir data/recovered
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "[*] Creating virtualenv…"
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip -q
  .venv/bin/pip install -r requirements.txt -q
fi

exec .venv/bin/python -m app.scrape "$@"
