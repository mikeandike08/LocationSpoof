"""Hardware drivers: where the screen, buttons, LED and buzzer actually live.

* DisplayHatMiniDriver: Pimoroni Display HAT Mini (2.0" 320x240 LCD, buttons A/B/X/Y, RGB LED),
  plus an optional piezo buzzer on a GPIO pin (LOCSPOOF_BUZZER_PIN, BCM numbering).
  THEORETICAL: written against the `displayhatmini` library's documented API, never run on a Pi.
* SimDriver: the same screen and buttons as a web page (/pi-sim), for developing anywhere.

A driver reports button presses as on_button(name, long_press); the controller does the rest.
"""

import io
import logging
import os
import threading
import time
from typing import Callable, Optional

from PIL import Image

logger = logging.getLogger("locationspoof.pi.drivers")

LONG_PRESS_S = 1.0
POLL_S = 0.02

ButtonCallback = Callable[[str, bool], None]


class Driver:
    name = "base"

    def start(self, on_button: ButtonCallback) -> None:
        raise NotImplementedError

    def show(self, image: Image.Image) -> None:
        raise NotImplementedError

    def set_led(self, r: float, g: float, b: float) -> None:
        """Brightness 0..1 per channel."""

    def beep(self, pattern: str) -> None:
        """pattern: 'alarm' (something went wrong) or 'ok' (it recovered)."""

    def stop(self) -> None:
        pass


class DisplayHatMiniDriver(Driver):
    name = "hat"
    BUZZ_PATTERNS = {"alarm": [(0.15, 0.1)] * 3, "ok": [(0.06, 0.0)]}  # (on, off) seconds

    def __init__(self) -> None:
        from displayhatmini import DisplayHATMini  # Pi only: pip install displayhatmini

        self._buffer = Image.new("RGB", (DisplayHATMini.WIDTH, DisplayHATMini.HEIGHT))
        self._hat = DisplayHATMini(self._buffer, backlight_pwm=True)
        self._hat.set_backlight(1.0)
        self._buttons = {
            "A": DisplayHATMini.BUTTON_A,
            "B": DisplayHATMini.BUTTON_B,
            "X": DisplayHATMini.BUTTON_X,
            "Y": DisplayHATMini.BUTTON_Y,
        }
        self._stop = threading.Event()
        self._lock = threading.Lock()  # the display and LED share one SPI/GPIO handle
        self._buzzer = None
        pin = os.environ.get("LOCSPOOF_BUZZER_PIN")
        if pin:
            try:
                from gpiozero import Buzzer

                self._buzzer = Buzzer(int(pin))
            except Exception as e:
                logger.warning(f"buzzer on GPIO {pin} unavailable: {e!r}")

    def start(self, on_button: ButtonCallback) -> None:
        threading.Thread(target=self._poll, args=(on_button,), name="pi-buttons", daemon=True).start()

    def _poll(self, on_button: ButtonCallback) -> None:
        """Poll the buttons: a press is 'long' once held LONG_PRESS_S (fires while still held)."""
        down_at: dict[str, Optional[float]] = {b: None for b in self._buttons}
        fired_long: dict[str, bool] = {b: False for b in self._buttons}
        while not self._stop.is_set():
            now = time.monotonic()
            for name, pin in self._buttons.items():
                with self._lock:
                    pressed = bool(self._hat.read_button(pin))
                if pressed and down_at[name] is None:
                    down_at[name], fired_long[name] = now, False
                elif pressed and not fired_long[name] and now - down_at[name] >= LONG_PRESS_S:
                    fired_long[name] = True
                    on_button(name, True)
                elif not pressed and down_at[name] is not None:
                    if not fired_long[name]:
                        on_button(name, False)
                    down_at[name] = None
            time.sleep(POLL_S)

    def show(self, image: Image.Image) -> None:
        with self._lock:
            self._buffer.paste(image)
            self._hat.display()

    def set_led(self, r: float, g: float, b: float) -> None:
        with self._lock:
            self._hat.set_led(r, g, b)

    def beep(self, pattern: str) -> None:
        if self._buzzer is None:
            return
        steps = self.BUZZ_PATTERNS.get(pattern, [])

        def play():
            for on, off in steps:
                self._buzzer.on()
                time.sleep(on)
                self._buzzer.off()
                time.sleep(off)

        threading.Thread(target=play, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            self._hat.set_led(0, 0, 0)
            self._hat.set_backlight(0.0)


class SimDriver(Driver):
    """The device as a web page: the screen is served as a PNG, the buttons are clickable."""

    name = "sim"

    def __init__(self) -> None:
        self.frame = b""
        self.frame_id = 0
        self.led = (0.0, 0.0, 0.0)
        self.last_beep: Optional[str] = None
        self.beep_id = 0
        self._on_button: Optional[ButtonCallback] = None

    def start(self, on_button: ButtonCallback) -> None:
        self._on_button = on_button

    def press(self, button: str, long_press: bool) -> None:
        if self._on_button:
            self._on_button(button, long_press)

    def show(self, image: Image.Image) -> None:
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        self.frame = buf.getvalue()
        self.frame_id += 1

    def set_led(self, r: float, g: float, b: float) -> None:
        self.led = (r, g, b)

    def beep(self, pattern: str) -> None:
        self.last_beep = pattern
        self.beep_id += 1


def create(name: str) -> Driver:
    """'hat', 'sim', or 'auto' (the HAT if its library imports, else the simulator)."""
    if name == "sim":
        return SimDriver()
    if name == "hat":
        return DisplayHatMiniDriver()
    try:
        return DisplayHatMiniDriver()
    except Exception as e:
        logger.warning(f"Display HAT Mini unavailable ({e!r}); using the simulator at /pi-sim")
        return SimDriver()
