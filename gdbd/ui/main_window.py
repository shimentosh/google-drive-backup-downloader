"""The application shell: sidebar navigation and signal wiring."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QProcess, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QStatusBar,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from ..auth.google_auth import CredentialsNotConfigured, client_config_from_pair
from ..config.settings import EXISTING_REPLACE, EXISTING_SKIP
from ..constants import APP_NAME, APP_SUBTITLE, SETUP_GUIDE_URL
from ..database.models import Account, DriveItemRecord
from ..downloader.manager import BackupOutcome, BackupProgress
from ..downloader.worker import TaskResult
from ..drive.scanner import ScanProgress, ScanResult
from ..filesystem.storage import disk_space, open_in_file_manager
from ..services.controller import AppController
from ..services.event_bus import AppState
from ..utils.logging import get_logger
from ..utils.formatting import format_bytes, format_duration
from ..utils.power import keep_awake
from .backup import BackupPage
from .dashboard import DashboardPage
from .drive_browser import DriveBrowserPage
from .history import HistoryPage
from .icons import icon
from .logs import LogsPage
from .settings_page import SettingsPage
from .setup import BackupSetupPage
from .theme import Palette
from .welcome import WelcomePage

log = get_logger(__name__)

_NAV = [
    ("Overview", "overview"),
    ("My Drive", "drive"),
    ("Backup", "backup"),
    ("History", "history"),
    ("Settings", "settings"),
    ("Logs", "logs"),
]

#: Page indices in :attr:`MainWindow.pages`.
PAGE_OVERVIEW, PAGE_DRIVE, PAGE_SETUP, PAGE_RUNNING = 0, 1, 2, 3
PAGE_HISTORY, PAGE_SETTINGS, PAGE_LOGS = 4, 5, 6

#: The "Backup" nav item owns two pages - setup before a run, progress during one.
_NAV_TO_PAGE = [PAGE_OVERVIEW, PAGE_DRIVE, PAGE_SETUP, PAGE_HISTORY,
                PAGE_SETTINGS, PAGE_LOGS]
_PAGE_TO_NAV = {PAGE_OVERVIEW: 0, PAGE_DRIVE: 1, PAGE_SETUP: 2, PAGE_RUNNING: 2,
                PAGE_HISTORY: 3, PAGE_SETTINGS: 4, PAGE_LOGS: 5}

_SETUP_HELP = f"""<b>Connecting {APP_NAME} to your Google account</b>
<ol>
<li>Open the <b>Google Cloud Console</b> and create (or select) a project.</li>
<li>Under <b>APIs &amp; Services &rarr; Library</b>, enable the <b>Google Drive API</b>.</li>
<li>Under <b>OAuth consent screen</b>, choose <b>External</b>, fill in the app name and
your e-mail, and add your own Google account under <b>Test users</b>.</li>
<li>Under <b>Credentials &rarr; Create credentials &rarr; OAuth client ID</b>,
choose application type <b>Desktop app</b>.</li>
<li>Download the JSON, then click <b>Choose downloaded JSON file</b> on the welcome
screen (or drag the file onto the window, or paste the Client ID and secret).</li>
</ol>
The app requests the read-only <code>drive.readonly</code> scope, never asks for your
password, and keeps every token on this PC.<br><br>
Stuck? <b>Open the illustrated guide</b> for screenshots of every step and fixes for
the common errors.
"""


class MainWindow(QMainWindow):
    """The one window the application shows."""

    def __init__(self, controller: AppController, palette: Palette) -> None:
        super().__init__()
        self.controller = controller
        self.palette_colors = palette
        self._base_title = f"{APP_NAME} - {APP_SUBTITLE}"
        self.setWindowTitle(self._base_title)
        self._tray: QSystemTrayIcon | None = None
        self.resize(1280, 840)
        self.setMinimumSize(1060, 700)

        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)

        self.shell = QStackedWidget()
        outer.addWidget(self.shell)

        # -- welcome ---------------------------------------------------------
        self.welcome = WelcomePage(palette)
        self.shell.addWidget(self.welcome)

        # -- main shell ------------------------------------------------------
        main = QWidget()
        main.setObjectName("Root")
        main_layout = QHBoxLayout(main)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        main_layout.addWidget(self._build_sidebar())

        self.pages = QStackedWidget()
        self.dashboard = DashboardPage(palette)
        self.browser = DriveBrowserPage(controller.repos, palette)
        self.setup_page = BackupSetupPage(controller.settings, palette)
        self.backup_page = BackupPage(palette)
        self.history = HistoryPage(controller.repos, palette)
        self.settings_page = SettingsPage(controller.settings, controller.context.paths, palette)
        self.logs = LogsPage(controller.context.paths.logs, palette)
        for page in (self.dashboard, self.browser, self.setup_page, self.backup_page,
                     self.history, self.settings_page, self.logs):
            self.pages.addWidget(page)
        main_layout.addWidget(self.pages, 1)
        self.shell.addWidget(main)

        self.status_bar = QStatusBar()
        self.status_bar.setSizeGripEnabled(False)
        self.setStatusBar(self.status_bar)

        self._wire()

        # Drives the indeterminate animations at a steady 30 fps.
        self._animation = QTimer(self)
        self._animation.setInterval(33)
        self._animation.timeout.connect(self._animate)
        self._animation.start()

        self._refresh_credentials()
        self._on_state(controller.state)
        self._refresh_static()

    # -- construction ------------------------------------------------------
    def _build_sidebar(self) -> QWidget:
        p = self.palette_colors
        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(232)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(14, 20, 14, 16)
        layout.setSpacing(4)

        brand_row = QHBoxLayout()
        brand_row.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(icon("shield", p.accent, 26).pixmap(26, 26))
        brand_row.addWidget(logo)
        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        name = QLabel("Drive Backup")
        name.setObjectName("BrandName")
        sub = QLabel("Downloader")
        sub.setObjectName("BrandSub")
        brand_text.addWidget(name)
        brand_text.addWidget(sub)
        brand_row.addLayout(brand_text)
        brand_row.addStretch(1)
        layout.addLayout(brand_row)
        layout.addSpacing(18)

        section = QLabel("NAVIGATION")
        section.setObjectName("NavSection")
        layout.addWidget(section)

        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons: list[QPushButton] = []
        for index, (label, glyph) in enumerate(_NAV):
            button = QPushButton(f"   {label}")
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setIcon(icon(glyph, p.text_muted, 17))
            button.clicked.connect(lambda _c=False, i=index: self._navigate(i))
            self.nav_group.addButton(button, index)
            self.nav_buttons.append(button)
            layout.addWidget(button)
        self.nav_buttons[0].setChecked(True)

        layout.addStretch(1)

        self.account_chip = QLabel("Not connected")
        self.account_chip.setObjectName("Faint")
        self.account_chip.setWordWrap(True)
        layout.addWidget(self.account_chip)
        self.disconnect_button = QPushButton("Disconnect")
        self.disconnect_button.setObjectName("Ghost")
        self.disconnect_button.clicked.connect(self._disconnect)
        layout.addWidget(self.disconnect_button)
        return sidebar

    def _wire(self) -> None:
        c = self.controller
        c.state_changed.connect(self._on_state)
        c.account_changed.connect(self._on_account)
        c.scan_progress.connect(self._on_scan_progress)
        c.scan_finished.connect(self._on_scan_finished)
        c.backup_progress.connect(self._on_backup_progress)
        c.backup_item.connect(self._on_backup_item)
        c.backup_finished.connect(self._on_backup_finished)
        c.destination_changed.connect(self._on_destination_changed)
        c.notify.connect(self._notify)
        c.error.connect(self._show_error)
        c.conflict_requested.connect(self._ask_conflict)

        self.welcome.connect_requested.connect(c.connect_account)
        self.welcome.continue_requested.connect(self._enter_main)
        self.welcome.setup_help_requested.connect(self._show_setup_help)
        self.welcome.credentials_file_chosen.connect(
            lambda path: self._install_credentials(Path(path))
        )
        self.welcome.credentials_entered.connect(self._install_credential_pair)

        self.dashboard.scan_requested.connect(self._start_scan)
        self.dashboard.cancel_scan_requested.connect(c.cancel_scan)
        self.dashboard.backup_requested.connect(lambda: self._show_page(PAGE_SETUP))
        self.dashboard.resume_requested.connect(self._start_backup)
        self.dashboard.destination_requested.connect(self._choose_destination)
        self.dashboard.open_folder_requested.connect(self._open_destination)

        self.setup_page.start_requested.connect(self._start_backup)
        self.setup_page.destination_requested.connect(self._choose_destination)
        self.setup_page.rescan_requested.connect(self._start_scan)
        self.setup_page.settings_changed.connect(self._refresh_plan)

        self.backup_page.pause_requested.connect(c.pause_backup)
        self.backup_page.resume_requested.connect(self._resume_backup)
        self.backup_page.cancel_requested.connect(self._confirm_cancel)
        self.backup_page.retry_failed_requested.connect(c.retry_failed)
        self.backup_page.open_folder_requested.connect(self._open_destination)

        self.settings_page.reconnect_requested.connect(c.connect_account)
        self.settings_page.disconnect_requested.connect(self._disconnect)
        self.settings_page.destination_requested.connect(self._choose_destination)
        self.settings_page.restart_requested.connect(self._restart)
        self.settings_page.settings_changed.connect(self._refresh_plan)
        self.settings_page.settings_changed.connect(self._refresh_credentials)
        self.settings_page.theme_changed.connect(
            lambda _t: self._notify("info", "Theme applies after restart - "
                                            "use 'Restart to apply' in Settings")
        )

    # -- navigation --------------------------------------------------------
    def _navigate(self, nav_index: int) -> None:
        """Handle a sidebar click."""
        page = _NAV_TO_PAGE[nav_index]
        if page == PAGE_SETUP and self.controller.state.is_backup_active:
            page = PAGE_RUNNING
        self._show_page(page)

    def _show_page(self, page: int) -> None:
        """Switch to a page and sync the sidebar selection."""
        self.pages.setCurrentIndex(page)
        self.nav_buttons[_PAGE_TO_NAV[page]].setChecked(True)
        if page == PAGE_DRIVE:
            account = self.controller.account
            self.browser.set_scope(account.id if account else None,
                                   self.controller.destination)
        elif page == PAGE_SETUP:
            self._refresh_plan()
        elif page == PAGE_HISTORY:
            self.history.refresh(self.controller.account.id
                                 if self.controller.account else None)

    def _enter_main(self) -> None:
        self.shell.setCurrentIndex(1)
        self._refresh_static()

    # -- controller events -------------------------------------------------
    def _on_state(self, state: AppState) -> None:
        self.welcome.set_state(state)
        self.dashboard.set_state(state)
        self.setup_page.set_state(state)
        self.backup_page.set_state(state)
        self.settings_page.set_state(state)
        self.disconnect_button.setEnabled(not state.is_backup_active)

        # A paused run may sit for hours, so only hold the PC awake while bytes move.
        keep_awake(self.controller.settings.prevent_sleep and state in (
            AppState.BACKUP_RUNNING, AppState.BACKUP_CANCELLING))
        if not state.is_backup_active:
            self.setWindowTitle(self._base_title)
        elif state is AppState.BACKUP_PAUSED:
            self.setWindowTitle(f"Paused - {self._base_title}")

        if state in (AppState.DISCONNECTED, AppState.AUTHENTICATING):
            self.shell.setCurrentIndex(0)
        elif self.shell.currentIndex() == 0 and state is not AppState.CONNECTED:
            self.shell.setCurrentIndex(1)

        if state is AppState.BACKUP_RUNNING and self.pages.currentIndex() != PAGE_RUNNING:
            self._show_page(PAGE_RUNNING)
        self.status_bar.showMessage(state.label)

    def _on_account(self, account: Account | None) -> None:
        self.dashboard.set_account(account)
        self.settings_page.set_account(account)
        self.account_chip.setText(account.email if account else "Not connected")
        if account is None:
            self.shell.setCurrentIndex(0)
            return
        self.welcome.show_account(account.email, account.storage_used,
                                  account.storage_limit)
        self._refresh_static()
        # A first-time user has no destination yet; ask before scanning.
        if not self.controller.destination:
            self._choose_destination(first_run=True)
        else:
            self._start_scan()

    def _on_scan_progress(self, progress: ScanProgress) -> None:
        self.dashboard.set_scan_progress(progress)
        self.status_bar.showMessage(
            f"Scanning - {progress.files:,} files, {progress.folders:,} folders"
        )

    def _on_scan_finished(self, result: ScanResult) -> None:
        self.dashboard.set_scan_result(result)
        self._refresh_static()
        self._refresh_plan()
        if self.shell.currentIndex() == 0:
            self.welcome.show_account(
                self.controller.account.email if self.controller.account else "",
                None, None, result.files, result.folders,
            )

    def _on_backup_progress(self, progress: BackupProgress) -> None:
        self.backup_page.set_progress(progress)
        # Visible on the taskbar button, so progress can be checked at a glance.
        if self.controller.state is AppState.BACKUP_RUNNING:
            self.setWindowTitle(f"{progress.byte_fraction * 100:.0f}% backed up - {APP_NAME}")
        self.status_bar.showMessage(
            f"{progress.byte_fraction * 100:.1f}%  ·  "
            f"{progress.finished_files:,}/{progress.total_files:,} files"
        )

    def _on_backup_item(self, result: TaskResult) -> None:
        self.backup_page.add_result(result)

    def _on_backup_finished(self, outcome: BackupOutcome) -> None:
        self.backup_page.set_outcome(outcome, self.controller.destination)
        self._refresh_static()
        self._refresh_plan()
        self.history.refresh(self.controller.account.id if self.controller.account else None)
        if outcome.cancelled or outcome.paused:
            return
        self._announce(outcome)
        if outcome.fatal_error:
            return
        self._show_completion(outcome)

    def _on_destination_changed(self, path: str) -> None:
        self.settings_page.set_destination(path)
        self._refresh_static()
        self._refresh_plan()

    # -- actions -----------------------------------------------------------
    def _start_scan(self) -> None:
        if not self.controller.destination:
            self._choose_destination(first_run=True)
            return
        self.controller.start_scan()

    def _start_backup(self) -> None:
        self.backup_page.reset()
        self.backup_page.set_destination(self.controller.destination)
        self._show_page(PAGE_RUNNING)
        self.controller.start_backup()

    def _resume_backup(self) -> None:
        if self.controller.state is AppState.BACKUP_PAUSED and self.controller.has_active_run:
            self.controller.resume_backup()
        else:
            self._start_backup()

    def _confirm_cancel(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("Cancel backup?")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText("Cancel the backup?")
        box.setInformativeText(
            "Your progress will be saved. Files already downloaded are kept and you "
            "can resume this backup later."
        )
        keep = box.addButton("Continue Backup", QMessageBox.ButtonRole.RejectRole)
        box.addButton("Cancel Backup", QMessageBox.ButtonRole.AcceptRole)
        box.setDefaultButton(keep)
        box.exec()
        if box.clickedButton() is not keep:
            self.controller.cancel_backup()

    def _choose_destination(self, first_run: bool = False) -> None:
        if self.controller.state.is_backup_active:
            self._notify("warning", "Finish or cancel the running backup before "
                                    "changing the destination")
            return
        start = self.controller.destination or str(Path.home())
        folder = QFileDialog.getExistingDirectory(
            self, "Choose the backup destination folder", start,
            QFileDialog.Option.ShowDirsOnly,
        )
        if not folder:
            if first_run:
                self._notify("warning", "Choose a destination folder to continue")
            return
        self.controller.set_destination(folder)
        if first_run:
            self._start_scan()

    def _open_destination(self) -> None:
        path = self.controller.destination
        if not path or not open_in_file_manager(path):
            self._notify("warning", "The backup folder is not available")

    def _disconnect(self) -> None:
        answer = QMessageBox.question(
            self, "Disconnect account",
            "Disconnect this Google account?\n\n"
            "Files already downloaded are never deleted. You can reconnect at any time.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer is QMessageBox.StandardButton.Yes:
            self.controller.disconnect_account()

    # -- dialogs -----------------------------------------------------------
    def _ask_conflict(self, item: DriveItemRecord) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("File already exists")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(item.name)
        box.setInformativeText(
            f"A file already exists at:\n{item.local_path}\n\nWhat should happen?"
        )
        skip = box.addButton("Keep existing", QMessageBox.ButtonRole.RejectRole)
        box.addButton("Replace", QMessageBox.ButtonRole.AcceptRole)
        box.exec()
        self.controller.answer_conflict(
            EXISTING_SKIP if box.clickedButton() is skip else EXISTING_REPLACE
        )

    def _show_completion(self, outcome: BackupOutcome) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("Backup complete")
        box.setIcon(
            QMessageBox.Icon.Warning if (outcome.failed or outcome.unsupported)
            else QMessageBox.Icon.Information
        )
        box.setText("Backup complete")
        box.setInformativeText(
            f"{outcome.total:,} items processed\n"
            f"{outcome.completed:,} downloaded\n"
            f"{outcome.skipped:,} skipped\n"
            f"{outcome.failed:,} failed\n"
            f"{outcome.unsupported:,} unsupported"
        )
        open_button = box.addButton("Open Backup Folder", QMessageBox.ButtonRole.ActionRole)
        failed_button = None
        if outcome.failed:
            failed_button = box.addButton("View Failed Files",
                                          QMessageBox.ButtonRole.ActionRole)
        box.addButton("Close", QMessageBox.ButtonRole.AcceptRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is open_button:
            self._open_destination()
        elif failed_button is not None and clicked is failed_button:
            self._show_page(PAGE_DRIVE)
            self.browser.status_box.setCurrentIndex(
                max(0, self.browser.status_box.findText("Failed"))
            )

    def _announce(self, outcome: BackupOutcome) -> None:
        """Tell a user who is not looking at the window that the run ended."""
        if not self.controller.settings.notify_on_finish:
            return
        QApplication.alert(self)  # flashes the taskbar button
        if self.isActiveWindow() or not QSystemTrayIcon.isSystemTrayAvailable():
            return
        if self._tray is None:
            self._tray = QSystemTrayIcon(QApplication.windowIcon(), self)
            self._tray.setToolTip(APP_NAME)
            self._tray.messageClicked.connect(self._bring_to_front)
            self._tray.activated.connect(lambda _reason: self._bring_to_front())
        self._tray.show()
        if outcome.fatal_error:
            title, body = "Backup stopped", outcome.fatal_error
            kind = QSystemTrayIcon.MessageIcon.Critical
        else:
            title = ("Backup finished with problems" if outcome.failed
                     else "Backup complete")
            body = (f"{outcome.completed:,} downloaded, {outcome.skipped:,} unchanged"
                    + (f", {outcome.failed:,} failed" if outcome.failed else "")
                    + f" - {format_bytes(outcome.bytes_downloaded)} in "
                    f"{format_duration(outcome.duration)}")
            kind = (QSystemTrayIcon.MessageIcon.Warning if outcome.failed
                    else QSystemTrayIcon.MessageIcon.Information)
        self._tray.showMessage(title, body, kind, 15000)

    def _bring_to_front(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _restart(self) -> None:
        """Relaunch the app, e.g. so a new theme takes effect."""
        if self.controller.state.is_backup_active:
            self._notify("warning", "Restart after the backup finishes")
            return
        # A frozen .exe is its own interpreter; from source, re-run app.py.
        args = sys.argv[1:] if getattr(sys, "frozen", False) else sys.argv
        if self.close():
            QProcess.startDetached(sys.executable, args)

    def _install_credentials(self, source: Path | dict) -> None:
        """Save a user-supplied OAuth client and unlock the Connect button."""
        try:
            self.controller.install_credentials(source)
        except CredentialsNotConfigured as exc:
            self.welcome.show_setup_error(str(exc))
            return
        self._refresh_credentials()
        if self.controller.credentials_configured():
            self._notify("info", "Google OAuth client saved - you can connect now")

    def _install_credential_pair(self, client_id: str, client_secret: str) -> None:
        try:
            config = client_config_from_pair(client_id, client_secret)
        except CredentialsNotConfigured as exc:
            self.welcome.show_setup_error(str(exc))
            return
        self._install_credentials(config)

    def _refresh_credentials(self) -> None:
        self.welcome.set_credentials_missing(
            not self.controller.credentials_configured(), self.controller.client_id()
        )

    def _show_setup_help(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("Google Cloud setup")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(_SETUP_HELP)
        guide = box.addButton("Open illustrated guide", QMessageBox.ButtonRole.ActionRole)
        console = box.addButton("Open Google Cloud Console",
                                QMessageBox.ButtonRole.ActionRole)
        box.addButton("Close", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is guide:
            QDesktopServices.openUrl(QUrl(SETUP_GUIDE_URL))
        elif box.clickedButton() is console:
            QDesktopServices.openUrl(QUrl("https://console.cloud.google.com/apis/credentials"))

    def _show_error(self, title: str, message: str) -> None:
        log.error("%s: %s", title, message)
        box = QMessageBox(self)
        box.setWindowTitle(title)
        box.setIcon(QMessageBox.Icon.Critical)
        box.setText(title)
        box.setInformativeText(message)
        box.exec()

    def _notify(self, level: str, message: str) -> None:
        # Warnings usually need an action, so give them longer on screen.
        self.status_bar.showMessage(message, 6000 if level == "info" else 12000)

    # -- refresh -----------------------------------------------------------
    def _refresh_static(self) -> None:
        """Update everything that reads straight from the database."""
        account = self.controller.account
        destination = self.controller.destination
        free = None
        if destination:
            try:
                free = disk_space(destination).free
            except OSError:
                free = None
        self.dashboard.set_destination(destination, free)
        self.settings_page.set_destination(destination)
        if account is None:
            return
        self.dashboard.set_totals(self.controller.repos.items.totals(account.id, destination))
        self.dashboard.set_last_session(
            self.controller.repos.sessions.latest_finished(account.id)
        )
        self.dashboard.set_resume_available(self.controller.resume_candidate())

    def _refresh_plan(self) -> None:
        self.setup_page.set_plan(self.controller.build_plan())

    def _animate(self) -> None:
        state = self.controller.state
        if state is AppState.AUTHENTICATING:
            self.welcome.advance_animation()
        elif state is AppState.SCANNING:
            self.dashboard.scan_bar.advance()

    # -- shutdown ----------------------------------------------------------
    def closeEvent(self, event) -> None:  # noqa: D102, N802 - Qt override
        if self.controller.state.is_backup_active:
            answer = QMessageBox.question(
                self, "Backup in progress",
                "A backup is still running.\n\nClose anyway? Progress is saved and the "
                "backup can be resumed next time.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer is not QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self._animation.stop()
        keep_awake(False)
        if self._tray is not None:
            self._tray.hide()
        self.controller.shutdown()
        event.accept()
