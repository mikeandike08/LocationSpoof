"""Moves the spoofed location along a route at per-segment target speeds.

Runs server-side so playback continues even if the browser tab is in the background.
Speed changes are smoothed with simple acceleration limits, and the car slows ahead of
lower-speed stretches instead of braking instantly.
"""

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Optional

from backend.geo import LatLng, Segment, bearing_deg, build_segments, lerp
from backend.location import LocationSession

logger = logging.getLogger("locationspoof.playback")

TICK_S = 1.0  # iOS location updates are ~1 Hz; faster ticks just add load
ACCEL_MPS2 = 2.0
DECEL_MPS2 = 2.5


@dataclass
class PlaybackStatus:
    state: str = "idle"  # idle | playing | paused | finished | error
    position: Optional[LatLng] = None
    heading: float = 0.0
    speed_kmh: float = 0.0
    target_kmh: float = 0.0
    traveled_m: float = 0.0
    total_m: float = 0.0
    remaining_s: float = 0.0
    segment_index: int = 0
    loop: bool = False
    error: Optional[str] = None
    waiting: bool = False  # device not accepting updates; the route is holding position

    def to_json(self) -> dict:
        return {
            "state": self.state,
            "position": list(self.position) if self.position else None,
            "heading": round(self.heading, 1),
            "speed_kmh": round(self.speed_kmh, 1),
            "target_kmh": round(self.target_kmh, 1),
            "traveled_m": round(self.traveled_m, 1),
            "total_m": round(self.total_m, 1),
            "remaining_s": round(self.remaining_s),
            "progress": round(self.traveled_m / self.total_m, 4) if self.total_m else 0,
            "segment_index": self.segment_index,
            "loop": self.loop,
            "error": self.error,
            "waiting": self.waiting,
        }


class RoutePlayer:
    def __init__(self) -> None:
        self.task: Optional[asyncio.Task] = None
        self.status = PlaybackStatus()
        self.segments: list[Segment] = []
        self.session: Optional[LocationSession] = None
        self.speed_scale = 1.0
        self.jitter = 0.0
        self._paused = asyncio.Event()
        self._paused.set()  # set == running

    def start(
        self,
        session: LocationSession,
        points: list[LatLng],
        speeds_mps: list[float],
        loop: bool = False,
        speed_scale: float = 1.0,
        jitter: float = 0.05,
    ) -> None:
        self.stop()
        self.session = session
        self.segments = build_segments(points, speeds_mps)
        if not self.segments:
            raise ValueError("Route is empty")
        self.speed_scale = speed_scale
        self.jitter = jitter
        total = sum(s.length_m for s in self.segments)
        self.status = PlaybackStatus(state="playing", total_m=total, loop=loop, position=self.segments[0].start)
        self._paused.set()
        self.task = asyncio.create_task(self._run())

    def pause(self) -> None:
        if self.status.state == "playing":
            self._paused.clear()
            self.status.state = "paused"
            self.status.speed_kmh = 0

    def resume(self) -> None:
        if self.status.state == "paused":
            self._paused.set()
            self.status.state = "playing"

    def stop(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
        self.task = None
        if self.status.state in ("playing", "paused"):
            self.status.state = "idle"
            self.status.speed_kmh = 0

    def set_speed_scale(self, scale: float) -> None:
        self.speed_scale = max(0.1, min(scale, 10.0))

    def _target_mps(self, index: int) -> float:
        return self.segments[index].speed_mps * self.speed_scale

    def _braking_target(self, index: int, offset_m: float, speed: float) -> float:
        """Lowest speed we must already be at, given slower segments within braking distance ahead."""
        target = self._target_mps(index)
        braking_distance = speed * speed / (2 * DECEL_MPS2)
        ahead = self.segments[index].length_m - offset_m
        j = index + 1
        while j < len(self.segments) and ahead < braking_distance:
            limit = self._target_mps(j)
            # v^2 = u^2 + 2as  ->  max speed now that still lets us reach `limit` in `ahead` meters
            allowed = (limit * limit + 2 * DECEL_MPS2 * ahead) ** 0.5
            target = min(target, allowed)
            ahead += self.segments[j].length_m
            j += 1
        return target

    def _remaining_seconds(self, index: int, offset_m: float) -> float:
        remaining = (self.segments[index].length_m - offset_m) / max(self._target_mps(index), 0.3)
        for seg in self.segments[index + 1 :]:
            remaining += seg.length_m / max(seg.speed_mps * self.speed_scale, 0.3)
        return remaining

    async def _send(self, pos: LatLng) -> bool:
        # A dropped connection mid-route shouldn't end playback: hold position until it's back.
        try:
            await self.session.set(*pos)
            if self.status.waiting:
                logger.info("device reachable again, resuming route")
            self.status.error = None
            self.status.waiting = False
            return True
        except Exception as e:
            self.status.error = f"Paused until the iPhone reconnects. {e}"
            self.status.waiting = True
            return False

    async def _run(self) -> None:
        st = self.status
        index, offset, speed = 0, 0.0, 0.0
        variance = 1.0
        last = time.monotonic()
        try:
            await self.session.set(*self.segments[0].start)
            while True:
                if st.waiting:
                    # Keep re-sending the current point; don't advance while the phone isn't listening.
                    await asyncio.sleep(2)
                    if await self._send(st.position):
                        last = time.monotonic()
                        speed = 0.0
                    continue
                if not self._paused.is_set():
                    await self._paused.wait()
                    last = time.monotonic()
                await asyncio.sleep(TICK_S)
                now = time.monotonic()
                dt = min(now - last, 3 * TICK_S)
                last = now
                if not self._paused.is_set():
                    continue

                # Drift the cruising speed a little so it doesn't look robotic.
                if self.jitter:
                    variance += random.uniform(-0.25, 0.25) * self.jitter
                    variance = max(1 - self.jitter, min(1 + self.jitter, variance))

                target = self._braking_target(index, offset, speed) * variance
                if speed < target:
                    speed = min(target, speed + ACCEL_MPS2 * dt)
                else:
                    speed = max(target, speed - DECEL_MPS2 * dt)
                speed = max(speed, 0.5)

                travel = speed * dt
                st.traveled_m += travel
                offset += travel
                while index < len(self.segments) and offset >= self.segments[index].length_m:
                    offset -= self.segments[index].length_m
                    index += 1

                if index >= len(self.segments):
                    if st.loop:
                        index, offset, st.traveled_m = 0, 0.0, 0.0
                        continue
                    end = self.segments[-1].end
                    await self._send(end)
                    st.position, st.state, st.speed_kmh, st.remaining_s = end, "finished", 0, 0
                    st.traveled_m = st.total_m
                    return

                seg = self.segments[index]
                pos = lerp(seg.start, seg.end, offset / seg.length_m)
                await self._send(pos)
                st.position = pos
                st.heading = bearing_deg(seg.start, seg.end)
                st.speed_kmh = speed * 3.6
                st.target_kmh = self._target_mps(index) * 3.6
                st.segment_index = index
                st.remaining_s = self._remaining_seconds(index, offset)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("playback failed")
            st.state, st.error = "error", str(e) or e.__class__.__name__
