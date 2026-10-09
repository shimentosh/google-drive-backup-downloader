"""Building local paths that mirror the Google Drive hierarchy."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .sanitizer import dedupe_name, sanitize_component

#: Windows refuses ordinary paths longer than this without the extended prefix.
WINDOWS_MAX_PATH = 259

_EXTENDED_PREFIX = "\\\\?\\"


def is_windows() -> bool:
    """True when running on Windows."""
    return sys.platform.startswith("win")


def long_path(path: str | os.PathLike[str]) -> str:
    r"""Return a path usable for file operations regardless of its length.

    On Windows, paths longer than ``MAX_PATH`` need the ``\\?\`` extended
    prefix. The prefix requires a fully-qualified path with no relative
    segments, so the path is made absolute first. On other platforms the path is
    returned unchanged.
    """
    text = os.fspath(path)
    if not is_windows() or text.startswith(_EXTENDED_PREFIX):
        return text
    absolute = os.path.abspath(text)
    if absolute.startswith("\\\\"):  # UNC share -> \\?\UNC\server\share\...
        if len(absolute) <= WINDOWS_MAX_PATH:
            return absolute
        return _EXTENDED_PREFIX + "UNC" + absolute[1:]
    if len(absolute) <= WINDOWS_MAX_PATH:
        return absolute
    return _EXTENDED_PREFIX + absolute


def display_path(path: str | os.PathLike[str]) -> str:
    """Strip any extended-length prefix so a path is readable in the UI."""
    text = os.fspath(path)
    if text.startswith(_EXTENDED_PREFIX + "UNC\\"):
        return "\\\\" + text[len(_EXTENDED_PREFIX) + 4:]
    if text.startswith(_EXTENDED_PREFIX):
        return text[len(_EXTENDED_PREFIX):]
    return text


def to_local_path(root: str | os.PathLike[str], relative: str) -> Path:
    """Join a ``/``-separated relative Drive path onto a local root."""
    parts = [p for p in relative.replace("\\", "/").split("/") if p]
    return Path(root).joinpath(*parts) if parts else Path(root)


class PathAllocator:
    """Assigns a unique, stable relative path to every scanned Drive item.

    Drive identifies items by ``file_id``; two siblings may legitimately share a
    name. The allocator sanitizes each name and, when a sibling name is already
    used, appends ``(1)``, ``(2)`` and so on. Because the scanner feeds items in
    a deterministic order, the same Drive always produces the same layout.
    """

    def __init__(self, max_component: int = 150) -> None:
        self._max_component = max_component
        #: parent relative path (casefolded) -> set of used child names
        self._used: dict[str, set[str]] = {}
        #: file_id -> relative path already assigned
        self._assigned: dict[str, str] = {}
        #: (original name, local name) pairs that differ, for the rename log
        self.renames: list[tuple[str, str]] = []

    def reset(self) -> None:
        """Forget every allocation (used when a scan restarts)."""
        self._used.clear()
        self._assigned.clear()
        self.renames.clear()

    def assigned(self, file_id: str) -> str | None:
        """Relative path previously assigned to ``file_id``, if any."""
        return self._assigned.get(file_id)

    def reserve(self, relative: str) -> None:
        """Mark an existing relative path as taken (used when replaying state)."""
        parent, _, leaf = relative.rpartition("/")
        self._used.setdefault(parent.casefold(), set()).add(leaf.casefold())

    def allocate(
        self,
        file_id: str,
        name: str,
        parent_relative: str = "",
        extra_extension: str = "",
    ) -> str:
        """Allocate the relative path for one item and return it.

        ``extra_extension`` appends an export suffix (``.docx``) for Google
        Workspace files whose Drive name carries no useful extension.
        """
        cached = self._assigned.get(file_id)
        if cached is not None:
            return cached

        safe = sanitize_component(name, self._max_component)
        if extra_extension and not safe.casefold().endswith(extra_extension.casefold()):
            safe = sanitize_component(safe + extra_extension, self._max_component)

        parent_key = parent_relative.casefold()
        taken = self._used.setdefault(parent_key, set())
        unique = dedupe_name(safe, taken)

        if unique != name:
            self.renames.append((name, unique))

        relative = f"{parent_relative}/{unique}" if parent_relative else unique
        self._assigned[file_id] = relative
        return relative
