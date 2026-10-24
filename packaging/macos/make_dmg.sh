#!/bin/bash
# Wraps dist/LocationSpoof.app in a drag-to-Applications disk image. Run by packaging/build.py.
#   usage: packaging/macos/make_dmg.sh <version>
set -euo pipefail
cd "$(dirname "$0")/../.."

VERSION="${1:?version required}"
APP=dist/LocationSpoof.app
STAGE=build/dmg
OUT="dist/installer/LocationSpoof-$VERSION-macOS-$(uname -m).dmg"

[ -d "$APP" ] || { echo "$APP not found; run PyInstaller first" >&2; exit 1; }
rm -rf "$STAGE"
mkdir -p "$STAGE" dist/installer
ditto "$APP" "$STAGE/LocationSpoof.app"  # ditto keeps the bundle's symlinks and signature intact
ln -s /Applications "$STAGE/Applications"
rm -f "$OUT"
hdiutil create -volname "LocationSpoof" -srcfolder "$STAGE" -ov -format UDZO "$OUT"
echo "$OUT"
