# LocationSpoof on a Raspberry Pi (theoretical)

> **Status: theoretical.** This build was written without a Raspberry Pi to test on. Everything except
> the Display HAT Mini driver (`backend/pi/drivers.py`) runs and is tested on a computer through the
> **simulator** below. The first real Pi run may need fixes; see [Unknowns](#unknowns).

A pocket device: a Raspberry Pi with a 2" screen and four buttons, plugged into the iPhone. Each button
teleports the phone to a preset place; holding any button puts the phone back on its real location.
No computer needed once it's set up.

```
   A  ┌───────────────────────────┐  X
 (1)  │ LocationSpoof   [SPOOFING]│ (3)
      │ A1 Home          Gym 3X   │
      │ SPOOFING                  │      LED: green = ready, blue = spoofing,
      │ Home                      │           amber = setting up, red = problem
      │ 40.71280, -74.00600       │
   B  │ B2 Work        Beach 4Y   │  Y
 (2)  └───────────────────────────┘ (4)
```

## Hardware
| Part | Suggested | Notes |
|---|---|---|
| Board | **Raspberry Pi Zero 2 W** (or a Pi 4 if the Zero is too slow) | Pi 5 works but draws far more power. |
| Screen + buttons | **Pimoroni Display HAT Mini**: 2.0" 320×240 LCD, buttons A/B/X/Y, RGB LED | The only display this build has a driver for. |
| Battery | PiSugar 3 (Zero) or any USB power bank | The phone will try to charge from the Pi: a Zero can't supply much. |
| Cable | USB OTG (micro-USB) to Lightning / USB-C | The Zero has one data port. |
| Buzzer (optional) | Any 3.3 V active piezo buzzer on a free GPIO | Set `LOCSPOOF_BUZZER_PIN` (BCM number) in the service file. |

## Install
On **Raspberry Pi OS Trixie (64-bit)**, which ships Python 3.13:
```bash
git clone https://github.com/mikeandike08/LocationSpoof.git
cd LocationSpoof
bash pi/install.sh
sudo reboot
```
`pi/install.sh` installs `usbmuxd` and build tools, enables SPI, creates `.venv`, installs
`requirements.txt` + `pi/requirements-pi.txt`, and installs a systemd service that starts the device at
boot (`pi/locationspoof.service`). Data (saved places, logs) lives in `~/.local/share/LocationSpoof`.

```bash
sudo systemctl status locationspoof     # running?
journalctl -u locationspoof -f          # live logs
sudo systemctl stop locationspoof       # stop (restores the iPhone's real location)
```

## Using it
1. **Assign presets** (once): the full web app still runs on the Pi, but only on the Pi itself
   (`127.0.0.1`). From a laptop on the same network, tunnel to it over SSH:
   ```bash
   ssh -L 8765:localhost:8765 pi@<pi-address>
   ```
   Then open http://localhost:8765, save some places, and click the **#** button next to a place to put
   it on device button 1–4 (click again to cycle; a slot can belong to one place at a time).
2. **Plug in the iPhone.** The device prepares it automatically: tap **Trust** on the phone when asked;
   if Developer Mode is off, the switch is revealed in Settings and the screen tells you where. Once
   the chip says **READY**, it's set.
3. **Short press** A / B / X / Y: teleport to preset 1 / 2 / 3 / 4.
   **Hold** any button for a second: stop any route and restore the real location.
4. Problems (disconnect, real location showing) turn the screen frame and LED red and beep; a chirp
   means it recovered. Routes started from the web app show their progress on screen.

## Develop without a Pi: the simulator
Run the device on any computer; the screen and buttons become a web page:
```bash
python -m backend.pi --driver sim            # then open http://127.0.0.1:8765/pi-sim
```
Clicks are short presses and holding a button for a second is a long press. The LED and buzzer are
shown under the screen. It drives a real iPhone if one is plugged into the computer.

## How it's built
| File | What it does |
|---|---|
| `backend/pi/__main__.py` | Entry point: runs the normal web app and the controller on one event loop. |
| `backend/pi/controller.py` | Buttons → teleports, auto-prepare, status screen, LED/buzzer from the app's alerts. |
| `backend/pi/screen.py` | Draws the 320×240 status screen with Pillow (no hardware). |
| `backend/pi/drivers.py` | `DisplayHatMiniDriver` (hardware) and `SimDriver` (web page). |
| `web/pi-sim.html` | The simulator page. |
| `pi/install.sh`, `pi/locationspoof.service`, `pi/requirements-pi.txt` | Install, boot service, Pi-only packages. |

Presets are stored on the saved places themselves (`"preset": 1-4` in `places.json`), so the desktop
app and the Pi share the same data format.

## Unknowns
To check on the first real Pi, roughly in order:
1. **Installing on ARM:** some dependencies (`pylzss`, `qh3`, PyAV) may compile from source on the Pi
   (slow on a Zero, maybe 20–40 min) if there's no prebuilt wheel.
2. **The Display HAT Mini driver:** written against the `displayhatmini` library's documented API
   (`display()`, `read_button()`, `set_led()`, `set_backlight()`), never run. Button polling and
   long-press timing live in the driver.
3. **Zero 2 W performance:** the developer tunnel runs a network stack in Python; location updates are
   tiny, but building the tunnel involves heavy cryptography. Fall back to a Pi 4 if it's too slow.
4. **Re-mounting after a phone restart without internet:** the disk image is cached after the first
   download, and pymobiledevice3 asks the phone for its existing signature before contacting Apple, so
   it's *probably* offline-safe. If not, turn on Personal Hotspot on the iPhone so the Pi gets internet
   over the same USB cable (untested alongside the developer connection).
5. **Wi-Fi to the phone isn't supported on the Pi:** Linux's usbmuxd doesn't list Wi-Fi iPhones the
   way Apple's does on Mac/Windows. USB only.
6. **Power:** a phone charging from a Zero can brown it out; power both from one decent supply.
