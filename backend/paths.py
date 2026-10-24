"""Where LocationSpoof's files live, both in a source checkout and in a packaged app.

Running from source, everything stays in the checkout (web/ and data/). A packaged build
(PyInstaller sets sys.frozen) can't write inside its own bundle, so saved places, routes,
the tile cache and logs go to the per-user app data folder instead.
"""

import os
import sys
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)
IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

# Read-only files shipped with the app (PyInstaller unpacks them under sys._MEIPASS).
APP_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
WEB_DIR = APP_DIR / "web"


def _user_data_dir() -> Path:
    if IS_WINDOWS:
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif IS_MAC:
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "LocationSpoof"


if os.environ.get("LOCSPOOF_DATA_DIR"):
    DATA_DIR = Path(os.environ["LOCSPOOF_DATA_DIR"]).expanduser()
elif FROZEN:
    DATA_DIR = _user_data_dir()
else:
    DATA_DIR = APP_DIR / "data"
