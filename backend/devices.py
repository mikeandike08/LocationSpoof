"""Device discovery, preparation (pair / Developer Mode / disk image) and the developer tunnel.

The iOS 17+ developer tunnel is pymobiledevice3's *userspace* tunnel (UserspaceRsdTunnel): a
pure-Python network stack in this process, so it needs no root and no kernel utun device.
We deliberately don't use TunneldCore: its monitors periodically suspend Apple's `remoted`
daemon and race each other building tunnels, and its kernel tunnel writes packets with a
blocking write on the event loop, which can freeze the whole server when the link stalls.

Exactly one tunnel is kept (the userspace stack is process-global). A watchdog rescans every
few seconds, keeps the tunnel up for the active device, rebuilds it with backoff when it dies,
and records what happened so the UI can explain it.
"""

import asyncio
import contextlib
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from packaging.version import Version
from pymobiledevice3 import usbmux
from pymobiledevice3.exceptions import AlreadyMountedError, PyMobileDevice3Exception
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel
from pymobiledevice3.services.amfi import AmfiService
from pymobiledevice3.services.mobile_image_mounter import auto_mount

from backend.errors import CABLE_HINT, DeviceError, explain

logger = logging.getLogger("locationspoof.devices")

TUNNEL_MIN_VERSION = Version("17.0")
INFO_TTL_S = 30
SCAN_INTERVAL_S = 2.5
FORGET_DISCONNECTED_S = 15 * 60
TUNNEL_OPEN_TIMEOUT_S = 30


def is_root() -> bool:
    return os.geteuid() == 0


def _version(text: Optional[str]) -> Version:
    try:
        return Version(text or "0")
    except Exception:
        return Version("0")


def _clock() -> str:
    return time.strftime("%-I:%M:%S %p")


@dataclass
class DeviceRecord:
    udid: str
    name: Optional[str] = None
    product_type: Optional[str] = None
    ios_version: Optional[str] = None
    connections: set[str] = field(default_factory=set)  # "USB", "Wi-Fi"; empty = disconnected
    paired: Optional[bool] = None
    developer_mode: Optional[bool] = None
    wifi_enabled: Optional[bool] = None
    image_mounted: Optional[bool] = None  # None = unknown (checked/mounted on demand)
    info_fetched_at: float = 0.0
    info_error: Optional[str] = None
    busy: Optional[str] = None  # human-readable step while a job runs
    error: Optional[str] = None
    needs: Optional[str] = None  # "developer_mode" | "passcode"
    disconnected_at: Optional[float] = None
    # tunnel health
    tunnel_state: str = "none"  # none | connecting | ok | failed
    tunnel_attempts: int = 0
    tunnel_error: Optional[str] = None
    next_tunnel_try: float = 0.0
    events: deque = field(default_factory=lambda: deque(maxlen=12))

    @property
    def uses_tunnel(self) -> bool:
        return _version(self.ios_version) >= TUNNEL_MIN_VERSION

    @property
    def connected(self) -> bool:
        return bool(self.connections)

    def log(self, message: str, level: str = "info") -> None:
        # Collapse repeats so a flapping error doesn't flood the list.
        if self.events and self.events[0]["msg"] == message:
            self.events[0]["time"] = _clock()
            return
        self.events.appendleft({"time": _clock(), "msg": message, "level": level})
        log = logger.warning if level in ("warn", "error") else logger.info
        log(f"[{self.name or self.udid}] {message}")


