"""LocationSpoof local web server.

Run with ./run.sh (macOS) or run.bat (Windows), or `python -m backend.launcher` for the tray app.
No administrator rights are needed: the iOS 17+ developer tunnel runs in userspace.
"""

import argparse
import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Literal, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend import geocode, tiles
from backend.alerts import AlertCenter
from backend.errors import DeviceError, explain
from backend.devices import DeviceManager
from backend.drivelog import LOG_PATH, DriveLog
from backend.geo import offset_m
from backend.keepawake import SUPPORTED as KEEP_AWAKE_SUPPORTED
from backend.keepawake import KeepAwake
from backend.location import LocationService, PreviewSession
from backend.paths import DATA_DIR, WEB_DIR
from backend.places import JsonStore, PlacesStore, RoutesStore
from backend.playback import RoutePlayer
from backend.routing import FIXED_SPEEDS_KMH, Route, build_route

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
for noisy in ("httpx", "pymobiledevice3", "zeroconf", "asyncio"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
logger = logging.getLogger("locationspoof")


def _setup_file_log() -> None:
    """Also log to data/server.log (rotated) so problems can be diagnosed after the fact."""
    from logging.handlers import RotatingFileHandler

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / "server.log"
    handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    logging.getLogger().addHandler(handler)


def _raise_open_file_limit() -> None:
    # macOS defaults to 256 open files, which a burst of device/tile connections can exhaust.
    try:
        import resource
    except ImportError:
        return  # Windows: no such limit to raise

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = min(8192, hard) if hard != resource.RLIM_INFINITY else 8192
    if soft < target:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
        except (ValueError, OSError):
            pass


def _loop_exception_handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    # Log stray background errors once per kind instead of letting them spam the terminal.
    exc = context.get("exception")
    key = (type(exc).__name__, str(context.get("message")))
    seen = _loop_exception_handler.__dict__.setdefault("seen", {})
    seen[key] = seen.get(key, 0) + 1
    count = seen[key]
    if count == 1 or count in (10, 100, 1000):
        logger.warning(
            "background error (%sx): %s %s", count, context.get("message"), repr(exc) if exc else "",
            exc_info=exc if count == 1 else None,
        )


devices = DeviceManager()
locations = LocationService(devices)
places = PlacesStore()
player = RoutePlayer()
drive_log = DriveLog(player)
devices.listeners.append(drive_log.on_event)
alerts = AlertCenter(
    lambda udid: player.device_udid == udid
    or (udid in locations.sessions and locations.sessions[udid].last is not None)
)
devices.listeners.append(alerts.on_event)
saved_routes = RoutesStore()
settings = JsonStore("settings.json", {})
keep_awake = KeepAwake()
keep_awake.enabled = bool(settings.read().get("keep_awake", False))
drive_log.header_extra = lambda: (
    f"Keep computer awake: {'on' if keep_awake.enabled else 'off'}" if KEEP_AWAKE_SUPPORTED else "Keep computer awake: not supported here"
)
POWER_CHECK_S = 5
SLEEP_GAP_S = 30  # a check that runs this late means the computer was asleep
routes: dict[str, Route] = {}  # recently built/loaded routes, by id
MAX_ROUTES_IN_MEMORY = 10


def _remember_route(route: Route, route_id: Optional[str] = None) -> str:
    route_id = route_id or uuid.uuid4().hex[:10]
    routes.pop(route_id, None)
    routes[route_id] = route
    while len(routes) > MAX_ROUTES_IN_MEMORY:
        routes.pop(next(iter(routes)))
    return route_id


def _spoof_in_use() -> bool:
    """A location is being held on a phone, or a route is driving one."""
    return player.device_udid is not None or any(s.last is not None for s in locations.sessions.values())


async def _power_watch() -> None:
    """Hold the no-sleep request while spoofing (if enabled), and notice when the computer slept anyway."""
    last = time.time()
    while True:
        await asyncio.sleep(POWER_CHECK_S)
        now = time.time()
        gap = now - last - POWER_CHECK_S
        last = now
        if gap > SLEEP_GAP_S and _spoof_in_use():
            logger.warning(f"computer was asleep for about {int(gap)}s")
            for session in locations.sessions.values():
                if session.last is not None:
                    devices.emit("computer_slept", session.udid, asleep_s=gap)
        try:
            keep_awake.update(_spoof_in_use())
        except Exception:
            logger.exception("keep-awake update failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    asyncio.get_running_loop().set_exception_handler(_loop_exception_handler)
    await devices.start()
    power_task = asyncio.create_task(_power_watch())
    yield
    power_task.cancel()
    keep_awake.release()
    player.stop()
    await locations.shutdown(clear=True)  # give the phone its real location back
    await devices.stop()


app = FastAPI(title="LocationSpoof", lifespan=lifespan)


def _http_error(e: Exception) -> HTTPException:
    if isinstance(e, KeyError):
        return HTTPException(404, "That iPhone isn't connected anymore. Plug it back in and unlock it.")
    return HTTPException(400, explain(e))


# ---------- app / devices ----------


@app.get("/api/status")
async def status():
    return {"admin_required": False, "tunnel_error": None}


@app.get("/api/devices")
async def list_devices():
    result = await devices.list_devices()
    for d in result:
        session = locations.sessions.get(d["udid"])
        d["location"] = {
            "active": bool(session and session.last),
            "lat": session.last[0] if session and session.last else None,
            "lng": session.last[1] if session and session.last else None,
            "connected": bool(session and session.connected),
            "error": session.error if session else None,
        }
    return result


@app.post("/api/devices/{udid}/{action}")
async def device_action(udid: str, action: Literal["prepare", "reveal-devmode", "enable-devmode", "enable-wifi"]):
    handlers = {
        "prepare": devices.start_prepare,
        "reveal-devmode": devices.start_reveal_developer_mode,
        "enable-devmode": devices.start_enable_developer_mode,
        "enable-wifi": devices.start_enable_wifi,
    }
    try:
        handlers[action](udid)
    except Exception as e:
        raise _http_error(e)
    return {"ok": True}


# ---------- location ----------


class SetLocation(BaseModel):
    udid: str
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class Nudge(BaseModel):
    udid: str
    north_m: float = 0
    east_m: float = 0


class UdidBody(BaseModel):
    udid: str


@app.post("/api/location/set")
async def set_location(body: SetLocation):
    player.stop()
    try:
        await locations.session(body.udid).set(body.lat, body.lng)
    except Exception as e:
        raise _http_error(e)
    return {"ok": True, "lat": body.lat, "lng": body.lng}


@app.post("/api/location/nudge")
async def nudge_location(body: Nudge):
    session = locations.session(body.udid)
    if session.last is None:
        raise HTTPException(400, "Set a location first")
    if player.status.state == "playing":
        raise HTTPException(409, "Stop the route before moving manually")
    if player.status.holding:
        player.stop()  # otherwise the next re-apply would snap back to the destination
    lat, lng = offset_m(session.last, body.north_m, body.east_m)
    try:
        await session.set(lat, lng)
    except Exception as e:
        raise _http_error(e)
    return {"ok": True, "lat": lat, "lng": lng}


@app.post("/api/location/clear")
async def clear_location(body: UdidBody):
    player.stop()
    try:
        await locations.session(body.udid).clear()
    except Exception as e:
        raise _http_error(e)
    return {"ok": True}


# ---------- geocoding ----------


@app.get("/api/suggest")
async def suggest(q: str, lat: Optional[float] = None, lng: Optional[float] = None):
    if len(q.strip()) < 2:
        return []
    try:
        return await geocode.suggest(q, (lat, lng) if lat is not None and lng is not None else None)
    except Exception as e:
        raise HTTPException(502, f"Suggestions unavailable: {e!r}")


@app.get("/api/search")
async def search(q: str, lat: Optional[float] = None, lng: Optional[float] = None):
    if len(q.strip()) < 2:
        return []
    try:
        return await geocode.search(q, (lat, lng) if lat is not None and lng is not None else None)
    except Exception as e:
        raise HTTPException(502, f"Search unavailable: {e!r}")


@app.get("/api/approximate-location")
async def approximate_location():
    try:
        result = await geocode.approximate_location()
    except Exception as e:
        raise HTTPException(502, f"Couldn't determine your location: {e!r}")
    if result is None:
        raise HTTPException(502, "Couldn't determine your location")
    return result


@app.get("/api/reverse")
async def reverse(lat: float, lng: float):
    try:
        return await geocode.reverse(lat, lng)
    except Exception as e:
        raise HTTPException(502, f"Reverse geocoding unavailable: {e}")


# ---------- saved places ----------


class PlaceIn(BaseModel):
    name: str
    lat: float
    lng: float
    address: Optional[str] = None


class PlacePatch(BaseModel):
    name: Optional[str] = None
    address: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None


@app.get("/api/places")
async def list_places():
    return places.list()


@app.post("/api/places")
async def add_place(body: PlaceIn):
    return places.add(body.name, body.lat, body.lng, body.address)


@app.patch("/api/places/{place_id}")
async def update_place(place_id: str, body: PlacePatch):
    place = places.update(place_id, **body.model_dump())
    if place is None:
        raise HTTPException(404, "Place not found")
    return place


@app.delete("/api/places/{place_id}")
async def delete_place(place_id: str):
    if not places.delete(place_id):
        raise HTTPException(404, "Place not found")
    return {"ok": True}


# ---------- routes & playback ----------


class RouteRequest(BaseModel):
    stops: list[tuple[float, float]] = Field(min_length=2, max_length=20)
    travel: Literal["auto", "bicycle", "pedestrian"] = "auto"
    speed_mode: Literal["limit", "traffic", "fixed"] = "limit"
    fixed_kmh: float = Field(50.0, gt=0, le=300)
    limit_factor: float = Field(1.0, gt=0.1, le=3)


class PlaybackStart(BaseModel):
    udid: Optional[str] = None
    route_id: str
    preview: bool = False  # play on the map only, without moving a phone
    loop: bool = False
    speed_scale: float = Field(1.0, gt=0, le=20)
    jitter: float = Field(0.05, ge=0, le=0.3)


class SpeedScale(BaseModel):
    speed_scale: float = Field(gt=0, le=20)


@app.get("/api/route/presets")
async def route_presets():
    return {"fixed_kmh": FIXED_SPEEDS_KMH}


@app.post("/api/route")
async def make_route(body: RouteRequest):
    try:
        route = await build_route(body.stops, body.travel, body.speed_mode, body.fixed_kmh, body.limit_factor)
    except Exception as e:
        raise HTTPException(502, f"Routing failed: {e}")
    route_id = _remember_route(route)
    return {"id": route_id, **route.to_json()}


@app.post("/api/playback/start")
async def playback_start(body: PlaybackStart):
    route = routes.get(body.route_id)
    if route is None:
        raise HTTPException(404, "Route expired; build it again")
    if not body.preview and body.speed_scale > 10:
        raise HTTPException(400, "Speeds above 10x are only allowed in preview")
    try:
        if body.preview:
            session = PreviewSession()
        elif body.udid:
            session = locations.session(body.udid)
        else:
            raise DeviceError("Connect an iPhone, or turn on preview to play the route without one.")
        player.start(session, route.points, route.segment_speeds_mps, body.loop, body.speed_scale, body.jitter)
    except Exception as e:
        raise _http_error(e)
    return player.status.to_json()


@app.post("/api/playback/speed")
async def playback_speed(body: SpeedScale):
    player.set_speed_scale(body.speed_scale)
    return player.status.to_json()


@app.post("/api/playback/{action}")
async def playback_control(action: Literal["pause", "resume", "stop"]):
    getattr(player, action)()
    return player.status.to_json()


@app.get("/api/playback")
async def playback_status():
    return player.status.to_json()


# ---------- keep the computer awake while spoofing ----------


class KeepAwakeToggle(BaseModel):
    enabled: bool


def _keep_awake_status() -> dict:
    return {
        "supported": KEEP_AWAKE_SUPPORTED,
        "enabled": keep_awake.enabled,
        "active": keep_awake.active,
        "spoofing": _spoof_in_use(),
        "error": keep_awake.error,
    }


@app.get("/api/keep-awake")
async def keep_awake_status():
    return _keep_awake_status()


@app.post("/api/keep-awake")
async def keep_awake_toggle(body: KeepAwakeToggle):
    if body.enabled and not KEEP_AWAKE_SUPPORTED:
        raise HTTPException(400, "Keeping the computer awake isn't supported on this system.")
    keep_awake.enabled = body.enabled
    settings.write({**settings.read(), "keep_awake": body.enabled})
    keep_awake.update(_spoof_in_use())
    return _keep_awake_status()


# ---------- alerts (sound / screen flash for critical moments) ----------

ALERT_DEFAULTS = {"sound": False, "flash": False, "recovery_chime": True, "volume": 0.7}
SSE_PING_S = 15
GRACEFUL_SHUTDOWN_S = 5  # cap on waiting for open connections when the app quits


def _server_exiting() -> bool:
    """True once uvicorn has been asked to stop (set by main() and the tray launcher)."""
    server = getattr(app.state, "uvicorn_server", None)
    return bool(server and server.should_exit)


class AlertSettings(BaseModel):
    sound: Optional[bool] = None
    flash: Optional[bool] = None
    recovery_chime: Optional[bool] = None
    volume: Optional[float] = Field(None, ge=0, le=1)


def _alert_settings() -> dict:
    return {**ALERT_DEFAULTS, **settings.read().get("alerts", {})}


@app.get("/api/alert-settings")
async def get_alert_settings():
    return _alert_settings()


@app.post("/api/alert-settings")
async def set_alert_settings(body: AlertSettings):
    merged = {**_alert_settings(), **body.model_dump(exclude_none=True)}
    settings.write({**settings.read(), "alerts": merged})
    return merged


@app.get("/api/alerts")
async def list_alerts(after: int = 0):
    return alerts.describe(after)


@app.post("/api/alerts/test")
async def test_alert():
    return alerts.test()


@app.get("/api/alerts/stream")
async def alert_stream(request: Request):
    """Server-Sent Events: one `data:` message per alert as it happens."""
    queue = alerts.subscribe()

    async def events():
        try:
            yield f"event: hello\ndata: {json.dumps(alerts.describe(after=alerts.last_id))}\n\n"
            last_ping = time.monotonic()
            # End the stream as soon as the app is quitting: an open stream would otherwise hold up
            # shutdown, and with it the shutdown hook that stops routes and clears the spoof.
            while not _server_exiting() and not await request.is_disconnected():
                try:
                    alert = await asyncio.wait_for(queue.get(), timeout=1)
                    yield f"id: {alert['id']}\ndata: {json.dumps(alert)}\n\n"
                except asyncio.TimeoutError:
                    if time.monotonic() - last_ping >= SSE_PING_S:
                        last_ping = time.monotonic()
                        # Heartbeat: keeps the stream open and lets the page notice if it goes quiet.
                        yield "event: ping\ndata: {}\n\n"
        finally:
            alerts.unsubscribe(queue)

    return StreamingResponse(
        events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


# ---------- drive log (disconnects / lost spoof during a route) ----------


class DriveLogToggle(BaseModel):
    enabled: bool
    lat: Optional[float] = Field(None, ge=-90, le=90)
    lng: Optional[float] = Field(None, ge=-180, le=180)
    label: Optional[str] = Field(None, max_length=200)


@app.get("/api/drive-log")
async def drive_log_status():
    return drive_log.describe()


@app.post("/api/drive-log")
async def drive_log_toggle(body: DriveLogToggle):
    if not body.enabled:
        drive_log.disable()
        return drive_log.describe()
    # The iPhone's real location can't be read from here; the computer it's plugged into is next to it.
    real = None
    if body.lat is not None and body.lng is not None:
        real = {"lat": body.lat, "lng": body.lng, "label": body.label or "this computer"}
    else:
        try:
            approx = await geocode.approximate_location()
        except Exception:
            approx = None  # offline: log without it
        if approx:
            real = {"lat": approx["lat"], "lng": approx["lng"], "label": f"approximate, near {approx.get('label')}"}
    drive_log.enable(real)
    return drive_log.describe()


@app.get("/drive-log.txt")
async def drive_log_file():
    text = LOG_PATH.read_text(encoding="utf-8") if LOG_PATH.exists() else "No incidents logged yet.\n"
    return Response(content=text, media_type="text/plain; charset=utf-8")


# ---------- saved routes (stored with full geometry: replay works offline) ----------


class SaveRoute(BaseModel):
    route_id: str
    name: str = Field(max_length=120)
    start: dict
    dest: dict
    settings: dict = {}


class RenameBody(BaseModel):
    name: str = Field(min_length=1, max_length=120)


@app.get("/api/saved-routes")
async def list_saved_routes():
    return saved_routes.list()


@app.post("/api/saved-routes")
async def save_route(body: SaveRoute):
    route = routes.get(body.route_id)
    if route is None:
        raise HTTPException(404, "That route is no longer in memory; build it again, then save")
    entry = saved_routes.add(body.name, body.start, body.dest, body.settings, route.to_json())
    # The saved copy and the in-memory one are the same route; reuse the saved id from now on.
    _remember_route(route, entry["id"])
    return entry


@app.post("/api/saved-routes/{route_id}/load")
async def load_saved_route(route_id: str):
    entry = saved_routes.get(route_id)
    if entry is None:
        raise HTTPException(404, "Saved route not found")
    route = Route.from_json(entry["route"])
    _remember_route(route, route_id)
    return {
        "id": route_id,
        "name": entry["name"],
        "start": entry["start"],
        "dest": entry["dest"],
        "settings": entry.get("settings", {}),
        **route.to_json(),
    }


@app.patch("/api/saved-routes/{route_id}")
async def rename_saved_route(route_id: str, body: RenameBody):
    entry = saved_routes.rename(route_id, body.name)
    if entry is None:
        raise HTTPException(404, "Saved route not found")
    return entry


@app.delete("/api/saved-routes/{route_id}")
async def delete_saved_route(route_id: str):
    if not saved_routes.delete(route_id):
        raise HTTPException(404, "Saved route not found")
    return {"ok": True}


# ---------- map tiles (cached on disk for offline use) ----------


@app.get("/tiles/{z}/{x}/{y}.png")
async def tile(z: int, x: int, y: int):
    data = await tiles.get_tile(z, x, y)
    if data is None:
        return Response(status_code=504)
    return Response(content=data, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


# ---------- web UI ----------


@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    # Localhost only: this process runs as root and controls your phone.
    _setup_file_log()
    _raise_open_file_limit()
    # Standard asyncio loop (not uvloop): it's what pymobiledevice3's userspace tunnel is built on.
    config = uvicorn.Config(
        app, host="127.0.0.1", port=args.port, log_level="warning", loop="asyncio",
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
    )
    server = uvicorn.Server(config)
    app.state.uvicorn_server = server  # lets long-lived streams end promptly on shutdown
    server.run()


if __name__ == "__main__":
    main()
