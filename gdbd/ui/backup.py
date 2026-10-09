"""The live backup page: overall progress, current files, queue and controls."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..database.schema import ItemStatus
from ..downloader.manager import BackupOutcome, BackupProgress
from ..downloader.worker import TaskResult
from ..services.event_bus import AppState
from ..utils.formatting import (
    format_bytes,
    format_count,
    format_duration,
    format_speed,
)
from .icons import icon
from .theme import Palette
from .widgets.common import Banner, Card, Divider, PageHeader, StatCard, StatusPill
from .widgets.progress import Ring, TransferPanel
from .widgets.queue import DownloadQueueView

_FILTERS = [
    ("All", None),
    ("Downloading", ItemStatus.DOWNLOADING),
    ("Completed", ItemStatus.COMPLETED),
    ("Skipped", ItemStatus.SKIPPED),
    ("Failed", ItemStatus.FAILED),
    ("Unsupported", ItemStatus.UNSUPPORTED),
]


class BackupPage(QWidget):
    """Shows exactly what the download engine is doing, in real time."""

    pause_requested = Signal()
    resume_requested = Signal()
    cancel_requested = Signal()
    retry_failed_requested = Signal()
    open_folder_requested = Signal()
    start_requested = Signal()

    def __init__(self, palette: Palette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._palette = palette
        self._max_panels = 4

        # Scrollable like the other pages, so nothing gets crushed on short windows.
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
        root.setContentsMargins(28, 26, 28, 22)
        root.setSpacing(14)

        self.header = PageHeader("Backup", "Not started")
        self.status_pill = StatusPill("Idle")
        self.status_pill.apply(palette)
        self.header.actions.addWidget(self.status_pill)
        root.addWidget(self.header)

        self.banner = Banner()
        self.banner.apply(palette)
        root.addWidget(self.banner)

        # -- overall -----------------------------------------------------
        overall = Card(padding=20, spacing=14)
        top = QHBoxLayout()
        top.setSpacing(24)

        self.ring = Ring(size=136, palette=palette)
        top.addWidget(self.ring, 0, Qt.AlignmentFlag.AlignTop)

        right = QVBoxLayout()
        right.setSpacing(10)
        title = QLabel("Overall backup")
        title.setObjectName("SectionTitle")
        right.addWidget(title)

        self.overall_panel = TransferPanel(big=True)
        self.overall_panel.apply(palette)
        self.overall_panel.title.setText("Waiting to start")
        right.addWidget(self.overall_panel)

        stats = QGridLayout()
        stats.setSpacing(10)
        self.files_stat = StatCard("Files", "0 / 0", small=True)
        self.speed_stat = StatCard("Speed", "-", small=True)
        self.eta_stat = StatCard("Time remaining", "-", small=True)
        self.failed_stat = StatCard("Failed", "0", small=True)
        stats.addWidget(self.files_stat, 0, 0)
        stats.addWidget(self.speed_stat, 0, 1)
        stats.addWidget(self.eta_stat, 0, 2)
        stats.addWidget(self.failed_stat, 0, 3)
        right.addLayout(stats)
        top.addLayout(right, 1)
        overall.add_layout(top)

        overall.add(Divider())
        controls = QHBoxLayout()
        controls.setSpacing(9)
        self.pause_button = QPushButton("  Pause")
        self.pause_button.setIcon(icon("pause", palette.text, 15))
        self.pause_button.clicked.connect(self.pause_requested.emit)
        self.resume_button = QPushButton("  Resume")
        self.resume_button.setObjectName("Primary")
        self.resume_button.setIcon(icon("play", palette.on_accent, 15))
        self.resume_button.clicked.connect(self.resume_requested.emit)
        self.cancel_button = QPushButton("  Cancel")
        self.cancel_button.setObjectName("Danger")
        self.cancel_button.setIcon(icon("stop", palette.danger, 15))
        self.cancel_button.clicked.connect(self.cancel_requested.emit)
        self.retry_button = QPushButton("  Retry failed")
        self.retry_button.setIcon(icon("refresh", palette.text, 15))
        self.retry_button.clicked.connect(self.retry_failed_requested.emit)
        self.clear_button = QPushButton("Clear completed")
        self.clear_button.clicked.connect(self._clear_completed)
        self.open_button = QPushButton("  Open folder")
        self.open_button.setIcon(icon("external", palette.text, 15))
        self.open_button.clicked.connect(self.open_folder_requested.emit)
        for button in (self.pause_button, self.resume_button, self.cancel_button,
                       self.retry_button, self.clear_button, self.open_button):
            controls.addWidget(button)
        controls.addStretch(1)
        overall.add_layout(controls)
        root.addWidget(overall)

        # -- active transfers ---------------------------------------------
        self.active_card = Card(padding=18, spacing=12)
        active_title = QLabel("Currently downloading")
        active_title.setObjectName("SectionTitle")
        self.active_card.add(active_title)
        self.active_card.add(Divider())
        self.panels: list[TransferPanel] = []
        for _ in range(self._max_panels):
            panel = TransferPanel()
            panel.apply(palette)
            panel.setVisible(False)
            self.panels.append(panel)
            self.active_card.add(panel)
        self.idle_label = QLabel("No transfers in progress.")
        self.idle_label.setObjectName("Faint")
        self.active_card.add(self.idle_label)
        root.addWidget(self.active_card)

        # -- queue ---------------------------------------------------------
        queue_card = Card(padding=18, spacing=10)
        queue_top = QHBoxLayout()
        queue_title = QLabel("Download queue")
        queue_title.setObjectName("SectionTitle")
        queue_top.addWidget(queue_title)
        queue_top.addStretch(1)
        self.filter_box = QComboBox()
        for label, _ in _FILTERS:
            self.filter_box.addItem(label)
        self.filter_box.setFixedWidth(150)
        self.filter_box.currentIndexChanged.connect(self._apply_filter)
        queue_top.addWidget(self.filter_box)
        queue_card.add_layout(queue_top)
        self.queue = DownloadQueueView()
        self.queue.setMinimumHeight(260)
        self.queue.apply(palette)
        queue_card.add(self.queue, 1)
        root.addWidget(queue_card, 1)

        self.set_state(AppState.READY)

    # -- updates -----------------------------------------------------------
    def reset(self) -> None:
        """Clear the page for a new run."""
        self.queue.reset_queue()
        self.banner.hide_message()
        self.overall_panel.clear()
        self.overall_panel.title.setText("Starting...")
        self.ring.set_fraction(0, "of 0 B")
        for panel in self.panels:
            panel.setVisible(False)
        self.idle_label.setVisible(True)
        self.failed_stat.set_value("0")

    def set_progress(self, progress: BackupProgress) -> None:
        """Render one progress snapshot."""
        fraction = progress.byte_fraction
        self.ring.set_fraction(fraction, f"of {format_bytes(progress.total_bytes)}")
        self.overall_panel.update_transfer(
            f"{format_count(progress.finished_files)} of "
            f"{format_count(progress.total_files)} files processed",
            progress.done_bytes, progress.total_bytes,
            progress.speed, progress.eta_seconds,
        )
        self.files_stat.set_value(
            f"{format_count(progress.finished_files)} / {format_count(progress.total_files)}",
            f"{progress.completed_files:,} downloaded · {progress.skipped_files:,} skipped",
        )
        self.speed_stat.set_value(format_speed(progress.speed))
        self.eta_stat.set_value(
            format_duration(progress.eta_seconds) if progress.eta_seconds else "-"
        )
        self.failed_stat.set_value(
            format_count(progress.failed_files),
            f"{progress.unsupported_files:,} unsupported"
            if progress.unsupported_files else "",
        )

        active = progress.active[: self._max_panels]
        for index, panel in enumerate(self.panels):
            if index < len(active):
                transfer = active[index]
                panel.setVisible(True)
                panel.update_transfer(
                    transfer.name, transfer.bytes_done, transfer.bytes_total,
                    transfer.speed,
                    indeterminate=transfer.bytes_total <= 0,
                )
            else:
                panel.setVisible(False)
        self.idle_label.setVisible(not active)
        self.queue.update_active(progress.active)

    def add_result(self, result: TaskResult) -> None:
        """Move one finished item into the queue view."""
        self.queue.mark_finished(result)

    def set_destination(self, destination: str) -> None:
        """Show where the files are going."""
        self.header.set_subtitle(f"Saving to {destination}" if destination else "Not started")

    def set_outcome(self, outcome: BackupOutcome, destination: str) -> None:
        """Show the completion summary."""
        if outcome.fatal_error:
            self.banner.show_message(f"Backup stopped: {outcome.fatal_error}", "danger")
        elif outcome.cancelled:
            self.banner.show_message(
                "Backup cancelled. Everything downloaded so far has been kept and the "
                "run can be resumed.", "warning",
            )
        elif outcome.paused:
            self.banner.show_message("Backup paused. Resume to continue.", "warning")
        else:
            summary = (
                f"Backup complete - {outcome.completed:,} downloaded, "
                f"{outcome.skipped:,} skipped, {outcome.failed:,} failed, "
                f"{outcome.unsupported:,} unsupported. "
                f"{format_bytes(outcome.bytes_downloaded)} transferred in "
                f"{format_duration(outcome.duration)}."
            )
            tone = "warning" if (outcome.failed or outcome.unsupported) else "success"
            self.banner.show_message(summary, tone)
        self.header.set_subtitle(destination)

    def set_state(self, state: AppState) -> None:
        """Enable only the controls that make sense right now."""
        self.status_pill.set_status(state.label, {
            AppState.BACKUP_RUNNING: "info",
            AppState.BACKUP_PAUSED: "warning",
            AppState.BACKUP_CANCELLING: "warning",
            AppState.BACKUP_COMPLETED: "success",
            AppState.BACKUP_COMPLETED_WITH_ERRORS: "warning",
            AppState.BACKUP_FAILED: "danger",
        }.get(state, "neutral"))

        running = state is AppState.BACKUP_RUNNING
        paused = state is AppState.BACKUP_PAUSED
        cancelling = state is AppState.BACKUP_CANCELLING
        finished = state in (AppState.BACKUP_COMPLETED,
                             AppState.BACKUP_COMPLETED_WITH_ERRORS,
                             AppState.BACKUP_FAILED, AppState.READY)

        self.pause_button.setEnabled(running)
        self.resume_button.setEnabled(paused or (finished and state is not AppState.READY))
        self.resume_button.setText("  Run again" if finished and not paused else "  Resume")
        self.cancel_button.setEnabled(running or paused)
        self.retry_button.setEnabled(finished and not cancelling)
        self.clear_button.setEnabled(not cancelling)
        self.open_button.setEnabled(True)

    # -- helpers -----------------------------------------------------------
    def _apply_filter(self, index: int) -> None:
        self.queue.set_filter(_FILTERS[index][1])

    def _clear_completed(self) -> None:
        self.queue.clear_completed()
