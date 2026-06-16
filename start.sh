#!/usr/bin/env bash
#
# start.sh — bring the Team server up with one command.
#
#   ./start.sh                 # serve on the configured host/port (default 127.0.0.1:8000)
#   TEAM_PORT=9000 ./start.sh  # override via any TEAM_* env var (see app/config/settings.py)
#
# Self-bootstrapping: creates .venv and installs deps on first run, then serves
# the real app (real claude/codex adapters) via `python -m app.main`.

set -euo pipefail

# Always operate from the repo root (the dir this script lives in).
cd "$(dirname "$0")"

VENV=".venv"
PYTHON="${PYTHON:-python3}"

# 1. Ensure the virtualenv exists.
if [ ! -x "$VENV/bin/python" ]; then
  echo "[team] creating virtualenv in $VENV ..."
  "$PYTHON" -m venv "$VENV"
fi

VENV_PY="$VENV/bin/python"

# 2. Ensure dependencies are installed (cheap import probe; install on miss).
if ! "$VENV_PY" -c "import fastapi, uvicorn, app" >/dev/null 2>&1; then
  echo "[team] installing dependencies ..."
  "$VENV_PY" -m pip install --quiet --upgrade pip
  "$VENV_PY" -m pip install --quiet -e .
fi

# 3. Serve. main() prints the URL and binds to the loopback host/port.
echo "[team] starting server (Ctrl-C to stop) ..."
exec "$VENV_PY" -m app.main
