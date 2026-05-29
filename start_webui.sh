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

export XENON_WEBUI_HOST="${XENON_WEBUI_HOST:-127.0.0.1}"
export XENON_WEBUI_PORT="${XENON_WEBUI_PORT:-8000}"
export XENON_WEBUI_PREWARM="${XENON_WEBUI_PREWARM:-auto}"
export XENON_WEBUI_FILE_WATCHER="${XENON_WEBUI_FILE_WATCHER:-auto}"
export XENON_WEBUI_BACKGROUND_THEME="${XENON_WEBUI_BACKGROUND_THEME:-1}"

echo "Starting Xenon Web UI on http://${XENON_WEBUI_HOST}:${XENON_WEBUI_PORT}"
echo "Local check endpoint: http://127.0.0.1:${XENON_WEBUI_PORT}/health"
exec "$PYTHON" webui/main.py
