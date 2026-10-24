"""Desktop launcher: runs the server in the background behind a tray / menu-bar icon.

    python -m backend.launcher            (pythonw on Windows: no console window)

The icon's menu opens the app in the browser, opens the drive log or the data folder, and quits.
Quitting shuts the server down cleanly, which gives the iPhone its real location back.
If LocationSpoof is already running, this just opens it in the browser.
"""

import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser

# pythonw (Windows, no console) has no stdout/stderr; give stray writes somewhere to go.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import uvicorn  # noqa: E402

from backend import main as server_main  # noqa: E402
from backend.paths import DATA_DIR, IS_MAC, IS_WINDOWS  # noqa: E402

logger = logging.getLogger("locationspoof.launcher")

DEFAULT_PORT = 8765
STARTUP_TIMEOUT_S = 30
SHUTDOWN_TIMEOUT_S = 20


def _is_locationspoof(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=1) as resp:
            return "admin_required" in json.loads(resp.read())
    except Exception:
        return False


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _open_path(path) -> None:
    path = str(path)
    if IS_WINDOWS:
        os.startfile(path)  # noqa: S606 - opens in Explorer
    elif IS_MAC:
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def _icon_image():
    """A map-pin icon drawn in code, so no image file has to ship with the app."""
    from PIL import Image, ImageDraw

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    blue = (37, 99, 235, 255)
    d.ellipse((12, 4, 52, 44), fill=blue)
    d.polygon([(16, 32), (48, 32), (32, 62)], fill=blue)
    d.ellipse((24, 16, 40, 32), fill=(255, 255, 255, 255))
    return img


class Server:
    """uvicorn in a background thread (uvicorn skips signal handlers off the main thread)."""

    def __init__(self, port: int) -> None:
        config = uvicorn.Config(server_main.app, host="127.0.0.1", port=port, log_level="warning", loop="asyncio")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, name="locationspoof-server", daemon=True)

    def start(self) -> bool:
        self.thread.start()
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.server.started:
                return True
            if not self.thread.is_alive():
                return False
            time.sleep(0.1)
        return False

    def stop(self) -> None:
        # Lets the app's shutdown hook run: stops any route and clears the spoofed location.
        self.server.should_exit = True
        self.thread.join(SHUTDOWN_TIMEOUT_S)


def _run_tray(url: str, server: Server) -> None:
    import pystray

    def open_app(*_):
        webbrowser.open(url)

    def open_log(*_):
        webbrowser.open(f"{url}/drive-log.txt")

    def open_data(*_):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _open_path(DATA_DIR)

    def quit_app(icon, _item):
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open LocationSpoof", open_app, default=True),
        pystray.MenuItem("Open drive log", open_log),
        pystray.MenuItem("Open data folder", open_data),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit (restores real location)", quit_app),
    )
    icon = pystray.Icon("LocationSpoof", _icon_image(), f"LocationSpoof ({url})", menu)
    icon.run()  # blocks until Quit; must be on the main thread (required on macOS)


def main() -> None:
    parser = argparse.ArgumentParser(description="Start LocationSpoof with a tray icon")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true", help="don't open the browser on start")
    parser.add_argument("--no-tray", action="store_true", help="run without a tray icon (Ctrl+C to quit)")
    args = parser.parse_args()

    if _is_locationspoof(args.port):
        webbrowser.open(f"http://127.0.0.1:{args.port}")
        return
    port = next((p for p in range(args.port, args.port + 10) if _port_free(p)), None)
    if port is None:
        sys.exit(f"Ports {args.port}-{args.port + 9} are all in use")

    server_main._setup_file_log()
    server_main._raise_open_file_limit()
    url = f"http://127.0.0.1:{port}"
    server = Server(port)
    if not server.start():
        logger.error("server failed to start; see %s", DATA_DIR / "server.log")
        sys.exit(1)
    logger.info("LocationSpoof running at %s", url)
    if not args.no_browser:
        webbrowser.open(url)

    try:
        if args.no_tray:
            raise ImportError("tray disabled")
        _run_tray(url, server)
    except ImportError:
        print(f"LocationSpoof running at {url}  (Ctrl+C to quit and restore your real location)")
        try:
            while server.thread.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
    finally:
        server.stop()


if __name__ == "__main__":
    main()
