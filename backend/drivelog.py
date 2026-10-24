"""Drive log: a plain-text record of every time a route loses the iPhone or its spoofed location.

An entry is added when, during a route (or while parked at its destination afterwards), the iPhone
disconnects or stops receiving the spoof, and again when it recovers, with how long that took,
where the route was at the time, and the likely cause (cable/Wi-Fi, a rebuilt developer tunnel, a
timed-out update, the computer sleeping). A route that reaches its destination gets an ARRIVED entry
with its distance, how long it took, and how many incidents it had.

The computer can't read the iPhone's actual GPS fix, so "reset to real location" is detected from
its cause: on iOS 17+ the phone falls back to its real location whenever the location-simulation
channel breaks (a failed send, a replaced developer tunnel, a lost cable or Wi-Fi link).
"""

import re
import threading
import time
from collections import deque
from datetime import datetime
from typing import Callable, Optional

from backend.paths import DATA_DIR
from backend.playback import RoutePlayer

LOG_PATH = DATA_DIR / "drive-log.txt"
WAKE_WINDOW_S = 60  # a lost spoof this soon after the computer wakes is blamed on the sleep


def _cause(reason: str) -> str:
    """A short, plain cause for a lost spoof, from the low-level reason."""
    r = reason.lower()
    if "disconnected" in r or "isn't connected" in r or "lost the connection" in r:
        return "iPhone connection dropped"
    if "tunnel was replaced" in r or "tunnel" in r:
        return "developer tunnel was rebuilt"
    if "timed out" in r:
        return "location update timed out"
    if "channel closed" in r:
        return "location channel was closed"
    return "location update failed"


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
        self._woke_at: Optional[float] = None  # when the computer was last found to have slept
        self.header_extra: Optional[Callable[[], str]] = None  # extra line for each route's header
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
            after_sleep = self._woke_at is not None and now - self._woke_at < WAKE_WINDOW_S
            if after_sleep:
                cause = "the computer was asleep (see COMPUTER SLEPT above)"
            else:
                self._count_incident()
                cause = _cause(self._lost_reason)
            self._write(
                "error",
                "REAL LOCATION",
                f"Spoof interrupted. Cause: {cause}. Details: {self._lost_reason.rstrip('.')}. The iPhone has likely fallen "
                f"back to its real location ({self._real()}). Spoofed position was {where}.",
            )
        elif kind == "spoof_restored":
            down = data.get("down_s", 0)
            self._lost_at = None
            self._write("ok", "SPOOF RESTORED", f"Spoofed location re-applied after {_duration(down)} at {_coord(data.get('position'))}.")
        elif kind == "computer_slept":
            self._woke_at = now
            self._count_incident()
            self._write(
                "error",
                "COMPUTER SLEPT",
                f"The computer was asleep for about {_duration(data.get('asleep_s', 0))}. The app was paused, so the "
                f"iPhone was most likely at its real location ({self._real()}) for that long. Turn on 'Keep "
                f"computer awake' to prevent idle sleep (closing a laptop lid still sleeps). Route position: {where}.",
            )
        elif kind == "tunnel_reset":
            self._write(
                "warn",
                "TUNNEL REBUILT",
                f"The developer tunnel stopped responding ({data.get('reason', 'unknown')}) and is being rebuilt.",
            )
        elif kind == "device_check_failed":
            self._write(
                "warn",
                "DEVICE CHECK",
                f"Apple's device service didn't answer ({data.get('error', 'unknown')}). Not treated as a "
                f"disconnect: the connection to the iPhone was kept.",
            )
        elif kind == "device_check_ok":
            self._write("ok", "DEVICE CHECK", f"Apple's device service is answering again after {_duration(data.get('down_s', 0))}.")

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
            if self.header_extra:
                lines.append(f"    {self.header_extra()}")
        lines.append(f"{stamp}  {tag:<15} {message}")
        with self._lock:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with LOG_PATH.open("a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")


# ---------- reading the log back (for the viewer page) ----------

_HEADER_RE = re.compile(
    r"^=== Route started (?P<started>.+?) \| (?P<mi>[\d.]+) mi \| logging since (?P<since>.+?) ===$"
)
_ENTRY_RE = re.compile(r"^(?P<stamp>\d{4}-\d{2}-\d{2} \d{1,2}:\d{2}:\d{2} [AP]M)  (?P<tag>.{15}) (?P<msg>.*)$")
_DURATION_RE = re.compile(r"(?:(\d+)h )?(?:(\d+)m )?(\d+)s")
_COORD_RE = re.compile(r"(-?\d{1,2}\.\d{3,}), (-?\d{1,3}\.\d{3,})")
_STAMP_FMT = "%Y-%m-%d %I:%M:%S %p"

