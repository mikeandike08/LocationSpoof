"""PyInstaller entry point: the packaged app is the tray launcher.

A crash during startup is also written to crash.log in the data folder, since the windowed app's
error dialog is easy to lose and hard to copy from.
"""

import multiprocessing
import sys
import traceback


def _write_crash_log() -> None:
    try:
        from backend.paths import DATA_DIR

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "crash.log").write_text(traceback.format_exc(), encoding="utf-8")
    except Exception:
        pass


if __name__ == "__main__":
    multiprocessing.freeze_support()
    try:
        from backend.launcher import main

        main()
    except SystemExit:
        raise
    except BaseException:
        _write_crash_log()
        if "--smoke-test" in sys.argv:
            traceback.print_exc()
            sys.exit(1)  # no dialog in the build check
        raise
