#!/bin/bash
# One-time setup: creates .venv and installs dependencies.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
if [ ! -x .venv/bin/python ]; then
  echo "Creating virtualenv..."
  "$PYTHON" -m venv .venv
fi
.venv/bin/pip install -q -U pip

# pymobiledevice3 needs `pylzss`, which compiles from source. Some Command Line Tools
# releases ship a linker that can't read the newest SDK; building against an older
# installed SDK avoids the "tapi error: unknown architecture" failure.
SDK_DIR=/Library/Developer/CommandLineTools/SDKs
if ! .venv/bin/pip install -q -r requirements.txt 2>/dev/null; then
  for sdk in "$SDK_DIR"/MacOSX26.5.sdk "$SDK_DIR"/MacOSX26.sdk "$SDK_DIR"/MacOSX15.sdk; do
    [ -d "$sdk" ] || continue
    echo "Retrying build against $(basename "$sdk")..."
    if SDKROOT="$sdk" ARCHFLAGS="-arch $(uname -m)" .venv/bin/pip install -q -r requirements.txt; then
      break
    fi
  done
fi
.venv/bin/python -c "import pymobiledevice3, fastapi" && echo "Setup complete. Start with: ./run.sh"
