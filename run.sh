#!/bin/bash
# Starts LocationSpoof. Re-launches itself with sudo (needed for iOS 17+ device tunnels).
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8765}"

if [ ! -x .venv/bin/python ]; then
  ./setup.sh
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "LocationSpoof needs administrator rights to create device tunnels."
  exec sudo PORT="$PORT" "$0" "$@"
fi

URL="http://127.0.0.1:$PORT"
echo "LocationSpoof running at $URL  (Ctrl+C to quit and restore your real location)"
# Open the browser as the real user, not root, once the server is up.
(
  for _ in $(seq 1 40); do
    curl -s -o /dev/null "$URL/api/status" && break
    sleep 0.25
  done
  if [ -n "${SUDO_USER:-}" ]; then sudo -u "$SUDO_USER" open "$URL"; else open "$URL"; fi
) &

exec .venv/bin/python -m backend.main --port "$PORT"
