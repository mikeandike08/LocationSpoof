"""Address search.

* Suggestions while typing: Photon (komoot), an OSM geocoder built for search-as-you-type.
* Explicit searches (Enter) and reverse lookups: Nominatim, which is fast but whose usage policy
  forbids autocomplete, so it is only hit on deliberate actions and at most once per second.
"""

import asyncio
import os
import time
from typing import Optional

import httpx

from backend.geo import haversine_m

PHOTON_URL = os.environ.get("LOCSPOOF_PHOTON_URL", "https://photon.komoot.io")
NOMINATIM_URL = os.environ.get("LOCSPOOF_NOMINATIM_URL", "https://nominatim.openstreetmap.org")
USER_AGENT = "LocationSpoof/0.1 (personal, local use)"
CACHE_TTL_S = 600

_client: Optional[httpx.AsyncClient] = None
_cache: dict[tuple, tuple[float, object]] = {}
_nominatim_lock = asyncio.Lock()
_nominatim_last = 0.0


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=10, headers={"User-Agent": USER_AGENT})
    return _client


def _cached(key: tuple):
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL_S:
        return hit[1]
    return None


def _store(key: tuple, value):
    if len(_cache) > 500:
        _cache.clear()
    _cache[key] = (time.monotonic(), value)
    return value


def _result(title: str, parts: list[Optional[str]], lat: float, lng: float, kind: Optional[str]) -> dict:
    seen = {title}
    subtitle_parts = []
    for part in parts:
        if part and part not in seen:
            seen.add(part)
            subtitle_parts.append(part)
    subtitle = ", ".join(subtitle_parts)
    return {
        "title": title,
        "subtitle": subtitle,
        "address": f"{title}, {subtitle}" if subtitle else title,
        "lat": lat,
        "lng": lng,
        "kind": kind,
    }


def _from_photon(feature: dict) -> dict:
    p = feature.get("properties", {})
    lng, lat = feature["geometry"]["coordinates"]
    street = " ".join(x for x in (p.get("housenumber"), p.get("street")) if x)
    title = p.get("name") or street or p.get("city") or p.get("state") or p.get("country") or "Unnamed"
    return _result(title, [street, p.get("city"), p.get("state"), p.get("postcode"), p.get("country")], lat, lng, p.get("osm_value"))


def _from_nominatim(item: dict) -> dict:
    a = item.get("address", {})
    street = " ".join(x for x in (a.get("house_number"), a.get("road")) if x)
    city = a.get("city") or a.get("town") or a.get("village") or a.get("hamlet") or a.get("suburb")
    title = item.get("name") or street or city or item.get("display_name", "Unnamed").split(",")[0]
    return _result(
        title,
        [street, city, a.get("state"), a.get("postcode"), a.get("country")],
        float(item["lat"]),
        float(item["lon"]),
        item.get("type"),
    )


async def _nominatim(path: str, params: dict):
    global _nominatim_last
    async with _nominatim_lock:  # policy: max 1 request/second
        wait = 1.0 - (time.monotonic() - _nominatim_last)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            resp = await _http().get(f"{NOMINATIM_URL}/{path}", params={**params, "format": "jsonv2", "addressdetails": 1})
        finally:
            _nominatim_last = time.monotonic()
    resp.raise_for_status()
    return resp.json()


def _with_distance(results: list[dict], near: Optional[tuple[float, float]]) -> list[dict]:
    if not near:
        return results
    for r in results:
        r["distance_m"] = round(haversine_m(near, (r["lat"], r["lng"])))
    return results


async def suggest(query: str, near: Optional[tuple[float, float]] = None, limit: int = 6) -> list[dict]:
    """Search-as-you-type suggestions (Photon), strongly biased toward `near`."""
    bias = (round(near[0], 2), round(near[1], 2)) if near else None
    key = ("suggest", query.lower().strip(), bias)
    if (hit := _cached(key)) is not None:
        return hit
    params: dict = {"q": query, "limit": limit, "lang": "en"}
    if near:
        # zoom ~ city scale; a higher bias scale weights distance more heavily than prominence.
        params.update({"lat": near[0], "lon": near[1], "zoom": 12, "location_bias_scale": 0.6})
    resp = await _http().get(f"{PHOTON_URL}/api/", params=params, timeout=4)
    resp.raise_for_status()
    return _store(key, _with_distance([_from_photon(f) for f in resp.json().get("features", [])], near))


async def search(query: str, near: Optional[tuple[float, float]] = None, limit: int = 6) -> list[dict]:
    """Deliberate search, e.g. when the user presses Enter (Nominatim), preferring results near `near`."""
    bias = (round(near[0], 2), round(near[1], 2)) if near else None
    key = ("search", query.lower().strip(), bias)
    if (hit := _cached(key)) is not None:
        return hit
    results: list[dict] = []
    if near:
        # First look only nearby (~40 mi box), then widen if that finds too little.
        lat, lng = near
        local = await _nominatim(
            "search",
            {"q": query, "limit": 40, "viewbox": f"{lng - 0.8},{lat + 0.6},{lng + 0.8},{lat - 0.6}", "bounded": 1},
        )
        results = [_from_nominatim(item) for item in local]
    if len(results) < 3:
        seen = {(round(r["lat"], 4), round(r["lng"], 4)) for r in results}
        for item in await _nominatim("search", {"q": query, "limit": limit}):
            r = _from_nominatim(item)
            if (round(r["lat"], 4), round(r["lng"], 4)) not in seen:
                results.append(r)
    results = _with_distance(results, near)
    if near:
        results.sort(key=lambda r: r["distance_m"])
    results = results[:limit]
    return _store(key, results)


async def approximate_location() -> Optional[dict]:
    """City-level location of this Mac from its public IP (fallback when browser geolocation is denied)."""
    key = ("ip-location",)
    if (hit := _cached(key)) is not None:
        return hit
    resp = await _http().get("https://ipinfo.io/json", timeout=8)
    resp.raise_for_status()
    data = resp.json()
    if "loc" not in data:
        return None
    lat, lng = (float(x) for x in data["loc"].split(","))
    label = ", ".join(x for x in (data.get("city"), data.get("region")) if x) or "Approximate location"
    return _store(key, {"lat": lat, "lng": lng, "label": label})


async def reverse(lat: float, lng: float) -> Optional[dict]:
    key = ("reverse", round(lat, 5), round(lng, 5))
    if (hit := _cached(key)) is not None:
        return hit
    data = await _nominatim("reverse", {"lat": lat, "lon": lng, "zoom": 18})
    return _store(key, _from_nominatim(data) if data and "lat" in data else None)
