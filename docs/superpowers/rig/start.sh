#!/usr/bin/env bash
# Bring up the full rig: fake-gcs-server and both backend servers.
# Idempotent — skips anything already running. Safe to re-run.
#
# The rig is db-less, mirroring production (DB retirement Phase 4, 2026-07-27):
# the only stateful component is the GCS emulator, which holds the Parquet
# tables, the state snapshot, and the site blobs.
set -euo pipefail

# Test the code we actually ship. RIG_BACKEND used to be hard-wired to
# .worktrees/rig/backend, a worktree pinned to a2cea55 — so the rig silently
# exercised a months-old snapshot instead of cph-staging, and nothing said so.
# Default to the repo root's backend (this file lives 3 levels under it), and
# resolve the interpreter from poetry so the venv tracks backend/pyproject.toml.
# Override either with RIG_BACKEND / RIG_VENV.
RIG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="${RIG_BACKEND:-$(cd "$RIG_DIR/../../../backend" && pwd)}"
if [ -n "${RIG_VENV:-}" ]; then
  VENV="$RIG_VENV"
elif [ -x "$BACKEND/.venv/bin/python" ]; then
  VENV="$BACKEND/.venv/bin/python"
else
  VENV="$( cd "$BACKEND" && poetry env info -p 2>/dev/null )/bin/python"
fi
if [ ! -x "$VENV" ]; then
  echo "ERROR: no usable interpreter for $BACKEND" >&2
  echo "  Tried \$RIG_VENV, $BACKEND/.venv, and 'poetry env info -p'." >&2
  echo "  Run 'cd $BACKEND && poetry install', or set RIG_VENV=/path/to/python." >&2
  exit 1
fi
echo "rig backend: $BACKEND"
echo "rig python:  $VENV"

# --- fake-gcs-server ---
if ! docker ps --format '{{.Names}}' | grep -q '^fake-gcs-rig$'; then
  if docker ps -a --format '{{.Names}}' | grep -q '^fake-gcs-rig$'; then
    docker start fake-gcs-rig
  else
    docker run -d --name fake-gcs-rig -p 4443:4443 \
      fsouza/fake-gcs-server:latest -scheme http -public-host localhost:4443
  fi
  sleep 2
fi
curl -s -X POST "http://localhost:4443/storage/v1/b?project=test" \
  -H "Content-Type: application/json" -d '{"name":"site_dev_v1"}' >/dev/null || true

# --- backend servers (8000 api/site, 8001 data) ---
mkdir -p "$RIG_DIR/logs"
set -a; source "$RIG_DIR/rig.env"; set +a
export PYTHONPATH="$BACKEND"
for port in 8000 8001; do
  if ! curl -s -o /dev/null "http://127.0.0.1:$port/"; then
    PORT=$port nohup "$VENV" "$RIG_DIR/serve.py" \
      > "$RIG_DIR/logs/server$port.log" 2>&1 &
    echo "started server on $port (pid $!)"
  fi
done
# Poll for readiness rather than guessing. A fixed `sleep 3` was not enough:
# importing the app pulls in duckdb + pyarrow and routinely takes longer, so
# the script would claim "rig up" while both ports still refused connections.
for port in 8000 8001; do
  for _ in $(seq 1 60); do
    curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$port/" && break
    sleep 1
  done
  if ! curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$port/"; then
    echo "ERROR: server on $port never came up — see $RIG_DIR/logs/server$port.log" >&2
    exit 1
  fi
done
echo "rig up (db-less): gcs=4443 api=8000 data=8001"
