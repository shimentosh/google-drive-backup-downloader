"""The Qt-facing application controller.

Owns the database, authenticator, Drive client and settings, and exposes every
long-running operation as a background task that reports back through signals.
The Qt main thread never blocks on network or disk I/O.
"""

from __future__ import annotations

import threading
import traceback
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from ..auth.google_auth import (
    AuthError,
    CredentialsNotConfigured,
    GoogleAuthenticator,
    install_client_config,
)
from ..config.settings import AppContext, EXISTING_SKIP
from ..database.db import Database
from ..database.models import Account, DriveItemRecord
from ..database.repositories import Repositories
from ..database.schema import ItemStatus
from ..downloader.manager import BackupOutcome, BackupProgress
from ..downloader.worker import TaskResult
from ..drive.client import DriveClient
from ..drive.scanner import ScanProgress, ScanResult
from ..utils.logging import get_logger
from .backup_service import BackupPlan, BackupRunner
from .event_bus import AppState
from .scan_service import ScanRunner

log = get_logger(__name__)


class _TaskSignals(QObject):
    """Signals emitted by a background task."""

    finished = Signal(object)
    failed = Signal(str, str)


class _Task(QRunnable):
    """Runs one callable on a worker thread and reports the result."""

    def __init__(self, fn: Callable[[], Any], title: str = "Operation failed") -> None:
        super().__init__()
        self.fn = fn
        self.title = title
        self.signals = _TaskSignals()
        self.setAutoDelete(True)

    @Slot()
    def run(self) -> None:  # noqa: D102 - Qt entry point
        try:
            result = self.fn()
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            log.error("%s: %s", self.title, exc)
            log.debug("%s", traceback.format_exc())
            self.signals.failed.emit(self.title, str(exc))
            return
        self.signals.finished.emit(result)


