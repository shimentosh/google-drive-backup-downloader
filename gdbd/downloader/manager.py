"""The download manager: concurrency, live progress, pause, resume and cancel.

One manager instance runs one backup pass. It owns a fixed pool of worker
threads, a dispatch loop that never oversubscribes the pool, and the aggregate
counters the UI renders. All byte accounting is absolute-per-item so a transfer
that restarts cannot inflate the totals.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Callable, Sequence

from ..constants import DISK_SPACE_MARGIN
from ..database.models import DriveItemRecord
from ..database.repositories import ItemRepository
from ..database.schema import ItemStatus
from ..drive.client import DriveClient
from ..filesystem.storage import disk_space
from ..utils.logging import get_logger
from .base import TransferControl
from .retry import InsufficientSpace, RetryPolicy
from .worker import ItemDownloader, TaskResult

log = get_logger(__name__)

_TICK = 0.2
_SPEED_WINDOW = 8.0


@dataclass
class ActiveTransfer:
    """A file currently being written, as shown in the queue and header."""

    item_id: int
    name: str
    relative_path: str
    bytes_done: int = 0
    bytes_total: int = 0
    started_at: float = 0.0
    baseline: int = 0

    @property
    def speed(self) -> float:
        """Average rate for this file since it started, in bytes per second."""
        elapsed = time.monotonic() - self.started_at
        if elapsed <= 0:
            return 0.0
        return max(0.0, (self.bytes_done - self.baseline) / elapsed)


@dataclass
class BackupProgress:
    """Immutable snapshot handed to the UI several times per second."""

    total_files: int = 0
    finished_files: int = 0
    completed_files: int = 0
    skipped_files: int = 0
    failed_files: int = 0
    unsupported_files: int = 0
    total_bytes: int = 0
    done_bytes: int = 0
    speed: float = 0.0
    eta_seconds: float | None = None
    active: list[ActiveTransfer] = field(default_factory=list)
    paused: bool = False
    cancelling: bool = False

    @property
    def byte_fraction(self) -> float:
        """Overall progress as bytes done over bytes planned."""
        if self.total_bytes <= 0:
            return 1.0 if self.finished_files >= self.total_files else 0.0
        return min(1.0, self.done_bytes / self.total_bytes)


@dataclass
class BackupOutcome:
    """Final tally for one backup run."""

    completed: int = 0
    skipped: int = 0
    failed: int = 0
    unsupported: int = 0
    cancelled: bool = False
    paused: bool = False
    bytes_downloaded: int = 0
    duration: float = 0.0
    fatal_error: str | None = None

    @property
    def total(self) -> int:
        """Every item the run reached a decision about."""
        return self.completed + self.skipped + self.failed + self.unsupported


class DownloadManager:
    """Coordinates the worker pool for one backup run."""

    def __init__(
        self,
        client: DriveClient,
        items: ItemRepository,
        destination: str,
        *,
        workers: int = 4,
        chunk_size: int = 8 * 1024 * 1024,
        retry_count: int = 5,
        verify: bool = True,
        existing_policy: str = "skip",
        session_id: int | None = None,
        base_bytes: int = 0,
        base_finished: int = 0,
        total_bytes: int = 0,
        total_files: int = 0,
        on_progress: Callable[[BackupProgress], None] | None = None,
        on_item_finished: Callable[[TaskResult], None] | None = None,
        conflict_resolver: Callable[[DriveItemRecord], str] | None = None,
    ) -> None:
        self.client = client
        self.items = items
        self.destination = destination
        self.workers = max(1, workers)
        self.control = TransferControl()
        self.on_progress = on_progress or (lambda _p: None)
        self.on_item_finished = on_item_finished or (lambda _r: None)
        self.session_id = session_id

        self._downloader = ItemDownloader(
            items=items,
            control=self.control,
            policy=RetryPolicy(max_attempts=max(1, retry_count)),
            existing_policy=existing_policy,
            verify=verify,
            chunk_size=chunk_size,
            session_id=session_id,
            conflict_resolver=conflict_resolver,
        )

        self._lock = threading.Lock()
        self._active: dict[int, ActiveTransfer] = {}
        self._samples: deque[tuple[float, int]] = deque()
        self._moved = 0            # bytes actually transferred during this run
        self._finished_bytes = 0   # bytes attributable to finished items
        self._base_bytes = base_bytes
        # Files already in a terminal state before this run started (already
        # downloaded, or reported unsupported during the scan).
        self._base_finished = base_finished
        self._total_bytes = total_bytes
        self._total_files = total_files
        self._counts = {
            ItemStatus.COMPLETED: 0, ItemStatus.SKIPPED: 0,
            ItemStatus.FAILED: 0, ItemStatus.UNSUPPORTED: 0,
        }
        self._resume_event = threading.Event()
        self._resume_event.set()
        self._space_free = 0
        self._space_checked = 0.0
        self._fatal: str | None = None

    # -- controls ----------------------------------------------------------
    def pause(self) -> None:
        """Stop dispatching and ask active transfers to stop at a chunk boundary."""
        self.control.pause_event.set()
        self._resume_event.clear()
        log.info("Backup paused")

    def resume(self) -> None:
        """Continue a paused backup."""
        self.control.pause_event.clear()
        self._resume_event.set()
        log.info("Backup resumed")

    def cancel(self) -> None:
        """Stop everything; partial files and completed files are kept."""
        self.control.cancel_event.set()
        self._resume_event.set()
        log.info("Backup cancellation requested")

    @property
    def is_paused(self) -> bool:
        """True while the backup is paused."""
        return self.control.paused

    # -- main loop ---------------------------------------------------------
    def run(self, queue: Sequence[DriveItemRecord]) -> BackupOutcome:
        """Process every item in ``queue`` and return the run's tally.

        Blocks until the queue drains, the user cancels, or a fatal condition
        (lost authorisation, full disk) stops the run.
        """
        started = time.monotonic()
        pending: deque[DriveItemRecord] = deque(queue)
        outcome = BackupOutcome()
        active_futures: dict[Future, DriveItemRecord] = {}

        with ThreadPoolExecutor(
            max_workers=self.workers, thread_name_prefix="gdbd-dl"
        ) as pool:
            while True:
                if self.control.cancelled and not active_futures:
                    break
                if not pending and not active_futures:
                    break

                while (
                    pending
                    and not self.control.paused
                    and not self.control.cancelled
                    and len(active_futures) < self.workers
                ):
                    item = pending.popleft()
                    future = pool.submit(self._run_item, item)
                    active_futures[future] = item

                if not active_futures:
                    # Paused with nothing running: idle until resumed/cancelled.
                    if self.control.paused and not self.control.cancelled:
                        self._emit()
                        self._resume_event.wait(timeout=0.5)
                        continue
                    if self.control.cancelled or not pending:
                        break
                    continue

                done, _ = wait(set(active_futures), timeout=_TICK,
                               return_when=FIRST_COMPLETED)
                for future in done:
                    item = active_futures.pop(future)
                    try:
                        result = future.result()
                    except BaseException as exc:  # pragma: no cover - defensive
                        log.exception("Worker crashed on %s", item.name)
                        self.items.set_status(
                            item.id, ItemStatus.FAILED, error=f"Internal error: {exc}"
                        )
                        result = TaskResult(item, ItemStatus.FAILED, error=str(exc))
                    self._absorb(result, pending)
                self._emit()

        self._emit()
        outcome.completed = self._counts[ItemStatus.COMPLETED]
        outcome.skipped = self._counts[ItemStatus.SKIPPED]
        outcome.failed = self._counts[ItemStatus.FAILED]
        outcome.unsupported = self._counts[ItemStatus.UNSUPPORTED]
        outcome.cancelled = self.control.cancelled
        outcome.paused = self.control.paused and bool(pending)
        outcome.bytes_downloaded = self._moved
        outcome.duration = time.monotonic() - started
        outcome.fatal_error = self._fatal
        log.info(
            "Run finished: %d completed, %d skipped, %d failed, %d unsupported, %s bytes",
            outcome.completed, outcome.skipped, outcome.failed, outcome.unsupported,
            outcome.bytes_downloaded,
        )
        return outcome

    # -- per item ----------------------------------------------------------
    def _run_item(self, item: DriveItemRecord) -> TaskResult:
        with self._lock:
            self._active[item.id] = ActiveTransfer(
                item_id=item.id,
                name=item.name,
                relative_path=item.relative_path,
                bytes_done=item.downloaded_bytes,
                bytes_total=item.total_bytes,
                started_at=time.monotonic(),
                baseline=item.downloaded_bytes,
            )
        try:
            return self._downloader.process(
                item, self.client, self._on_item_progress, self._ensure_space
            )
        finally:
            with self._lock:
                self._active.pop(item.id, None)

    def _on_item_progress(self, item: DriveItemRecord, total: int) -> None:
        with self._lock:
            state = self._active.get(item.id)
            if state is None:
                return
            if total < state.baseline:
                # The transfer restarted from zero; re-baseline so the global
                # counter never runs backwards or double-counts.
                state.baseline = total
            delta = total - state.bytes_done
            state.bytes_done = total
            if delta > 0:
                self._moved += delta

    def _absorb(self, result: TaskResult, pending: deque[DriveItemRecord]) -> None:
        status = result.status
        if status == ItemStatus.PAUSED:
            pending.appendleft(result.item)
            return
        if status == ItemStatus.CANCELLED:
            return
        if status == ItemStatus.VERIFY_FAILED:
            status = ItemStatus.FAILED

        with self._lock:
            if status in self._counts:
                self._counts[status] += 1
            else:
                self._counts[ItemStatus.FAILED] += 1
            if status in (ItemStatus.COMPLETED, ItemStatus.SKIPPED):
                self._finished_bytes += max(result.bytes_written, result.item.total_bytes)

        if result.fatal and self._fatal is None:
            self._fatal = result.error
            log.error("Stopping backup: %s", result.error)
            self.pause()

        self.on_item_finished(result)

    # -- disk space --------------------------------------------------------
    def _ensure_space(self, needed: int) -> None:
        """Raise when the destination volume cannot absorb ``needed`` bytes."""
        now = time.monotonic()
        if now - self._space_checked > 3.0:
            try:
                self._space_free = disk_space(self.destination).free
            except OSError as exc:
                raise InsufficientSpace(
                    f"Destination unavailable: {exc}"
                ) from exc
            self._space_checked = now
        if self._space_free - needed < DISK_SPACE_MARGIN:
            raise InsufficientSpace(
                f"Insufficient disk space: {needed:,} bytes needed, "
                f"{self._space_free:,} bytes free."
            )

    # -- progress ----------------------------------------------------------
    def snapshot(self) -> BackupProgress:
        """Build a consistent progress snapshot."""
        with self._lock:
            active = [
                ActiveTransfer(
                    a.item_id, a.name, a.relative_path, a.bytes_done, a.bytes_total,
                    a.started_at, a.baseline,
                )
                for a in self._active.values()
            ]
            active_bytes = sum(a.bytes_done for a in active)
            counts = dict(self._counts)
            finished_bytes = self._finished_bytes
            moved = self._moved

        now = time.monotonic()
        self._samples.append((now, moved))
        while self._samples and now - self._samples[0][0] > _SPEED_WINDOW:
            self._samples.popleft()
        speed = 0.0
        if len(self._samples) >= 2:
            span = self._samples[-1][0] - self._samples[0][0]
            if span > 0.5:
                speed = max(0.0, (self._samples[-1][1] - self._samples[0][1]) / span)

        done_bytes = self._base_bytes + finished_bytes + active_bytes
        remaining = max(0, self._total_bytes - done_bytes)
        eta = remaining / speed if speed > 1024 else None

        finished_files = self._base_finished + sum(counts.values())
        return BackupProgress(
            total_files=self._total_files,
            finished_files=finished_files,
            completed_files=counts.get(ItemStatus.COMPLETED, 0),
            skipped_files=counts.get(ItemStatus.SKIPPED, 0),
            failed_files=counts.get(ItemStatus.FAILED, 0),
            unsupported_files=counts.get(ItemStatus.UNSUPPORTED, 0),
            total_bytes=self._total_bytes,
            done_bytes=min(done_bytes, self._total_bytes) if self._total_bytes else done_bytes,
            speed=speed,
            eta_seconds=eta,
            active=sorted(active, key=lambda a: a.started_at),
            paused=self.control.paused,
            cancelling=self.control.cancelled,
        )

    def _emit(self) -> None:
        try:
            self.on_progress(self.snapshot())
        except Exception:  # pragma: no cover - a UI slot must never kill the run
            log.exception("Progress callback failed")
