"""Partial-file bookkeeping that makes interrupted downloads resumable.

Bytes always land in ``<name>.gdbd.part``. The size of that file on disk - not
the number in the database - is the authority on how much has been transferred,
because it is the only value that survives a power cut.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..filesystem.paths import long_path
from ..filesystem.storage import ensure_directory, file_size, partial_path, remove
from ..utils.logging import get_logger

log = get_logger(__name__)


@dataclass
class ResumeState:
    """Where a transfer should pick up from."""

    part_file: Path
    offset: int
    restart: bool = False

    @property
    def resuming(self) -> bool:
        """True when bytes already on disk will be kept."""
        return self.offset > 0 and not self.restart


def prepare(
    final_path: str | Path,
    expected_size: int | None,
    allow_resume: bool = True,
) -> ResumeState:
    """Decide the starting offset for a download.

    A partial file larger than the expected size is discarded: it cannot be a
    prefix of the correct content, so continuing from it would produce a
    corrupt file.
    """
    final = Path(final_path)
    part = partial_path(final)
    ensure_directory(final.parent)

    existing = file_size(part)
    if existing <= 0:
        return ResumeState(part_file=part, offset=0)

    if not allow_resume:
        remove(part)
        return ResumeState(part_file=part, offset=0, restart=True)

    if expected_size is not None and expected_size > 0:
        if existing > expected_size:
            log.warning(
                "Discarding oversized partial file (%s > %s bytes): %s",
                existing, expected_size, final.name,
            )
            remove(part)
            return ResumeState(part_file=part, offset=0, restart=True)
        if existing == expected_size:
            # Everything arrived last time; verification will confirm it.
            return ResumeState(part_file=part, offset=existing)

    return ResumeState(part_file=part, offset=existing)


def truncate_to(part_file: str | Path, offset: int) -> None:
    """Cut a partial file back to ``offset`` bytes."""
    try:
        with open(long_path(part_file), "r+b") as handle:
            handle.truncate(offset)
    except OSError as exc:  # pragma: no cover - defensive
        log.debug("Could not truncate %s: %s", part_file, exc)


def discard(final_path: str | Path) -> None:
    """Delete the partial file for ``final_path``."""
    remove(partial_path(final_path))
