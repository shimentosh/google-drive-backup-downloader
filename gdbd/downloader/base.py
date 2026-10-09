"""Shared types for the download strategies."""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..constants import DEFAULT_CHUNK_SIZE
from ..database.models import DriveItemRecord
from ..drive.client import DriveClient


class Interrupted(Exception):
    """Raised inside a transfer when the user pauses or cancels."""

    def __init__(self, cancelled: bool = False) -> None:
        super().__init__("cancelled" if cancelled else "paused")
        self.cancelled = cancelled


@dataclass
class TransferControl:
    """Cooperative pause/cancel signalling shared by every worker."""

    pause_event: threading.Event = field(default_factory=threading.Event)
    cancel_event: threading.Event = field(default_factory=threading.Event)

    @property
    def paused(self) -> bool:
        """True while the backup is paused."""
        return self.pause_event.is_set()

    @property
    def cancelled(self) -> bool:
        """True once the backup has been cancelled."""
        return self.cancel_event.is_set()

    def running(self) -> bool:
        """True while work should continue."""
        return not (self.paused or self.cancelled)

    def check(self) -> None:
        """Raise :class:`Interrupted` if the transfer must stop now."""
        if self.cancel_event.is_set():
            raise Interrupted(cancelled=True)
        if self.pause_event.is_set():
            raise Interrupted(cancelled=False)


@dataclass
class DownloadContext:
    """Everything a strategy needs that is not part of the item itself."""

    client: DriveClient
    control: TransferControl
    chunk_size: int = DEFAULT_CHUNK_SIZE
    verify: bool = True
    #: Called with the *absolute* number of bytes written for the current item.
    #: Absolute rather than incremental so a restarted transfer (server ignored
    #: our Range header) cannot double-count bytes in the global total.
    on_progress: Callable[[int], None] = lambda _total: None
    #: Raises :class:`~gdbd.downloader.retry.InsufficientSpace` when the
    #: destination volume cannot hold ``needed`` more bytes.
    ensure_space: Callable[[int], None] = lambda _needed: None


@dataclass
class DownloadOutcome:
    """Result of one strategy invocation."""

    status: str
    bytes_written: int = 0
    error: str | None = None
    checksum: str | None = None


class DownloadStrategy(ABC):
    """Base class for the different ways a Drive item becomes a local file."""

    name = "base"

    @abstractmethod
    def download(
        self, item: DriveItemRecord, target: Path, context: DownloadContext
    ) -> DownloadOutcome:
        """Fetch ``item`` to ``target`` and report the outcome."""
        raise NotImplementedError

    @property
    def supports_resume(self) -> bool:
        """True when a partially transferred file can continue where it stopped."""
        return False
