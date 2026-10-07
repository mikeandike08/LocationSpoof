"""Route building: road geometry plus a target speed for every stretch of road.

Primary source is Valhalla (free public FOSSGIS instance, no API key):
  * /route            -> road geometry between the stops
  * /trace_attributes -> per-edge OSM speed limit, Valhalla's speed estimate and road class
Fallback is OSRM (also FOSSGIS), which only gives its own speed estimate.
"""

import logging
import os
from dataclasses import dataclass, field
from typing import Literal, Optional

import httpx

from backend.geo import LatLng, haversine_m

logger = logging.getLogger("locationspoof.routing")

VALHALLA_URL = os.environ.get("LOCSPOOF_VALHALLA_URL", "https://valhalla1.openstreetmap.de")
OSRM_URL = os.environ.get("LOCSPOOF_OSRM_URL", "https://routing.openstreetmap.de")
USER_AGENT = "LocationSpoof/0.1 (personal, local use)"

Travel = Literal["auto", "bicycle", "pedestrian"]
SpeedMode = Literal["limit", "traffic", "fixed"]

KMH = 1 / 3.6  # km/h -> m/s

# Guessed limits (km/h) for roads with no maxspeed tag, by Valhalla road class.
# US-flavoured defaults: 65 / 55 / 45 / 40 / 35 / 30 / 25 / 15 mph.
ROAD_CLASS_GUESS_KMH = {
    "motorway": 105,
    "trunk": 88,
    "primary": 72,
    "secondary": 64,
    "tertiary": 56,
    "unclassified": 48,
    "residential": 40,
    "service_other": 24,
}

FIXED_SPEEDS_KMH = {"walk": 5.0, "run": 10.0, "bike": 18.0}


@dataclass
class RouteStretch:
    """A run of consecutive shape points sharing one road/edge."""

    start_index: int
    end_index: int
    name: Optional[str]
    road_class: Optional[str]
    speed_limit_kmh: Optional[float]
    estimated_kmh: Optional[float]
    target_kmh: float = 0.0
    speed_source: str = ""


@dataclass
class Route:
    points: list[LatLng]
    segment_speeds_mps: list[float]  # one per (points[i], points[i+1])
    stretches: list[RouteStretch] = field(default_factory=list)
    distance_m: float = 0.0
    duration_s: float = 0.0
    provider: str = ""
    limit_coverage: float = 0.0  # fraction of distance with a real posted limit

    @classmethod
    def from_json(cls, data: dict) -> "Route":
        """Rebuild a route saved with to_json() (no network needed)."""
        return cls(
            points=[(lat, lng) for lat, lng in data["points"]],
            segment_speeds_mps=[kmh * KMH for kmh in data["segment_speeds_kmh"]],
            stretches=[
                RouteStretch(
                    start_index=st["start"],
                    end_index=st["end"],
                    name=st.get("name"),
                    road_class=st.get("road_class"),
                    speed_limit_kmh=st.get("speed_limit_kmh"),
                    estimated_kmh=None,
                    target_kmh=st.get("target_kmh", 0.0),
                    speed_source=st.get("source", ""),
                )
                for st in data.get("stretches", [])
            ],
            distance_m=data.get("distance_m", 0.0),
            duration_s=data.get("duration_s", 0.0),
            provider=data.get("provider", ""),
            limit_coverage=data.get("limit_coverage", 0.0),
        )

    def to_json(self) -> dict:
        return {
            "points": [[lat, lng] for lat, lng in self.points],
            "segment_speeds_kmh": [round(s / KMH, 1) for s in self.segment_speeds_mps],
            "stretches": [
                {
                    "start": s.start_index,
                    "end": s.end_index,
                    "name": s.name,
                    "road_class": s.road_class,
                    "speed_limit_kmh": s.speed_limit_kmh,
                    "target_kmh": round(s.target_kmh, 1),
                    "source": s.speed_source,
                }
                for s in self.stretches
            ],
            "distance_m": round(self.distance_m, 1),
            "duration_s": round(self.duration_s, 1),
            "provider": self.provider,
            "limit_coverage": round(self.limit_coverage, 3),
        }


def decode_polyline(encoded: str, precision: int = 6) -> list[LatLng]:
    """Decode a Google-style encoded polyline (Valhalla uses precision 6)."""
    points: list[LatLng] = []
    index = lat = lng = 0
    factor = 10**precision
    while index < len(encoded):
        for is_lng in (False, True):
            shift = result = 0
            while True:
                byte = ord(encoded[index]) - 63
                index += 1
                result |= (byte & 0x1F) << shift
                shift += 5
                if byte < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if is_lng:
                lng += delta
            else:
                lat += delta
        points.append((lat / factor, lng / factor))
    return points


def _choose_speed(
    stretch: RouteStretch, travel: Travel, mode: SpeedMode, fixed_kmh: float, limit_factor: float
) -> tuple[float, str]:
    if mode == "fixed":
        return fixed_kmh, "fixed"
    if travel != "auto":
        # Speed limits are for cars; walkers and cyclists use the fixed speed regardless.
        return fixed_kmh, "fixed"
    if mode == "traffic" and stretch.estimated_kmh:
        return stretch.estimated_kmh, "estimate"
    if stretch.speed_limit_kmh:
        return stretch.speed_limit_kmh * limit_factor, "posted"
    guess = ROAD_CLASS_GUESS_KMH.get(stretch.road_class or "")
    if guess:
        return guess * limit_factor, "guessed"
    if stretch.estimated_kmh:
        return stretch.estimated_kmh, "estimate"
    return 40.0, "default"


