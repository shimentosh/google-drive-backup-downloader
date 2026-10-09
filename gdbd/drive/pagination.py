"""Correct, complete pagination over Drive API list endpoints.

The Drive API never returns a whole Drive in one response. Every list call must
be followed while ``nextPageToken`` is present; this module is the single place
that loop is implemented so no caller can get it wrong.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Iterator

from ..constants import RETRYABLE_STATUS
from ..utils.backoff import backoff_delay
from ..utils.logging import get_logger

log = get_logger(__name__)


class RetryableApiError(Exception):
    """A Drive API failure that is worth retrying."""


class PermanentApiError(Exception):
    """A Drive API failure that retrying cannot fix."""

    def __init__(self, message: str, status: int | None = None, reason: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.reason = reason


def classify_http_error(exc: Exception) -> tuple[bool, int | None, str, str]:
    """Inspect an exception and decide whether retrying makes sense.

    Returns ``(retryable, status, reason, message)``.
    """
    from googleapiclient.errors import HttpError

    if isinstance(exc, HttpError):
        status = getattr(exc.resp, "status", None)
        reason = ""
        message = ""
        try:
            payload = exc.error_details  # type: ignore[attr-defined]
            if isinstance(payload, list) and payload:
                reason = str(payload[0].get("reason", ""))
                message = str(payload[0].get("message", ""))
        except Exception:  # pragma: no cover - error_details is best effort
            pass
        if not message:
            message = getattr(exc, "reason", "") or str(exc)
        rate_limited = reason in (
            "rateLimitExceeded", "userRateLimitExceeded", "sharingRateLimitExceeded",
            "backendError", "internalError",
        )
        retryable = status in RETRYABLE_STATUS or rate_limited
        # 403 is overloaded: rate limits are retryable, permission errors are not.
        if status == 403 and not rate_limited:
            retryable = False
        return retryable, status, reason, message

    # Transport-level failures: DNS, reset connections, timeouts.
    import http.client
    import socket

    import requests.exceptions as rex

    transport = (
        socket.timeout, socket.gaierror, ConnectionError, TimeoutError,
        http.client.HTTPException, rex.RequestException, OSError,
    )
    if isinstance(exc, transport):
        return True, None, "network", str(exc) or type(exc).__name__
    return False, None, "", str(exc)


def execute_with_retry(
    call: Callable[[], Any],
    max_attempts: int = 6,
    should_continue: Callable[[], bool] | None = None,
    description: str = "Drive API call",
) -> Any:
    """Run ``call``, retrying transient failures with exponential backoff.

    ``should_continue`` lets a caller abort the retry loop (used for cancel).
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 - classified immediately below
            retryable, status, reason, message = classify_http_error(exc)
            if not retryable or attempt >= max_attempts:
                if retryable:
                    raise RetryableApiError(
                        f"{description} failed after {attempt} attempts: {message}"
                    ) from exc
                raise PermanentApiError(message, status, reason) from exc
            delay = backoff_delay(attempt)
            log.warning(
                "%s failed (%s %s); retry %d/%d in %.1fs",
                description, status or "network", reason or "", attempt,
                max_attempts - 1, delay,
            )
            if should_continue is not None:
                # Sleep in short slices so cancellation is responsive.
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline:
                    if not should_continue():
                        raise PermanentApiError("Cancelled", None, "cancelled") from exc
                    time.sleep(min(0.25, deadline - time.monotonic()))
            else:
                time.sleep(delay)


def paginate(
    request_builder: Callable[[str | None], Any],
    page_key: str = "files",
    should_continue: Callable[[], bool] | None = None,
    on_page: Callable[[int], None] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield every resource across every page of a Drive list endpoint.

    ``request_builder`` receives the current page token (``None`` for the first
    page) and returns an unexecuted googleapiclient request.
    """
    token: str | None = None
    page_number = 0
    while True:
        if should_continue is not None and not should_continue():
            return
        request = request_builder(token)
        response = execute_with_retry(
            request.execute,
            should_continue=should_continue,
            description="files.list",
        )
        page_number += 1
        items = response.get(page_key) or []
        if on_page is not None:
            on_page(page_number)
        for item in items:
            yield item
        token = response.get("nextPageToken")
        if not token:
            return


class RateLimiter:
    """Simple token-bucket limiter shared by all workers.

    Keeps the application well inside Drive's per-user quota even with 16
    concurrent workers, which is cheaper than absorbing 429s after the fact.
    """

    def __init__(self, rate_per_second: float = 12.0, burst: int = 12) -> None:
        self.rate = max(0.1, rate_per_second)
        self.burst = max(1, burst)
        self._tokens = float(self.burst)
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Block briefly until a request slot is available."""
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self.burst, self._tokens + (now - self._updated) * self.rate
                )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self.rate
            time.sleep(min(wait, 0.25))
