"""Domain objects for Drive metadata and MIME-type classification."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..constants import FOLDER_MIME, GOOGLE_APPS_PREFIX, SHORTCUT_MIME

#: Coarse buckets used by the statistics panel and the browser filter.
CATEGORY_VIDEO = "Videos"
CATEGORY_IMAGE = "Images"
CATEGORY_AUDIO = "Audio"
CATEGORY_DOCUMENT = "Documents"
CATEGORY_ARCHIVE = "Archives"
CATEGORY_OTHER = "Other"

CATEGORY_ORDER = (
    CATEGORY_VIDEO, CATEGORY_IMAGE, CATEGORY_AUDIO,
    CATEGORY_DOCUMENT, CATEGORY_ARCHIVE, CATEGORY_OTHER,
)

_DOCUMENT_MIMES = frozenset({
    "application/pdf",
    "application/rtf",
    "application/msword",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/vnd.oasis.opendocument.presentation",
    "application/epub+zip",
    "application/json",
    "application/xml",
})

_ARCHIVE_MIMES = frozenset({
    "application/zip",
    "application/x-zip-compressed",
    "application/x-zip",
    "application/x-rar-compressed",
    "application/x-rar",
    "application/rar",
    "application/vnd.rar",
    "application/x-7z-compressed",
    "application/x-tar",
    "application/gzip",
    "application/x-bzip2",
})


def classify(mime_type: str) -> str:
    """Map a MIME type to one of the coarse display categories."""
    if not mime_type:
        return CATEGORY_OTHER
    if mime_type.startswith("video/"):
        return CATEGORY_VIDEO
    if mime_type.startswith("image/"):
        return CATEGORY_IMAGE
    if mime_type.startswith("audio/"):
        return CATEGORY_AUDIO
    if mime_type.startswith("text/"):
        return CATEGORY_DOCUMENT
    if mime_type in _DOCUMENT_MIMES:
        return CATEGORY_DOCUMENT
    if mime_type in _ARCHIVE_MIMES:
        return CATEGORY_ARCHIVE
    if mime_type.startswith(GOOGLE_APPS_PREFIX):
        if mime_type.endswith(("document", "spreadsheet", "presentation", "drawing",
                               "script", "form")):
            return CATEGORY_DOCUMENT
        if mime_type.endswith(("vids", "video")):
            return CATEGORY_VIDEO
        if mime_type.endswith("photo"):
            return CATEGORY_IMAGE
    return CATEGORY_OTHER


def category_mime_clause(category: str) -> tuple[str, list[str]]:
    """SQL fragment + params selecting one category, for filtered listings."""
    if category == CATEGORY_VIDEO:
        return "(mime_type LIKE 'video/%' OR mime_type LIKE '%google-apps.vids%')", []
    if category == CATEGORY_IMAGE:
        return "mime_type LIKE 'image/%'", []
    if category == CATEGORY_AUDIO:
        return "mime_type LIKE 'audio/%'", []
    if category == CATEGORY_DOCUMENT:
        placeholders = ", ".join("?" * len(_DOCUMENT_MIMES))
        clause = (
            "(mime_type LIKE 'text/%' OR mime_type LIKE"
            " 'application/vnd.google-apps.%'"
            f" OR mime_type IN ({placeholders}))"
        )
        return clause, sorted(_DOCUMENT_MIMES)
    if category == CATEGORY_ARCHIVE:
        placeholders = ", ".join("?" * len(_ARCHIVE_MIMES))
        return f"mime_type IN ({placeholders})", sorted(_ARCHIVE_MIMES)
    if category == CATEGORY_OTHER:
        known = sorted(_DOCUMENT_MIMES | _ARCHIVE_MIMES)
        placeholders = ", ".join("?" * len(known))
        clause = (
            "(mime_type NOT LIKE 'video/%' AND mime_type NOT LIKE 'image/%'"
            " AND mime_type NOT LIKE 'audio/%' AND mime_type NOT LIKE 'text/%'"
            " AND mime_type NOT LIKE 'application/vnd.google-apps.%'"
            f" AND mime_type NOT IN ({placeholders}))"
        )
        return clause, known
    return "", []


@dataclass
class DriveFile:
    """One item as returned by ``files.list`` / ``files.get``."""

    id: str
    name: str
    mime_type: str
    parents: list[str] = field(default_factory=list)
    size: int = 0
    modified_time: str | None = None
    created_time: str | None = None
    md5_checksum: str | None = None
    sha256_checksum: str | None = None
    trashed: bool = False
    drive_id: str | None = None
    owned_by_me: bool = True
    can_download: bool = True
    export_links: dict[str, str] = field(default_factory=dict)
    shortcut_target_id: str | None = None
    shortcut_target_mime: str | None = None
    web_view_link: str | None = None
    file_extension: str | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> DriveFile:
        """Build a :class:`DriveFile` from a raw Drive API resource."""
        shortcut = data.get("shortcutDetails") or {}
        capabilities = data.get("capabilities") or {}
        try:
            size = int(data.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        return cls(
            id=data.get("id", ""),
            name=data.get("name") or "unnamed",
            mime_type=data.get("mimeType") or "application/octet-stream",
            parents=list(data.get("parents") or []),
            size=size,
            modified_time=data.get("modifiedTime"),
            created_time=data.get("createdTime"),
            md5_checksum=data.get("md5Checksum"),
            sha256_checksum=data.get("sha256Checksum"),
            trashed=bool(data.get("trashed")),
            drive_id=data.get("driveId"),
            owned_by_me=bool(data.get("ownedByMe", True)),
            can_download=bool(capabilities.get("canDownload", True)),
            export_links=dict(data.get("exportLinks") or {}),
            shortcut_target_id=shortcut.get("targetId"),
            shortcut_target_mime=shortcut.get("targetMimeType"),
            web_view_link=data.get("webViewLink"),
            file_extension=data.get("fileExtension"),
        )

    @property
    def is_folder(self) -> bool:
        """True for Drive folders."""
        return self.mime_type == FOLDER_MIME

    @property
    def is_shortcut(self) -> bool:
        """True for Drive shortcuts, which point at another item."""
        return self.mime_type == SHORTCUT_MIME

    @property
    def is_google_app(self) -> bool:
        """True for native Google editor files (Docs, Sheets, Vids, ...)."""
        return self.mime_type.startswith(GOOGLE_APPS_PREFIX)

    @property
    def category(self) -> str:
        """Coarse display category."""
        return classify(self.mime_type)


@dataclass
class SharedDrive:
    """A shared drive (formerly Team Drive) the user can access."""

    id: str
    name: str

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> SharedDrive:
        """Build from a ``drives.list`` resource."""
        return cls(id=data.get("id", ""), name=data.get("name") or "Shared drive")
