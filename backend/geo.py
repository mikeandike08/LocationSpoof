"""Small geodesy helpers: distances and walking along a polyline."""

import math
from dataclasses import dataclass

EARTH_RADIUS_M = 6_371_000.0

LatLng = tuple[float, float]


def haversine_m(a: LatLng, b: LatLng) -> float:
    """Great-circle distance between two (lat, lng) points, in meters."""
    lat1, lng1 = map(math.radians, a)
    lat2, lng2 = map(math.radians, b)
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(h)))


def bearing_deg(a: LatLng, b: LatLng) -> float:
    """Initial compass bearing from a to b, 0-360 degrees."""
    lat1, lng1 = map(math.radians, a)
    lat2, lng2 = map(math.radians, b)
    dlng = lng2 - lng1
    x = math.sin(dlng) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlng)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def lerp(a: LatLng, b: LatLng, t: float) -> LatLng:
    """Linear interpolation; fine for the short segments of a road polyline."""
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def offset_m(p: LatLng, north_m: float, east_m: float) -> LatLng:
    """Move a point by a number of meters north/east."""
    dlat = north_m / EARTH_RADIUS_M
    dlng = east_m / (EARTH_RADIUS_M * math.cos(math.radians(p[0])))
    return (p[0] + math.degrees(dlat), p[1] + math.degrees(dlng))


@dataclass
class Segment:
    start: LatLng
    end: LatLng
    length_m: float
    speed_mps: float


def build_segments(points: list[LatLng], speeds_mps: list[float]) -> list[Segment]:
    """Pair consecutive points with the speed for that stretch. Zero-length segments are dropped."""
    segments = []
    for i in range(len(points) - 1):
        length = haversine_m(points[i], points[i + 1])
        if length <= 0.01:
            continue
        speed = speeds_mps[i] if i < len(speeds_mps) else speeds_mps[-1]
        segments.append(Segment(points[i], points[i + 1], length, max(speed, 0.3)))
    return segments
