"""LocationSpoof local web server.

Run with:  sudo ./run.sh   (root is needed to create device tunnels on iOS 17+)
"""

import argparse
import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend import geocode, tiles
from backend.errors import DeviceError, explain
from backend.devices import DeviceManager, is_root
from backend.geo import offset_m
from backend.location import LocationService, PreviewSession
from backend.places import PlacesStore, RoutesStore
from backend.playback import RoutePlayer
from backend.routing import FIXED_SPEEDS_KMH, Route, build_route

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
for noisy in ("httpx", "pymobiledevice3", "zeroconf", "asyncio"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

devices = DeviceManager()
locations = LocationService(devices)
places = PlacesStore()
player = RoutePlayer()
saved_routes = RoutesStore()
routes: dict[str, Route] = {}  # recently built/loaded routes, by id
MAX_ROUTES_IN_MEMORY = 10


def _remember_route(route: Route, route_id: Optional[str] = None) -> str:
    route_id = route_id or uuid.uuid4().hex[:10]
    routes.pop(route_id, None)
    routes[route_id] = route
    while len(routes) > MAX_ROUTES_IN_MEMORY:
        routes.pop(next(iter(routes)))
    return route_id


@asynccontextmanager
async def lifespan(_: FastAPI):
    await devices.start()
    yield
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
    return {"root": is_root(), "tunnel_error": devices.tunneld_error}


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
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