class DeviceManager:
    def __init__(self) -> None:
        self.devices: dict[str, DeviceRecord] = {}
        self.tunnel: Optional[UserspaceRsdTunnel] = None
        self.tunnel_udid: Optional[str] = None
        self.tunnel_generation = 0  # bumps every time a new tunnel is opened
        self._tunnel_lock = asyncio.Lock()
        self.preferred_udid: Optional[str] = None  # device the user is working with
        self._jobs: dict[str, asyncio.Task] = {}
        self._watchdog: Optional[asyncio.Task] = None
        self.usbmux_error: Optional[str] = None

    # ---------- lifecycle ----------

    async def start(self) -> None:
        self._watchdog = asyncio.create_task(self._watch())

    async def stop(self) -> None:
        if self._watchdog:
            self._watchdog.cancel()
        for job in self._jobs.values():
            job.cancel()
        async with self._tunnel_lock:
            await self._close_tunnel()

    # ---------- watchdog ----------

    async def _watch(self) -> None:
        while True:
            try:
                await self._scan()
                target = self._tunnel_target()
                if target is not None:
                    await self._maintain_tunnel(target)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("device watchdog iteration failed")
            await asyncio.sleep(SCAN_INTERVAL_S)

    async def _scan(self) -> None:
        try:
            mux_devices = await usbmux.list_devices()
            if self.usbmux_error:
                logger.info("usbmuxd reachable again")
            self.usbmux_error = None
        except Exception as e:
            self.usbmux_error = explain(e)
            mux_devices = []

        connections: dict[str, set[str]] = {}
        for dev in mux_devices:
            connections.setdefault(dev.serial, set()).add("USB" if dev.is_usb else "Wi-Fi")

        now = time.time()
        for udid, conns in connections.items():
            record = self.devices.get(udid)
            if record is None:
                record = self.devices[udid] = DeviceRecord(udid=udid)
                record.log(f"Found iPhone via {' + '.join(sorted(conns))}")
            elif not record.connected:
                away = int(now - (record.disconnected_at or now))
                record.log(f"Reconnected via {' + '.join(sorted(conns))} after {away}s", "ok")
                record.next_tunnel_try = 0
                record.tunnel_attempts = 0
            elif conns != record.connections:
                record.log(f"Now connected via {' + '.join(sorted(conns))}")
            record.connections = conns
            record.disconnected_at = None
            if not record.busy:
                await self._refresh_info(record)

        for udid, record in list(self.devices.items()):
            if udid in connections:
                continue
            if record.connected:
                record.connections = set()
                record.disconnected_at = now
                record.tunnel_state = "none"
                record.log(f"iPhone disconnected. {CABLE_HINT}", "error")
                if self.tunnel_udid == udid:
                    async with self._tunnel_lock:
                        await self._close_tunnel()
            elif not record.busy and now - (record.disconnected_at or now) > FORGET_DISCONNECTED_S:
                del self.devices[udid]

    def _tunnel_target(self) -> Optional[DeviceRecord]:
        """The one device to keep a tunnel open for (the userspace stack allows only one)."""
        candidates = [
            r for r in self.devices.values()
            if r.connected and r.paired and r.uses_tunnel and r.developer_mode is not False
        ]
        if not candidates:
            return None
        for r in candidates:
            if r.udid == self.preferred_udid:
                return r
        for r in candidates:
            if r.udid == self.tunnel_udid:
                return r
        return candidates[0]

    async def _maintain_tunnel(self, record: DeviceRecord) -> None:
        if record.busy or self._tunnel_lock.locked():
            return
        if self.tunnel_alive(record.udid):
            return
        if record.tunnel_state == "ok":
            record.log("Developer tunnel dropped, rebuilding it", "warn")
            record.tunnel_state = "connecting"
            record.next_tunnel_try = 0
        if time.monotonic() < record.next_tunnel_try:
            return
        with contextlib.suppress(DeviceError):
            await self.get_rsd(record.udid)

    # ---------- tunnel ----------

    def tunnel_alive(self, udid: str) -> bool:
        return self.tunnel is not None and self.tunnel_udid == udid and self.tunnel.rsd is not None

    def tunnel_key(self, udid: str) -> Optional[int]:
        """Changes whenever the device's tunnel is replaced (used to detect stale channels)."""
        return self.tunnel_generation if self.tunnel_alive(udid) else None

    async def _close_tunnel(self) -> None:
        tunnel, self.tunnel, self.tunnel_udid = self.tunnel, None, None
        if tunnel is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(tunnel.aclose(), timeout=10)

    async def get_rsd(self, udid: str) -> RemoteServiceDiscoveryService:
        """Return the shared RSD for this device, opening (or reopening) the tunnel if needed.

        Callers must NOT close the returned RSD; the tunnel owns it.
        """
        async with self._tunnel_lock:
            if self.tunnel_alive(udid):
                return self.tunnel.rsd
            record = self.devices.get(udid)
            if record is None or not record.connected:
                raise DeviceError(f"The iPhone is disconnected. {CABLE_HINT}")
            if record.developer_mode is False:
                raise DeviceError("Developer Mode is off on the iPhone.")
            await self._close_tunnel()  # only one userspace tunnel per process

            record.tunnel_state = "connecting"
            record.tunnel_attempts += 1
            # Back off 2, 4, 8 … 30 s between failed attempts.
            record.next_tunnel_try = time.monotonic() + min(30, 2**record.tunnel_attempts)
            tunnel = UserspaceRsdTunnel(serial=udid, autopair=False)
            try:
                rsd = await asyncio.wait_for(tunnel.aopen(), timeout=TUNNEL_OPEN_TIMEOUT_S)
            except BaseException as e:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(tunnel.aclose(), timeout=10)
                if isinstance(e, asyncio.CancelledError):
                    raise
                record.tunnel_state = "failed"
                record.tunnel_error = explain(e)
                record.log(f"Couldn't open the developer tunnel: {record.tunnel_error}", "error")
                raise DeviceError(f"Couldn't open the developer tunnel: {record.tunnel_error}") from e

            self.tunnel, self.tunnel_udid = tunnel, udid
            self.tunnel_generation += 1
            record.tunnel_state = "ok"
            record.tunnel_attempts = 0
            record.tunnel_error = None
            record.log("Developer tunnel connected", "ok")
            return rsd

    async def reset_tunnel(self, udid: str, reason: str) -> None:
        """Throw away a tunnel that stopped working; the next get_rsd() builds a fresh one."""
        async with self._tunnel_lock:
            if self.tunnel_udid != udid:
                return
            record = self.devices.get(udid)
            if record:
                record.log(f"Developer tunnel stopped responding ({reason}), replacing it", "warn")
                record.tunnel_state = "connecting"
                record.next_tunnel_try = 0
            await self._close_tunnel()

    def _no_tunnel_error(self, udid: str) -> DeviceError:
        record = self.devices.get(udid)
        if record is None or not record.connected:
            return DeviceError(f"The iPhone is disconnected. {CABLE_HINT}")
        if record.developer_mode is False:
            return DeviceError("Developer Mode is off on the iPhone.")
        if record.tunnel_error:
            return DeviceError(f"Couldn't open the developer tunnel: {record.tunnel_error} Retrying automatically.")
        return DeviceError("The developer tunnel isn't up yet. Keep the iPhone unlocked; it's retrying automatically.")

    # ---------- info ----------

    async def _lockdown(self, udid: str, autopair: bool = False, pair_timeout: Optional[float] = None):
        record = self.devices.get(udid)
        conn = "USB" if record and "USB" in record.connections else None
        return await create_using_usbmux(udid, connection_type=conn, autopair=autopair, pair_timeout=pair_timeout)

    async def _refresh_info(self, record: DeviceRecord, force: bool = False) -> None:
        if not force and time.time() - record.info_fetched_at < INFO_TTL_S:
            return
        try:
            async with await self._lockdown(record.udid) as lockdown:
                info = lockdown.short_info
                record.name = info.get("DeviceName") or record.name
                record.product_type = info.get("ProductType") or record.product_type
                record.ios_version = info.get("ProductVersion") or record.ios_version
                record.paired = bool(lockdown.paired)
                if record.paired:
                    with contextlib.suppress(PyMobileDevice3Exception):
                        record.wifi_enabled = await lockdown.get_enable_wifi_connections()
                    if _version(record.ios_version) >= Version("16.0"):
                        with contextlib.suppress(PyMobileDevice3Exception):
                            record.developer_mode = await lockdown.get_developer_mode_status()
                    else:
                        record.developer_mode = True  # no Developer Mode before iOS 16
            record.info_fetched_at = time.time()
            record.info_error = None
        except Exception as e:
            record.info_error = explain(e)
            record.info_fetched_at = time.time() - INFO_TTL_S + 5  # retry soon
            logger.debug(f"info refresh failed for {record.udid}: {e!r}")

    async def list_devices(self) -> list[dict]:
        return [self.describe(r) for r in self.devices.values()]

    def _health(self, r: DeviceRecord) -> dict:
        if r.busy:
            return {"level": "busy", "message": r.busy}
        if not r.connected:
            since = time.strftime("%-I:%M %p", time.localtime(r.disconnected_at or time.time()))
            return {
                "level": "error",
                "message": f"Disconnected at {since}. {CABLE_HINT} Your spoofed location is re-applied automatically when it reconnects.",
            }
        if r.error:
            return {"level": "error", "message": r.error}
        if r.paired is False:
            return {"level": "warn", "message": "Not trusted yet. Click 'Trust & prepare', then tap 'Trust' on the iPhone."}
        if r.paired is None and r.info_error:
            return {"level": "warn", "message": r.info_error}
        if r.developer_mode is False:
            return {"level": "warn", "message": "Developer Mode is off."}
        if r.uses_tunnel:
            if r.tunnel_state == "failed":
                wait = max(0, int(r.next_tunnel_try - time.monotonic()))
                return {"level": "error", "message": f"Developer tunnel failed: {r.tunnel_error} Retrying in {wait}s."}
            if r.tunnel_state != "ok":
                extra = f" (attempt {r.tunnel_attempts})" if r.tunnel_attempts > 1 else ""
                return {"level": "busy", "message": f"Opening developer tunnel{extra}… keep the iPhone unlocked."}
        via = " + ".join(sorted(r.connections))
        return {"level": "ok", "message": f"Connected via {via}. Ready."}

    def describe(self, r: DeviceRecord) -> dict:
        alive = self.tunnel_alive(r.udid)
        return {
            "udid": r.udid,
            "name": r.name or "iPhone",
            "product_type": r.product_type,
            "ios_version": r.ios_version,
            "connections": sorted(r.connections),
            "connected": r.connected,
            "paired": r.paired,
            "developer_mode": r.developer_mode,
            "wifi_enabled": r.wifi_enabled,
            "image_mounted": r.image_mounted,
            "uses_tunnel": r.uses_tunnel,
            "tunnel": {"type": "userspace"} if alive else None,
            "tunnel_state": r.tunnel_state if r.uses_tunnel else "n/a",
            "busy": r.busy,
            "error": r.error,
            "needs": r.needs,
            "health": self._health(r),
            "events": list(r.events),
        }

    # ---------- jobs ----------

    def get(self, udid: str) -> DeviceRecord:
        record = self.devices.get(udid)
        if record is None:
            raise KeyError(f"Unknown device {udid}")
        return record

    def _run_job(self, udid: str, coro) -> None:
        existing = self._jobs.get(udid)
        if existing and not existing.done():
            raise RuntimeError("The device is busy with another step")
        record = self.get(udid)
        record.error = None
        record.needs = None

        async def wrapper():
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(f"job failed for {udid}")
                record.error = explain(e)
                if "passcode is set" in record.error:
                    record.needs = "passcode"
                record.log(record.error, "error")
            finally:
                record.busy = None
                record.info_fetched_at = 0

        self._jobs[udid] = asyncio.create_task(wrapper())

    # ---------- preparation steps ----------

    def start_prepare(self, udid: str) -> None:
        """Pair -> check Developer Mode -> mount the developer disk image."""
        self._run_job(udid, self._prepare(udid))

    async def _prepare(self, udid: str) -> None:
        record = self.get(udid)
        if not record.connected:
            raise DeviceError(f"The iPhone is disconnected. {CABLE_HINT}")

        record.busy = "Pairing: tap 'Trust' on the iPhone and enter your passcode"
        async with await self._lockdown(udid, autopair=True, pair_timeout=120) as lockdown:
            if not record.paired:
                record.log("Paired with this Mac", "ok")
            record.paired = True
            record.ios_version = lockdown.product_version
            if _version(record.ios_version) >= Version("16.0"):
                record.developer_mode = await lockdown.get_developer_mode_status()
            else:
                record.developer_mode = True

            if not record.developer_mode:
                record.needs = "developer_mode"
                raise DeviceError("Developer Mode is off.")

            if not record.uses_tunnel:
                record.busy = "Mounting developer disk image"
                await self.mount(udid, lockdown)
                return

        record.busy = "Opening developer tunnel"
        record.next_tunnel_try = 0
        self.preferred_udid = udid
        rsd = await self.get_rsd(udid)
        record.busy = "Mounting developer disk image (first time downloads ~20 MB)"
        await self.mount(udid, rsd)

    async def mount(self, udid: str, provider) -> None:
        record = self.get(udid)
        try:
            await auto_mount(provider)
            record.log("Developer disk image mounted", "ok")
        except AlreadyMountedError:
            pass
        record.image_mounted = True

    def start_reveal_developer_mode(self, udid: str) -> None:
        async def job():
            self.get(udid).busy = "Revealing the Developer Mode toggle"
            async with await self._lockdown(udid) as lockdown:
                await AmfiService(lockdown).reveal_developer_mode_option_in_ui()
            record = self.get(udid)
            record.needs = "developer_mode"
            record.error = (
                "On the iPhone: Settings > Privacy & Security > Developer Mode > On. "
                "It will restart; after it boots, confirm 'Turn On', then click Prepare again."
            )

        self._run_job(udid, job())

    def start_enable_developer_mode(self, udid: str) -> None:
        async def job():
            record = self.get(udid)
            record.busy = "Enabling Developer Mode. The iPhone will restart; confirm 'Turn On' when it boots"
            async with await self._lockdown(udid) as lockdown:
                await AmfiService(lockdown).enable_developer_mode(enable_post_restart=True)
            record.developer_mode = True
            record.log("Developer Mode enabled", "ok")

        self._run_job(udid, job())

    def start_enable_wifi(self, udid: str) -> None:
        async def job():
            self.get(udid).busy = "Enabling Wi-Fi connections"
            async with await self._lockdown(udid) as lockdown:
                await lockdown.set_enable_wifi_connections(True)
            record = self.get(udid)
            record.wifi_enabled = True
            record.log("Wi-Fi connections enabled", "ok")

        self._run_job(udid, job())
