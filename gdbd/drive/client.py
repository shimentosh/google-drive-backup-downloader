"""Authenticated access to the Google Drive API v3.

Two transports are used, deliberately:

* ``googleapiclient`` for metadata (``files.list``, ``about.get``, ``drives.list``)
* a raw :class:`~google.auth.transport.requests.AuthorizedSession` for content,
  because it exposes HTTP streaming and ``Range`` headers - which is what makes
  chunked writes and byte-accurate resume possible.

Both are built per thread: googleapiclient service objects are not thread-safe.
"""

from __future__ import annotations

import threading
import urllib.parse
from typing import Any, Callable, Iterator

from google.auth.transport.requests import AuthorizedSession
from googleapiclient.discovery import build

from ..auth.google_auth import GoogleAuthenticator
from ..constants import LIST_FIELDS
from ..utils.logging import get_logger
from .models import DriveFile, SharedDrive
from .pagination import RateLimiter, execute_with_retry, paginate

log = get_logger(__name__)

DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"

#: Fallback export map, used only if ``about.get(exportFormats)`` is unavailable.
FALLBACK_EXPORT_FORMATS: dict[str, list[str]] = {
    "application/vnd.google-apps.document": [
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/pdf", "application/vnd.oasis.opendocument.text", "text/plain",
        "text/html", "application/rtf", "application/epub+zip",
    ],
    "application/vnd.google-apps.spreadsheet": [
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/x-vnd.oasis.opendocument.spreadsheet",
        "application/vnd.oasis.opendocument.spreadsheet",
        "application/pdf", "text/csv", "text/tab-separated-values", "application/zip",
    ],
    "application/vnd.google-apps.presentation": [
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.oasis.opendocument.presentation", "application/pdf",
        "text/plain",
    ],
    "application/vnd.google-apps.drawing": [
        "image/png", "image/jpeg", "image/svg+xml", "application/pdf",
    ],
    "application/vnd.google-apps.script": ["application/vnd.google-apps.script+json"],
    "application/vnd.google-apps.jam": ["application/pdf"],
}


