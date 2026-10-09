"""Orchestration of a backup run: preflight, session bookkeeping, execution.

Deliberately free of Qt so the whole flow can be exercised in tests with a fake
Drive client.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from ..config.settings import Settings
from ..constants import DISK_SPACE_MARGIN
from ..database.models import DriveItemRecord
from ..database.repositories import Repositories
from ..database.schema import ItemStatus, SessionStatus
from ..downloader.manager import BackupOutcome, BackupProgress, DownloadManager
from ..downloader.worker import TaskResult
from ..drive.client import DriveClient
from ..drive.scanner import create_local_folders
from ..filesystem.storage import disk_space, is_writable
from ..utils.formatting import format_bytes
from ..utils.logging import get_logger, start_backup_log, stop_backup_log
from ..utils.timeutil import now_utc

log = get_logger(__name__)


@dataclass
class BackupPlan:
    """What a backup run is about to do, shown on the setup screen."""

    destination: str
    total_files: int = 0
    total_folders: int = 0
    total_bytes: int = 0
    pending_files: int = 0
    pending_bytes: int = 0
    already_done_bytes: int = 0
    free_space: int = 0
    export_files: int = 0
    unsupported_files: int = 0
    writable: bool = True
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def has_previous_backup(self) -> bool:
        """True when some files were already downloaded to this destination."""
        return self.already_done_bytes > 0

    @property
    def can_start(self) -> bool:
        """True when nothing blocks the run."""
        return not self.problems

    @property
    def required_extra_space(self) -> int:
        """Bytes still needed beyond what the volume currently offers."""
        return max(0, self.pending_bytes + DISK_SPACE_MARGIN - self.free_space)


class BackupRunner:
    """Runs one backup pass against one destination."""

    def __init__(
        self,
        client: DriveClient,
        repos: Repositories,
        settings: Settings,
        account_id: int,
        destination: str,
    ) -> None:
        self.client = client
        self.repos = repos
        self.settings = settings
        self.account_id = account_id
        self.destination = destination
        self.manager: DownloadManager | None = None
        self.session_id: int | None = None
        self._last_persist = 0.0

    # -- planning ----------------------------------------------------------
    def plan(self) -> BackupPlan:
        """Assemble the pre-flight report for this destination."""
        totals = self.repos.items.totals(self.account_id, self.destination)
        pending = self.repos.items.pending_files(self.account_id, self.destination)
        pending_bytes = sum(max(0, i.total_bytes - i.downloaded_bytes) for i in pending)

        plan = BackupPlan(
            destination=self.destination,
            total_files=totals.get("files", 0),
            total_folders=totals.get("folders", 0),
            total_bytes=totals.get("total_bytes", 0),
            pending_files=len(pending),
            pending_bytes=pending_bytes,
            already_done_bytes=totals.get("done_bytes", 0),
            export_files=totals.get("export_files", 0),
            unsupported_files=totals.get("unsupported_files", 0),
        )

        writable, reason = is_writable(self.destination)
        plan.writable = writable
        if not writable:
            plan.problems.append(reason)
            return plan

        try:
            plan.free_space = disk_space(self.destination).free
        except OSError as exc:
            plan.problems.append(f"Destination unavailable: {exc}")
            return plan

        if plan.pending_bytes + DISK_SPACE_MARGIN > plan.free_space:
            plan.problems.append(
                f"Not enough disk space. About {format_bytes(plan.pending_bytes)} is "
                f"needed and {format_bytes(plan.free_space)} is free."
            )
        elif plan.pending_bytes * 1.1 + DISK_SPACE_MARGIN > plan.free_space:
            plan.warnings.append(
                "Free space is only just sufficient; the backup may fill this drive."
            )

        if plan.unsupported_files:
            plan.warnings.append(
                f"{plan.unsupported_files:,} item(s) cannot be downloaded by the Drive "
                "API and will be reported as unsupported."
            )
        if plan.export_files and not self.settings.export_workspace:
            plan.warnings.append(
                f"{plan.export_files:,} Google Workspace file(s) will be skipped "
                "because export is turned off in Settings."
            )
        return plan

    # -- execution ---------------------------------------------------------
    def run(
        self,
        on_progress: Callable[[BackupProgress], None] | None = None,
        on_item_finished: Callable[[TaskResult], None] | None = None,
        retry_failed: bool = True,
        conflict_resolver: Callable[[DriveItemRecord], str] | None = None,
    ) -> BackupOutcome:
        """Execute the backup and return its outcome.

        Blocks the calling thread; the UI runs it inside a worker thread.
        """
        started = now_utc()
        start_backup_log(
            self.repos.db.path.parent / "logs", started.strftime("%Y-%m-%d")
        )
        log.info("Starting backup of account %s to %s", self.account_id, self.destination)

        try:
            return self._run_inner(started, on_progress, on_item_finished,
                                   retry_failed, conflict_resolver)
        finally:
            stop_backup_log()

    def _run_inner(
        self,
        started,
        on_progress: Callable[[BackupProgress], None] | None,
        on_item_finished: Callable[[TaskResult], None] | None,
        retry_failed: bool,
        conflict_resolver: Callable[[DriveItemRecord], str] | None,
    ) -> BackupOutcome:
        items_repo = self.repos.items

        # Anything left mid-flight by a crash resumes rather than restarting.
        recovered = items_repo.reset_interrupted(self.account_id, self.destination)
        if recovered:
            log.info("Recovered %d interrupted item(s) for resume", recovered)
        if retry_failed and self.settings.retry_failed_automatically:
            items_repo.retry_failed(self.account_id, self.destination)

        folders = [f.relative_path for f in items_repo.folders(self.account_id, self.destination)]
        create_local_folders(self.destination, folders)

        queue = items_repo.pending_files(self.account_id, self.destination,
                                         include_failed=retry_failed)
        totals = items_repo.totals(self.account_id, self.destination)

        session = self.repos.sessions.create(
            account_id=self.account_id,
            destination=self.destination,
            total_files=totals.get("files", 0),
            total_folders=totals.get("folders", 0),
            total_bytes=totals.get("total_bytes", 0),
        )
        self.session_id = session.id
        self.repos.sessions.add_event(
            session.id, "backup.start",
            f"{len(queue):,} item(s) queued for {self.destination}",
        )

        already_done = int(totals.get("done_bytes", 0))

        def progress_hook(progress: BackupProgress) -> None:
            self._persist_progress(progress)
            if on_progress is not None:
                on_progress(progress)

        def item_hook(result: TaskResult) -> None:
            if result.status in (ItemStatus.FAILED, ItemStatus.UNSUPPORTED):
                self.repos.sessions.add_event(
                    session.id, f"item.{result.status.lower()}",
                    f"{result.item.relative_path}: {result.error or ''}",
                    level="ERROR" if result.status == ItemStatus.FAILED else "WARNING",
                    item_id=result.item.id,
                )
            self.repos.sessions.upsert_task(
                session.id, result.item.id, result.status, result.bytes_written,
                result.attempts, result.error, finished=True,
            )
            if on_item_finished is not None:
                on_item_finished(result)

        manager = DownloadManager(
            client=self.client,
            items=items_repo,
            destination=self.destination,
            workers=self.settings.workers,
            chunk_size=self.settings.chunk_size,
            retry_count=self.settings.retry_count,
            verify=self.settings.verify_downloads,
            existing_policy=self.settings.existing_files,
            session_id=session.id,
            base_bytes=already_done,
            base_finished=max(0, totals.get("files", 0) - len(queue)),
            total_bytes=totals.get("total_bytes", 0),
            total_files=totals.get("files", 0),
            on_progress=progress_hook,
            on_item_finished=item_hook,
            conflict_resolver=conflict_resolver,
        )
        self.manager = manager

        outcome = manager.run(queue)
        self._finalise(session.id, outcome, started)
        self.manager = None
        return outcome

    def _persist_progress(self, progress: BackupProgress) -> None:
        """Write the session counters occasionally, not on every tick."""
        now = time.monotonic()
        if now - self._last_persist < 3.0 or self.session_id is None:
            return
        self._last_persist = now
        self.repos.sessions.update_progress(
            self.session_id,
            completed_files=progress.completed_files,
            completed_bytes=progress.done_bytes,
            skipped_files=progress.skipped_files,
            failed_files=progress.failed_files,
            unsupported_files=progress.unsupported_files,
        )

    def _finalise(self, session_id: int, outcome: BackupOutcome, started) -> None:
        duration = int((now_utc() - started).total_seconds())
        self.repos.sessions.update_progress(
            session_id,
            completed_files=outcome.completed,
            completed_bytes=outcome.bytes_downloaded,
            skipped_files=outcome.skipped,
            failed_files=outcome.failed,
            unsupported_files=outcome.unsupported,
        )

        if outcome.fatal_error:
            status = SessionStatus.FAILED
            message = outcome.fatal_error
        elif outcome.cancelled:
            status = SessionStatus.CANCELLED
            message = "Cancelled by the user. Progress was saved."
        elif outcome.paused:
            status = SessionStatus.PAUSED
            message = "Paused. Resume to continue where it stopped."
        elif outcome.failed or outcome.unsupported:
            status = SessionStatus.COMPLETED_WITH_ERRORS
            message = (
                f"{outcome.failed} failed, {outcome.unsupported} unsupported."
            )
        else:
            status = SessionStatus.COMPLETED
            message = "All files backed up successfully."

        if status == SessionStatus.PAUSED:
            self.repos.sessions.set_status(session_id, status, message)
        else:
            self.repos.sessions.finish(session_id, status, duration, message)
        self.repos.sessions.add_event(session_id, "backup.finish", message)
        log.info("Backup session %s -> %s (%s)", session_id, status, message)

    # -- controls ----------------------------------------------------------
    def pause(self) -> None:
        """Pause the active run, if any."""
        if self.manager is not None:
            self.manager.pause()

    def resume(self) -> None:
        """Resume a paused run."""
        if self.manager is not None:
            self.manager.resume()

    def cancel(self) -> None:
        """Cancel the active run; downloaded files are kept."""
        if self.manager is not None:
            self.manager.cancel()
