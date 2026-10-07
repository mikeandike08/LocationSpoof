# Location Spoofing: Functions We Need

Reference notes from reading [GeoPort](https://github.com/davesc63/GeoPort) (`src/main.py` and `src/templates/map.html`, read 2026-10-06).
Nothing is built yet. This doc lists the pieces our own app needs.

---

## 1. How GeoPort actually works

GeoPort is a thin **Flask + Leaflet** wrapper around **[pymobiledevice3](https://github.com/doronz88/pymobiledevice3)**. The library does all the device work; GeoPort only adds the UI.

```
Browser UI (Leaflet map)  ──HTTP──▶  Flask (main.py)  ──pymobiledevice3──▶  iPhone
                                                        │
                                                        ├─ usbmux / lockdown   (discover, pair, dev mode)
                                                        ├─ CoreDevice tunnel   (iOS 17+, needs sudo)
                                                        ├─ RSD (RemoteServiceDiscovery) over tunnel
                                                        └─ DVT → LocationSimulation.set(lat, lng) / clear()
```

Spoofing goes through Apple's **developer instruments** (the same path Xcode uses to simulate location). There is no jailbreak and no app on the phone. Requirements:
- Developer Mode is enabled on the device.
- The Developer Disk Image (DDI) is mounted. iOS 17+ uses a "personalized" DDI, and fetching it requires internet access to Apple.
- On iOS 17 and later, a tunnel is running, which needs **root (sudo)** on macOS.
- The DVT connection **stays open** while the spoof is active. GeoPort holds it open in a loop. If it closes, newer iOS reverts to the real location.

---

## 2. Connection pipeline (order matters)

| # | Step | pymobiledevice3 API GeoPort used | Notes |
|---|------|----------------------------------|-------|
| 1 | List devices | `usbmux.list_devices()` → `MuxDevice(serial, connection_type)` | `connection_type` is `USB` or `Network` |
| 2 | Get device info | `create_using_usbmux(udid, connection_type=..., autopair=True).short_info` | Gives `ProductVersion`, `DeviceName`, `DeviceClass`, `Identifier` |
| 3 | Enable Wi-Fi connections (optional) | `lockdown.enable_wifi_connections = True` | Needed for wireless later |
| 4 | Check Developer Mode | `lockdown.developer_mode_status` | |
| 5 | Enable Developer Mode (if off) | `AmfiService(lockdown).enable_developer_mode()` | Fails with `DeviceHasPasscodeSetError` if a passcode is set. The user must remove the passcode temporarily |
| 6 | Mount DDI | `pymobiledevice3.cli.mounter.auto_mount(lockdown)` | |
| 7 | Start tunnel (iOS 17.4+) | `CoreDeviceTunnelProxy(lockdown).start_tcp_tunnel()` → `(address, port)` | **Main path for modern iOS.** Equivalent to `sudo pymobiledevice3 lockdown start-tunnel` |
| 7a | Start tunnel (iOS 17.0–17.3) | `get_rsds()` → `create_core_device_tunnel_service_using_rsd(rsd, autopair=True).start_quic_tunnel()` | Legacy QUIC path. Calls `stop_remoted_if_required()` / `resume_remoted_if_required()` |
| 7b | Wi-Fi tunnel (17.0–17.3) | `get_remote_pairing_tunnel_services()` → `create_core_device_tunnel_service_using_remotepairing(udid, host, port).start_quic_tunnel()` | Needs an existing pair record (one USB connection first) |
| 8 | Connect RSD | `RemoteServiceDiscoveryService((rsd_host, rsd_port))` | Only for iOS 17+ |
| 9 | Open DVT | `DvtSecureSocketProxyService(rsd)` for iOS 17+, `DvtSecureSocketProxyService(lockdown=lockdown)` for iOS 16 and below | |
| 10 | Set location | `LocationSimulation(dvt).set(lat, lng)` | Hold the DVT connection open afterward |
| 11 | Clear location | `LocationSimulation(dvt).clear()` | |

> **Verify before building:** GeoPort pinned an older pymobiledevice3. Newer releases have changed several of these APIs (more `async`, some modules moved). Check every import against the current pymobiledevice3 version.

---

## 3. Functions our app needs

### Device layer
- `list_devices()`: USB and Wi-Fi devices with name, iOS version, UDID, and connection type.
- `get_device_info(udid)`: the `short_info` fields.
- `pair_device(udid)`: trust/pair over USB and save the pair record. Wi-Fi requires this first.
- `get_pair_record(udid)`: `get_preferred_pair_record(udid, get_home_folder())`.
- `enable_wifi_connections(udid)`.

### Developer prerequisites
- `is_developer_mode_enabled(udid)`.
- `enable_developer_mode(udid)`: handles the passcode error with a clear message to the user.
- `mount_developer_image(udid)`.

### Tunnel / session manager (long-lived, background)
- `start_tunnel(udid, transport)`: selects TCP (17.4+) or QUIC (17.0–17.3), and USB or Wi-Fi. Returns `(rsd_host, rsd_port)`.
- `stop_tunnel(udid)`.
- `get_or_reuse_tunnel(udid, conn_type)`: GeoPort caches this in `rsd_data_map[udid][conn_type]`.
- Health check and auto-reconnect. GeoPort has none, and it fails silently.

### Location core
- `set_location(lat, lng)`: opens or reuses the DVT session, calls `.set()`, and keeps the session alive.
- `clear_location()`: calls `.clear()` and closes the session.
- `move_by(d_lat, d_lng)`: arrow-key/joystick nudge. GeoPort uses a 0.0001° step, about 11 m.
- `play_route(points, speed_kmh)`: steps through points, timing each step by haversine distance divided by speed. Presets are walk 6, run 12, ride 20, drive 50 km/h.
  - Should **interpolate** between points (GeoPort teleports from vertex to vertex) and add optional jitter.
  - Should **reuse a single DVT session** instead of reconnecting per point. GeoPort hit `Errno 54 Connection reset` from opening too many sessions.
- `pause_route()` / `resume_route()` / `stop_route()`.

### Route / data helpers (UI side)
- `haversine(a, b)` and `travel_time(distance, speed)`.
- `parse_gpx(file)` / `parse_geojson(file)` → list of points.
- `export_geojson(markers, tracks)`.
- Saved favorites: add, rename, delete, and teleport to a favorite.
- Address search / geocoding. GeoPort used `leaflet-geosearch` with OSM Nominatim.
- Draw route on map. Routing between clicked points can use OSRM via `leaflet-routing-machine`.

### App / lifecycle
- Privilege check: warn if not running as root on macOS.
- Graceful shutdown: clear the location, close DVT, stop the tunnel. GeoPort's version of this is messy.

---

## 4. GeoPort features we can drop
- Australian fuel-price mode (`projectzerothree.info` API, `/api/fuel_types`, `/api/data/<type>`).
- **Telemetry**: the UI POSTs the device UDID, name, iOS version, and country to `api.geoport.me` on every connect (`updateDynamoDB`, `recordEvent`).
- GitHub version check and broadcast banner (the `BROADCAST` file currently holds a NordVPN referral link).
- Windows-only pieces: `pyuac` admin elevation, WeTest driver for 17.0–17.3, iTunes dependency.

---

## 5. Gaps and bugs found in GeoPort's source
- **The public source is outdated.** `main.py` says `APP_VERSION_NUMBER = "2.3.3"`, but the release binaries are **v4.0.2**. What you ran was probably a newer, unpublished build.
- **GPX playback in this source only moves the map marker.** `simulateGPXPlayback()` never calls `/set_location`, and the `/upload_gpx` endpoint the UI posts to doesn't exist in `main.py`.
- Global mutable state everywhere (`udid`, `rsd_host`, `location`, ...), so it only supports one device at a time.
- `set_location` returns success right away, before the device confirms anything. That explains "reports success but nothing moves" reports.
- Threads poll `terminate_*` flags with `time.sleep` inside async code, so there is no real cancellation.

---

## 6. Why it may have broken (iOS 26 / 27)
Open GeoPort issues as of today:
- [#188](https://github.com/davesc63/GeoPort/issues/188): iOS 26.4.1, connects fine but the location never changes.
- [#189](https://github.com/davesc63/GeoPort/issues/189): iOS 26.4.2, device detected in logs but not shown in the UI.
- [#190](https://github.com/davesc63/GeoPort/issues/190): iOS 26.5, location reverts after disconnecting. Expected: modern iOS does not persist the spoof without a live session.
- iOS 27: "location reports success but Maps does not move".

GeoPort doesn't appear to be maintained, so these have no fixes. Upstream pymobiledevice3's maintainer says static `set` [still works on iOS 26](https://github.com/doronz88/pymobiledevice3/discussions/1463). GPX `play` has [reported problems](https://github.com/doronz88/pymobiledevice3/issues/998), and Find My may [snap back to the real location](https://github.com/matt-ceran/location-changer/issues/1).

**Most likely cause:** a stale pymobiledevice3 bundled in the GeoPort binary, not a dead technique. First step for us is to test the raw CLI on your device with the latest pymobiledevice3 before writing any UI:

```bash
python3 -m pip install -U pymobiledevice3
```
```bash
sudo pymobiledevice3 lockdown start-tunnel
```
```bash
pymobiledevice3 developer dvt simulate-location set --rsd <RSD_HOST> <RSD_PORT> -- 40.7580 -73.9855
```

If that moves the blue dot in Maps, the pipeline above is sound, and our app is a better UI and session manager on top of it.

---

## 7. Open decisions for us
- **Stack:** keep Python + pymobiledevice3 as the backend (lowest risk), then choose the UI: native macOS (SwiftUI) talking to a Python helper, an Electron/Tauri web UI, or a local web UI like GeoPort.
- Do we need Wi-Fi mode, or is USB only acceptable to start?
- Do we need multiple devices at once?
- Which iOS versions to support? Supporting only 17.4+ removes the whole QUIC/remoted/driver branch.
