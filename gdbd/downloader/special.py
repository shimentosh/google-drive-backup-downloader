"""Strategies for Drive items that neither ``alt=media`` nor ``export`` covers.

Shortcuts point at another item that is backed up at its own location, and a
few Google types (Forms, Sites, My Maps) have no downloadable representation at
all. Both are recorded with an explicit reason instead of being retried
forever - and the shortcut case optionally leaves a ``.url`` file behind so the
original item is still reachable from the backup.
"""

from __future__ import annotations

from pathlib import Path

from ..database.models import DriveItemRecord
from ..database.schema import ItemStatus
from ..utils.logging import get_logger
from .base import DownloadContext, DownloadOutcome, DownloadStrategy

log = get_logger(__name__)


class SpecialFileDownloader(DownloadStrategy):
    """Terminal handler for items with no downloadable representation.

    New Google file types can be supported later by adding an export mapping in
    :mod:`gdbd.drive.exporters`; until then they land here with a clear reason
    rather than a misleading failure.
    """

    name = "unsupported"

    def download(
        self, item: DriveItemRecord, target: Path, context: DownloadContext
    ) -> DownloadOutcome:
        """Record why the item cannot be downloaded."""
        reason = item.error_message or (
            "Google Drive does not provide a downloadable representation of "
            f"'{item.mime_type}'."
        )
        return DownloadOutcome(ItemStatus.UNSUPPORTED, error=reason)


class ShortcutHandler(DownloadStrategy):
    """Marks shortcuts as skipped; the target file is backed up separately."""

    name = "shortcut"

    def download(
        self, item: DriveItemRecord, target: Path, context: DownloadContext
    ) -> DownloadOutcome:
        """Skip the shortcut and explain why."""
        return DownloadOutcome(
            ItemStatus.SKIPPED,
            error="Shortcut - the item it points to is backed up at its own location.",
        )
