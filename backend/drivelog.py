"""Drive log: a plain-text record of every time a route loses the iPhone or its spoofed location.

An entry is added when, during a route (or while parked at its destination afterwards), the iPhone
disconnects or stops receiving the spoof, and again when it recovers, with how long that took and
where the route was at the time. A route that reaches its destination gets an ARRIVED entry with
its distance, how long it took, and how many incidents it had.

The computer can't read the iPhone's actual GPS fix, so "reset to real location" is detected from
its cause: on iOS 17+ the phone falls back to its real location whenever the location-simulation
channel breaks (a failed send, a replaced developer tunnel, a lost cable or Wi-Fi link).
"""

import threading
import time
from collections import deque
from datetime import datetime
from typing import Optional

from backend.paths import DATA_DIR
from backend.playback import RoutePlayer

LOG_PATH = DATA_DIR / "drive-log.txt"


def _stamp(t: Optional[float] = None) -> str:
    return datetime.fromtimestamp(t or time.time()).strftime("%Y-%m-%d %I:%M:%S %p")


def _duration(seconds: float) -> str:
    seconds = int(round(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    if h:
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s" if m else f"{s}s"


def _coord(p) -> str:
    return f"{p[0]:.5f}, {p[1]:.5f}" if p else "unknown"


class DriveLog:
    def __init__(self, player: RoutePlayer) -> None:
        self.player = player
        self.enabled = False
        self.enabled_at: Optional[float] = None
        self.real_location: Optional[dict] = None  # {"lat", "lng", "label"}
        self.incidents = 0
        self.route_incidents = 0  # incidents since the current route started
        self._counted_for: Optional[float] = None  # route start time route_incidents belongs to
        self.entries: deque = deque(maxlen=100)  # recent entries for the UI, newest first
        self._lock = threading.Lock()
        self._header_for: Optional[float] = None  # route start time the header was written for
        self._disconnected_at: Optional[float] = None
        self._lost_at: Optional[float] = None
        self._lost_reason: Optional[str] = None
        player.on_stop = self._route_stopped
        player.on_finish = self._route_finished

    # ---------- control ----------

    def enable(self, real_location: Optional[dict]) -> None:
        self.enabled = True
        self.enabled_at = time.time()
        self.real_location = real_location
        self.incidents = 0
        self.route_incidents = 0
        self._counted_for = None
        self._header_for = None
        self._disconnected_at = self._lost_at = None

    def disable(self) -> None:
        if self.enabled:
            self._close_open("logging was turned off")
        self.enabled = False

    def describe(self) -> dict:
        return {
            "enabled": self.enabled,
            "since": _stamp(self.enabled_at) if self.enabled_at else None,
            "real_location": self.real_location,
            "monitoring": self.enabled and self.player.device_udid is not None,
            "incidents": self.incidents,
            "problem_now": self._disconnected_at is not None or self._lost_at is not None,
            "path": str(LOG_PATH),
            "entries": list(self.entries),
        }

    # ---------- events (DeviceManager listener) ----------

    def on_event(self, kind: str, udid: str, data: dict) -> None:
        if not self.enabled or self.player.device_udid != udid:
            return
        now = time.monotonic()
        where = self._where()
        if kind == "disconnected":
            self._disconnected_at = now
            self._count_incident()
            self._write("error", "DISCONNECTED", f"iPhone disconnected. Spoofed position at the time: {where}.")
        elif kind == "reconnected":
            away = now - self._disconnected_at if self._disconnected_at is not None else data.get("away_s", 0)
            self._disconnected_at = None
            self._write("ok", "RECONNECTED", f"iPhone reconnected via {data.get('via', '?')} after {_duration(away)}. Route position now: {where}.")
        elif kind == "spoof_lost":
            self._lost_at = now
            self._lost_reason = data.get("reason") or "unknown"
            self._count_incident()
            self._write(
                "error",
                "REAL LOCATION",
                f"Spoof interrupted ({self._lost_reason}). The iPhone has likely fallen back to its real location "
                f"({self._real()}). Spoofed position was {where}.",
            )
        elif kind == "spoof_restored":
            down = data.get("down_s", 0)
            self._lost_at = None
            self._write("ok", "SPOOF RESTORED", f"Spoofed location re-applied after {_duration(down)} at {_coord(data.get('position'))}.")

    def _count_incident(self) -> None:
        if self._counted_for != self.player.started_at:
            self._counted_for = self.player.started_at
            self.route_incidents = 0
        self.incidents += 1
        self.route_incidents += 1

    def _route_finished(self) -> None:
        if not self.enabled or self.player.device_udid is None:
            return
        st = self.player.status
        had = self.route_incidents if self._counted_for == self.player.started_at else 0
        issues = "no incidents" if had == 0 else f"{had} incident{'s' if had != 1 else ''}"
        self._write(
            "ok" if had == 0 else "warn",
            "ARRIVED",
            f"Route completed: {st.total_m / 1609.34:.1f} mi in {_duration(time.time() - self.player.started_at)}, "
            f"{issues}. Holding at the destination {_coord(st.position)}.",
        )

    def _route_stopped(self) -> None:
        if self.enabled:
            self._close_open("the route was stopped")

    def _close_open(self, why: str) -> None:
        now = time.monotonic()
        if self._disconnected_at is not None:
            self._write("warn", "UNRESOLVED", f"Still disconnected when {why} ({_duration(now - self._disconnected_at)} so far).")
        if self._lost_at is not None:
            self._write("warn", "UNRESOLVED", f"Spoof still not restored when {why} ({_duration(now - self._lost_at)} so far).")
        self._disconnected_at = self._lost_at = None

    # ---------- writing ----------

    def _real(self) -> str:
        r = self.real_location
        if not r:
            return "unknown"
        return f"{_coord((r['lat'], r['lng']))}, {r.get('label') or 'this computer'}"

    def _where(self) -> str:
        st = self.player.status
        if st.holding:
            phase = "parked at the destination"
        elif st.total_m:
            phase = f"{st.traveled_m / st.total_m:.0%} of the route"
        else:
            phase = "route start"
        return f"{_coord(st.position)} ({phase})"

    def _write(self, level: str, tag: str, message: str) -> None:
        stamp = _stamp()
        self.entries.appendleft({"time": stamp, "level": level, "msg": f"{tag}: {message}"})
        lines = []
        if self._header_for != self.player.started_at:
            # One header per route, written with its first entry so quiet routes leave no trace.
            self._header_for = self.player.started_at
            st = self.player.status
            lines += [
                "",
                f"=== Route started {_stamp(self.player.started_at)} | {st.total_m / 1609.34:.1f} mi | "
                f"logging since {_stamp(self.enabled_at)} ===",
                f"    Real location (before spoofing): {self._real()}",
            ]
        lines.append(f"{stamp}  {tag:<15} {message}")
        with self._lock:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with LOG_PATH.open("a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
