"""Application states and a tiny in-process event bus.

Kept free of Qt so the service layer can be unit-tested without a UI.
"""

from __future__ import annotations

import threading
from collections import defaultdict
from enum import Enum
from typing import Any, Callable

from ..utils.logging import get_logger

log = get_logger(__name__)


class AppState(str, Enum):
    """Every state the application can be in.

    The UI enables and disables actions purely from this value, which is why
    invalid combinations (for example starting a second backup) are impossible
    rather than merely discouraged.
    """

    DISCONNECTED = "DISCONNECTED"
    AUTHENTICATING = "AUTHENTICATING"
    CONNECTED = "CONNECTED"
    SCANNING = "SCANNING"
    READY = "READY"
    BACKUP_RUNNING = "BACKUP_RUNNING"
    BACKUP_PAUSED = "BACKUP_PAUSED"
    BACKUP_CANCELLING = "BACKUP_CANCELLING"
    BACKUP_COMPLETED = "BACKUP_COMPLETED"
    BACKUP_COMPLETED_WITH_ERRORS = "BACKUP_COMPLETED_WITH_ERRORS"
    BACKUP_FAILED = "BACKUP_FAILED"

    @property
    def is_busy(self) -> bool:
        """True while a long-running operation owns the account."""
        return self in (
            AppState.AUTHENTICATING, AppState.SCANNING, AppState.BACKUP_RUNNING,
            AppState.BACKUP_PAUSED, AppState.BACKUP_CANCELLING,
        )

    @property
    def is_backup_active(self) -> bool:
        """True while a backup run exists, running or paused."""
        return self in (
            AppState.BACKUP_RUNNING, AppState.BACKUP_PAUSED,
            AppState.BACKUP_CANCELLING,
        )

    @property
    def is_connected(self) -> bool:
        """True once an account is attached."""
        return self not in (AppState.DISCONNECTED, AppState.AUTHENTICATING)

    @property
    def label(self) -> str:
        """Human-readable form for status chips."""
        return {
            AppState.DISCONNECTED: "Not connected",
            AppState.AUTHENTICATING: "Connecting...",
            AppState.CONNECTED: "Connected",
            AppState.SCANNING: "Scanning Drive",
            AppState.READY: "Ready to back up",
            AppState.BACKUP_RUNNING: "Backing up",
            AppState.BACKUP_PAUSED: "Paused",
            AppState.BACKUP_CANCELLING: "Cancelling",
            AppState.BACKUP_COMPLETED: "Up to date",
            AppState.BACKUP_COMPLETED_WITH_ERRORS: "Completed with errors",
            AppState.BACKUP_FAILED: "Backup failed",
        }[self]


class EventBus:
    """Minimal thread-safe publish/subscribe used by the non-Qt services."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callable[..., None]]] = defaultdict(list)
        self._lock = threading.RLock()

    def subscribe(self, topic: str, handler: Callable[..., None]) -> Callable[[], None]:
        """Register ``handler`` for ``topic``; returns an unsubscribe callable."""
        with self._lock:
            self._subscribers[topic].append(handler)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._subscribers[topic].remove(handler)
                except ValueError:
                    pass

        return unsubscribe

    def publish(self, topic: str, *args: Any, **kwargs: Any) -> None:
        """Notify every subscriber of ``topic``; handler errors are contained."""
        with self._lock:
            handlers = list(self._subscribers.get(topic, ()))
        for handler in handlers:
            try:
                handler(*args, **kwargs)
            except Exception:  # pragma: no cover - a subscriber must not break publishers
                log.exception("Event handler for %r failed", topic)


#: Topic names, so publishers and subscribers cannot drift apart.
TOPIC_STATE = "state"
TOPIC_SCAN_PROGRESS = "scan.progress"
TOPIC_BACKUP_PROGRESS = "backup.progress"
TOPIC_ITEM_FINISHED = "backup.item"
TOPIC_NOTIFY = "notify"
