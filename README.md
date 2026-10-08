# LocationSpoof

Spoof your iPhone's GPS location from your Mac using a local web page. It works over USB or Wi-Fi and needs no jailbreak and no app on the phone.

- **Teleport**: search any address or click the map.
- **Saved places**: save addresses and jump back to them. Search results are ranked by distance from you.
- **GPS-style routes**: type a start and destination. The phone drives the route along real roads at each road's **posted speed limit**, or a guessed limit where none is posted. Walk and bike modes use a fixed speed.
- **Saved routes**: saved with the full road geometry and speeds, so they load and play **without internet**. Map areas you've viewed are cached too.
- **Route preview**: play a route on the map without moving the phone (automatic when no iPhone is connected), at up to 20× speed, to check a route before driving it for real.
- **Arrow keys / WASD** nudge the location a few meters at a time.

It uses Apple's developer location simulation, the same mechanism Xcode uses, through [pymobiledevice3](https://github.com/doronz88/pymobiledevice3).

## Quick start

```bash
./run.sh
```

The first run creates `.venv` and installs dependencies, then opens http://127.0.0.1:8765. No administrator password is needed: the iOS 17+ developer tunnel runs inside the app (pymobiledevice3's userspace tunnel). Logs go to `data/server.log`.

Press **Ctrl+C** to quit. Quitting restores the phone's real location.

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
- Saved places live in `data/places.json`, saved routes in `data/routes.json`.
- Your location (for "Your location" and nearby search) comes from the browser if you allow it, otherwise from your network (approximate, via ipinfo.io).

## Connection problems
The Device card shows a plain-English status line and a **Connection log** of recent events (disconnects, tunnel rebuilds, re-applied locations).

The app recovers on its own from:
- a developer tunnel that drops while the cable stays plugged in (it's rebuilt automatically, with backoff);
- a tunnel that exists but stops responding (it's replaced);
- an unplugged cable (the spoofed location is re-applied when the phone reconnects, and a running route holds its position until then);
- a phone restart (the developer disk image is re-mounted automatically).

## Offline use
Searching and building **new** routes need internet. Saved routes and saved places don't. The map background works offline for areas you've already viewed (tiles are cached in `data/tiles/`). The iPhone must stay connected to the Mac (USB needs no Wi-Fi at all).

## Troubleshooting
- **Install fails with `tapi error: unknown architecture`**: your Command Line Tools linker is older than the newest SDK. `setup.sh` retries the build against an older SDK automatically. Updating the Command Line Tools also fixes it.
- **Device not listed**: unlock the phone, check that the cable carries data, and tap Trust.
- **"The developer tunnel isn't up yet"**: keep the phone unlocked and check that Developer Mode is on; the app retries automatically.
- **Page says it can't reach the server**: check that only one copy of LocationSpoof is running (an old `sudo` copy from an earlier version will fight over the phone). Quit all copies and start again with `./run.sh`. Details are in `data/server.log`.

## Project layout
```
backend/
  main.py       FastAPI app + API routes
  devices.py    discovery, pairing, Developer Mode, disk image, userspace developer tunnel
  location.py   holds the location channel open, auto-reconnects, re-applies the last fix
  playback.py   moves along a route with per-segment speeds
  routing.py    Valhalla/OSRM routing + speed limits
  geocode.py    address search
  places.py     saved places store
web/            single-page UI (Leaflet, no build step)
```
