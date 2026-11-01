"""Raspberry Pi build: a pocket device with a small screen and preset buttons.

    python -m backend.pi                 (on the Pi; see docs/raspberry-pi.md)
    python -m backend.pi --driver sim    (anywhere: the screen and buttons as a web page at /pi-sim)

The web app runs exactly as on a computer; this package adds a controller that turns button presses
into teleports to preset places, draws a status screen, and lights an LED (and an optional buzzer)
from the same alerts the web UI uses. Hardware access lives only in drivers.py.

THEORETICAL: written without a Raspberry Pi to test on. Everything except the Display HAT Mini
driver runs and is tested through the simulator driver.
"""