# tag -> (kind, level, label) for the viewer. Levels: error (spoof down), ok (recovered/arrived), warn.
TAGS = {
    "DISCONNECTED": ("disconnected", "error", "iPhone disconnected"),
    "RECONNECTED": ("reconnected", "ok", "iPhone reconnected"),
    "REAL LOCATION": ("real_location", "error", "Real location showing"),
    "SPOOF RESTORED": ("spoof_restored", "ok", "Spoof restored"),
    "COMPUTER SLEPT": ("computer_slept", "error", "Computer slept"),
    "TUNNEL REBUILT": ("tunnel_rebuilt", "warn", "Tunnel rebuilt"),
    "DEVICE CHECK": ("device_check", "warn", "Device check"),
    "ARRIVED": ("arrived", "ok", "Arrived"),
    "UNRESOLVED": ("unresolved", "warn", "Unresolved"),
}


def _seconds(text: str) -> Optional[int]:
    """First '1h 2m 3s' / '2m 3s' / '3s' duration in the text, in seconds."""
    m = _DURATION_RE.search(text)
    if not m:
        return None
    h, mins, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mins * 60 + s


def _iso(stamp: str) -> Optional[str]:
    try:
        return datetime.strptime(stamp.strip(), _STAMP_FMT).isoformat()
    except ValueError:
        return None


def _interrupted_seconds(entries: list[dict]) -> int:
    """Roughly how long the spoof was down during a route.

    A SPOOF RESTORED entry carries the whole outage (including any sleep before it). A sleep or an
    unresolved outage with no restore after it is counted on its own.
    """
    total = 0
    pending = 0  # outage time from sleep / unresolved entries not yet covered by a restore
    for e in entries:
        if e["kind"] == "spoof_restored":
            total += e["duration_s"] or 0
            pending = 0
        elif e["kind"] == "computer_slept" or (e["kind"] == "unresolved" and "Spoof" in e["message"]):
            pending = max(pending, e["duration_s"] or 0)
    return total + pending


def parse_log(text: str) -> dict:
    """Turn drive-log.txt back into routes and entries, newest route first."""
    routes: list[dict] = []
    current: Optional[dict] = None

    def new_route(header: Optional[re.Match]) -> dict:
        route = {
            "started": header["started"] if header else None,
            "started_iso": _iso(header["started"]) if header else None,
            "distance_mi": float(header["mi"]) if header else None,
            "logging_since": header["since"] if header else None,
            "real_location": None,
            "details": [],
            "entries": [],
        }
        routes.append(route)
        return route

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        if header := _HEADER_RE.match(line):
            current = new_route(header)
            continue
        if line.startswith("    ") and current is not None and not current["entries"]:
            detail = line.strip()
            if detail.startswith("Real location (before spoofing):"):
                current["real_location"] = detail.split(":", 1)[1].strip()
            else:
                current["details"].append(detail)
            continue
        entry = _ENTRY_RE.match(line)
        if not entry:
            continue  # unknown line (older format); skip rather than fail
        if current is None:
            current = new_route(None)  # entries written before route headers existed
        tag = entry["tag"].strip()
        kind, level, label = TAGS.get(tag, (tag.lower().replace(" ", "_"), "warn", tag.title()))
        message = entry["msg"]
        if tag == "DEVICE CHECK" and "answering again" in message:
            level = "ok"
        if tag == "ARRIVED" and "no incidents" not in message:
            level = "warn"
        current["entries"].append({
            "time": entry["stamp"],
            "time_iso": _iso(entry["stamp"]),
            "tag": tag,
            "kind": kind,
            "level": level,
            "label": label,
            "message": message,
            "duration_s": _seconds(message),
            "coords": [[float(a), float(b)] for a, b in _COORD_RE.findall(message)],
        })

    for route in routes:
        entries = route["entries"]
        arrived = next((e for e in entries if e["kind"] == "arrived"), None)
        blamed_on_sleep = lambda e: e["kind"] == "real_location" and "computer was asleep" in e["message"]
        route["incidents"] = sum(
            1 for e in entries if e["kind"] in ("disconnected", "real_location", "computer_slept") and not blamed_on_sleep(e)
        )
        route["arrived"] = arrived is not None
        route["trip_s"] = arrived["duration_s"] if arrived else None
        route["interrupted_s"] = _interrupted_seconds(entries)
        route["unresolved"] = any(e["kind"] == "unresolved" for e in entries)
    # Newest first. File order is chronological; the start time breaks ties and fixes any odd ordering.
    routes = [r for _, r in sorted(enumerate(routes), key=lambda ir: (ir[1]["started_iso"] or "", ir[0]), reverse=True)]
    return {
        "routes": routes,
        "totals": {
            "routes": len(routes),
            "incidents": sum(r["incidents"] for r in routes),
            "arrived": sum(1 for r in routes if r["arrived"]),
            "interrupted_s": sum(r["interrupted_s"] for r in routes),
        },
    }
