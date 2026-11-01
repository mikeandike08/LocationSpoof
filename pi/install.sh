#!/bin/bash
# Installs LocationSpoof on a Raspberry Pi as a device that starts at boot.
# THEORETICAL: written without a Pi to test on; see docs/raspberry-pi.md.
#
#   git clone https://github.com/mikeandike08/LocationSpoof.git && cd LocationSpoof
#   bash pi/install.sh          (as the normal user, e.g. "pi"; it uses sudo where needed)
#
# Target: Raspberry Pi OS Trixie (Debian 13), 64-bit, which ships Python 3.13.
set -euo pipefail
cd "$(dirname "$0")/.."
APP_DIR="$PWD"
RUN_USER="${SUDO_USER:-$USER}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
DATA_DIR="$RUN_HOME/.local/share/LocationSpoof"

echo "==> Installing system packages"
sudo apt-get update
# usbmuxd: talks to iPhones over USB. build-essential/python3-dev: some Python packages
# (pylzss, qh3, ...) may have to compile on ARM if there's no prebuilt wheel.
sudo apt-get install -y python3 python3-venv python3-dev build-essential usbmuxd \
  libjpeg-dev zlib1g-dev libffi-dev libssl-dev git

if ! python3 -c 'import sys; sys.exit(sys.version_info[:2] < (3, 13))'; then
  echo "!! Python $(python3 -V) found; 3.13+ is recommended (Raspberry Pi OS Trixie)."
  echo "   On 3.12 and older the iOS 17.4+ tunnel needs sslpsk_pmd3, which must compile here."
fi

echo "==> Enabling SPI (for the Display HAT Mini)"
sudo raspi-config nonint do_spi 0 || echo "!! couldn't enable SPI automatically; use: sudo raspi-config"
# GPIO/SPI access without root
sudo usermod -aG spi,gpio "$RUN_USER" || true

echo "==> Creating the Python environment (this can take a while on a Pi Zero)"
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install -q -U pip
.venv/bin/pip install -r requirements.txt -r pi/requirements-pi.txt
.venv/bin/python -c "import pymobiledevice3, fastapi, PIL; print('Python packages OK')"

echo "==> Installing the boot service"
mkdir -p "$DATA_DIR"
sed -e "s#@APP_DIR@#$APP_DIR#g" -e "s#@USER@#$RUN_USER#g" -e "s#@DATA_DIR@#$DATA_DIR#g" \
  pi/locationspoof.service | sudo tee /etc/systemd/system/locationspoof.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now locationspoof

echo
echo "Done. LocationSpoof starts at boot. Useful commands:"
echo "  sudo systemctl status locationspoof      # is it running?"
echo "  journalctl -u locationspoof -f           # live logs"
echo "  ssh -L 8765:localhost:8765 $RUN_USER@<pi-address>   # then open http://localhost:8765 on your laptop"
echo "A reboot is needed once for SPI and group changes: sudo reboot"
