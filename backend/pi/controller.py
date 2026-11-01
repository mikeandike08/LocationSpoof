"""The device's brain: buttons -> teleports, the status screen, the LED and buzzer.

It drives the same objects the web API uses (devices, location sessions, the route player, saved
places, alerts), so the Pi behaves exactly like the desktop app, just without a browser:

* short press A/B/X/Y: teleport to the saved place on preset button 1-4 (assigned in the web UI);
* long press (any button): stop any route and restore the iPhone's real location;
* a connected iPhone is prepared automatically (trust prompt, Developer Mode hint, disk image),
  since there's no web page to click "Trust & prepare" on;
* alerts (disconnect, real location showing, ...) light the screen frame and LED red and beep.
"""

import asyncio
import contextlib
import logging
import time
from typing import Optional

from backend.devices import DeviceManager, DeviceRecord
from backend.errors import explain
from backend.geo import haversine_m
from backend.pi.drivers import Driver
from backend.pi.screen import BUTTON_SLOTS, ScreenState, render
from backend.places import PlacesStore
from backend.playback import RoutePlayer

logger = logging.getLogger("locationspoof.pi")

TICK_S = 0.25
REDRAW_S = 1.0
TOAST_S = 2.5
PREPARE_RETRY_S = 60  # don't re-trigger an automatic prepare for the same phone more often than this
SAME_PLACE_M = 15  # a spoofed spot this close to a preset shows the preset's name