class DriveClient:
    """Thin, retry-aware wrapper over the Drive API."""

    def __init__(self, authenticator: GoogleAuthenticator, rate_limiter: RateLimiter | None = None) -> None:
        self.auth = authenticator
        self.rate_limiter = rate_limiter or RateLimiter()
        self._local = threading.local()
        self._sessions: list[AuthorizedSession] = []
        self._sessions_lock = threading.Lock()
        self._export_formats: dict[str, list[str]] | None = None
        self._root_id: str | None = None

    # -- transports --------------------------------------------------------
    @property
    def service(self) -> Any:
        """This thread's googleapiclient Drive service."""
        service = getattr(self._local, "service", None)
        if service is None:
            creds = self.auth.ensure_valid()
            service = build(
                "drive", "v3", credentials=creds,
                cache_discovery=False, static_discovery=True,
            )
            self._local.service = service
        return service

    @property
    def session(self) -> AuthorizedSession:
        """This thread's authorised HTTP session for content transfers."""
        session = getattr(self._local, "session", None)
        if session is None:
            session = AuthorizedSession(self.auth.ensure_valid())
            session.headers["User-Agent"] = "GoogleDriveBackupDownloader/1.0"
            self._local.session = session
            with self._sessions_lock:
                self._sessions.append(session)
        return session

    def reset(self) -> None:
        """Drop cached transports (after reconnecting a different account)."""
        with self._sessions_lock:
            for session in self._sessions:
                try:
                    session.close()
                except Exception:  # pragma: no cover - defensive
                    pass
            self._sessions.clear()
        self._local = threading.local()
        self._export_formats = None
        self._root_id = None

    # -- metadata ----------------------------------------------------------
    def about(self) -> dict[str, Any]:
        """``about.get`` with the fields the app needs (user + quota + exports)."""
        self.rate_limiter.acquire()
        return execute_with_retry(
            self.service.about().get(
                fields="user(displayName,emailAddress),storageQuota,exportFormats"
            ).execute,
            description="about.get",
        )

    def user_info(self) -> tuple[str, str | None, int | None, int | None]:
        """Return ``(email, display_name, storage_limit, storage_used)``."""
        data = self.about()
        user = data.get("user") or {}
        quota = data.get("storageQuota") or {}
        formats = data.get("exportFormats")
        if isinstance(formats, dict) and formats:
            self._export_formats = {k: list(v) for k, v in formats.items()}

        def as_int(value: Any) -> int | None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        return (
            user.get("emailAddress") or "",
            user.get("displayName"),
            as_int(quota.get("limit")),
            as_int(quota.get("usage")),
        )

    def export_formats(self) -> dict[str, list[str]]:
        """Export targets Drive itself advertises, with a static fallback.

        Asking the API means the app never invents an unsupported conversion.
        """
        if self._export_formats is None:
            try:
                data = self.about()
                formats = data.get("exportFormats")
                self._export_formats = (
                    {k: list(v) for k, v in formats.items()}
                    if isinstance(formats, dict) and formats
                    else dict(FALLBACK_EXPORT_FORMATS)
                )
            except Exception as exc:  # pragma: no cover - network dependent
                log.warning("Could not read exportFormats (%s); using built-in map",
                            type(exc).__name__)
                self._export_formats = dict(FALLBACK_EXPORT_FORMATS)
        return self._export_formats

    def root_id(self) -> str:
        """File id of "My Drive"."""
        if self._root_id is None:
            self.rate_limiter.acquire()
            data = execute_with_retry(
                self.service.files().get(fileId="root", fields="id").execute,
                description="files.get(root)",
            )
            self._root_id = data.get("id", "root")
        return self._root_id

    def get_file(self, file_id: str, fields: str = "id,name,mimeType,size,parents") -> DriveFile:
        """Fetch one file's metadata."""
        self.rate_limiter.acquire()
        data = execute_with_retry(
            self.service.files().get(
                fileId=file_id, fields=fields, supportsAllDrives=True
            ).execute,
            description=f"files.get({file_id})",
        )
        return DriveFile.from_api(data)

    def list_shared_drives(self) -> list[SharedDrive]:
        """Every shared drive the account can see (may be empty)."""
        drives: list[SharedDrive] = []
        try:
            for item in paginate(
                lambda token: self.service.drives().list(
                    pageSize=100, fields="nextPageToken,drives(id,name)", pageToken=token
                ),
                page_key="drives",
            ):
                drives.append(SharedDrive.from_api(item))
        except Exception as exc:
            # Personal accounts have no shared drives; that must not break scanning.
            log.info("Shared drives unavailable for this account (%s)", type(exc).__name__)
        return drives

    def iter_all_files(
        self,
        include_trashed: bool = False,
        drive_id: str | None = None,
        should_continue: Callable[[], bool] | None = None,
        on_page: Callable[[int], None] | None = None,
        page_size: int = 1000,
    ) -> Iterator[DriveFile]:
        """Stream every item in a corpus, one page at a time.

        A single flat listing is far cheaper than walking folder by folder: the
        hierarchy is rebuilt locally from each item's ``parents``.
        """
        query = "trashed = false" if not include_trashed else None

        def build_request(token: str | None) -> Any:
            self.rate_limiter.acquire()
            params: dict[str, Any] = {
                "pageSize": page_size,
                "fields": LIST_FIELDS,
                "pageToken": token,
                "supportsAllDrives": True,
                "includeItemsFromAllDrives": True,
                "orderBy": "folder,name",
            }
            if query:
                params["q"] = query
            if drive_id:
                params["corpora"] = "drive"
                params["driveId"] = drive_id
            else:
                params["corpora"] = "user"
                params["spaces"] = "drive"
            return self.service.files().list(**params)

        for raw in paginate(build_request, should_continue=should_continue, on_page=on_page):
            yield DriveFile.from_api(raw)

    # -- content transfers -------------------------------------------------
    def media_url(self, file_id: str, acknowledge_abuse: bool = False) -> str:
        """URL that streams a binary file's bytes."""
        params = {"alt": "media", "supportsAllDrives": "true"}
        if acknowledge_abuse:
            params["acknowledgeAbuse"] = "true"
        return f"{DRIVE_API_BASE}/files/{file_id}?{urllib.parse.urlencode(params)}"

    def export_url(self, file_id: str, mime_type: str) -> str:
        """URL that streams a Workspace file converted to ``mime_type``."""
        params = {"mimeType": mime_type}
        return f"{DRIVE_API_BASE}/files/{file_id}/export?{urllib.parse.urlencode(params)}"

    def open_stream(
        self,
        url: str,
        offset: int = 0,
        timeout: tuple[float, float] = (30.0, 120.0),
    ) -> Any:
        """Open a streaming GET, optionally resuming from ``offset``.

        The caller must close the returned response. A ``206 Partial Content``
        reply confirms the resume was honoured; a ``200`` means the server sent
        the whole file and the caller must restart from byte zero.
        """
        headers: dict[str, str] = {}
        if offset > 0:
            headers["Range"] = f"bytes={offset}-"
        self.rate_limiter.acquire()
        response = self.session.get(url, headers=headers, stream=True, timeout=timeout)
        return response
