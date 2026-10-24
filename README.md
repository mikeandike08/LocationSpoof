# LocationSpoof

Spoof your iPhone's GPS location from your Mac or Windows PC using a local web page. It works over USB or Wi-Fi and needs no jailbreak and no app on the phone.

- **Teleport**: search any address or click the map.
- **Saved places**: save addresses and jump back to them. Search results are ranked by distance from you.
- **GPS-style routes**: type a start and destination. The phone drives the route along real roads at each road's **posted speed limit**, or a guessed limit where none is posted. Walk and bike modes use a fixed speed.
- **Saved routes**: saved with the full road geometry and speeds, so they load and play **without internet**. Map areas you've viewed are cached too.
- **Route preview**: play a route on the map without moving the phone (automatic when no iPhone is connected), at up to 20× speed, to check a route before driving it for real.
- **Arrow keys / WASD** nudge the location a few meters at a time.

It uses Apple's developer location simulation, the same mechanism Xcode uses, through [pymobiledevice3](https://github.com/doronz88/pymobiledevice3).

## Install

Download the installer from the [Releases](../../releases) page. Nothing else is needed, not even Python.

- **Windows:** run `LocationSpoof-Setup-<version>.exe`. It installs for your user account only (no admin prompt) and adds a Start menu shortcut. Because the installer isn't code-signed, SmartScreen may say "Windows protected your PC": click **More info → Run anyway**. You also need Apple's iPhone driver, the free **Apple Devices** app from the Microsoft Store (or iTunes); the installer offers a link if it's missing.
- **macOS (Apple Silicon):** open the `.dmg` and drag LocationSpoof to Applications. The app isn't notarized, so the first time, right-click it and choose **Open**, or allow it under System Settings → Privacy & Security → **Open Anyway**.

LocationSpoof runs in the background with an icon in the system tray (Windows) or menu bar (macOS). Click it to open the app in your browser. **Quit** in its menu stops the app and restores the phone's real location. The installed app keeps its data in `%APPDATA%\LocationSpoof` (Windows) or `~/Library/Application Support/LocationSpoof` (macOS).

## Run from source

**macOS**
```bash
./run.sh
```
Press **Ctrl+C** to quit.

**Windows**

