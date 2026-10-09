"""Settings: backup behaviour, Workspace exports, account, appearance, advanced."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..config.settings import (
    EXISTING_ASK,
    EXISTING_REPLACE,
    EXISTING_SKIP,
    EXPORT_CHOICES,
    AppPaths,
    Settings,
)
from ..constants import (
    APP_NAME,
    APP_VERSION,
    AUTHOR_NAME,
    AUTHOR_URL,
    ISSUES_URL,
    MAX_WORKERS,
    RELEASES_URL,
    REPO_URL,
    WEBSITE_URL,
)
from ..database.models import Account
from ..services.event_bus import AppState
from ..filesystem.storage import open_in_file_manager
from ..utils.formatting import format_bytes
from .theme import Palette
from .widgets.common import Card, Divider, PageHeader

_EXISTING_LABELS = [
    ("Skip unchanged files (recommended)", EXISTING_SKIP),
    ("Replace changed files", EXISTING_REPLACE),
    ("Ask for each conflict", EXISTING_ASK),
]


class SettingsPage(QWidget):
    """Every persisted preference, grouped the way the user thinks about them."""

    reconnect_requested = Signal()
    disconnect_requested = Signal()
    destination_requested = Signal()
    restart_requested = Signal()
    theme_changed = Signal(str)
    settings_changed = Signal()

    def __init__(self, settings: Settings, paths: AppPaths, palette: Palette,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.paths = paths
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
        root.addWidget(PageHeader("Settings", "Preferences are saved as you change them"))

        root.addWidget(self._backup_card())
        root.addWidget(self._workspace_card())
        root.addWidget(self._account_card())
        root.addWidget(self._behaviour_card())
        root.addWidget(self._appearance_card())
        root.addWidget(self._advanced_card())
        root.addWidget(self._about_card())
        root.addStretch(1)

        self.load()

    # -- sections ----------------------------------------------------------
    def _backup_card(self) -> Card:
        card = Card(padding=18, spacing=10)
        title = QLabel("Backup")
        title.setObjectName("SectionTitle")
        card.add(title)
        card.add(Divider())

        dest_row = QHBoxLayout()
        dest_row.setSpacing(10)
        dest_row.addWidget(QLabel("Default destination"))
        self.dest_edit = QLineEdit()
        self.dest_edit.setReadOnly(True)
        dest_row.addWidget(self.dest_edit, 1)
        self.browse_button = QPushButton("Browse...")
        self.browse_button.clicked.connect(self.destination_requested.emit)
        dest_row.addWidget(self.browse_button)
        card.add_layout(dest_row)

        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(9)

        grid.addWidget(QLabel("Concurrent downloads"), 0, 0)
        self.workers_spin = QSpinBox()
        self.workers_spin.setRange(1, MAX_WORKERS)
        self.workers_spin.valueChanged.connect(self._save)
        grid.addWidget(self.workers_spin, 0, 1)

        grid.addWidget(QLabel("Retry attempts per file"), 1, 0)
        self.retry_spin = QSpinBox()
        self.retry_spin.setRange(0, 20)
        self.retry_spin.valueChanged.connect(self._save)
        grid.addWidget(self.retry_spin, 1, 1)

        grid.addWidget(QLabel("Chunk size"), 2, 0)
        self.chunk_box = QComboBox()
        for label, value in (("256 KB", 262144), ("1 MB", 1048576), ("4 MB", 4194304),
                             ("8 MB (default)", 8388608), ("16 MB", 16777216),
                             ("32 MB", 33554432), ("64 MB", 67108864)):
            self.chunk_box.addItem(label, value)
        self.chunk_box.currentIndexChanged.connect(self._save)
        grid.addWidget(self.chunk_box, 2, 1)

        grid.addWidget(QLabel("Existing files"), 3, 0)
        self.existing_box = QComboBox()
        for label, value in _EXISTING_LABELS:
            self.existing_box.addItem(label, value)
        self.existing_box.currentIndexChanged.connect(self._save)
        grid.addWidget(self.existing_box, 3, 1)
        grid.setColumnStretch(2, 1)
        card.add_layout(grid)

        self.verify_check = QCheckBox("Verify downloads (size, and checksum when Drive provides one)")
        self.verify_check.toggled.connect(self._save)
        card.add(self.verify_check)
        self.auto_retry_check = QCheckBox("Automatically re-queue previously failed files")
        self.auto_retry_check.toggled.connect(self._save)
        card.add(self.auto_retry_check)
        self.trashed_check = QCheckBox("Include items in the Drive trash")
        self.trashed_check.toggled.connect(self._save)
        card.add(self.trashed_check)
        return card

    def _workspace_card(self) -> Card:
        card = Card(padding=18, spacing=10)
        title = QLabel("Google Workspace export")
        title.setObjectName("SectionTitle")
        card.add(title)
        card.add(Divider())
        note = QLabel(
            "Docs, Sheets and Slides have no stored file to download, so they are "
            "converted on export. Available formats come from the Drive API itself."
        )
        note.setObjectName("Faint")
        note.setWordWrap(True)
        card.add(note)

        self.export_check = QCheckBox("Export Google Workspace files")
        self.export_check.toggled.connect(self._save)
        card.add(self.export_check)

        grid = QGridLayout()
        grid.setHorizontalSpacing(24)
        grid.setVerticalSpacing(9)
        self.export_boxes: dict[str, QComboBox] = {}
        labels = {
            "document": "Documents", "spreadsheet": "Spreadsheets",
            "presentation": "Presentations", "drawing": "Drawings",
        }
        for row, (kind, label) in enumerate(labels.items()):
            grid.addWidget(QLabel(label), row, 0)
            box = QComboBox()
            for name, mime in EXPORT_CHOICES[kind]:
                box.addItem(name, mime)
            box.currentIndexChanged.connect(self._save)
            self.export_boxes[kind] = box
            grid.addWidget(box, row, 1)
        grid.setColumnStretch(2, 1)
        card.add_layout(grid)
        return card

    def _account_card(self) -> Card:
        card = Card(padding=18, spacing=10)
        title = QLabel("Google Drive account")
        title.setObjectName("SectionTitle")
        card.add(title)
        card.add(Divider())
        self.account_label = QLabel("Not connected")
        self.account_label.setObjectName("Muted")
        card.add(self.account_label)
        self.storage_label = QLabel("")
        self.storage_label.setObjectName("Faint")
        card.add(self.storage_label)

        row = QHBoxLayout()
        row.setSpacing(9)
        self.reconnect_button = QPushButton("Reconnect")
        self.reconnect_button.clicked.connect(self.reconnect_requested.emit)
        self.disconnect_button = QPushButton("Disconnect")
        self.disconnect_button.setObjectName("Danger")
        self.disconnect_button.clicked.connect(self.disconnect_requested.emit)
        row.addWidget(self.reconnect_button)
        row.addWidget(self.disconnect_button)
        row.addStretch(1)
        card.add_layout(row)

        privacy = QLabel(
            "Access is read-only. Tokens are stored in the Windows Credential Manager "
            "when available, are never written to logs, and never leave this PC."
        )
        privacy.setObjectName("Faint")
        privacy.setWordWrap(True)
        card.add(privacy)
        return card

    def _behaviour_card(self) -> Card:
        card = Card(padding=18, spacing=10)
        title = QLabel("While a backup runs")
        title.setObjectName("SectionTitle")
        card.add(title)
        card.add(Divider())
        self.sleep_check = QCheckBox("Keep this PC awake until the backup finishes")
        self.sleep_check.setToolTip(
            "Stops Windows from going to sleep mid-backup. The screen can still turn off."
        )
        self.sleep_check.toggled.connect(self._save)
        card.add(self.sleep_check)
        self.notify_check = QCheckBox("Show a desktop notification when a backup finishes")
        self.notify_check.toggled.connect(self._save)
        card.add(self.notify_check)
        return card

    def _appearance_card(self) -> Card:
        card = Card(padding=18, spacing=10)
        title = QLabel("Appearance")
        title.setObjectName("SectionTitle")
        card.add(title)
        card.add(Divider())
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(QLabel("Theme"))
        self.theme_box = QComboBox()
        for label, value in (("System", "system"), ("Dark", "dark"), ("Light", "light")):
            self.theme_box.addItem(label, value)
        self.theme_box.setFixedWidth(180)
        self.theme_box.currentIndexChanged.connect(self._on_theme)
        row.addWidget(self.theme_box)
        self.restart_button = QPushButton("Restart to apply")
        self.restart_button.setObjectName("Primary")
        self.restart_button.setVisible(False)
        self.restart_button.clicked.connect(self.restart_requested.emit)
        row.addWidget(self.restart_button)
        row.addStretch(1)
        card.add_layout(row)
        hint = QLabel("Theme changes apply after the application is restarted.")
        hint.setObjectName("Faint")
        card.add(hint)
        return card

    def _advanced_card(self) -> Card:
        card = Card(padding=18, spacing=10)
        title = QLabel("Advanced")
        title.setObjectName("SectionTitle")
        card.add(title)
        card.add(Divider())

        self.debug_check = QCheckBox("Enable debug logging")
        self.debug_check.toggled.connect(self._save)
        card.add(self.debug_check)

        creds_row = QHBoxLayout()
        creds_row.setSpacing(10)
        creds_row.addWidget(QLabel("OAuth client file"))
        self.creds_edit = QLineEdit()
        self.creds_edit.setPlaceholderText("Auto-detected (credentials.json)")
        self.creds_edit.setReadOnly(True)
        creds_row.addWidget(self.creds_edit, 1)
        pick = QPushButton("Choose...")
        pick.clicked.connect(self._choose_credentials)
        clear = QPushButton("Clear")
        clear.clicked.connect(self._clear_credentials)
        creds_row.addWidget(pick)
        creds_row.addWidget(clear)
        card.add_layout(creds_row)

        card.add(Divider())
        for label, path in (("Database", self.paths.database),
                            ("Logs", self.paths.logs),
                            ("Settings", self.paths.settings_file)):
            row = QHBoxLayout()
            row.setSpacing(10)
            key = QLabel(label)
            key.setObjectName("Muted")
            key.setFixedWidth(80)
            value = QLabel(str(path))
            value.setObjectName("Mono")
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            open_button = QPushButton("Open")
            open_button.setObjectName("Ghost")
            target = path if path.is_dir() else path.parent
            open_button.clicked.connect(
                lambda _checked=False, t=target: open_in_file_manager(t)
            )
            row.addWidget(key)
            row.addWidget(value, 1)
            row.addWidget(open_button)
            card.add_layout(row)
        return card

    def _about_card(self) -> Card:
        card = Card(padding=18, spacing=10)
        title = QLabel("About")
        title.setObjectName("SectionTitle")
        card.add(title)
        card.add(Divider())
        name = QLabel(f"{APP_NAME}  ·  version {APP_VERSION}")
        name.setObjectName("Muted")
        card.add(name)
        credit = QLabel(
            f'Free and open source. Made by <a href="{AUTHOR_URL}">{AUTHOR_NAME}</a>.'
        )
        credit.setObjectName("Faint")
        credit.setOpenExternalLinks(True)
        card.add(credit)
        row = QHBoxLayout()
        row.setSpacing(9)
        for label, url in (("Check for updates", RELEASES_URL),
                           ("Website", WEBSITE_URL),
                           ("Source code", REPO_URL),
                           ("Report a problem", ISSUES_URL)):
            button = QPushButton(label)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(
                lambda _checked=False, u=url: QDesktopServices.openUrl(QUrl(u))
            )
            row.addWidget(button)
        row.addStretch(1)
        card.add_layout(row)
        return card

    # -- binding -----------------------------------------------------------
    def load(self) -> None:
        """Populate every control from the settings object."""
        self._loading = True
        s = self.settings
        self.dest_edit.setText(s.destination)
        self.workers_spin.setValue(s.workers)
        self.retry_spin.setValue(s.retry_count)
        index = self.chunk_box.findData(s.chunk_size)
        self.chunk_box.setCurrentIndex(index if index >= 0 else 3)
        index = self.existing_box.findData(s.existing_files)
        self.existing_box.setCurrentIndex(max(0, index))
        self.verify_check.setChecked(s.verify_downloads)
        self.auto_retry_check.setChecked(s.retry_failed_automatically)
        self.trashed_check.setChecked(s.include_trashed)
        self.export_check.setChecked(s.export_workspace)
        for kind, box in self.export_boxes.items():
            mime = getattr(s, f"export_{kind}")
            index = box.findData(mime)
            box.setCurrentIndex(max(0, index))
        self.sleep_check.setChecked(s.prevent_sleep)
        self.notify_check.setChecked(s.notify_on_finish)
        index = self.theme_box.findData(s.theme)
        self.theme_box.setCurrentIndex(max(0, index))
        self.debug_check.setChecked(s.debug_logging)
        self.creds_edit.setText(s.credentials_path)
        self._loading = False

    def _save(self) -> None:
        if self._loading:
            return
        self.settings.update(
            workers=self.workers_spin.value(),
            retry_count=self.retry_spin.value(),
            chunk_size=self.chunk_box.currentData(),
            existing_files=self.existing_box.currentData(),
            verify_downloads=self.verify_check.isChecked(),
            retry_failed_automatically=self.auto_retry_check.isChecked(),
            include_trashed=self.trashed_check.isChecked(),
            export_workspace=self.export_check.isChecked(),
            export_document=self.export_boxes["document"].currentData(),
            export_spreadsheet=self.export_boxes["spreadsheet"].currentData(),
            export_presentation=self.export_boxes["presentation"].currentData(),
            export_drawing=self.export_boxes["drawing"].currentData(),
            debug_logging=self.debug_check.isChecked(),
            prevent_sleep=self.sleep_check.isChecked(),
            notify_on_finish=self.notify_check.isChecked(),
        )
        self.settings_changed.emit()

    def _on_theme(self) -> None:
        if self._loading:
            return
        value = self.theme_box.currentData()
        self.settings.update(theme=value)
        self.restart_button.setVisible(True)
        self.theme_changed.emit(value)

    def _choose_credentials(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose OAuth client file", str(Path.home()), "JSON files (*.json)"
        )
        if path:
            self.settings.update(credentials_path=path)
            self.creds_edit.setText(path)
            self.settings_changed.emit()

    def _clear_credentials(self) -> None:
        self.settings.update(credentials_path="")
        self.creds_edit.clear()
        self.settings_changed.emit()

    # -- external updates --------------------------------------------------
    def set_state(self, state: AppState) -> None:
        """Lock account and destination changes while a backup is running."""
        busy = state.is_backup_active
        for widget in (self.browse_button, self.reconnect_button, self.disconnect_button):
            widget.setEnabled(not busy)
            widget.setToolTip("Not available while a backup is running" if busy else "")

    def set_account(self, account: Account | None) -> None:
        """Show which account is connected."""
        if account is None:
            self.account_label.setText("Not connected")
            self.storage_label.setText("")
            return
        self.account_label.setText(f"{account.display_name or 'Google account'}  ·  {account.email}")
        if account.storage_used is not None:
            text = f"{format_bytes(account.storage_used)} used"
            if account.storage_limit:
                text += f" of {format_bytes(account.storage_limit)}"
            self.storage_label.setText(text)

    def set_destination(self, path: str) -> None:
        """Reflect a destination chosen elsewhere in the app."""
        self.dest_edit.setText(path)
