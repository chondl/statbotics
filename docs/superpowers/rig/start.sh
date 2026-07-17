#!/usr/bin/env bash
# Bring up the full rig: CockroachDB, fake-gcs-server, and both backend servers.
# Idempotent — skips anything already running. Safe to re-run.
set -euo pipefail

RIG_DIR="/Users/chondl/learn/statbotics/docs/superpowers/rig"
BACKEND="/Users/chondl/learn/statbotics/.worktrees/rig/backend"
VENV="$BACKEND/.venv/bin/python"

# --- CockroachDB (single node, insecure) ---
if ! docker ps --format '{{.Names}}' | grep -q '^crdb-rig$'; then
  if docker ps -a --format '{{.Names}}' | grep -q '^crdb-rig$'; then
    docker start crdb-rig
  else
    docker run -d --name crdb-rig -p 26257:26257 -p 8080:8080 \
      cockroachdb/cockroach:latest-v23.2 start-single-node --insecure
  fi
  sleep 4
fi
docker exec crdb-rig cockroach sql --insecure \
  -e "CREATE DATABASE IF NOT EXISTS statbotics3;" >/dev/null
# Large upserts (honest-diff writes many big team_years rows) exceed the default
# 16 MiB per-message buffer on a single node; raise it.
docker exec crdb-rig cockroach sql --insecure \
  -e "SET CLUSTER SETTING sql.conn.max_read_buffer_message_size = '64MiB';" >/dev/null

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
sleep 3
echo "rig up: crdb=26257 gcs=4443 api=8000 data=8001"
