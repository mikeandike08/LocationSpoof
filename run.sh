#!/bin/bash
# Starts LocationSpoof. No administrator rights needed: the iOS 17+ developer tunnel runs in userspace.
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8765}"

if [ ! -x .venv/bin/python ]; then
  ./setup.sh
fi

URL="http://127.0.0.1:$PORT"
echo "LocationSpoof running at $URL  (Ctrl+C to quit and restore your real location)"
echo "Log file: data/server.log"
# Open the browser once the server is up.
(
  for _ in $(seq 1 40); do
    curl -s -o /dev/null "$URL/api/status" && break
    sleep 0.25
  done
  open "$URL"
) &

exec .venv/bin/python -m backend.main --port "$PORT"
