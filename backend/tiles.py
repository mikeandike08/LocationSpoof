"""Map tile proxy with an on-disk cache, so maps you've already viewed still show offline.

Only tiles you actually look at are cached (no bulk pre-downloading, per the OSM tile policy).
Fresh tiles are served from disk; stale ones are refreshed when online and served as-is offline.
"""

import os
import time
from pathlib import Path
from typing import Optional

import httpx

from backend.places import DATA_DIR, _chown_to_invoking_user

TILE_URL = os.environ.get("LOCSPOOF_TILE_URL", "https://tile.openstreetmap.org/{z}/{x}/{y}.png")
USER_AGENT = "LocationSpoof/0.1 (personal, local use; caches viewed tiles)"
FRESH_S = 14 * 24 * 3600
CACHE_DIR = DATA_DIR / "tiles"

_client: Optional[httpx.AsyncClient] = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=8, headers={"User-Agent": USER_AGENT})
    return _client


def _path(z: int, x: int, y: int) -> Path:
    return CACHE_DIR / str(z) / str(x) / f"{y}.png"


def _save(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    for parent in (CACHE_DIR, path.parent.parent, path.parent):
        _chown_to_invoking_user(parent)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    _chown_to_invoking_user(path)


async def get_tile(z: int, x: int, y: int) -> Optional[bytes]:
    """Return PNG bytes, or None if the tile isn't cached and can't be downloaded."""
    if not (0 <= z <= 19 and 0 <= x < 2**z and 0 <= y < 2**z):
        return None
    path = _path(z, x, y)
    if path.exists() and time.time() - path.stat().st_mtime < FRESH_S:
        return path.read_bytes()
    try:
        resp = await _http().get(TILE_URL.format(z=z, x=x, y=y))
        resp.raise_for_status()
        _save(path, resp.content)
        return resp.content
    except Exception:
        # Offline (or server hiccup): fall back to whatever we have, even if old.
        return path.read_bytes() if path.exists() else None


def cache_size_mb() -> float:
    if not CACHE_DIR.exists():
        return 0.0
    return sum(f.stat().st_size for f in CACHE_DIR.rglob("*.png")) / 1e6
