# LocationSpoof

Spoof your iPhone's GPS location from your Mac or Windows PC using a local web page. It works over USB or Wi-Fi and needs no jailbreak and no app on the phone.

**[⬇ Download the latest version](https://github.com/mikeandike08/LocationSpoof/releases/latest)**: Windows installer and macOS disk image. See [Releases](#releases) for which file to pick.

- **Teleport**: search any address or click the map.
- **Saved places**: save addresses and jump back to them. Search results are ranked by distance from you.
- **GPS-style routes**: type a start and destination. The phone drives the route along real roads at each road's **posted speed limit**, or a guessed limit where none is posted. Walk and bike modes use a fixed speed.
- **Stays at the destination**: when a route finishes, the destination is re-applied every minute until you stop it.
- **Saved routes**: saved with the full road geometry and speeds, so they load and play **without internet**. Map areas you've viewed are cached too.
- **Route preview**: play a route on the map without moving the phone (automatic when no iPhone is connected), at up to 20× speed, to check a route before driving it for real.
- **Disconnect log**: for long or overnight drives. A plain-text log of every time the iPhone disconnects or drops back to its real location, how long it took to recover, and when the route arrived.
- **Arrow keys / WASD** nudge the location a few meters at a time.

It uses Apple's developer location simulation, the same mechanism Xcode uses, through [pymobiledevice3](https://github.com/doronz88/pymobiledevice3).

## Install

Download the installer for your computer from the [latest release](https://github.com/mikeandike08/LocationSpoof/releases/latest). Nothing else is needed, not even Python.

### Windows
1. Run `LocationSpoof-Setup-<version>.exe`. It installs for your user account only (no admin prompt) and adds a Start menu shortcut.
   - The installer isn't code-signed, so SmartScreen may say "Windows protected your PC". Click **More info → Run anyway**.
2. Install Apple's iPhone driver if you don't have it: the free **Apple Devices** app from the Microsoft Store, or iTunes. The installer offers a link if it's missing. Open it once with the iPhone plugged in.

### macOS (Apple Silicon)
1. Open `LocationSpoof-<version>-macOS-arm64.dmg` and drag LocationSpoof to **Applications**.
2. The app isn't notarized. The first time, right-click it and choose **Open**, or allow it under System Settings → Privacy & Security → **Open Anyway**.

Intel Macs aren't supported by the installer; [run from source](#run-from-source) instead.

### Using the app
- LocationSpoof runs in the background with a map-pin icon in the **system tray** (Windows, possibly under the **^** arrow) or the **menu bar** (macOS). It opens in your browser.
- **Closing the browser does not stop it.** That's on purpose: a running route or spoofed location keeps going. To get back to the page, click the tray icon or start LocationSpoof again from the Start menu or Applications.
- **To stop it**, choose **Quit** in the tray / menu-bar icon's menu. Quitting gives the phone its real location back.
- The installed app keeps its data (saved places, routes, map cache, logs) in `%APPDATA%\LocationSpoof` (Windows) or `~/Library/Application Support/LocationSpoof` (macOS). The tray menu's **Open data folder** takes you there.

### Upgrading and uninstalling
- **Upgrade:** quit LocationSpoof from the tray first, then run the new installer (Windows) or drag the new app over the old one (macOS). Saved places and routes are kept. If the Windows installer says LocationSpoof is still running, it can close it for you, but quitting first is better: it makes sure the phone gets its real location back.
- **Uninstall:** Windows Settings → Apps → LocationSpoof → Uninstall, or on macOS drag the app to the Trash. Your data folder is left in place; delete it too for a clean removal.

## First-time phone setup

1. Plug the iPhone in with a cable and unlock it.
2. In the Device card, click **Trust & prepare**, then tap **Trust** on the phone and enter your passcode.
3. If Developer Mode is off, click **Show toggle in Settings**. On the phone go to Settings → Privacy & Security → Developer Mode → On. The phone restarts; confirm **Turn On**. Then click **Prepare device** again.
4. The app mounts Apple's developer disk image. The first time, it downloads about 20 MB. When all four checks are green, you're ready.

### Wi-Fi
While on USB, click **Enable Wi-Fi**. After that the phone also shows up over Wi-Fi when it's unlocked and on the same network. You can unplug the cable, and a spoofed location or running route carries over to Wi-Fi automatically.

## Disconnect log (long and overnight drives)

On the Route card, click **● Start disconnect log** before pressing Play. The log only gets an entry when something happens during a route:

| Entry | Meaning |
|---|---|
| `DISCONNECTED` / `RECONNECTED` | The iPhone dropped off USB/Wi-Fi, and how long until it came back. |
| `REAL LOCATION` / `SPOOF RESTORED` | The phone stopped receiving the spoof (so it likely showed its real location), and how long until it was re-applied. |
| `ARRIVED` | The route reached its destination: distance, time taken, and how many incidents it had. |
| `UNRESOLVED` | A problem was still open when the route was stopped or logging was turned off. |

Open it with **Open log file** on the Route card or **Open drive log** in the tray menu. It's saved as `drive-log.txt` in the data folder.

The computer can't read the iPhone's actual GPS position, so a "real location" entry is detected from its cause: on iOS 17+ the phone falls back to its real location whenever the location-simulation link breaks, and every such break is logged.

## How routes pick speeds

Routes come from [Valhalla](https://valhalla1.openstreetmap.de), a free public server run by FOSSGIS. For every stretch of road it returns the OpenStreetMap `maxspeed` (posted limit) and the road class.

| Speed mode | What it does |
|---|---|
| **Speed limits** | Drives at the posted limit × your percentage. Where no limit is posted, it guesses from the road type (residential 25 mph, primary 45 mph, motorway 65 mph, …). |
| **Realistic** | Uses Valhalla's own speed estimate, which is slower in town. |
| **Fixed** | One constant speed. Walk and bike always use this. |

The route summary shows what fraction of the route had a real posted limit. Playback runs in the app, not the browser, so it keeps going when the tab is in the background or closed. It speeds up and slows down smoothly, brakes ahead of slower roads, and adds a little speed variation so the movement looks natural.

## Notes and limits
- **iOS 17+ only holds the spoof while the app is running.** Keep the app running. If you quit or the phone disconnects for good, the phone returns to its real location (expected on modern iOS).
- iOS 16 and older work over USB without the tunnel.
- Free public services: OpenStreetMap tiles, Photon (suggestions as you type), Nominatim (Enter-key search and reverse lookup), Valhalla (routing), with OSRM as a fallback. Please keep usage personal and light. You can point the app at other servers with the `LOCSPOOF_VALHALLA_URL`, `LOCSPOOF_PHOTON_URL`, `LOCSPOOF_NOMINATIM_URL` and `LOCSPOOF_OSRM_URL` environment variables.
- The app only listens on `127.0.0.1`, so other devices on your network can't reach it.
- Data location: the installed app uses the per-user folder above. Running from source, it uses `data/` in the checkout. Set `LOCSPOOF_DATA_DIR` to keep it somewhere else.
- Your location (for "Your location" and nearby search) comes from the browser if you allow it, otherwise from your network (approximate, via ipinfo.io).

## Connection problems
The Device card shows a plain-English status line and a **Connection log** of recent events (disconnects, tunnel rebuilds, re-applied locations).

The app recovers on its own from:
- a developer tunnel that drops while the cable stays plugged in (it's rebuilt automatically, with backoff);
- a tunnel that exists but stops responding (it's replaced);
- an unplugged cable (the spoofed location is re-applied when the phone reconnects, and a running route holds its position until then);
- a phone restart (the developer disk image is re-mounted automatically).

## Offline use
Searching and building **new** routes need internet. Saved routes and saved places don't. The map background works offline for areas you've already viewed. The iPhone must stay connected to the computer (USB needs no Wi-Fi at all).

## Troubleshooting
- **Device not listed**: unlock the phone, check that the cable carries data, and tap Trust.
- **Windows: "Apple's device service isn't running"**: install the Apple Devices app (Microsoft Store) or iTunes, open it once with the iPhone plugged in, and try again.
- **"The developer tunnel isn't up yet"**: keep the phone unlocked and check that Developer Mode is on; the app retries automatically.
- **The app doesn't start, or shows an error box**: the error is also saved as `crash.log` in the data folder. Please include it when reporting a problem.
- **Page says it can't reach the server**: the app isn't running. Start it again from the Start menu / Applications. If it still fails, check that only one copy is running (quit all copies from the tray), then look at `server.log` in the data folder.
- **Install from source fails with `tapi error: unknown architecture`** (macOS): your Command Line Tools linker is older than the newest SDK. `setup.sh` retries the build against an older SDK automatically. Updating the Command Line Tools also fixes it.

## Run from source

**macOS**
```bash
./run.sh
```
Press **Ctrl+C** to quit. For the menu-bar app instead: `.venv/bin/python -m backend.launcher`.

**Windows**

1. Install [Python](https://www.python.org/downloads/) **3.13** (`winget install Python.Python.3.13`). Other versions don't work on Windows: on 3.12 and older the iOS 17.4+ tunnel depends on a package whose Windows build is broken, and 3.14 has no Windows build of `pylzss` yet. It can sit alongside other Python versions; setup finds it.
2. Install Apple's iPhone driver: the **Apple Devices** app from the Microsoft Store (or iTunes). Open it once with the iPhone plugged in so the phone gets recognized.
3. Double-click **`run.bat`**. It runs in the tray like the installed app. For a console with live logs, run `run.bat --console`.

Either way, the first run creates `.venv` and installs dependencies, then opens http://127.0.0.1:8765. No administrator password is needed: the iOS 17+ developer tunnel runs inside the app (pymobiledevice3's userspace tunnel).

## Releases

### Downloading
Every version is on the [Releases page](https://github.com/mikeandike08/LocationSpoof/releases). The newest is at the top, marked **Latest**. Under **Assets**, pick:

| Your computer | File |
|---|---|
| Windows 10 / 11 (64-bit) | `LocationSpoof-Setup-<version>.exe` |
| Mac with Apple Silicon (M1 or newer) | `LocationSpoof-<version>-macOS-arm64.dmg` |

Ignore the "Source code" downloads unless you want to [run from source](#run-from-source). Then follow [Install](#install).

### Making a release (maintainers)
Installers are built by GitHub Actions ([`installers.yml`](.github/workflows/installers.yml)), because each one has to be built on its own OS.

1. **Bump the version** in [`backend/version.py`](backend/version.py), e.g. `0.3.1`, and merge everything you want released into `main`.
2. **Tag that commit and push the tag.** The tag must start with `v` and match the version:
   ```bash
   git checkout main
   git pull
   git tag v0.3.1
   git push origin v0.3.1
   ```
3. **Wait about 10 minutes** while the [Actions](https://github.com/mikeandike08/LocationSpoof/actions) run builds the Windows installer and the macOS disk image. Each build launches the bundled app in a self-check mode and fails if it doesn't start, so a broken bundle never gets attached.
4. **Review the draft.** When the run is green, a **draft** release for the tag appears on the [Releases page](https://github.com/mikeandike08/LocationSpoof/releases) (only you can see drafts) with both files attached. Download them and try them, ideally on a clean machine.
5. **Publish.** Click **Edit**, add release notes (what changed), and click **Publish release**. It becomes **Latest**, and the download link at the top of this README points to it.

**If a build fails:** open the failed run in Actions to see the error. Fix it, delete the tag, and push it again on the fixed commit. Also delete the draft release if one was created.
```bash
git tag -d v0.3.1
git push origin :refs/tags/v0.3.1
git tag v0.3.1
git push origin v0.3.1
```
To build without making a release, run the workflow by hand from the Actions tab (**Installers → Run workflow**); the installers are attached to the run as downloadable artifacts.

### Building an installer locally
From the project's venv, on the OS you're building for:
```bash
pip install -r packaging/requirements-build.txt
python packaging/build.py
```
The result goes to `dist/installer/`. Windows needs [Inno Setup 6](https://jrsoftware.org/isinfo.php) (`winget install JRSoftware.InnoSetup`). To use your own app icon, add a 1024×1024 `packaging/icon.png`; otherwise the build draws the map pin the tray uses.

## Project layout
```
backend/
  main.py       FastAPI app + API routes
  launcher.py   tray / menu-bar launcher (runs the server in the background)
  paths.py      where web files and user data live (source checkout vs packaged app)
  version.py    app version (bump before tagging a release)
  drivelog.py   disconnect / lost-spoof / arrival log written during routes
  devices.py    discovery, pairing, Developer Mode, disk image, userspace developer tunnel
  location.py   holds the location channel open, auto-reconnects, re-applies the last fix
  playback.py   moves along a route with per-segment speeds
  routing.py    Valhalla/OSRM routing + speed limits
  geocode.py    address search
  places.py     saved places store
web/            single-page UI (Leaflet, no build step)
packaging/
  build.py      builds the installer for the current OS (PyInstaller + Inno Setup / hdiutil)
  LocationSpoof.spec, entry.py   PyInstaller config and entry point
  windows/      Inno Setup script
  macos/        disk image script
.github/workflows/installers.yml   builds both installers on a version tag
```
