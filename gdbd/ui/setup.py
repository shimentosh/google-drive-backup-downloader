"""Backup setup screen: source, destination, options and pre-flight checks."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ..config.settings import EXISTING_ASK, EXISTING_REPLACE, EXISTING_SKIP, Settings
from ..constants import MAX_WORKERS
from ..services.backup_service import BackupPlan
from ..services.event_bus import AppState
from ..utils.formatting import format_bytes, format_count
from .icons import icon
from .theme import Palette
from .widgets.common import Banner, Card, Divider, KeyValue, PageHeader


class BackupSetupPage(QWidget):
    """Everything the user reviews before the first byte is downloaded."""

    start_requested = Signal()
    destination_requested = Signal()
    rescan_requested = Signal()
    settings_changed = Signal()

    def __init__(self, settings: Settings, palette: Palette,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self._palette = palette
        self._loading = False

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

        header = PageHeader(
            "Full Drive Backup",
            "Everything in your Google Drive, copied to a local folder",
        )
        root.addWidget(header)

        self.banner = Banner()
        self.banner.apply(palette)
        root.addWidget(self.banner)

        # -- source / destination -------------------------------------------
        pair = QHBoxLayout()
        pair.setSpacing(14)

        source = Card(padding=18, spacing=8)
        source_title = QLabel("Source")
        source_title.setObjectName("CardLabel")
        source.add(source_title)
        source_row = QHBoxLayout()
        source_row.setSpacing(10)
        source_icon = QLabel()
        source_icon.setPixmap(icon("drive", palette.accent, 26).pixmap(26, 26))
        source_row.addWidget(source_icon)
        source_text = QVBoxLayout()
        source_text.setSpacing(1)
        source_name = QLabel("Google Drive")
        source_name.setObjectName("CardValueSmall")
        source_scope = QLabel("Entire Drive - every folder and file")
        source_scope.setObjectName("Faint")
        source_text.addWidget(source_name)
        source_text.addWidget(source_scope)
        source_row.addLayout(source_text)
        source_row.addStretch(1)
        source.add_layout(source_row)
        source.add(Divider())
        self.source_files = KeyValue("Files", "-")
        self.source_folders = KeyValue("Folders", "-")
        self.source_size = KeyValue("Estimated size", "-")
        source.add(self.source_files)
        source.add(self.source_folders)
        source.add(self.source_size)
        pair.addWidget(source, 1)

        destination = Card(padding=18, spacing=8)
        dest_title = QLabel("Destination")
        dest_title.setObjectName("CardLabel")
        destination.add(dest_title)
        dest_row = QHBoxLayout()
        dest_row.setSpacing(10)
        dest_icon = QLabel()
        dest_icon.setPixmap(icon("folder", palette.accent, 26).pixmap(26, 26))
        dest_row.addWidget(dest_icon)
        self.dest_label = QLabel("No folder selected")
        self.dest_label.setObjectName("Mono")
        self.dest_label.setWordWrap(True)
        dest_row.addWidget(self.dest_label, 1)
        change = QPushButton("Change")
        change.clicked.connect(self.destination_requested.emit)
        dest_row.addWidget(change)
        destination.add_layout(dest_row)
        destination.add(Divider())
        self.dest_free = KeyValue("Available space", "-")
        self.dest_needed = KeyValue("Still to download", "-")
        self.dest_existing = KeyValue("Already backed up", "-")
        destination.add(self.dest_free)
        destination.add(self.dest_needed)
        destination.add(self.dest_existing)
        pair.addWidget(destination, 1)
        root.addLayout(pair)

        # -- options ---------------------------------------------------------
        options = Card(padding=18, spacing=10)
        options_title = QLabel("Options")
        options_title.setObjectName("SectionTitle")
        options.add(options_title)
        options.add(Divider())

        grid = QGridLayout()
        grid.setHorizontalSpacing(30)
        grid.setVerticalSpacing(4)

        self.opt_structure = QCheckBox("Preserve folder structure")
        self.opt_structure.setChecked(True)
        self.opt_structure.setEnabled(False)
        self.opt_structure.setToolTip(
            "The Drive hierarchy is always mirrored locally."
        )
        self.opt_skip = QCheckBox("Skip unchanged files")
        self.opt_verify = QCheckBox("Verify downloaded files")
        self.opt_retry = QCheckBox("Retry failed downloads")
        self.opt_export = QCheckBox("Export Google Workspace files")
        self.opt_shared_me = QCheckBox("Include files shared with me")
        self.opt_shared_drives = QCheckBox("Include shared drives")
        self.opt_owned_only = QCheckBox("Only files I own")
        self.opt_owned_only.setToolTip(
            "Skip files other people own, even when they sit inside your folders.\n"
            "These are the files that count toward your own storage quota."
        )

        for widget in (self.opt_skip, self.opt_verify, self.opt_retry,
                       self.opt_export, self.opt_shared_me, self.opt_shared_drives,
                       self.opt_owned_only):
            widget.toggled.connect(self._on_option_changed)

        grid.addWidget(self.opt_structure, 0, 0)
        grid.addWidget(self.opt_skip, 1, 0)
        grid.addWidget(self.opt_verify, 2, 0)
        grid.addWidget(self.opt_retry, 0, 1)
        grid.addWidget(self.opt_export, 1, 1)
        grid.addWidget(self.opt_shared_me, 2, 1)
        grid.addWidget(self.opt_shared_drives, 3, 1)
        grid.addWidget(self.opt_owned_only, 3, 0)
        options.add_layout(grid)

        options.add(Divider())
        workers_row = QHBoxLayout()
        workers_row.setSpacing(14)
        workers_label = QLabel("Concurrent downloads")
        workers_row.addWidget(workers_label)
        self.workers_slider = QSlider(Qt.Orientation.Horizontal)
        self.workers_slider.setRange(1, MAX_WORKERS)
        self.workers_slider.setFixedWidth(240)
        self.workers_slider.valueChanged.connect(self._on_workers_changed)
        workers_row.addWidget(self.workers_slider)
        self.workers_value = QLabel("4")
        self.workers_value.setObjectName("CardValueSmall")
        self.workers_value.setFixedWidth(28)
        workers_row.addWidget(self.workers_value)
        workers_row.addStretch(1)
        options.add_layout(workers_row)

        note = QLabel(
            "Changing shared-drive, shared-with-me or 'Only files I own' rescans "
            "your Drive automatically."
        )
        note.setObjectName("Faint")
        options.add(note)
        root.addWidget(options)

        # -- action bar ------------------------------------------------------
        action = Card(padding=18, spacing=12)
        action_row = QHBoxLayout()
        action_row.setSpacing(12)
        self.summary_label = QLabel("Scan your Drive to see what will be backed up.")
        self.summary_label.setObjectName("Muted")
        self.summary_label.setWordWrap(True)
        action_row.addWidget(self.summary_label, 1)
        self.rescan_button = QPushButton("  Rescan Drive")
        self.rescan_button.setIcon(icon("refresh", palette.text, 16))
        self.rescan_button.clicked.connect(self.rescan_requested.emit)
        action_row.addWidget(self.rescan_button)
        self.start_button = QPushButton("  Start Backup")
        self.start_button.setObjectName("PrimaryLarge")
        self.start_button.setIcon(icon("backup", palette.on_accent, 18))
        self.start_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.start_button.clicked.connect(self.start_requested.emit)
        action_row.addWidget(self.start_button)
        action.add_layout(action_row)
        root.addWidget(action)
        root.addStretch(1)

        self.load_settings()

    # -- settings binding ---------------------------------------------------
    def load_settings(self) -> None:
        """Copy the persisted settings into the controls."""
        self._loading = True
        self.opt_skip.setChecked(self.settings.existing_files == EXISTING_SKIP)
        self.opt_verify.setChecked(self.settings.verify_downloads)
        self.opt_retry.setChecked(self.settings.retry_failed_automatically)
        self.opt_export.setChecked(self.settings.export_workspace)
        self.opt_shared_me.setChecked(self.settings.include_shared_with_me)
        self.opt_shared_drives.setChecked(self.settings.include_shared_drives)
        self.opt_owned_only.setChecked(self.settings.owned_only)
        self.workers_slider.setValue(self.settings.workers)
        self.workers_value.setText(str(self.settings.workers))
        self._loading = False

    def _on_option_changed(self) -> None:
        if self._loading:
            return
        existing = self.settings.existing_files
        if self.opt_skip.isChecked():
            existing = EXISTING_SKIP
        elif existing == EXISTING_SKIP:
            existing = EXISTING_REPLACE
        scope_before = self._scope()
        self.settings.update(
            existing_files=existing,
            verify_downloads=self.opt_verify.isChecked(),
            retry_failed_automatically=self.opt_retry.isChecked(),
            export_workspace=self.opt_export.isChecked(),
            include_shared_with_me=self.opt_shared_me.isChecked(),
            include_shared_drives=self.opt_shared_drives.isChecked(),
            owned_only=self.opt_owned_only.isChecked(),
        )
        self.settings_changed.emit()
        # Scope options only apply to a scan, so run one right away.
        if self._scope() != scope_before:
            self.rescan_requested.emit()

    def _scope(self) -> tuple[bool, bool, bool]:
        s = self.settings
        return (s.include_shared_with_me, s.include_shared_drives, s.owned_only)

    def _on_workers_changed(self, value: int) -> None:
        self.workers_value.setText(str(value))
        if not self._loading:
            self.settings.update(workers=value)
            self.settings_changed.emit()

    # -- plan display -------------------------------------------------------
    def set_plan(self, plan: BackupPlan | None) -> None:
        """Render the pre-flight report."""
        if plan is None:
            self.start_button.setEnabled(False)
            self.summary_label.setText("Scan your Drive to see what will be backed up.")
            return

        self.dest_label.setText(plan.destination or "No folder selected")
        self.source_files.set_value(format_count(plan.total_files))
        self.source_folders.set_value(format_count(plan.total_folders))
        self.source_size.set_value(format_bytes(plan.total_bytes))
        self.dest_free.set_value(format_bytes(plan.free_space))
        self.dest_needed.set_value(
            f"{format_bytes(plan.pending_bytes)}   ({format_count(plan.pending_files)} files)"
        )
        self.dest_existing.set_value(
            format_bytes(plan.already_done_bytes) if plan.has_previous_backup else "-"
        )

        messages = []
        tone = "info"
        if plan.problems:
            messages = plan.problems
            tone = "danger"
        elif plan.warnings:
            messages = plan.warnings
            tone = "warning"
        if messages:
            self.banner.show_message("\n".join(messages), tone)
        else:
            self.banner.hide_message()

        self.start_button.setEnabled(plan.can_start and plan.pending_files >= 0)
        if plan.has_previous_backup:
            self.summary_label.setText(
                f"This destination already holds {format_bytes(plan.already_done_bytes)} "
                f"of backup. {format_count(plan.pending_files)} file(s) remain."
            )
            self.start_button.setText("  Continue Backup")
        else:
            self.summary_label.setText(
                f"{format_count(plan.pending_files)} file(s), "
                f"{format_bytes(plan.pending_bytes)} to download."
            )
            self.start_button.setText("  Back Up Entire Drive")

    def set_state(self, state: AppState) -> None:
        """Disable controls that are invalid for the current state."""
        idle = not state.is_backup_active and state is not AppState.SCANNING
        self.start_button.setEnabled(idle and self.start_button.isEnabled() and
                                     state in (AppState.READY, AppState.BACKUP_COMPLETED,
                                               AppState.BACKUP_COMPLETED_WITH_ERRORS,
                                               AppState.BACKUP_FAILED))
        self.rescan_button.setEnabled(idle)
        for widget in (self.opt_skip, self.opt_verify, self.opt_retry, self.opt_export,
                       self.opt_shared_me, self.opt_shared_drives, self.opt_owned_only,
                       self.workers_slider):
            widget.setEnabled(idle)
