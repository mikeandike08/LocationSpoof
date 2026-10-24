"""Keeps the computer from going to sleep while the iPhone is being spoofed.

A sleeping computer freezes the app, the developer tunnel dies, and iOS 17+ drops back to the
phone's real location until the computer wakes. When the user turns "Keep computer awake" on,
idle sleep is blocked for as long as a location is spoofed or a route is running, and allowed
again as soon as neither is. The display can still turn off, and closing a laptop lid (or
choosing Sleep) still sleeps: only automatic idle sleep is prevented.

Windows: SetThreadExecutionState from a dedicated thread (the request lasts while that thread lives).
macOS:   `caffeinate -i`, tied to this process so it can't outlive the app.
"""

import ctypes
import logging
import os
import subprocess
import threading
from typing import Optional

from backend.paths import IS_MAC, IS_WINDOWS

logger = logging.getLogger("locationspoof.keepawake")

SUPPORTED = IS_WINDOWS or IS_MAC

# Windows SetThreadExecutionState flags
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


class _WindowsHold:
    def __init__(self) -> None:
        self._release = threading.Event()
        self._started = threading.Event()
        self.ok = False
        self._thread = threading.Thread(target=self._run, name="keep-awake", daemon=True)
        self._thread.start()
        self._started.wait(5)

    def _run(self) -> None:
        kernel32 = ctypes.windll.kernel32
        self.ok = bool(kernel32.SetThreadExecutionState(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED))
        self._started.set()
        self._release.wait()
        kernel32.SetThreadExecutionState(_ES_CONTINUOUS)

    def release(self) -> None:
        self._release.set()
        self._thread.join(5)


class _MacHold:
    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        try:
            self._proc = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
        except OSError as e:
            logger.warning(f"caffeinate failed: {e!r}")
        self.ok = self._proc is not None

    def release(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(5)
            except subprocess.TimeoutExpired:
                self._proc.kill()


class KeepAwake:
    def __init__(self) -> None:
        self.enabled = False  # the user's choice
        self._hold = None
        self.error: Optional[str] = None

    @property
    def active(self) -> bool:
        return self._hold is not None

    def update(self, needed: bool) -> None:
        """Hold or release the no-sleep request: held only while enabled and needed."""
        want = self.enabled and needed and SUPPORTED
        if want and self._hold is None:
            hold = _WindowsHold() if IS_WINDOWS else _MacHold()
            if hold.ok:
                self._hold, self.error = hold, None
                logger.info("keeping the computer awake")
            else:
                hold.release()
                self.error = "The system refused the request to stay awake."
        elif not want and self._hold is not None:
            hold, self._hold = self._hold, None
            hold.release()
            logger.info("computer may sleep again")

    def release(self) -> None:
        self.update(needed=False)
