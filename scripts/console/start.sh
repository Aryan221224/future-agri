#!/usr/bin/env bash
# Start the Future Agri Mission Console on Linux or macOS: sets up .venv on the first run,
# then serves http://127.0.0.1:8099 and opens the browser. Ctrl+C stops it.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
VENV="$ROOT/.venv"
REQ="$ROOT/services/console/requirements.txt"
PY="${PYTHON:-python3}"
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || { echo "Python 3.10 or newer is needed."; exit 1; }
if [ ! -x "$VENV/bin/python" ]; then
  echo "First start: creating $VENV and installing packages (a few minutes)..."
  "$PY" -m venv "$VENV"
fi
STAMP="$VENV/.fa-requirements"
WANT="$(cat "$REQ" "$ROOT/scripts/console/requirements-worker.txt" | cksum)"
if [ "$(cat "$STAMP" 2>/dev/null)" != "$WANT" ]; then
  "$VENV/bin/pip" install -q --disable-pip-version-check --no-cache-dir -r "$REQ"
  echo "$WANT" > "$STAMP"
fi
[ -d "$ROOT/data_cache" ] && export FA_DATA_CACHE="${FA_DATA_CACHE:-$ROOT/data_cache}"
export PYTHONPATH="$ROOT/services/console"
exec "$VENV/bin/python" -m fa_console --open "$@"
