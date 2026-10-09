"""Destination-volume checks and safe file placement."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..constants import PARTIAL_SUFFIX
from ..utils.logging import get_logger
from .paths import display_path, long_path

log = get_logger(__name__)


@dataclass(frozen=True)
class DiskSpace:
    """Snapshot of a volume's capacity, in bytes."""

    total: int
    used: int
    free: int


def disk_space(path: str | os.PathLike[str]) -> DiskSpace:
    """Capacity of the volume holding ``path`` (walks up to an existing dir)."""
    probe = Path(path)
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    usage = shutil.disk_usage(long_path(probe))
    return DiskSpace(total=usage.total, used=usage.used, free=usage.free)


def ensure_directory(path: str | os.PathLike[str]) -> Path:
    """Create ``path`` (and parents) if missing and return it."""
    target = Path(path)
    os.makedirs(long_path(target), exist_ok=True)
    return target


def is_writable(path: str | os.PathLike[str]) -> tuple[bool, str]:
    """Check that a directory exists and accepts writes.

    Returns ``(ok, reason)``; ``reason`` is empty when the check passed.
    """
    target = Path(path)
    try:
        ensure_directory(target)
    except OSError as exc:
        return False, f"Cannot create destination folder: {exc.strerror or exc}"
    if not target.is_dir():
        return False, "Destination is not a folder."
    try:
        handle = tempfile.NamedTemporaryFile(
            dir=long_path(target), prefix=".gdbd-write-test-", delete=True
        )
        handle.close()
    except OSError as exc:
        return False, f"Destination is not writable: {exc.strerror or exc}"
    return True, ""


def partial_path(final_path: str | os.PathLike[str]) -> Path:
    """Path of the ``.part`` file that backs an in-progress download."""
    return Path(str(final_path) + PARTIAL_SUFFIX)


def file_size(path: str | os.PathLike[str]) -> int:
    """Size of ``path`` in bytes, or 0 when it does not exist."""
    try:
        return os.path.getsize(long_path(path))
    except OSError:
        return 0


def modified_time(path: str | os.PathLike[str]) -> float | None:
    """Local modification time of ``path`` as a POSIX timestamp, if it exists."""
    try:
        return os.path.getmtime(long_path(path))
    except OSError:
        return None


def exists(path: str | os.PathLike[str]) -> bool:
    """True when ``path`` exists (long-path safe)."""
    return os.path.exists(long_path(path))


def finalize(temp_file: str | os.PathLike[str], final_file: str | os.PathLike[str]) -> None:
    """Move a verified ``.part`` file into its final name, replacing any old copy."""
    ensure_directory(Path(final_file).parent)
    os.replace(long_path(temp_file), long_path(final_file))


def remove(path: str | os.PathLike[str]) -> None:
    """Delete a file if it exists, ignoring failures."""
    try:
        os.remove(long_path(path))
    except OSError:
        pass


def set_modified_time(path: str | os.PathLike[str], epoch: float | None) -> None:
    """Stamp the local file with the Drive modification time when known."""
    if epoch is None:
        return
    try:
        os.utime(long_path(path), (epoch, epoch))
    except OSError as exc:  # pragma: no cover - filesystem dependent
        log.debug("Could not set mtime on %s: %s", display_path(path), exc)


def open_in_file_manager(path: str | os.PathLike[str]) -> bool:
    """Reveal ``path`` in Explorer / Finder / the desktop file manager."""
    target = Path(display_path(str(path)))
    if not target.exists():
        return False
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)])
        return True
    except Exception as exc:  # pragma: no cover - platform dependent
        log.warning("Could not open %s: %s", target, exc)
        return False


def find_partial_files(root: str | os.PathLike[str], limit: int = 5000) -> list[Path]:
    """List leftover ``.part`` files under a backup destination."""
    found: list[Path] = []
    root_path = Path(root)
    if not root_path.exists():
        return found
    for dirpath, _dirnames, filenames in os.walk(long_path(root_path)):
        for name in filenames:
            if name.endswith(PARTIAL_SUFFIX):
                found.append(Path(dirpath) / name)
                if len(found) >= limit:
                    return found
    return found
