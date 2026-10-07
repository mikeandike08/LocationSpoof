"""Saved places, stored as a JSON file under data/."""

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


def _chown_to_invoking_user(path: Path) -> None:
    # The server runs under sudo; hand files back to the real user so they stay editable.
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if uid and gid:
        try:
            os.chown(path, int(uid), int(gid))
        except OSError:
            pass


class JsonStore:
    def __init__(self, filename: str, default: Any):
        self.path = DATA_DIR / filename
        self.default = default
        self.lock = threading.Lock()

    def read(self) -> Any:
        with self.lock:
            if not self.path.exists():
                return json.loads(json.dumps(self.default))
            return json.loads(self.path.read_text())

    def write(self, data: Any) -> None:
        with self.lock:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            _chown_to_invoking_user(DATA_DIR)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(self.path)
            _chown_to_invoking_user(self.path)


class PlacesStore:
    def __init__(self) -> None:
        self.store = JsonStore("places.json", [])

    def list(self) -> list[dict]:
        return self.store.read()

    def add(self, name: str, lat: float, lng: float, address: Optional[str] = None) -> dict:
        places = self.store.read()
        place = {
            "id": uuid.uuid4().hex[:12],
            "name": name.strip() or "Untitled",
            "address": address,
            "lat": lat,
            "lng": lng,
            "created": int(time.time()),
        }
        places.append(place)
        self.store.write(places)
        return place

    def update(self, place_id: str, **fields: Any) -> Optional[dict]:
        places = self.store.read()
        for place in places:
            if place["id"] == place_id:
                for key in ("name", "address", "lat", "lng"):
                    if fields.get(key) is not None:
                        place[key] = fields[key]
                self.store.write(places)
                return place
        return None

    def delete(self, place_id: str) -> bool:
        places = self.store.read()
        remaining = [p for p in places if p["id"] != place_id]
        if len(remaining) == len(places):
            return False
        self.store.write(remaining)
        return True


class RoutesStore:
    """Saved routes, including full geometry and speeds so they replay without internet."""

    def __init__(self) -> None:
        self.store = JsonStore("routes.json", [])

    @staticmethod
    def summary(entry: dict) -> dict:
        return {k: v for k, v in entry.items() if k != "route"} | {
            "distance_m": entry["route"].get("distance_m"),
            "duration_s": entry["route"].get("duration_s"),
        }

    def list(self) -> list[dict]:
        return [self.summary(e) for e in self.store.read()]

    def get(self, route_id: str) -> Optional[dict]:
        return next((e for e in self.store.read() if e["id"] == route_id), None)

    def add(self, name: str, start: dict, dest: dict, settings: dict, route: dict) -> dict:
        entries = self.store.read()
        entry = {
            "id": uuid.uuid4().hex[:12],
            "name": name.strip() or "Untitled route",
            "start": start,
            "dest": dest,
            "settings": settings,
            "created": int(time.time()),
            "route": route,
        }
        entries.append(entry)
        self.store.write(entries)
        return self.summary(entry)

    def rename(self, route_id: str, name: str) -> Optional[dict]:
        entries = self.store.read()
        for entry in entries:
            if entry["id"] == route_id:
                entry["name"] = name.strip() or entry["name"]
                self.store.write(entries)
                return self.summary(entry)
        return None

    def delete(self, route_id: str) -> bool:
        entries = self.store.read()
        remaining = [e for e in entries if e["id"] != route_id]
        if len(remaining) == len(entries):
            return False
        self.store.write(remaining)
        return True
