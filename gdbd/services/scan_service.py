"""Orchestration of a full Drive scan (no Qt dependency)."""

from __future__ import annotations

from typing import Callable

from ..config.settings import Settings
from ..database.repositories import Repositories
from ..drive.client import DriveClient
from ..drive.exporters import ExportPlanner
from ..drive.scanner import DriveScanner, ScanProgress, ScanResult, create_local_folders
from ..utils.logging import get_logger

log = get_logger(__name__)


class ScanRunner:
    """Runs one scan end to end and records the account that produced it."""

    def __init__(
        self,
        client: DriveClient,
        repos: Repositories,
        settings: Settings,
        destination: str,
    ) -> None:
        self.client = client
        self.repos = repos
        self.settings = settings
        self.destination = destination
        self._scanner: DriveScanner | None = None

    def cancel(self) -> None:
        """Ask an in-flight scan to stop."""
        if self._scanner is not None:
            self._scanner.cancel()

    def run(
        self,
        account_id: int,
        on_progress: Callable[[ScanProgress], None] | None = None,
        create_folders: bool = False,
    ) -> ScanResult:
        """Scan the whole Drive into ``drive_items`` and return the summary.

        ``create_folders`` materialises the (possibly empty) folder tree on disk;
        it is deferred to backup time by default so a scan never touches the
        destination.
        """
        planner = ExportPlanner(
            available_formats=self.client.export_formats(),
            preferred=self.settings.export_map(),
            export_workspace=self.settings.export_workspace,
        )
        scanner = DriveScanner(
            client=self.client,
            items=self.repos.items,
            account_id=account_id,
            destination=self.destination,
            planner=planner,
            include_trashed=self.settings.include_trashed,
            include_shared_with_me=self.settings.include_shared_with_me,
            include_shared_drives=self.settings.include_shared_drives,
            owned_only=self.settings.owned_only,
        )
        self._scanner = scanner
        try:
            result = scanner.scan(on_progress)
        finally:
            self._scanner = None

        if not result.cancelled:
            self.repos.accounts.touch_scan(account_id)
            if create_folders:
                folders = [f.relative_path for f in
                           self.repos.items.folders(account_id, self.destination)]
                created = create_local_folders(self.destination, folders)
                log.info("Created %d local folders", created)
        return result
