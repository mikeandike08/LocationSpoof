"""Keeps a location-simulation channel open to a device and re-applies the location if it drops.

On iOS 17+ the spoof only lasts while the DVT channel stays open, so the session holds it,
detects breakage (send failure, tunnel address change), reconnects and re-sends the last fix.
"""

import asyncio
import contextlib
import logging
import time
from typing import Optional

from pymobiledevice3.exceptions import InvalidServiceError, StartServiceError
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation
from pymobiledevice3.services.simulate_location import DtSimulateLocation

from backend.devices import DeviceManager
from backend.errors import CABLE_HINT, DeviceError, explain

logger = logging.getLogger("locationspoof.location")

SEND_TIMEOUT_S = 8
KEEPALIVE_S = 20


class LocationSession:
    def __init__(self, devices: DeviceManager, udid: str):
        self.devices = devices
        self.udid = udid
        self.lock = asyncio.Lock()
        self.stack: Optional[contextlib.AsyncExitStack] = None
        self.sim = None
        self.tunnel_key: Optional[tuple] = None
        self.last: Optional[tuple[float, float]] = None
        self.last_sent_at = 0.0
        self.error: Optional[str] = None
        self.watchdog: Optional[asyncio.Task] = None
        self._was_broken = False

    @property
    def connected(self) -> bool:
        return self.sim is not None

    def _record(self):
        return self.devices.devices.get(self.udid)

    def _log(self, message: str, level: str = "info") -> None:
        record = self._record()
        if record:
            record.log(message, level)

    def _current_tunnel_key(self) -> Optional[tuple]:
        tunnel = self.devices.tunnel_for(self.udid)
        return (tunnel.address, tunnel.port) if tunnel else None

    async def _open_dvt(self, stack: contextlib.AsyncExitStack, rsd) -> None:
        dvt = await stack.enter_async_context(DvtProvider(rsd))
        self.sim = await stack.enter_async_context(LocationSimulation(dvt))

    async def _open(self) -> None:
        await self._close()
        record = self._record()
        if record is None or not record.connected:
            raise DeviceError(f"The iPhone is disconnected. {CABLE_HINT}")
        stack = contextlib.AsyncExitStack()
        try:
            if record.uses_tunnel:
                rsd = await self.devices.open_rsd(self.udid)
                stack.push_async_callback(rsd.close)
                try:
                    await self._open_dvt(stack, rsd)
                except (InvalidServiceError, StartServiceError):
                    # Usually the phone restarted and dropped the developer disk image.
                    self._log("Developer services unavailable, re-mounting the disk image", "warn")
                    await self.devices.mount(self.udid, rsd)
                    await self._open_dvt(stack, rsd)
                self.tunnel_key = self._current_tunnel_key()
            else:
                lockdown = await create_using_usbmux(self.udid)
                stack.push_async_callback(lockdown.close)
                self.sim = DtSimulateLocation(lockdown)
                self.tunnel_key = None
        except BaseException:
            self.sim = None
            await stack.aclose()
            raise
        self.stack = stack
        logger.info(f"[{self.udid}] location channel open")

    async def _close(self) -> None:
        self.sim = None
        if self.stack is not None:
            stack, self.stack = self.stack, None
            with contextlib.suppress(Exception):
                await asyncio.wait_for(stack.aclose(), timeout=5)

    def _stale(self) -> bool:
        if self.sim is None:
            return True
        record = self._record()
        if record is None or not record.connected:
            return True
        # Tunnel moved (e.g. cable unplugged, now on Wi-Fi): the old channel is dead.
        return record.uses_tunnel and self._current_tunnel_key() != self.tunnel_key

    async def _send(self, lat: float, lng: float) -> None:
        last_error: Optional[BaseException] = None
        for attempt in range(2):
            try:
                if self._stale():
                    await self._open()
                await asyncio.wait_for(self.sim.set(lat, lng), timeout=SEND_TIMEOUT_S)
                self.error = None
                self.last_sent_at = time.monotonic()
                if self._was_broken:
                    self._was_broken = False
                    self._log("Spoofed location re-applied", "ok")
                return
            except Exception as e:
                last_error = e
                logger.warning(f"[{self.udid}] set failed (attempt {attempt + 1}): {e!r}")
                await self._close()
                if isinstance(e, DeviceError):
                    break  # disconnected / no tunnel: retrying immediately won't help
        self.error = explain(last_error)
        if not self._was_broken:
            self._was_broken = True
            self._log(f"Couldn't update the location: {self.error}", "error")
        raise DeviceError(self.error) from last_error

    async def set(self, lat: float, lng: float) -> None:
        async with self.lock:
            self.last = (lat, lng)
            await self._send(lat, lng)
        self._ensure_watchdog()

    async def clear(self) -> None:
        async with self.lock:
            self.last = None
            try:
                if self._stale():
                    await self._open()
                await asyncio.wait_for(self.sim.clear(), timeout=SEND_TIMEOUT_S)
            except Exception as e:
                raise DeviceError(explain(e)) from e
            finally:
                await self._close()
        if self.watchdog:
            self.watchdog.cancel()
            self.watchdog = None

    def _ensure_watchdog(self) -> None:
        if self.watchdog is None or self.watchdog.done():
            self.watchdog = asyncio.create_task(self._watch())

    async def _watch(self) -> None:
        """Re-apply the last location when the channel goes stale, and nudge it periodically."""
        while self.last is not None:
            await asyncio.sleep(2)
            if self.last is None or self.lock.locked():
                continue
            record = self._record()
            if record is None or not record.connected:
                continue  # wait for it to come back; the scan logs the disconnect
            due = time.monotonic() - self.last_sent_at > KEEPALIVE_S
            if self._stale() or due:
                with contextlib.suppress(Exception):
                    async with self.lock:
                        if self.last is not None:
                            await self._send(*self.last)

    async def shutdown(self, clear: bool) -> None:
        if self.watchdog:
            self.watchdog.cancel()
        if clear and self.last is not None:
            with contextlib.suppress(Exception):
                await self.clear()
        await self._close()


class LocationService:
    def __init__(self, devices: DeviceManager):
        self.devices = devices
        self.sessions: dict[str, LocationSession] = {}

    def session(self, udid: str) -> LocationSession:
        self.devices.get(udid)  # raises for unknown devices
        if udid not in self.sessions:
            self.sessions[udid] = LocationSession(self.devices, udid)
        return self.sessions[udid]

    async def shutdown(self, clear: bool = False) -> None:
        for session in self.sessions.values():
            await session.shutdown(clear)
