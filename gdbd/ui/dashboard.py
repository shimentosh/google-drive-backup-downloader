"""Overview page: account, Drive statistics, destination and quick actions."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..database.models import Account, BackupSession
from ..drive.models import CATEGORY_ORDER
from ..drive.scanner import ScanProgress, ScanResult
from ..services.event_bus import AppState
from ..utils.formatting import format_bytes, format_count, format_duration
from ..utils.timeutil import format_relative_day
from .icons import icon
from .theme import Palette
from .widgets.common import Banner, Card, Divider, KeyValue, PageHeader, StatCard, StatusPill
from .widgets.progress import ProgressBar

#: Tone used for each application state's status chip.
_STATE_TONE = {
    AppState.DISCONNECTED: "neutral",
    AppState.AUTHENTICATING: "info",
    AppState.CONNECTED: "info",
    AppState.SCANNING: "info",
    AppState.READY: "info",
    AppState.BACKUP_RUNNING: "info",
    AppState.BACKUP_PAUSED: "warning",
    AppState.BACKUP_CANCELLING: "warning",
    AppState.BACKUP_COMPLETED: "success",
    AppState.BACKUP_COMPLETED_WITH_ERRORS: "warning",
    AppState.BACKUP_FAILED: "danger",
}


class DashboardPage(QWidget):
    """The landing page once an account is connected."""

    scan_requested = Signal()
    cancel_scan_requested = Signal()
    backup_requested = Signal()
    resume_requested = Signal()
    destination_requested = Signal()
    open_folder_requested = Signal()

    def __init__(self, palette: Palette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._palette = palette

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        host = QWidget()
        host.setObjectName("Root")
        scroll.setWidget(host)
        shell = QVBoxLayout(self)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.addWidget(scroll)

        root = QVBoxLayout(host)
        root.setContentsMargins(28, 26, 28, 28)
        root.setSpacing(16)

        self.header = PageHeader("Overview", "Your Google Drive at a glance")
        self.refresh_button = QPushButton("  Rescan Drive")
        self.refresh_button.setIcon(icon("refresh", palette.text, 16))
        self.refresh_button.clicked.connect(self.scan_requested.emit)
        self.backup_button = QPushButton("  Back Up Entire Drive")
        self.backup_button.setObjectName("Primary")
        self.backup_button.setIcon(icon("backup", palette.on_accent, 17))
        self.backup_button.clicked.connect(self.backup_requested.emit)
        self.header.actions.addWidget(self.refresh_button)
        self.header.actions.addWidget(self.backup_button)
        root.addWidget(self.header)

        self.banner = Banner()
        self.banner.apply(palette)
        root.addWidget(self.banner)

        # -- connection ----------------------------------------------------
        connection = Card(padding=18, spacing=10)
        top = QHBoxLayout()
        top.setSpacing(12)
        logo = QLabel()
        logo.setPixmap(icon("drive", palette.accent, 30).pixmap(30, 30))
        top.addWidget(logo)
        text = QVBoxLayout()
        text.setSpacing(2)
        self.account_label = QLabel("Google Drive")
        self.account_label.setObjectName("SectionTitle")
        self.email_label = QLabel("Not connected")
        self.email_label.setObjectName("Faint")
        text.addWidget(self.account_label)
        text.addWidget(self.email_label)
        top.addLayout(text)
        top.addStretch(1)
        self.status_pill = StatusPill("Not connected")
        self.status_pill.apply(palette)
        top.addWidget(self.status_pill)
        connection.add_layout(top)
        root.addWidget(connection)

        # -- headline statistics -------------------------------------------
        stats = QGridLayout()
        stats.setSpacing(14)
        self.files_card = StatCard("Files", "-")
        self.folders_card = StatCard("Folders", "-")
        self.size_card = StatCard("Total size", "-")
        self.backed_up_card = StatCard("Backed up", "-", "")
        stats.addWidget(self.files_card, 0, 0)
        stats.addWidget(self.folders_card, 0, 1)
        stats.addWidget(self.size_card, 0, 2)
        stats.addWidget(self.backed_up_card, 0, 3)
        root.addLayout(stats)

        # -- scan progress --------------------------------------------------
        self.scan_card = Card(padding=18, spacing=10)
        scan_top = QHBoxLayout()
        scan_title = QLabel("Scanning Google Drive...")
        scan_title.setObjectName("SectionTitle")
        scan_top.addWidget(scan_title)
        scan_top.addStretch(1)
        self.scan_cancel = QPushButton("Stop scan")
        self.scan_cancel.clicked.connect(self.cancel_scan_requested.emit)
        scan_top.addWidget(self.scan_cancel)
        self.scan_card.add_layout(scan_top)
        self.scan_counts = QLabel("Folders discovered: 0     Files discovered: 0")
        self.scan_counts.setObjectName("Muted")
        self.scan_card.add(self.scan_counts)
        self.scan_current = QLabel("")
        self.scan_current.setObjectName("Faint")
        self.scan_card.add(self.scan_current)
        self.scan_bar = ProgressBar(palette=palette)
        self.scan_bar.set_indeterminate(True)
        self.scan_card.add(self.scan_bar)
        self.scan_card.setVisible(False)
        root.addWidget(self.scan_card)

        # -- two-column detail ---------------------------------------------
        columns = QHBoxLayout()
        columns.setSpacing(14)

        breakdown = Card(padding=18, spacing=8)
        breakdown_title = QLabel("Content breakdown")
        breakdown_title.setObjectName("SectionTitle")
        breakdown.add(breakdown_title)
        breakdown.add(Divider())
        self.category_rows: dict[str, KeyValue] = {}
        for name in CATEGORY_ORDER:
            row = KeyValue(name, "-")
            self.category_rows[name] = row
            breakdown.add(row)
        breakdown.body().addStretch(1)
        columns.addWidget(breakdown, 1)

        right = QVBoxLayout()
        right.setSpacing(14)

        destination = Card(padding=18, spacing=8)
        dest_top = QHBoxLayout()
        dest_title = QLabel("Destination")
        dest_title.setObjectName("SectionTitle")
        dest_top.addWidget(dest_title)
        dest_top.addStretch(1)
        self.change_dest = QPushButton("Change")
        self.change_dest.setObjectName("Ghost")
        self.change_dest.clicked.connect(self.destination_requested.emit)
        dest_top.addWidget(self.change_dest)
        destination.add_layout(dest_top)
        destination.add(Divider())
        self.dest_path = QLabel("No folder selected")
        self.dest_path.setObjectName("Mono")
        self.dest_path.setWordWrap(True)
        self.dest_path.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        destination.add(self.dest_path)
        self.free_space = KeyValue("Free space", "-")
        destination.add(self.free_space)
        self.open_folder = QPushButton("  Open backup folder")
        self.open_folder.setIcon(icon("external", palette.text, 15))
        self.open_folder.clicked.connect(self.open_folder_requested.emit)
        destination.add(self.open_folder)
        right.addWidget(destination)

        last_backup = Card(padding=18, spacing=8)
        last_title = QLabel("Last backup")
        last_title.setObjectName("SectionTitle")
        last_backup.add(last_title)
        last_backup.add(Divider())
        self.last_when = KeyValue("When", "Never")
        self.last_result = KeyValue("Result", "-")
        self.last_duration = KeyValue("Duration", "-")
        last_backup.add(self.last_when)
        last_backup.add(self.last_result)
        last_backup.add(self.last_duration)
        self.resume_button = QPushButton("Resume unfinished backup")
        self.resume_button.setObjectName("Primary")
        self.resume_button.clicked.connect(self.resume_requested.emit)
        self.resume_button.setVisible(False)
        last_backup.add(self.resume_button)
        right.addWidget(last_backup)
        right.addStretch(1)

        columns.addLayout(right, 1)
        root.addLayout(columns)
        root.addStretch(1)

    # -- updates -----------------------------------------------------------
    def set_account(self, account: Account | None) -> None:
        """Show the connected account, or the disconnected placeholder."""
        if account is None:
            self.account_label.setText("Google Drive")
            self.email_label.setText("Not connected")
            return
        self.account_label.setText(account.display_name or "Google Drive")
        detail = account.email
        if account.storage_used is not None:
            detail += f"   ·   {format_bytes(account.storage_used)} used"
            if account.storage_limit:
                detail += f" of {format_bytes(account.storage_limit)}"
        self.email_label.setText(detail)

    def set_state(self, state: AppState) -> None:
        """Enable and disable actions according to the application state."""
        self.status_pill.set_status(state.label, _STATE_TONE.get(state, "neutral"))
        scanning = state is AppState.SCANNING
        self.scan_card.setVisible(scanning)
        self.refresh_button.setEnabled(
            state in (AppState.CONNECTED, AppState.READY, AppState.BACKUP_COMPLETED,
                      AppState.BACKUP_COMPLETED_WITH_ERRORS, AppState.BACKUP_FAILED)
        )
        self.backup_button.setEnabled(
            state in (AppState.READY, AppState.BACKUP_COMPLETED,
                      AppState.BACKUP_COMPLETED_WITH_ERRORS, AppState.BACKUP_FAILED)
        )
        self.change_dest.setEnabled(not state.is_backup_active)
        if state is AppState.BACKUP_RUNNING:
            self.backup_button.setText("  Backup running")
        else:
            self.backup_button.setText("  Back Up Entire Drive")

    def set_scan_progress(self, progress: ScanProgress) -> None:
        """Update the live scan counters."""
        self.scan_counts.setText(
            f"Folders discovered: {format_count(progress.folders)}"
            f"     Files discovered: {format_count(progress.files)}"
            f"     {format_bytes(progress.bytes_total)}"
        )
        stage = {
            "listing": "Listing Drive contents",
            "hierarchy": "Rebuilding folder structure",
            "planning": "Planning local paths",
        }.get(progress.stage, progress.stage)
        current = f"   ·   {progress.current_path}" if progress.current_path else ""
        self.scan_current.setText(f"{stage}{current}")
        self.scan_bar.advance()

    def set_scan_result(self, result: ScanResult) -> None:
        """Fill the statistics cards from a finished scan."""
        self.files_card.set_value(format_count(result.files))
        self.folders_card.set_value(format_count(result.folders))
        self.size_card.set_value(format_bytes(result.total_bytes))
        for name, row in self.category_rows.items():
            count = result.category_counts.get(name, 0)
            size = result.category_bytes.get(name, 0)
            row.set_value(f"{format_bytes(size)}   ({format_count(count)})")

        notes = []
        if result.export_files:
            notes.append(f"{result.export_files:,} Google Workspace file(s) will be exported")
        if result.unsupported_files:
            notes.append(f"{result.unsupported_files:,} item(s) cannot be downloaded")
        if result.removed_remotely:
            notes.append(
                f"{result.removed_remotely:,} previously backed-up item(s) no longer "
                "exist in Drive (local copies are kept)"
            )
        if notes:
            self.banner.show_message("  ·  ".join(notes), "info")
        else:
            self.banner.hide_message()

    def set_totals(self, totals: dict[str, int]) -> None:
        """Refresh the cards from database totals (used on launch)."""
        if not totals or not totals.get("files"):
            return
        self.files_card.set_value(format_count(totals.get("files", 0)))
        self.folders_card.set_value(format_count(totals.get("folders", 0)))
        self.size_card.set_value(format_bytes(totals.get("total_bytes", 0)))
        done = totals.get("done_bytes", 0)
        total = totals.get("total_bytes", 0)
        pct = (done / total * 100) if total else 0
        self.backed_up_card.set_value(format_bytes(done), f"{pct:.0f}% of your Drive")

    def set_destination(self, path: str, free_space: int | None) -> None:
        """Show the destination folder and its free space."""
        self.dest_path.setText(path or "No folder selected")
        self.free_space.set_value(format_bytes(free_space) if free_space else "-")
        self.open_folder.setEnabled(bool(path))

    def set_last_session(self, session: BackupSession | None) -> None:
        """Summarise the most recent finished backup."""
        if session is None:
            self.last_when.set_value("Never")
            self.last_result.set_value("-")
            self.last_duration.set_value("-")
            return
        self.last_when.set_value(format_relative_day(session.finished_at or session.started_at))
        self.last_result.set_value(
            f"{session.successful_files:,} ok · {session.failed_files:,} failed"
        )
        self.last_duration.set_value(format_duration(session.duration_seconds))

    def set_resume_available(self, remaining: tuple[int, int] | None) -> None:
        """Offer to resume when a previous run left work behind."""
        if remaining is None:
            self.resume_button.setVisible(False)
            return
        done, total = remaining
        self.resume_button.setText(f"Resume backup  ({done:,} / {total:,} files done)")
        self.resume_button.setVisible(True)
