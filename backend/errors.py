"""Turn pymobiledevice3 / network exceptions into messages a person can act on."""

import asyncio

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
        return "This Mac isn't trusted by the iPhone yet. Click 'Trust & prepare'."
    if isinstance(e, DeveloperModeIsNotEnabledError):
        return "Developer Mode is off on the iPhone (Settings > Privacy & Security > Developer Mode)."
    if isinstance(e, DeviceHasPasscodeSetError):
        return "iOS won't let a computer turn on Developer Mode while a passcode is set. Use 'Show toggle in Settings' instead."
    if isinstance(e, ConnectionFailedToUsbmuxdError):
        return "macOS's iPhone connection service isn't responding. Unplug and replug the iPhone; if that doesn't help, restart the Mac."
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
        return "Permission denied. Start LocationSpoof with ./run.sh so it has administrator rights."
    text = str(e).strip()
    return f"{type(e).__name__}: {text}" if text else type(e).__name__
