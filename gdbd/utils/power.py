"""Keep the computer awake while a backup is running.

A multi-hour backup is usually left unattended, and Windows' default sleep
timer would otherwise suspend the machine mid-run. ``SetThreadExecutionState``
only blocks *idle* sleep: the display may still turn off, and the user can
always sleep the PC manually.
"""

from __future__ import annotations

import sys

from .logging import get_logger

log = get_logger(__name__)

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001

_held = False


def keep_awake(enabled: bool) -> bool:
    """Ask the OS to stay awake (``True``) or restore normal sleep (``False``).

    Returns whether the request is now in effect. A no-op off Windows.
    """
    global _held
    if enabled == _held:
        return _held
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        flags = _ES_CONTINUOUS | (_ES_SYSTEM_REQUIRED if enabled else 0)
        if not ctypes.windll.kernel32.SetThreadExecutionState(flags):
            raise OSError("SetThreadExecutionState failed")
    except Exception as exc:  # pragma: no cover - platform dependent
        log.warning("Could not change the sleep policy: %s", exc)
        return False
    _held = enabled
    log.info("System sleep %s", "blocked during backup" if enabled else "restored")
    return _held