class PiController:
    def __init__(self, driver: Driver, devices: DeviceManager, locations, places: PlacesStore,
                 player: RoutePlayer, alerts) -> None:
        self.driver = driver
        self.devices = devices
        self.locations = locations
        self.places = places
        self.player = player
        self.alerts = alerts
        self._buttons: asyncio.Queue = asyncio.Queue()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._toast: Optional[str] = None
        self._toast_until = 0.0
        self._prepared_at: dict[str, float] = {}
        self._revealed: set[str] = set()
        self._last_frame: Optional[ScreenState] = None
        self._led_override_until = 0.0

    # ---------- input ----------

    def on_button(self, button: str, long_press: bool) -> None:
        """Called by the driver, from any thread."""
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._buttons.put_nowait, (button, long_press))

    async def _handle_button(self, button: str, long_press: bool) -> None:
        logger.info(f"button {button} {'long' if long_press else 'short'} press")
        record = self._device()
        if long_press:
            await self._restore_real(record)
            return
        slot = BUTTON_SLOTS.get(button)
        place = self.places.presets().get(slot)
        if place is None:
            self._show_toast(f"Button {slot}: no place assigned")
            return
        if record is None or not self._ready(record):
            self._show_toast("iPhone not ready")
            return
        self.player.stop()  # a teleport ends any running route, as in the web UI
        self._show_toast(f"Going to {place['name']}")
        try:
            await self.locations.session(record.udid).set(place["lat"], place["lng"])
        except Exception as e:
            self._show_toast("Couldn't move the iPhone")
            logger.warning(f"preset {slot} failed: {explain(e)}")

    async def _restore_real(self, record: Optional[DeviceRecord]) -> None:
        self.player.stop()
        if record is None or not record.connected:
            self._show_toast("No iPhone connected")
            return
        session = self.locations.sessions.get(record.udid)
        if session is None or session.last is None:
            self._show_toast("Already real location")
            return
        try:
            await session.clear()
            self._show_toast("Real location")
        except Exception as e:
            self._show_toast("Couldn't restore")
            logger.warning(f"restore failed: {explain(e)}")

    def _show_toast(self, text: str) -> None:
        self._toast, self._toast_until = text, time.monotonic() + TOAST_S

    # ---------- device state ----------

    def _device(self) -> Optional[DeviceRecord]:
        """The phone to act on: the one in use, else the first connected one."""
        records = list(self.devices.devices.values())
        for r in records:
            if r.udid == self.devices.preferred_udid and r.connected:
                return r
        return next((r for r in records if r.connected), None)

    def _ready(self, r: DeviceRecord) -> bool:
        return bool(r.connected and r.paired and r.developer_mode and (not r.uses_tunnel or self.devices.tunnel_alive(r.udid)))

    def _auto_prepare(self, r: DeviceRecord) -> None:
        """No browser to click 'Trust & prepare' on: do it automatically, like the web UI does when it can."""
        if not r.connected or r.busy or (r.image_mounted and self._ready(r)):
            return  # busy, or already all set
        if r.developer_mode is False:
            if r.udid not in self._revealed:  # make the Developer Mode switch appear in Settings, once
                self._revealed.add(r.udid)
                with contextlib.suppress(Exception):
                    self.devices.start_reveal_developer_mode(r.udid)
            return
        now = time.monotonic()
        if now - self._prepared_at.get(r.udid, -1e9) < PREPARE_RETRY_S:
            return  # tried recently (it may be waiting on the Trust prompt, or failed); don't hammer it
        # Prepare = pair (Trust prompt) -> check Developer Mode -> developer tunnel -> mount disk image.
        self._prepared_at[r.udid] = now
        with contextlib.suppress(Exception):
            self.devices.start_prepare(r.udid)

    # ---------- screen ----------

    def _preset_names(self) -> dict[int, str]:
        return {slot: p["name"] for slot, p in self.places.presets().items()}

    def _place_name(self, lat: float, lng: float) -> Optional[str]:
        for p in self.places.list():
            if haversine_m((lat, lng), (p["lat"], p["lng"])) <= SAME_PLACE_M:
                return p["name"]
        return None

    def build_state(self) -> ScreenState:
        s = ScreenState(presets=self._preset_names())
        r = self._device()
        if self._toast and time.monotonic() < self._toast_until:
            s.toast = self._toast
        if r is None:
            s.chip, s.level, s.headline = "No iPhone", "off", "Waiting for iPhone"
            s.message = "Plug the iPhone in with a cable and unlock it."
            return s
        s.alarm = any(udid == r.udid for udid, _ in self.alerts.active)
        health = self.devices.describe(r)["health"]
        if s.alarm:
            problem = next(a for (udid, _), a in self.alerts.active.items() if udid == r.udid)
            s.chip, s.level = "Problem", "error"
            s.headline, s.message = problem["title"], problem["message"]
        elif not self._ready(r) or r.busy:
            s.chip, s.level = "Setup", "busy" if health["level"] in ("busy", "ok") else health["level"]
            s.headline = r.name or "iPhone"
            s.message = health["message"]
            if r.paired is False and not r.busy:
                s.message = "Tap 'Trust' on the iPhone and enter its passcode."
            elif r.developer_mode is False:
                s.message = ("Turn on Developer Mode: on the iPhone, Settings > Privacy & Security > Developer Mode. "
                             "It restarts; confirm 'Turn On'.")
            return s

        st = self.player.status
        session = self.locations.sessions.get(r.udid)
        if self.player.device_udid == r.udid:
            if not s.alarm:
                s.chip, s.level = "Route", "spoofing"
                if st.holding:
                    s.headline, s.title = "Arrived", "Holding at destination"
                else:
                    pct = 100 * st.traveled_m / st.total_m if st.total_m else 0
                    s.headline = "Driving route" if st.state == "playing" else st.state.capitalize()
                    s.title = f"{pct:.0f}%  ·  {st.speed_kmh / 1.609:.0f} mph"
            if st.position:
                s.subtitle = f"{st.position[0]:.5f}, {st.position[1]:.5f}"
        elif session is not None and session.last is not None:
            lat, lng = session.last
            if not s.alarm:
                s.chip, s.level, s.headline = "Spoofing", "spoofing", "Spoofing"
                s.title = self._place_name(lat, lng) or "Custom location"
            s.subtitle = f"{lat:.5f}, {lng:.5f}"
        elif not s.alarm:
            s.chip, s.level, s.headline = "Ready", "ok", "Real location"
            s.title = r.name or "iPhone"
            s.message = "Press a button to teleport. Hold any button to go back to the real location."
        return s

    # ---------- LED / buzzer ----------

    def _led_for(self, s: ScreenState) -> tuple[float, float, float]:
        return {
            "ok": (0.0, 0.25, 0.0),
            "spoofing": (0.0, 0.0, 0.35),
            "busy": (0.35, 0.18, 0.0),
            "warn": (0.35, 0.18, 0.0),
            "error": (0.6, 0.0, 0.0),
            "off": (0.0, 0.0, 0.0),
        }.get(s.level, (0.0, 0.0, 0.0))

    async def _on_alert(self, alert: dict) -> None:
        if alert["level"] == "critical" and not alert.get("quiet"):
            self._led_override_until = time.monotonic() + 3
            self.driver.set_led(1.0, 0.0, 0.0)
            self.driver.beep("alarm")
        elif alert["level"] == "recovered":
            self.driver.beep("ok")

    # ---------- main loop ----------

    async def run(self, server=None) -> None:
        self._loop = asyncio.get_running_loop()
        self.driver.start(self.on_button)
        self.driver.show(render(ScreenState()))
        while server is not None and not server.started:  # wait for the app's startup (device watchdog)
            await asyncio.sleep(0.1)
        queue = self.alerts.subscribe()
        last_draw = 0.0
        try:
            while True:
                with contextlib.suppress(asyncio.TimeoutError):
                    button, long_press = await asyncio.wait_for(self._buttons.get(), TICK_S)
                    await self._handle_button(button, long_press)
                    last_draw = 0.0  # redraw right away
                while not queue.empty():
                    await self._on_alert(queue.get_nowait())
                    last_draw = 0.0
                now = time.monotonic()
                if now - last_draw >= REDRAW_S or (self._toast and now >= self._toast_until):
                    if self._toast and now >= self._toast_until:
                        self._toast = None
                    record = self._device()
                    if record is not None:
                        self._auto_prepare(record)
                    state = self.build_state()
                    if state != self._last_frame:
                        self.driver.show(render(state))
                        self._last_frame = state
                    if now >= self._led_override_until:
                        self.driver.set_led(*self._led_for(state))
                    last_draw = now
        finally:
            self.alerts.unsubscribe(queue)
