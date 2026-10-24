"""Alerts: the critical moments someone watching the app needs to hear about right away.

Listens to DeviceManager events and turns the ones that matter into alerts for the UI, which can
play a sound and flash the screen edges:

* critical: the iPhone disconnected while spoofing, the spoof stopped reaching the phone (so it has
  likely fallen back to its real location), or the computer slept while spoofing;
* recovered: the matching problem cleared (reconnected, spoofed location re-applied).

Problems that are still ongoing are tracked so the UI can keep the screen edges lit until they clear.
Alerts are pushed to the page over Server-Sent Events, which keep arriving in a background tab
(unlike polling timers, which browsers throttle there).
"""

import asyncio
import itertools
import time
from collections import deque
from typing import Callable, Optional

DEDUPE_S = 8  # a second critical alert this soon after one for the same phone stays silent


def _clock() -> str:
    # "%-I" (no leading zero) isn't supported on Windows, so strip the zero by hand.
    return time.strftime("%I:%M:%S %p").lstrip("0")


def _duration(seconds: float) -> str:
    seconds = int(round(seconds or 0))
    m, s = divmod(seconds, 60)
    return f"{m}m {s}s" if m else f"{s}s"


class AlertCenter:
    def __init__(self, spoof_in_use: Callable[[str], bool]) -> None:
        self.spoof_in_use = spoof_in_use  # is this phone being spoofed (held location or route)?
        self.alerts: deque = deque(maxlen=50)
        self.active: dict[tuple[str, str], dict] = {}  # (udid, problem) -> the alert that raised it
        self._ids = itertools.count(1)
        self.last_id = 0
        self._last_loud: dict[str, float] = {}  # udid -> when its last audible critical alert fired
        self._subscribers: set[asyncio.Queue] = set()

    # ---------- events (DeviceManager listener) ----------

    def on_event(self, kind: str, udid: str, data: dict) -> None:
        if kind == "disconnected":
            if self.spoof_in_use(udid):
                self.raise_problem(
                    udid,
                    "disconnected",
                    "iPhone disconnected",
                    "LocationSpoof lost the iPhone, so it has likely gone back to its real location. Check the "
                    "cable or Wi-Fi. The spoofed location is re-applied automatically when it reconnects.",
                )
        elif kind == "reconnected":
            self.resolve(
                udid,
                "disconnected",
                "iPhone reconnected",
                f"Back after {_duration(data.get('away_s'))} via {data.get('via', 'USB')}. Re-applying the spoofed location.",
            )
        elif kind == "spoof_lost":
            reason = (data.get("reason") or "unknown").rstrip(".")
            self.raise_problem(
                udid,
                "spoof_lost",
                "Real location may be showing",
                f"The iPhone stopped receiving the spoofed location ({reason}). It has likely fallen back to its "
                "real location. Retrying automatically.",
            )
        elif kind == "spoof_restored":
            self.resolve(
                udid,
                "spoof_lost",
                "Spoofed location restored",
                f"Re-applied after {_duration(data.get('down_s'))}.",
            )
        elif kind == "computer_slept":
            self.push(
                "critical",
                "computer_slept",
                "Computer was asleep",
                f"The computer slept for about {_duration(data.get('asleep_s'))}, so the iPhone was most likely at "
                "its real location for that long. Turn on 'Keep computer awake' to prevent idle sleep.",
                udid=udid,
            )

    # ---------- raising / resolving ----------

    def raise_problem(self, udid: str, problem: str, title: str, message: str) -> None:
        if (udid, problem) in self.active:
            return  # already reported and still ongoing
        alert = self.push("critical", problem, title, message, udid=udid, problem=problem)
        self.active[(udid, problem)] = alert

    def resolve(self, udid: str, problem: str, title: str, message: str) -> None:
        if self.active.pop((udid, problem), None) is None:
            return  # nothing was raised, so there's nothing to announce
        self.push("recovered", problem, title, message, udid=udid, resolves=problem)

    def push(
        self,
        level: str,
        kind: str,
        title: str,
        message: str,
        udid: Optional[str] = None,
        problem: Optional[str] = None,
        resolves: Optional[str] = None,
    ) -> dict:
        now = time.monotonic()
        quiet = False
        if level == "critical" and udid:
            # A disconnect is usually followed within seconds by "spoof lost" for the same incident:
            # show both, but only sound the alarm once.
            quiet = now - self._last_loud.get(udid, -1e9) < DEDUPE_S
            if not quiet:
                self._last_loud[udid] = now
        self.last_id = next(self._ids)
        alert = {
            "id": self.last_id,
            "time": _clock(),
            "level": level,
            "kind": kind,
            "title": title,
            "message": message,
            "udid": udid,
            "problem": problem,
            "resolves": resolves,
            "quiet": quiet,
        }
        self.alerts.append(alert)
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(alert)
            except asyncio.QueueFull:
                pass  # a stuck client misses alerts; it catches up from /api/alerts on reconnect
        return alert

    def test(self) -> dict:
        return self.push(
            "critical",
            "test",
            "Test alert",
            "This is what an alert looks and sounds like. Nothing is wrong.",
        )

    # ---------- reading ----------

    def describe(self, after: int = 0) -> dict:
        return {
            "last_id": self.last_id,
            "alerts": [a for a in self.alerts if a["id"] > after],
            "active": list(self.active.values()),
        }

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)
