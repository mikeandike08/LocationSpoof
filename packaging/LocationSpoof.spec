# PyInstaller spec for LocationSpoof. Run through packaging/build.py, which makes the icons first.
#   Windows: dist/LocationSpoof/LocationSpoof.exe  (wrapped by packaging/windows/LocationSpoof.iss)
#   macOS:   dist/LocationSpoof.app                (wrapped by packaging/macos/make_dmg.sh)
# A folder build (not one-file) so the app starts fast and the installers can ship it as-is.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent
ICONS = ROOT / "build" / "icons"
sys.path.insert(0, str(ROOT))
from backend.version import __version__  # noqa: E402

datas = [(str(ROOT / "web"), "web")]
binaries = []
hiddenimports = collect_submodules("backend") + collect_submodules("uvicorn")

# pymobiledevice3 and friends load modules, data files and native libraries dynamically.
for package in ("pymobiledevice3", "developer_disk_image", "pytun_pmd3", "qh3", "sslpsk_pmd3"):
    try:
        d, b, h = collect_all(package)
    except Exception:
        continue
    datas += d
    binaries += b
    hiddenimports += h

# Several of these packages read their own version from package metadata at import time
# (e.g. pyimg4 calls importlib.metadata.version), so ship the metadata of the whole dependency tree.
for package in ("pymobiledevice3", "fastapi", "uvicorn", "httpx", "pystray"):
    datas += copy_metadata(package, recursive=True)

hiddenimports.append("pystray._win32" if sys.platform == "win32" else "pystray._darwin")

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LocationSpoof",
    console=False,  # tray app: no console window
    upx=False,
    icon=str(ICONS / ("LocationSpoof.ico" if sys.platform == "win32" else "LocationSpoof.icns")),
)

coll = COLLECT(exe, a.binaries, a.datas, name="LocationSpoof", upx=False)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="LocationSpoof.app",
        icon=str(ICONS / "LocationSpoof.icns"),
        bundle_identifier="io.github.locationspoof",
        version=__version__,
        info_plist={
            "CFBundleShortVersionString": __version__,
            "CFBundleVersion": __version__,
            "LSUIElement": True,  # menu-bar app: no Dock icon
            "NSHighResolutionCapable": True,
        },
    )
