"""Retry policy and human-readable error classification for downloads."""

from __future__ import annotations

from dataclasses import dataclass

from ..constants import DEFAULT_MAX_RETRIES, RETRYABLE_STATUS
from ..utils.backoff import backoff_delay


class DownloadError(Exception):
    """A download failed. ``retryable`` decides whether to try again."""

    def __init__(self, message: str, retryable: bool = False, status: int | None = None,
                 reason: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.status = status
        self.reason = reason


class AuthorizationRequired(DownloadError):
    """The grant is gone or invalid; only the user can fix this."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False, status=401, reason="unauthorized")


class InsufficientSpace(DownloadError):
    """The destination volume ran out of room mid-transfer."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False, reason="diskFull")


@dataclass
class RetryPolicy:
    """How many times, and how patiently, to retry a failing download."""

    max_attempts: int = DEFAULT_MAX_RETRIES

    def should_retry(self, error: DownloadError, attempt: int) -> tuple[bool, float]:
        """Return ``(retry?, delay_seconds)`` for a failed attempt."""
        if not error.retryable or attempt >= self.max_attempts:
            return False, 0.0
        return True, backoff_delay(attempt)


def classify_status(status: int, body: str = "") -> DownloadError:
    """Turn an HTTP status from a content request into a typed error."""
    lowered = body.lower()

    if status in (401,):
        return AuthorizationRequired(
            "Google rejected the saved authorisation. Reconnect your account."
        )
    if status == 403:
        if ("accessnotconfigured" in lowered.replace(" ", "")
                or "service_disabled" in lowered):
            return AuthorizationRequired(
                "The Google Drive API is turned off for your Google Cloud project. "
                "Enable it, then resume the backup."
            )
        if "ratelimitexceeded" in lowered.replace(" ", "") or "quota" in lowered:
            return DownloadError(
                "Rate limit exceeded - Google is throttling requests.",
                retryable=True, status=status, reason="rateLimitExceeded",
            )
        if "cannotdownloadabusivefile" in lowered.replace(" ", ""):
            return DownloadError(
                "Drive flagged this file as abusive and requires acknowledgement.",
                retryable=False, status=status, reason="cannotDownloadAbusiveFile",
            )
        if "exportsizelimitexceeded" in lowered.replace(" ", ""):
            return DownloadError(
                "This Google Workspace file is too large for the Drive export API "
                "(10 MB limit for exports).",
                retryable=False, status=status, reason="exportSizeLimitExceeded",
            )
        return DownloadError(
            "Permission denied - your account cannot download this file.",
            retryable=False, status=status, reason="forbidden",
        )
    if status == 404:
        return DownloadError(
            "File unavailable - it was removed or is no longer shared with you.",
            retryable=False, status=status, reason="notFound",
        )
    if status == 416:
        # The saved resume offset is past the end of the file: restart it.
        return DownloadError(
            "Resume offset is no longer valid; the file will restart.",
            retryable=True, status=status, reason="rangeNotSatisfiable",
        )
    if status in RETRYABLE_STATUS:
        label = "Rate limit exceeded" if status == 429 else f"Google returned HTTP {status}"
        return DownloadError(f"{label} - retrying.", retryable=True, status=status,
                             reason="serverError")
    return DownloadError(f"Download failed with HTTP {status}.", retryable=False,
                         status=status)


def classify_exception(exc: BaseException) -> DownloadError:
    """Turn a transport-level exception into a typed error."""
    if isinstance(exc, DownloadError):
        return exc

    import socket

    import requests.exceptions as rex
    from google.auth.exceptions import RefreshError

    if isinstance(exc, RefreshError):
        return AuthorizationRequired(
            "The Google session could not be renewed. Reconnect your account."
        )
    if isinstance(exc, (rex.ConnectTimeout, rex.ReadTimeout, socket.timeout, TimeoutError)):
        return DownloadError("Network timeout.", retryable=True, reason="timeout")
    if isinstance(exc, rex.SSLError):
        return DownloadError(f"TLS error: {exc}", retryable=True, reason="ssl")
    if isinstance(exc, (rex.ConnectionError, ConnectionError, socket.gaierror)):
        return DownloadError(
            "Network unavailable - check your internet connection.",
            retryable=True, reason="network",
        )
    if isinstance(exc, rex.ChunkedEncodingError):
        return DownloadError("The connection dropped mid-transfer.", retryable=True,
                             reason="network")
    if isinstance(exc, OSError):
        errno = getattr(exc, "errno", None)
        winerror = getattr(exc, "winerror", None)
        if errno == 28 or winerror in (39, 112):  # ENOSPC / ERROR_DISK_FULL
            return InsufficientSpace("Insufficient disk space on the destination.")
        if errno == 13 or winerror == 5:
            return DownloadError(
                "Permission denied writing to the destination folder.",
                retryable=False, reason="localPermission",
            )
        if winerror in (3, 206) or errno == 36:
            return DownloadError(
                "Invalid or too-long local path for this file.",
                retryable=False, reason="invalidPath",
            )
        if winerror in (21, 53, 55, 64, 1231):  # device / network path unavailable
            return DownloadError(
                "Destination unavailable - the drive was disconnected.",
                retryable=True, reason="destinationUnavailable",
            )
        return DownloadError(f"Local file error: {exc}", retryable=False, reason="io")
    return DownloadError(f"Unexpected error: {exc}", retryable=True, reason="unknown")
