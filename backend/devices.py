"""Device discovery, preparation (pair / Developer Mode / disk image) and tunnels.

Tunnels come from pymobiledevice3's TunneldCore running inside this process. TunneldCore only
reacts to usbmux plug/unplug events, so a tunnel that dies while the cable stays connected is
never rebuilt. A watchdog here rescans every few seconds, rebuilds missing tunnels with backoff,
replaces tunnels that stop answering, and records what happened so the UI can explain it.
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
from pymobiledevice3.remote.common import TunnelProtocol
from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
from pymobiledevice3.remote.tunnel_service import CoreDeviceTunnelProxy
from pymobiledevice3.services.amfi import AmfiService
from pymobiledevice3.services.mobile_image_mounter import auto_mount

from backend.errors import CABLE_HINT, DeviceError, explain

logger = logging.getLogger("locationspoof.devices")

TUNNEL_MIN_VERSION = Version("17.0")
INFO_TTL_S = 30
SCAN_INTERVAL_S = 2.5
FORGET_DISCONNECTED_S = 15 * 60
RSD_CONNECT_TIMEOUT_S = 10


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
        getattr(logger, "warning" if level != "info" else "info")(f"[{self.name or self.udid}] {message}")


class DeviceManager:
    def __init__(self) -> None:
        self.devices: dict[str, DeviceRecord] = {}
        self.tunneld = None
        self.tunneld_error: Optional[str] = None
        self._jobs: dict[str, asyncio.Task] = {}
        self._watchdog: Optional[asyncio.Task] = None
        self.usbmux_error: Optional[str] = None

    # ---------- lifecycle ----------

    async def start(self) -> None:
        if not is_root():
            self.tunneld_error = "Not running as administrator. Start with ./run.sh"
            logger.error(self.tunneld_error)
        else:
            try:
                from pymobiledevice3.tunneld.server import TunneldCore

                self.tunneld = TunneldCore()
                self.tunneld.start()
                logger.info("Tunnel manager started (USB + Wi-Fi)")
            except Exception as e:
                self.tunneld_error = f"Could not start the tunnel manager: {explain(e)}"
                logger.exception(self.tunneld_error)
        self._watchdog = asyncio.create_task(self._watch())

    async def stop(self) -> None:
        if self._watchdog:
            self._watchdog.cancel()
        for job in self._jobs.values():
            job.cancel()
        if self.tunneld is not None:
            await self.tunneld.close()

    # ---------- watchdog ----------

    async def _watch(self) -> None:
        while True:
            try:
                await self._scan()
                if self.tunneld is not None:
                    for record in list(self.devices.values()):
                        await self._maintain_tunnel(record)
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
        for udid in self._tunnel_only_udids():
            connections.setdefault(udid, set()).add("Wi-Fi")

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
            elif not record.busy and now - (record.disconnected_at or now) > FORGET_DISCONNECTED_S:
                del self.devices[udid]

    async def _maintain_tunnel(self, record: DeviceRecord) -> None:
        if not record.connected or not record.paired or not record.uses_tunnel or record.busy:
            return
        if record.developer_mode is False:
            return

        if self.tunnel_for(record.udid) is not None:
            if record.tunnel_state != "ok":
                record.log("Developer tunnel connected", "ok")
            record.tunnel_state = "ok"
            record.tunnel_attempts = 0
            record.tunnel_error = None
            return

        if record.tunnel_state == "ok":
            record.log("Developer tunnel dropped, rebuilding it", "warn")
            record.next_tunnel_try = 0
        if self._tunnel_pending(record.udid) or time.monotonic() < record.next_tunnel_try:
            if record.tunnel_state not in ("failed",):
                record.tunnel_state = "connecting"
            return
        await self._spawn_tunnel(record)

    async def _spawn_tunnel(self, record: DeviceRecord) -> None:
        from pymobiledevice3.tunneld.server import TunnelTask

        record.tunnel_attempts += 1
        record.tunnel_state = "connecting"
        # Back off 2, 4, 8 … 30 s between attempts.
        record.next_tunnel_try = time.monotonic() + min(30, 2**record.tunnel_attempts)
        try:
            async with await create_using_usbmux(record.udid, autopair=False) as lockdown:
                service = await CoreDeviceTunnelProxy.create(lockdown)
        except Exception as e:
            record.tunnel_state = "failed"
            record.tunnel_error = explain(e)
            record.log(f"Couldn't open the developer tunnel: {record.tunnel_error}", "error")
            return
        ident = f"locationspoof-{record.udid}"
        self.tunneld.tunnel_tasks[ident] = TunnelTask(
            udid=record.udid,
            task=asyncio.create_task(self.tunneld.start_tunnel_task(ident, service, protocol=TunnelProtocol.TCP)),
        )
        if record.tunnel_attempts > 1:
            record.log(f"Rebuilding developer tunnel (attempt {record.tunnel_attempts})", "warn")

    def _tunnel_pending(self, udid: str) -> bool:
        if self.tunneld is None:
            return False
        return any(
            t.udid == udid and t.tunnel is None and not t.task.done() for t in self.tunneld.tunnel_tasks.values()
        )

    def _tunnel_only_udids(self) -> set[str]:
        """Devices reachable over a Wi-Fi tunnel (bonjour/mobdev2 or usbmux 'Network').

        USB tunnels are excluded so a USB-only phone isn't shown as also being on Wi-Fi.
        """
        if self.tunneld is None:
            return set()
        return {
            t.udid
            for key, t in self.tunneld.tunnel_tasks.items()
            if t.udid and t.tunnel is not None and (key.startswith("mobdev2-") or key.endswith("-Network"))
        }

    # ---------- tunnels ----------

    def tunnel_for(self, udid: str):
        if self.tunneld is None:
            return None
        return self.tunneld.get_tunnel(udid)

    def drop_tunnel(self, udid: str, reason: str) -> None:
        """Discard a tunnel that exists but no longer answers; the watchdog builds a fresh one."""
        if self.tunneld is None:
            return
        record = self.devices.get(udid)
        if record:
            record.log(f"Developer tunnel stopped responding ({reason}), replacing it", "warn")
            record.tunnel_state = "connecting"
            record.next_tunnel_try = 0
        with contextlib.suppress(Exception):
            self.tunneld.cancel(udid)

    def _no_tunnel_error(self, udid: str) -> DeviceError:
        record = self.devices.get(udid)
        if self.tunneld_error:
            return DeviceError(self.tunneld_error)
        if record is None or not record.connected:
            return DeviceError(f"The iPhone is disconnected. {CABLE_HINT}")
        if record.developer_mode is False:
            return DeviceError("Developer Mode is off on the iPhone.")
        if record.tunnel_error:
            return DeviceError(f"Couldn't open the developer tunnel: {record.tunnel_error} Retrying automatically.")
        return DeviceError("The developer tunnel isn't up yet. Keep the iPhone unlocked; it's retrying automatically.")

    async def wait_for_tunnel(self, udid: str, timeout: float = 20.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            tunnel = self.tunnel_for(udid)
            if tunnel is not None:
                return tunnel
            record = self.devices.get(udid)
            if record is not None and not record.connected and time.monotonic() > deadline - timeout + 3:
                break  # unplugged: don't make the caller wait the full timeout
            await asyncio.sleep(0.5)
        return None

    async def open_rsd(self, udid: str) -> RemoteServiceDiscoveryService:
        for attempt in range(2):
            tunnel = await self.wait_for_tunnel(udid)
            if tunnel is None:
                raise self._no_tunnel_error(udid)
            rsd = RemoteServiceDiscoveryService((tunnel.address, tunnel.port))
            try:
                await asyncio.wait_for(rsd.connect(), timeout=RSD_CONNECT_TIMEOUT_S)
                return rsd
            except Exception as e:
                with contextlib.suppress(Exception):
                    await rsd.close()
                self.drop_tunnel(udid, explain(e))
                if attempt == 1:
                    raise DeviceError(f"The developer tunnel isn't responding. {explain(e)}") from e
        raise self._no_tunnel_error(udid)

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
            if self.tunneld_error:
                return {"level": "error", "message": self.tunneld_error}
            if r.tunnel_state == "failed":
                wait = max(0, int(r.next_tunnel_try - time.monotonic()))
                return {"level": "error", "message": f"Developer tunnel failed: {r.tunnel_error} Retrying in {wait}s."}
            if r.tunnel_state != "ok":
                extra = f" (attempt {r.tunnel_attempts})" if r.tunnel_attempts > 1 else ""
                return {"level": "busy", "message": f"Opening developer tunnel{extra}… keep the iPhone unlocked."}
        via = " + ".join(sorted(r.connections))
        return {"level": "ok", "message": f"Connected via {via}. Ready."}

    def describe(self, r: DeviceRecord) -> dict:
        tunnel = self.tunnel_for(r.udid)
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
            "tunnel": {"address": tunnel.address, "port": tunnel.port} if tunnel else None,
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
        rsd = await self.open_rsd(udid)
        try:
            record.busy = "Mounting developer disk image (first time downloads ~20 MB)"
            await self.mount(udid, rsd)
        finally:
            await rsd.close()

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
