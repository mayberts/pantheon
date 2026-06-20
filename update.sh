#!/usr/bin/env bash
# Pull the latest code and redeploy. Run on the Unraid host:
#   ./update.sh
set -euo pipefail
cd "$(dirname "$0")"

echo ">> pulling latest"
git pull --ff-only

echo ">> rebuilding and restarting"
docker compose up -d --build

echo ">> pruning dangling images"
docker image prune -f >/dev/null

echo ">> status"
docker compose ps
echo ">> done"
