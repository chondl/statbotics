#!/usr/bin/env bash
# Stop backend servers. The fake-gcs container is left running by default (the
# Parquet tables, snapshot, and blobs persist). Pass --docker to stop it too.
set -uo pipefail

pkill -f "docs/superpowers/rig/serve.py" && echo "stopped backend servers" || echo "no backend servers running"

if [[ "${1:-}" == "--docker" ]]; then
  docker stop fake-gcs-rig 2>/dev/null && echo "stopped fake-gcs container"
  echo "note: 'docker start fake-gcs-rig' restores it with data intact"
fi
