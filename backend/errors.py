"""Turn pymobiledevice3 / network exceptions into messages a person can act on."""

import asyncio
import sys

from pymobiledevice3.exceptions import (
    ConnectionFailedError,
    ConnectionFailedToUsbmuxdError,
    ConnectionTerminatedError,
    DeveloperModeIsNotEnabledError,
    DeviceHasPasscodeSetError,
    DeviceNotFoundError,
    InvalidServiceError,
    MuxException,
    NotPairedError,
    PairingDialogResponsePendingError,
    PasswordRequiredError,
    StartServiceError,
    UserDeniedPairingError,
)

CABLE_HINT = "Check the cable (it must be a data cable, not charge-only), keep the iPhone unlocked, and avoid USB hubs."

if sys.platform == "win32":
    USBMUX_HINT = (
        "Windows can't talk to iPhones yet: Apple's device service isn't running. Install the Apple Devices "
        "app from the Microsoft Store (or iTunes), open it once with the iPhone plugged in, then try again."
    )
else:
    USBMUX_HINT = (
        "The computer's iPhone connection service isn't responding. Unplug and replug the iPhone; "
        "if that doesn't help, restart the computer."
    )


class DeviceError(Exception):
    """An error whose message is already written for the user."""


def explain(e: BaseException) -> str:
    if isinstance(e, DeviceError):
        return str(e)
    if isinstance(e, PasswordRequiredError):
        return "The iPhone is locked. Unlock it to continue."
    if isinstance(e, PairingDialogResponsePendingError):
        return "Waiting for you to tap 'Trust' on the iPhone."
    if isinstance(e, UserDeniedPairingError):
        return "'Don't Trust' was chosen on the iPhone. Unplug it, plug it back in, and tap 'Trust'."
    if isinstance(e, NotPairedError):
        return "This computer isn't trusted by the iPhone yet. Click 'Trust & prepare'."
    if isinstance(e, DeveloperModeIsNotEnabledError):
        return "Developer Mode is off on the iPhone (Settings > Privacy & Security > Developer Mode)."
    if isinstance(e, DeviceHasPasscodeSetError):
        return "iOS won't let a computer turn on Developer Mode while a passcode is set. Use 'Show toggle in Settings' instead."
    if isinstance(e, ConnectionFailedToUsbmuxdError):
        return USBMUX_HINT
    if isinstance(e, (InvalidServiceError, StartServiceError)):
        return (
            "The iPhone refused the developer service. Unlock it; if this keeps happening, click "
            "'Prepare device' (the developer disk image has to be re-mounted after the phone restarts)."
        )
    if isinstance(e, (DeviceNotFoundError, MuxException)):
        return f"The iPhone isn't connected. {CABLE_HINT}"
    if isinstance(
        e,
        (
            ConnectionTerminatedError,
            ConnectionFailedError,
            ConnectionResetError,
            ConnectionRefusedError,
            BrokenPipeError,
            asyncio.IncompleteReadError,
        ),
    ):
        return f"Lost the connection to the iPhone. {CABLE_HINT}"
    if isinstance(e, (TimeoutError, asyncio.TimeoutError)):
        return "The iPhone stopped responding (timed out). It may have locked or the cable may have slipped."
    if isinstance(e, PermissionError):
        return f"Permission denied: {e}"
    text = str(e).strip()
    return f"{type(e).__name__}: {text}" if text else type(e).__name__
