"""Build the LocationSpoof installer for the current OS.

    python packaging/build.py            (from the repo root, inside the project's venv)

Windows: dist/installer/LocationSpoof-Setup-<version>.exe   (needs Inno Setup 6)
macOS:   dist/installer/LocationSpoof-<version>-macOS-<arch>.dmg

Installers can't be cross-built: run this on each OS (CI does both, see .github/workflows/installers.yml).
"""

import argparse
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.launcher import icon_image  # noqa: E402
from backend.version import __version__  # noqa: E402

ICONS = ROOT / "build" / "icons"
DIST = ROOT / "dist"


def make_icons() -> None:
    """App icons from packaging/icon.png (1024x1024) if present, else the pin the tray draws."""
    from PIL import Image

    ICONS.mkdir(parents=True, exist_ok=True)
    custom = ROOT / "packaging" / "icon.png"
    big = Image.open(custom).convert("RGBA").resize((1024, 1024)) if custom.exists() else icon_image(1024)
    big.save(ICONS / "LocationSpoof.ico", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    big.save(ICONS / "LocationSpoof.icns")
    print(f"icons -> {ICONS}")


def pyinstaller() -> None:
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--distpath", str(DIST), "--workpath", str(ROOT / "build" / "pyinstaller"),
         str(ROOT / "packaging" / "LocationSpoof.spec")],
        check=True,
    )


def smoke_test() -> None:
    """Run the bundled app's self-check, so a broken bundle fails the build instead of the user."""
    exe = DIST / ("LocationSpoof.app/Contents/MacOS/LocationSpoof" if sys.platform == "darwin" else "LocationSpoof/LocationSpoof")
    if sys.platform == "win32":
        exe = exe.with_suffix(".exe")
    data = ROOT / "build" / "smoke-data"
    shutil.rmtree(data, ignore_errors=True)
    env = {**os.environ, "LOCSPOOF_DATA_DIR": str(data)}
    result = subprocess.run([str(exe), "--smoke-test"], env=env, timeout=120)
    for name in ("smoke-test.log", "crash.log"):
        if (data / name).exists():
            print(f"--- {name}\n" + (data / name).read_text(encoding="utf-8"))
    if result.returncode != 0:
        sys.exit(f"Smoke test of the bundled app failed (exit {result.returncode})")
    print("smoke test passed")


def find_iscc() -> str:
    candidates = [
        shutil.which("iscc"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"),
        r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        r"C:\Program Files\Inno Setup 6\ISCC.exe",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    sys.exit("Inno Setup 6 not found. Install it (winget install JRSoftware.InnoSetup) and run again.")


def windows_installer(version: str) -> None:
    subprocess.run(
        [find_iscc(), f"/DAppVersion={version}", str(ROOT / "packaging" / "windows" / "LocationSpoof.iss")],
        check=True,
    )


def _macos_version(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in text.split("."))


def _min_macos_versions(otool_output: str) -> list[str]:
    """The minimum-macOS values in `otool -l` output: only LC_BUILD_VERSION (minos) and
    LC_VERSION_MIN_MACOSX (version). Other load commands have "version" lines too (e.g.
    LC_SOURCE_VERSION), which aren't macOS versions."""
    found = []
    for block in otool_output.split("Load command")[1:]:
        if re.search(r"^\s*cmd LC_BUILD_VERSION$", block, re.M):
            # platform 1 = macOS; skip iOS/simulator slices if any
            if re.search(r"^\s*platform (1|MACOS)$", block, re.M):
                found += re.findall(r"^\s*minos (\d+(?:\.\d+)*)$", block, re.M)
        elif re.search(r"^\s*cmd LC_VERSION_MIN_MACOSX$", block, re.M):
            found += re.findall(r"^\s*version (\d+(?:\.\d+)*)$", block, re.M)
    return found


def check_macos_minimum() -> None:
    """Fail the build if any binary in the .app needs a newer macOS than we promise (MACOSX_DEPLOYMENT_TARGET).

    Each compiled file records the oldest macOS it runs on (LC_BUILD_VERSION "minos", or the older
    LC_VERSION_MIN_MACOSX "version"). One dependency built for a newer macOS is enough to make the whole
    app crash on older Macs, so check them all.
    """
    target = os.environ.get("MACOSX_DEPLOYMENT_TARGET", "11.0")
    app = DIST / "LocationSpoof.app"
    newest: dict[str, str] = {}
    for path in app.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        if path.suffix not in (".so", ".dylib") and not os.access(path, os.X_OK) and path.name != "Python":
            continue
        result = subprocess.run(["otool", "-l", str(path)], capture_output=True, text=True)
        if result.returncode != 0 or "Load command" not in result.stdout:
            continue  # not a Mach-O binary
        versions = _min_macos_versions(result.stdout)
        if versions:
            newest[str(path.relative_to(app))] = max(versions, key=_macos_version)
    if not newest:
        sys.exit("No Mach-O binaries found in the app; is it built?")
    too_new = {p: v for p, v in newest.items() if _macos_version(v) > _macos_version(target)}
    overall = max(newest.values(), key=_macos_version)
    arch = subprocess.run(["lipo", "-archs", str(app / "Contents" / "MacOS" / "LocationSpoof")],
                          capture_output=True, text=True).stdout.strip()
    print(f"macOS check: {len(newest)} binaries, architectures [{arch}], oldest supported macOS = {overall} "
          f"(target {target})")
    if too_new:
        for p, v in sorted(too_new.items(), key=lambda kv: _macos_version(kv[1]), reverse=True)[:15]:
            print(f"  needs macOS {v}: {p}")
        sys.exit(f"{len(too_new)} binaries need a newer macOS than {target}; pin an older dependency or raise the target")


def mac_dmg(version: str) -> None:
    subprocess.run(["bash", str(ROOT / "packaging" / "macos" / "make_dmg.sh"), version], check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", default=None, help=f"installer version (default: {__version__}, or the CI tag)")
    parser.add_argument("--skip-app", action="store_true", help="reuse the existing PyInstaller output")
    args = parser.parse_args()

    version = args.version
    if version is None and os.environ.get("GITHUB_REF_TYPE") == "tag":
        version = os.environ["GITHUB_REF_NAME"].lstrip("v")
    version = version or __version__

    make_icons()
    if not args.skip_app:
        pyinstaller()
    if sys.platform == "darwin":
        check_macos_minimum()
    smoke_test()
    if sys.platform == "win32":
        windows_installer(version)
    elif sys.platform == "darwin":
        mac_dmg(version)
    else:
        print(f"No installer for {platform.system()}; the app is in {DIST / 'LocationSpoof'}")
        return
    print("\nInstallers:")
    for f in sorted((DIST / "installer").glob("*")):
        print(f"  {f}  ({f.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
