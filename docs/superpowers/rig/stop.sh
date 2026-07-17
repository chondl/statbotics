#!/usr/bin/env bash
# Stop backend servers. Docker containers are left running by default (data
# persists). Pass --docker to also stop the CockroachDB + fake-gcs containers.
set -uo pipefail

pkill -f "docs/superpowers/rig/serve.py" && echo "stopped backend servers" || echo "no backend servers running"

if [[ "${1:-}" == "--docker" ]]; then
  docker stop crdb-rig fake-gcs-rig 2>/dev/null && echo "stopped docker containers"
  echo "note: 'docker start crdb-rig fake-gcs-rig' restores them with data intact"
fi
