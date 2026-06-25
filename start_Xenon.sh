#!/usr/bin/env sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$ROOT_DIR"

if [ -z "${PYTHON:-}" ]; then
  if [ -x "$ROOT_DIR/venv/bin/python" ]; then
    PYTHON="$ROOT_DIR/venv/bin/python"
  else
    PYTHON="python3"
  fi
fi

echo "Starting Xenon (launcher)…"
exec "$PYTHON" launcher.py