async def _valhalla_route(client: httpx.AsyncClient, stops: list[LatLng], travel: Travel) -> tuple[list[LatLng], list[RouteStretch]]:
    body = {
        "locations": [{"lat": lat, "lon": lng} for lat, lng in stops],
        "costing": travel,
        "units": "kilometers",
        "directions_type": "none",
    }
    resp = await client.post(f"{VALHALLA_URL}/route", json=body)
    resp.raise_for_status()
    legs = resp.json()["trip"]["legs"]

    points: list[LatLng] = []
    stretches: list[RouteStretch] = []
    for leg in legs:
        attrs_body = {
            "encoded_polyline": leg["shape"],
            "shape_match": "edge_walk",
            "costing": travel,
            "filters": {
                "attributes": [
                    "edge.speed_limit",
                    "edge.speed",
                    "edge.road_class",
                    "edge.names",
                    "edge.begin_shape_index",
                    "edge.end_shape_index",
                    "shape",
                ],
                "action": "include",
            },
        }
        attrs = await client.post(f"{VALHALLA_URL}/trace_attributes", json=attrs_body)
        attrs.raise_for_status()
        data = attrs.json()
        leg_points = decode_polyline(data.get("shape") or leg["shape"])

        # Legs share their junction point; drop the duplicate so indices stay contiguous.
        offset = len(points)
        if points and leg_points and points[-1] == leg_points[0]:
            leg_points = leg_points[1:]
            offset -= 1
        points.extend(leg_points)

        for edge in data.get("edges", []):
            limit = edge.get("speed_limit")
            # Valhalla reports 0 or 255 for "unknown"/"unlimited".
            if not limit or limit >= 250:
                limit = None
            names = edge.get("names") or []
            stretches.append(
                RouteStretch(
                    start_index=offset + edge["begin_shape_index"],
                    end_index=offset + edge["end_shape_index"],
                    name=names[0] if names else None,
                    road_class=edge.get("road_class"),
                    speed_limit_kmh=float(limit) if limit else None,
                    estimated_kmh=float(edge["speed"]) if edge.get("speed") else None,
                )
            )
    return points, stretches


async def _osrm_route(client: httpx.AsyncClient, stops: list[LatLng], travel: Travel) -> tuple[list[LatLng], list[RouteStretch]]:
    profile = {"auto": "routed-car", "bicycle": "routed-bike", "pedestrian": "routed-foot"}[travel]
    coords = ";".join(f"{lng},{lat}" for lat, lng in stops)
    resp = await client.get(
        f"{OSRM_URL}/{profile}/route/v1/driving/{coords}",
        params={"overview": "full", "geometries": "geojson", "annotations": "speed"},
    )
    resp.raise_for_status()
    route = resp.json()["routes"][0]
    points = [(lat, lng) for lng, lat in route["geometry"]["coordinates"]]
    speeds: list[float] = []
    for leg in route["legs"]:
        speeds.extend(leg["annotation"]["speed"])
    stretches = [
        RouteStretch(i, i + 1, None, None, None, s * 3.6) for i, s in enumerate(speeds[: len(points) - 1])
    ]
    return points, stretches


async def build_route(
    stops: list[LatLng],
    travel: Travel = "auto",
    mode: SpeedMode = "limit",
    fixed_kmh: float = 50.0,
    limit_factor: float = 1.0,
) -> Route:
    if len(stops) < 2:
        raise ValueError("A route needs at least two stops")

    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": USER_AGENT}) as client:
        try:
            points, stretches = await _valhalla_route(client, stops, travel)
            provider = "valhalla"
        except (httpx.HTTPError, KeyError, ValueError) as e:
            logger.warning(f"Valhalla failed ({e!r}); falling back to OSRM")
            points, stretches = await _osrm_route(client, stops, travel)
            provider = "osrm"

    if len(points) < 2:
        raise ValueError("Routing service returned an empty route")

    # Default every segment, then paint each stretch's chosen speed over its segments.
    default_mps = (fixed_kmh if mode == "fixed" else 40.0) * KMH
    segment_speeds = [default_mps] * (len(points) - 1)
    limited_m = 0.0
    total_m = 0.0
    for stretch in stretches:
        kmh, source = _choose_speed(stretch, travel, mode, fixed_kmh, limit_factor)
        stretch.target_kmh = kmh
        stretch.speed_source = source
        for i in range(stretch.start_index, min(stretch.end_index, len(segment_speeds))):
            segment_speeds[i] = kmh * KMH
            seg_len = haversine_m(points[i], points[i + 1])
            total_m += seg_len
            if stretch.speed_limit_kmh:
                limited_m += seg_len

    distance = sum(haversine_m(points[i], points[i + 1]) for i in range(len(points) - 1))
    duration = sum(
        haversine_m(points[i], points[i + 1]) / max(segment_speeds[i], 0.3) for i in range(len(points) - 1)
    )
    return Route(
        points=points,
        segment_speeds_mps=segment_speeds,
        stretches=stretches,
        distance_m=distance,
        duration_s=duration,
        provider=provider,
        limit_coverage=(limited_m / total_m) if total_m else 0.0,
    )