1. Install [Python](https://www.python.org/downloads/) **3.13** (`winget install Python.Python.3.13`). Other versions don't work on Windows: on 3.12 and older the iOS 17.4+ tunnel depends on a package whose Windows build is broken, and 3.14 has no Windows build of `pylzss` yet. It can sit alongside other Python versions; setup finds it.
2. Install Apple's iPhone driver: the **Apple Devices** app from the Microsoft Store (or iTunes). Open it once with the iPhone plugged in so the phone gets recognized.
3. Double-click **`run.bat`**.

LocationSpoof runs in the background with an icon in the system tray. Click the icon to open the app; use **Quit** in its menu to stop. For a console with live logs, run `run.bat --console`.

Either way, the first run creates `.venv` and installs dependencies, then opens http://127.0.0.1:8765. No administrator password is needed: the iOS 17+ developer tunnel runs inside the app (pymobiledevice3's userspace tunnel). Logs go to `data/server.log`. Quitting restores the phone's real location.

The tray launcher also works on macOS (menu-bar icon): `.venv/bin/python -m backend.launcher`.

## First-time phone setup

1. Plug the iPhone in with a cable and unlock it.
2. In the Device card, click **Trust & prepare**, then tap **Trust** on the phone and enter your passcode.
3. If Developer Mode is off, click **Show toggle in Settings**. On the phone go to Settings → Privacy & Security → Developer Mode → On. The phone restarts; confirm **Turn On**. Then click **Prepare device** again.
4. The app mounts Apple's developer disk image. The first time, it downloads about 20 MB. When all four checks are green, you're ready.

### Wi-Fi
While on USB, click **Enable Wi-Fi**. After that the phone also shows up over Wi-Fi when it's unlocked and on the same network. You can unplug the cable, and a spoofed location or running route carries over to Wi-Fi automatically.

## How routes pick speeds

Routes come from [Valhalla](https://valhalla1.openstreetmap.de), a free public server run by FOSSGIS. For every stretch of road it returns the OpenStreetMap `maxspeed` (posted limit) and the road class.

| Speed mode | What it does |
|---|---|
| **Speed limits** | Drives at the posted limit × your percentage. Where no limit is posted, it guesses from the road type (residential 25 mph, primary 45 mph, motorway 65 mph, …). |
| **Realistic** | Uses Valhalla's own speed estimate, which is slower in town. |
| **Fixed** | One constant speed. Walk and bike always use this. |

The route summary shows what fraction of the route had a real posted limit. Playback runs on the server, so it keeps going if the browser tab is in the background. It speeds up and slows down smoothly, brakes ahead of slower roads, and adds a little speed variation so the movement looks natural.

## Notes and limits
- **iOS 17+ only holds the spoof while the app is running.** Keep the server running. If you quit or the phone disconnects for good, the phone returns to its real location (expected on modern iOS).
- iOS 16 and older work over USB without the tunnel.
- Free public services: OpenStreetMap tiles, Photon (suggestions as you type), Nominatim (Enter-key search and reverse lookup), Valhalla (routing), with OSRM as a fallback. Please keep usage personal and light. You can point the app at other servers with the `LOCSPOOF_VALHALLA_URL`, `LOCSPOOF_PHOTON_URL`, `LOCSPOOF_NOMINATIM_URL` and `LOCSPOOF_OSRM_URL` environment variables.
- The server only listens on `127.0.0.1`.
- Saved places live in `data/places.json`, saved routes in `data/routes.json`. Set `LOCSPOOF_DATA_DIR` to keep them somewhere else. A packaged build stores them in `~/Library/Application Support/LocationSpoof` (macOS) or `%APPDATA%\\LocationSpoof` (Windows).
- Your location (for "Your location" and nearby search) comes from the browser if you allow it, otherwise from your network (approximate, via ipinfo.io).

## Connection problems
The Device card shows a plain-English status line and a **Connection log** of recent events (disconnects, tunnel rebuilds, re-applied locations).

The app recovers on its own from:
- a developer tunnel that drops while the cable stays plugged in (it's rebuilt automatically, with backoff);
- a tunnel that exists but stops responding (it's replaced);
- an unplugged cable (the spoofed location is re-applied when the phone reconnects, and a running route holds its position until then);
- a phone restart (the developer disk image is re-mounted automatically).

## Offline use
Searching and building **new** routes need internet. Saved routes and saved places don't. The map background works offline for areas you've already viewed (tiles are cached in `data/tiles/`). The iPhone must stay connected to the computer (USB needs no Wi-Fi at all).

## Troubleshooting
- **Install fails with `tapi error: unknown architecture`**: your Command Line Tools linker is older than the newest SDK. `setup.sh` retries the build against an older SDK automatically. Updating the Command Line Tools also fixes it.
- **Device not listed**: unlock the phone, check that the cable carries data, and tap Trust.
- **Windows: "Apple's device service isn't running"**: install the Apple Devices app (Microsoft Store) or iTunes, open it once with the iPhone plugged in, and try again.
- **"The developer tunnel isn't up yet"**: keep the phone unlocked and check that Developer Mode is on; the app retries automatically.
- **Page says it can't reach the server**: check that only one copy of LocationSpoof is running (an old `sudo` copy from an earlier version will fight over the phone). Quit all copies and start again with `./run.sh` (or `run.bat`). Details are in `data/server.log`.

## Building the installers
Each installer has to be built on its own OS. Pushing a version tag builds both on GitHub Actions (`.github/workflows/installers.yml`) and attaches them to a **draft** release:
```bash
git tag v0.3.0
git push origin v0.3.0
```
You can also run the workflow by hand from the Actions tab. To build locally, from the project's venv:
```bash
pip install -r packaging/requirements-build.txt
python packaging/build.py
```
The result goes to `dist/installer/`. Windows needs [Inno Setup 6](https://jrsoftware.org/isinfo.php) (`winget install JRSoftware.InnoSetup`). Bump `backend/version.py` before tagging. To use your own app icon, add a 1024×1024 `packaging/icon.png`; otherwise the build draws the map pin the tray uses.

## Project layout
```
backend/
  main.py       FastAPI app + API routes
  launcher.py   tray / menu-bar launcher (runs the server in the background)
  paths.py      where web files and user data live (source checkout vs packaged app)
  drivelog.py   disconnect / lost-spoof log written during routes
  devices.py    discovery, pairing, Developer Mode, disk image, userspace developer tunnel
  location.py   holds the location channel open, auto-reconnects, re-applies the last fix
  playback.py   moves along a route with per-segment speeds
  routing.py    Valhalla/OSRM routing + speed limits
  geocode.py    address search
  places.py     saved places store
web/            single-page UI (Leaflet, no build step)
```
