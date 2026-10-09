"""Downloading one Drive item, with retries, skip detection and persistence."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..config.settings import EXISTING_ASK, EXISTING_REPLACE, EXISTING_SKIP
from ..database.models import DriveItemRecord
from ..database.repositories import ItemRepository
from ..database.schema import ItemStatus
from ..filesystem.storage import exists
from ..utils.logging import get_logger
from .base import DownloadContext, DownloadOutcome, Interrupted, TransferControl
from .retry import AuthorizationRequired, DownloadError, InsufficientSpace, RetryPolicy, classify_exception
from .strategies import get_strategy
from .verifier import looks_unchanged

log = get_logger(__name__)

#: How often a running download persists its byte count for crash recovery.
_CHECKPOINT_SECONDS = 5.0


@dataclass
class TaskResult:
    """What happened to one item during one backup run."""

    item: DriveItemRecord
    status: str
    bytes_written: int = 0
    error: str | None = None
    attempts: int = 1
    duration: float = 0.0
    fatal: bool = False

    @property
    def succeeded(self) -> bool:
        """True when the item ended in a good terminal state."""
        return self.status in (ItemStatus.COMPLETED, ItemStatus.SKIPPED)


class ItemDownloader:
    """Runs one item to a terminal state, applying the retry policy."""

    def __init__(
        self,
        items: ItemRepository,
        control: TransferControl,
        policy: RetryPolicy,
        existing_policy: str = EXISTING_SKIP,
        verify: bool = True,
        chunk_size: int = 8 * 1024 * 1024,
        session_id: int | None = None,
        conflict_resolver: Callable[[DriveItemRecord], str] | None = None,
    ) -> None:
        self.items = items
        self.control = control
        self.policy = policy
        self.existing_policy = existing_policy
        self.verify = verify
        self.chunk_size = chunk_size
        self.session_id = session_id
        self.conflict_resolver = conflict_resolver

    def process(
        self,
        item: DriveItemRecord,
        client,
        on_progress: Callable[[DriveItemRecord, int], None],
        ensure_space: Callable[[int], None],
    ) -> TaskResult:
        """Download one item and persist the outcome."""
        started = time.monotonic()
        target = Path(item.local_path)

        skip = self._should_skip(item, target)
        if skip is not None:
            self.items.set_status(
                item.id, ItemStatus.SKIPPED, error=skip, session_id=self.session_id,
                downloaded_bytes=item.total_bytes,
            )
            return TaskResult(item, ItemStatus.SKIPPED, item.total_bytes, skip,
                              duration=time.monotonic() - started)

        strategy = get_strategy(item.strategy)
        last_checkpoint = [time.monotonic()]

        def progress(total: int) -> None:
            on_progress(item, total)
            now = time.monotonic()
            if now - last_checkpoint[0] >= _CHECKPOINT_SECONDS:
                last_checkpoint[0] = now
                self.items.set_progress(item.id, total)

        context = DownloadContext(
            client=client,
            control=self.control,
            chunk_size=self.chunk_size,
            verify=self.verify,
            on_progress=progress,
            ensure_space=ensure_space,
        )

        self.items.set_status(
            item.id, ItemStatus.DOWNLOADING, session_id=self.session_id
        )

        attempt = 0
        last_error: DownloadError | None = None
        while True:
            attempt += 1
            try:
                outcome = strategy.download(item, target, context)
            except Interrupted as stop:
                status = ItemStatus.CANCELLED if stop.cancelled else ItemStatus.PAUSED
                self.items.set_status(item.id, status, session_id=self.session_id)
                return TaskResult(item, status, attempts=attempt,
                                  duration=time.monotonic() - started)
            except (AuthorizationRequired, InsufficientSpace) as fatal:
                self.items.set_status(
                    item.id, ItemStatus.FAILED, error=fatal.message,
                    retry_count=attempt, session_id=self.session_id,
                )
                return TaskResult(item, ItemStatus.FAILED, error=fatal.message,
                                  attempts=attempt, fatal=True,
                                  duration=time.monotonic() - started)
            except BaseException as exc:  # noqa: BLE001 - normalised below
                error = classify_exception(exc)
                last_error = error
                retry, delay = self.policy.should_retry(error, attempt)
                if not retry:
                    self.items.set_status(
                        item.id, ItemStatus.FAILED, error=error.message,
                        retry_count=attempt, session_id=self.session_id,
                    )
                    return TaskResult(item, ItemStatus.FAILED, error=error.message,
                                      attempts=attempt,
                                      duration=time.monotonic() - started)
                log.warning("Retry %d/%d for %s in %.1fs: %s", attempt,
                            self.policy.max_attempts, item.name, delay, error.message)
                if not self._sleep(delay):
                    status = (ItemStatus.CANCELLED if self.control.cancelled
                              else ItemStatus.PAUSED)
                    self.items.set_status(item.id, status, session_id=self.session_id)
                    return TaskResult(item, status, attempts=attempt,
                                      duration=time.monotonic() - started)
                continue

            # Verification failures are retried like transport failures.
            if outcome.status == ItemStatus.VERIFY_FAILED:
                retry, delay = self.policy.should_retry(
                    DownloadError(outcome.error or "verification failed", retryable=True),
                    attempt,
                )
                if retry:
                    log.warning("Verification failed for %s (attempt %d): %s",
                                item.name, attempt, outcome.error)
                    from . import resume as resume_module

                    resume_module.discard(target)
                    if self._sleep(delay):
                        continue
            return self._persist(item, outcome, attempt, started)

        # Unreachable, but keeps type checkers happy.
        return TaskResult(item, ItemStatus.FAILED,
                          error=last_error.message if last_error else "unknown")

    # -- internals ---------------------------------------------------------
    def _should_skip(self, item: DriveItemRecord, target: Path) -> str | None:
        """Return a skip reason when the local copy is already good."""
        if not exists(target):
            return None
        if self.existing_policy == EXISTING_REPLACE:
            return None

        policy = self.existing_policy
        if policy == EXISTING_ASK and self.conflict_resolver is not None:
            policy = self.conflict_resolver(item)
            if policy == EXISTING_REPLACE:
                return None

        # Exports have no reliable size or checksum, so "already there" is the
        # only signal available for them.
        if item.strategy == "export":
            return "Already exported (existing file kept)."

        expected = item.total_bytes or item.size or None
        if looks_unchanged(target, expected, item.md5_checksum, verify_checksum=False):
            return "Unchanged since the last backup."
        return None

    def _sleep(self, seconds: float) -> bool:
        """Interruptible sleep. Returns False if paused or cancelled."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if not self.control.running():
                return False
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
        return self.control.running()

    def _persist(
        self, item: DriveItemRecord, outcome: DownloadOutcome, attempt: int, started: float
    ) -> TaskResult:
        self.items.set_status(
            item.id, outcome.status, error=outcome.error,
            downloaded_bytes=outcome.bytes_written, retry_count=attempt,
            session_id=self.session_id,
        )
        if outcome.status == ItemStatus.COMPLETED:
            log.info("Downloaded %s (%s bytes)", item.relative_path, outcome.bytes_written)
        elif outcome.status != ItemStatus.SKIPPED:
            log.warning("%s: %s - %s", outcome.status, item.relative_path, outcome.error)
        return TaskResult(item, outcome.status, outcome.bytes_written, outcome.error,
                          attempt, time.monotonic() - started)