class AppController(QObject):
    """Single source of truth the UI binds to."""

    state_changed = Signal(object)          # AppState
    account_changed = Signal(object)        # Account | None
    scan_progress = Signal(object)          # ScanProgress
    scan_finished = Signal(object)          # ScanResult
    backup_progress = Signal(object)        # BackupProgress
    backup_item = Signal(object)            # TaskResult
    backup_finished = Signal(object)        # BackupOutcome
    destination_changed = Signal(str)
    notify = Signal(str, str)               # level, message
    error = Signal(str, str)                # title, message
    conflict_requested = Signal(object)     # DriveItemRecord

    def __init__(self, context: AppContext, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self.settings = context.settings
        self.db = Database(context.paths.database)
        self.repos = Repositories(self.db)
        self.auth = GoogleAuthenticator(context.paths, self.settings.credentials_path)
        self.client = DriveClient(self.auth)

        self.pool = QThreadPool()
        self.pool.setMaxThreadCount(4)

        self._state = AppState.DISCONNECTED
        self._account: Account | None = None
        self._scan_runner: ScanRunner | None = None
        self._backup_runner: BackupRunner | None = None
        self._last_scan: ScanResult | None = None
        self._conflict_choice = EXISTING_SKIP
        self._conflict_event = threading.Event()

        # A previous process may have died mid-run; tidy those sessions up.
        closed = self.repos.sessions.close_stale()
        if closed:
            log.info("Marked %d interrupted session(s) as failed", closed)

    # -- state -------------------------------------------------------------
    @property
    def state(self) -> AppState:
        """Current application state."""
        return self._state

    def set_state(self, state: AppState) -> None:
        """Move to ``state`` and notify the UI."""
        if state is self._state:
            return
        log.debug("State %s -> %s", self._state.value, state.value)
        self._state = state
        self.state_changed.emit(state)

    @property
    def account(self) -> Account | None:
        """The connected account, if any."""
        return self._account

    @property
    def destination(self) -> str:
        """The configured backup destination."""
        return self.settings.destination

    def set_destination(self, path: str) -> None:
        """Change the destination and persist it."""
        self.settings.update(destination=str(path))
        self.destination_changed.emit(str(path))

    @property
    def has_active_run(self) -> bool:
        """True while a backup runner exists (running or parked on pause)."""
        return self._backup_runner is not None

    @property
    def last_scan(self) -> ScanResult | None:
        """Summary of the most recent scan in this session."""
        return self._last_scan

    # -- task plumbing -----------------------------------------------------
    def _submit(
        self,
        fn: Callable[[], Any],
        on_done: Callable[[Any], None],
        title: str,
        on_error: Callable[[str, str], None] | None = None,
    ) -> None:
        task = _Task(fn, title)
        task.signals.finished.connect(on_done)
        task.signals.failed.connect(on_error or self._default_error)
        self.pool.start(task)

    def _default_error(self, title: str, message: str) -> None:
        self.error.emit(title, message)

    # -- authentication ----------------------------------------------------
    def credentials_configured(self) -> bool:
        """True when an OAuth client file or env pair is available."""
        self.auth.credentials_path = self.settings.credentials_path
        return self.auth.client_configured()

    def client_id(self) -> str:
        """The OAuth Client ID in use, for display."""
        self.auth.credentials_path = self.settings.credentials_path
        return self.auth.client_id()

    def install_credentials(self, source: Path | dict) -> None:
        """Save an OAuth client (file or built config) as the app's own copy.

        Raises :class:`CredentialsNotConfigured` when ``source`` is not a
        usable Desktop-app client.
        """
        install_client_config(self.context.paths, source)
        # An explicit path from Settings would shadow the new copy.
        self.settings.update(credentials_path="")

    def restore_session(self) -> None:
        """Silently reconnect using stored credentials, if there are any."""
        if not self.auth.has_stored_credentials():
            self.set_state(AppState.DISCONNECTED)
            return
        self.set_state(AppState.AUTHENTICATING)
        self._submit(self._restore_worker, self._on_connected, "Reconnect failed",
                     self._on_auth_error)

    def _restore_worker(self) -> Account | None:
        creds = self.auth.load_stored()
        if creds is None:
            return None
        return self._attach_account()

    def connect_account(self) -> None:
        """Start the interactive OAuth flow."""
        if self._state.is_busy:
            return
        self.set_state(AppState.AUTHENTICATING)
        self._submit(self._connect_worker, self._on_connected,
                     "Google sign-in failed", self._on_auth_error)

    def _connect_worker(self) -> Account:
        self.auth.authorize_interactive()
        self.client.reset()
        return self._attach_account()

    def _attach_account(self) -> Account:
        """Read the account profile and record it locally."""
        email, name, limit, used = self.client.user_info()
        if not email:
            raise AuthError("Google did not return an account e-mail address.")
        account = self.repos.accounts.upsert(email, name, limit, used)
        self.settings.update(last_account_email=email)
        return account

    def _on_connected(self, account: Account | None) -> None:
        if account is None:
            self.set_state(AppState.DISCONNECTED)
            return
        self._account = account
        self.account_changed.emit(account)
        self.set_state(AppState.CONNECTED)
        self.notify.emit("info", f"Connected as {account.email}")

    def _on_auth_error(self, title: str, message: str) -> None:
        self._account = None
        self.set_state(AppState.DISCONNECTED)
        self.error.emit(title, message)

    def disconnect_account(self, forget_local_state: bool = False) -> None:
        """Sign out. Local files are never touched."""
        if self._state.is_backup_active:
            self.error.emit("Backup in progress",
                            "Stop the backup before disconnecting the account.")
            return
        account = self._account
        self.auth.disconnect()
        self.client.reset()
        if forget_local_state and account is not None:
            self.repos.accounts.delete(account.id)
        self._account = None
        self._last_scan = None
        self.account_changed.emit(None)
        self.set_state(AppState.DISCONNECTED)
        self.notify.emit("info", "Google account disconnected")

    # -- scanning ----------------------------------------------------------
    def start_scan(self) -> None:
        """Scan the entire Drive in the background."""
        if self._account is None:
            self.error.emit("Not connected", "Connect a Google account first.")
            return
        if self._state.is_backup_active or self._state is AppState.SCANNING:
            return
        destination = self.destination or str(Path.home() / "Google Drive Backup")
        self._scan_runner = ScanRunner(self.client, self.repos, self.settings, destination)
        self.set_state(AppState.SCANNING)
        account_id = self._account.id

        def work() -> ScanResult:
            assert self._scan_runner is not None
            return self._scan_runner.run(account_id, self._emit_scan_progress)

        self._submit(work, self._on_scan_done, "Drive scan failed", self._on_scan_error)

    def _emit_scan_progress(self, progress: ScanProgress) -> None:
        self.scan_progress.emit(progress)

    def _on_scan_done(self, result: ScanResult) -> None:
        self._scan_runner = None
        self._last_scan = result
        if result.cancelled:
            self.set_state(AppState.CONNECTED)
            self.notify.emit("warning", "Scan cancelled")
        else:
            self.set_state(AppState.READY)
            self.notify.emit(
                "success",
                f"Scan complete: {result.files:,} files in {result.folders:,} folders",
            )
        self.scan_finished.emit(result)

    def _on_scan_error(self, title: str, message: str) -> None:
        self._scan_runner = None
        self.set_state(AppState.CONNECTED)
        self.error.emit(title, message)

    def cancel_scan(self) -> None:
        """Stop an in-flight scan."""
        if self._scan_runner is not None:
            self._scan_runner.cancel()

    # -- backup ------------------------------------------------------------
    def build_plan(self) -> BackupPlan | None:
        """Pre-flight report for the current account and destination."""
        if self._account is None or not self.destination:
            return None
        runner = BackupRunner(self.client, self.repos, self.settings,
                              self._account.id, self.destination)
        return runner.plan()

    def start_backup(self, retry_failed: bool = True) -> None:
        """Begin (or restart) a backup of the entire Drive."""
        if self._account is None:
            self.error.emit("Not connected", "Connect a Google account first.")
            return
        if self._state.is_backup_active:
            return
        if not self.destination:
            self.error.emit("No destination", "Choose a backup folder first.")
            return

        runner = BackupRunner(self.client, self.repos, self.settings,
                              self._account.id, self.destination)
        plan = runner.plan()
        if not plan.can_start:
            self.error.emit("Cannot start backup", "\n".join(plan.problems))
            return

        self._backup_runner = runner
        self.set_state(AppState.BACKUP_RUNNING)

        def work() -> BackupOutcome:
            return runner.run(
                on_progress=self.backup_progress.emit,
                on_item_finished=self.backup_item.emit,
                retry_failed=retry_failed,
                conflict_resolver=self._resolve_conflict,
            )

        self._submit(work, self._on_backup_done, "Backup failed", self._on_backup_error)

    def _on_backup_done(self, outcome: BackupOutcome) -> None:
        self._backup_runner = None
        if outcome.fatal_error:
            self.set_state(AppState.BACKUP_FAILED)
        elif outcome.cancelled:
            self.set_state(AppState.READY)
        elif outcome.paused:
            self.set_state(AppState.BACKUP_PAUSED)
        elif outcome.failed or outcome.unsupported:
            self.set_state(AppState.BACKUP_COMPLETED_WITH_ERRORS)
        else:
            self.set_state(AppState.BACKUP_COMPLETED)
        self.backup_finished.emit(outcome)

    def _on_backup_error(self, title: str, message: str) -> None:
        self._backup_runner = None
        self.set_state(AppState.BACKUP_FAILED)
        self.error.emit(title, message)

    def pause_backup(self) -> None:
        """Pause the running backup."""
        if self._backup_runner is not None:
            self._backup_runner.pause()
            self.set_state(AppState.BACKUP_PAUSED)

    def resume_backup(self) -> None:
        """Resume a paused backup, restarting the run if it already finished."""
        if self._backup_runner is not None:
            self._backup_runner.resume()
            self.set_state(AppState.BACKUP_RUNNING)
        elif self._state in (AppState.BACKUP_PAUSED, AppState.READY):
            self.start_backup()

    def cancel_backup(self) -> None:
        """Cancel the running backup; nothing already downloaded is removed."""
        if self._backup_runner is not None:
            self.set_state(AppState.BACKUP_CANCELLING)
            self._backup_runner.cancel()

    def retry_failed(self) -> int:
        """Reset failed items and start a new run for them."""
        if self._account is None:
            return 0
        count = self.repos.items.retry_failed(self._account.id, self.destination)
        if count:
            self.notify.emit("info", f"Re-queued {count:,} failed file(s)")
            self.start_backup(retry_failed=True)
        else:
            self.notify.emit("info", "There are no failed files to retry")
        return count

    # -- conflict resolution ----------------------------------------------
    def _resolve_conflict(self, item: DriveItemRecord) -> str:
        """Ask the UI what to do about one existing file (blocking worker side)."""
        self._conflict_choice = EXISTING_SKIP
        self._conflict_event.clear()
        self.conflict_requested.emit(item)
        if not self._conflict_event.wait(timeout=120.0):
            return EXISTING_SKIP
        return self._conflict_choice

    def answer_conflict(self, choice: str) -> None:
        """Called from the UI thread with the user's decision."""
        self._conflict_choice = choice
        self._conflict_event.set()

    # -- queries used by the UI -------------------------------------------
    def resume_candidate(self) -> tuple[int, int] | None:
        """``(done_files, total_files)`` when an unfinished backup exists."""
        if self._account is None or not self.destination:
            return None
        counts = self.repos.items.status_counts(self._account.id, self.destination)
        total = sum(counts.values())
        if not total:
            return None
        done = counts.get(ItemStatus.COMPLETED, 0) + counts.get(ItemStatus.SKIPPED, 0)
        remaining = total - done - counts.get(ItemStatus.UNSUPPORTED, 0)
        return (done, total) if remaining > 0 else None

    def shutdown(self) -> None:
        """Stop background work and close the database."""
        self.cancel_scan()
        if self._backup_runner is not None:
            self._backup_runner.cancel()
        self.pool.waitForDone(5000)
        self.db.close()
