"""Deciding *how* each Drive item can be turned into a local file.

Not every Drive item supports ``files.get(alt=media)``. Native Google editor
files must go through ``files.export``, and a few types (Forms, Sites, Maps)
cannot be exported at all. This module makes that decision once, during the
scan, and records it on the item so the download engine never has to guess.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..constants import FOLDER_MIME, GOOGLE_APPS_PREFIX, SHORTCUT_MIME
from .models import DriveFile

#: Download strategies. Stored verbatim in ``drive_items.strategy``.
STRATEGY_BINARY = "binary"
STRATEGY_EXPORT = "export"
STRATEGY_FOLDER = "folder"
STRATEGY_SHORTCUT = "shortcut"
STRATEGY_UNSUPPORTED = "unsupported"

#: File extension to append for each export target MIME type.
EXTENSION_BY_MIME: dict[str, str] = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.oasis.opendocument.text": ".odt",
    "application/vnd.oasis.opendocument.spreadsheet": ".ods",
    "application/x-vnd.oasis.opendocument.spreadsheet": ".ods",
    "application/vnd.oasis.opendocument.presentation": ".odp",
    "application/pdf": ".pdf",
    "application/rtf": ".rtf",
    "application/epub+zip": ".epub",
    "application/zip": ".zip",
    "application/vnd.google-apps.script+json": ".json",
    "text/plain": ".txt",
    "text/html": ".html",
    "text/csv": ".csv",
    "text/tab-separated-values": ".tsv",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/svg+xml": ".svg",
    "video/mp4": ".mp4",
}

#: Preferred export target for Google types the settings screen does not cover,
#: tried in order against what the API says is actually available.
_DEFAULT_PREFERENCES: dict[str, list[str]] = {
    "application/vnd.google-apps.vids": ["video/mp4", "application/pdf"],
    "application/vnd.google-apps.jam": ["application/pdf"],
    "application/vnd.google-apps.script": ["application/vnd.google-apps.script+json"],
}

#: Google types that have no export representation at all. Downloading them is
#: impossible through any Drive API call, so they are reported, not retried.
_KNOWN_UNEXPORTABLE = {
    "application/vnd.google-apps.form": "Google Forms cannot be exported by the Drive API.",
    "application/vnd.google-apps.site": "Google Sites cannot be exported by the Drive API.",
    "application/vnd.google-apps.map": "Google My Maps cannot be exported by the Drive API.",
    "application/vnd.google-apps.fusiontable": "Fusion Tables are no longer available.",
    "application/vnd.google-apps.mail-layout": "Gmail layouts cannot be exported.",
}


@dataclass(frozen=True)
class DownloadPlan:
    """How one Drive item will be turned into a local file."""

    strategy: str
    export_mime: str | None = None
    extension: str = ""
    reason: str = ""

    @property
    def is_downloadable(self) -> bool:
        """True when this item will actually produce bytes on disk."""
        return self.strategy in (STRATEGY_BINARY, STRATEGY_EXPORT)


class ExportPlanner:
    """Chooses a :class:`DownloadPlan` for every scanned item.

    ``available_formats`` comes from ``about.get(exportFormats)`` so the planner
    only ever proposes conversions Google actually offers.
    """

    def __init__(
        self,
        available_formats: dict[str, list[str]],
        preferred: dict[str, str] | None = None,
        export_workspace: bool = True,
    ) -> None:
        self.available = available_formats or {}
        self.preferred = preferred or {}
        self.export_workspace = export_workspace

    def plan(self, item: DriveFile) -> DownloadPlan:
        """Decide how ``item`` should be downloaded."""
        if item.mime_type == FOLDER_MIME:
            return DownloadPlan(STRATEGY_FOLDER)

        if item.mime_type == SHORTCUT_MIME:
            return DownloadPlan(
                STRATEGY_SHORTCUT,
                reason="Shortcut - the item it points to is backed up at its own location.",
            )

        if item.mime_type.startswith(GOOGLE_APPS_PREFIX):
            return self._plan_google_file(item)

        if not item.can_download:
            return DownloadPlan(
                STRATEGY_UNSUPPORTED,
                reason="The owner has disabled downloading for this file.",
            )
        return DownloadPlan(STRATEGY_BINARY)

    # -- internals ---------------------------------------------------------
    def _plan_google_file(self, item: DriveFile) -> DownloadPlan:
        known_reason = _KNOWN_UNEXPORTABLE.get(item.mime_type)
        if known_reason:
            return DownloadPlan(STRATEGY_UNSUPPORTED, reason=known_reason)

        if not self.export_workspace:
            return DownloadPlan(
                STRATEGY_UNSUPPORTED,
                reason="Google Workspace export is turned off in Settings.",
            )

        targets = self._candidate_targets(item)
        if not targets:
            return DownloadPlan(
                STRATEGY_UNSUPPORTED,
                reason=f"Drive offers no export format for {item.mime_type}.",
            )
        chosen = targets[0]
        return DownloadPlan(
            STRATEGY_EXPORT,
            export_mime=chosen,
            extension=EXTENSION_BY_MIME.get(chosen, ""),
        )

    def _candidate_targets(self, item: DriveFile) -> list[str]:
        """Export MIME types for ``item``, best first."""
        supported = list(self.available.get(item.mime_type) or [])
        # exportLinks on the item itself is the most authoritative signal.
        if item.export_links:
            for mime in item.export_links:
                if mime not in supported:
                    supported.append(mime)
        if not supported:
            return []

        ordered: list[str] = []
        wanted = self.preferred.get(item.mime_type)
        if wanted and wanted in supported:
            ordered.append(wanted)
        for mime in _DEFAULT_PREFERENCES.get(item.mime_type, []):
            if mime in supported and mime not in ordered:
                ordered.append(mime)
        for mime in supported:
            if mime not in ordered:
                ordered.append(mime)
        return ordered

    def export_extension(self, mime_type: str) -> str:
        """File extension for an export target MIME type."""
        return EXTENSION_BY_MIME.get(mime_type, "")
