"""First-run screen: connect a Google account."""

from __future__ import annotations

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..constants import APP_NAME, APP_SUBTITLE
from ..services.event_bus import AppState
from ..utils.formatting import format_bytes
from .icons import icon
from .theme import Palette
from .widgets.common import Banner, Card, Divider, KeyValue
from .widgets.progress import ProgressBar


class WelcomePage(QWidget):
    """Shown until an account is connected, then again after disconnect."""

    connect_requested = Signal()
    continue_requested = Signal()
    setup_help_requested = Signal()
    credentials_file_chosen = Signal(str)       # path to a downloaded client JSON
    credentials_entered = Signal(str, str)      # client ID, client secret

    def __init__(self, palette: Palette, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Root")
        self._palette = palette
        self._credentials_missing = False
        self._changing_client = False
        self._client_id = ""
        self.setAcceptDrops(True)

        # Scrollable so the one-time setup form never gets squeezed on short windows.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content.setObjectName("Root")
        scroll.setWidget(content)
        page = QVBoxLayout(self)
        page.setContentsMargins(0, 0, 0, 0)
        page.addWidget(scroll)

        outer = QVBoxLayout(content)
        outer.setContentsMargins(40, 40, 40, 40)
        outer.addStretch(1)

        centre = QHBoxLayout()
        centre.addStretch(1)

        self.card = Card(padding=40, spacing=16)
        self.card.setFixedWidth(520)
        self.card.with_shadow(palette)

        logo = QLabel()
        logo.setPixmap(icon("shield", palette.accent, 52).pixmap(52, 52))
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.logo = logo
        self.card.add(logo)

        title = QLabel(APP_NAME)
        title.setObjectName("Hero")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setWordWrap(True)
        self.card.add(title)

        subtitle = QLabel(APP_SUBTITLE)
        subtitle.setObjectName("HeroSub")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.card.add(subtitle)

        blurb = QLabel(
            "Back up your entire Google Drive straight to a folder on this PC or an "
            "external disk. The folder structure is preserved, downloads resume after "
            "an interruption, and nothing is ever uploaded anywhere."
        )
        blurb.setObjectName("Muted")
        blurb.setWordWrap(True)
        blurb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.blurb = blurb
        self.card.add(blurb)
        self.card.body().addSpacing(6)

        self.banner = Banner()
        self.banner.apply(palette)
        self.card.add(self.banner)

        self.setup_panel = self._build_setup_panel()
        self.setup_panel.setVisible(False)
        self.card.add(self.setup_panel)

        self.connect_button = QPushButton("  Connect Google Drive")
        self.connect_button.setObjectName("PrimaryLarge")
        self.connect_button.setIcon(icon("cloud", palette.on_accent, 19))
        self.connect_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.connect_button.clicked.connect(self.connect_requested.emit)
        self.card.add(self.connect_button)

        self.progress = ProgressBar(height=6, palette=palette)
        self.progress.setVisible(False)
        self.card.add(self.progress)

        self.status = QLabel("")
        self.status.setObjectName("Faint")
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.card.add(self.status)

        # -- account summary, revealed after a successful connection --------
        self.summary = QWidget()
        summary_layout = QVBoxLayout(self.summary)
        summary_layout.setContentsMargins(0, 0, 0, 0)
        summary_layout.setSpacing(6)
        summary_layout.addWidget(Divider())
        self.account_row = KeyValue("Account", "-")
        self.storage_row = KeyValue("Drive storage used", "-")
        summary_layout.addWidget(self.account_row)
        summary_layout.addWidget(self.storage_row)
        self.continue_button = QPushButton("Continue")
        self.continue_button.setObjectName("Primary")
        self.continue_button.clicked.connect(self.continue_requested.emit)
        summary_layout.addSpacing(6)
        summary_layout.addWidget(self.continue_button)
        self.summary.setVisible(False)
        self.card.add(self.summary)

        # Lets a user who saved the wrong OAuth client replace it.
        self.change_client_button = QPushButton()
        self.change_client_button.setObjectName("Ghost")
        self.change_client_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.change_client_button.clicked.connect(self._toggle_change_client)
        self.change_client_button.setVisible(False)
        self.card.add(self.change_client_button)

        help_button = QPushButton("How do I set up Google OAuth credentials?")
        help_button.setObjectName("Ghost")
        help_button.setCursor(Qt.CursorShape.PointingHandCursor)
        help_button.clicked.connect(self.setup_help_requested.emit)
        self.card.add(help_button)

        centre.addWidget(self.card)
        centre.addStretch(1)
        outer.addLayout(centre)

        privacy = QLabel(
            "Read-only access · credentials stay on this PC · no third-party servers"
        )
        privacy.setObjectName("Faint")
        privacy.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addSpacing(18)
        outer.addWidget(privacy)
        outer.addStretch(1)

    def _build_setup_panel(self) -> QWidget:
        """One-time OAuth client setup, shown only while none is configured."""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        console_button = QPushButton("1.  Open Google Cloud Console")
        console_button.setCursor(Qt.CursorShape.PointingHandCursor)
        console_button.clicked.connect(
            lambda: QDesktopServices.openUrl(
                QUrl("https://console.cloud.google.com/apis/credentials")
            )
        )
        layout.addWidget(console_button)

        browse_button = QPushButton("2.  Choose downloaded JSON file...")
        browse_button.setObjectName("Primary")
        browse_button.setCursor(Qt.CursorShape.PointingHandCursor)
        browse_button.clicked.connect(self._browse_credentials)
        layout.addWidget(browse_button)

        hint = QLabel("You can also drag the JSON file onto this window, "
                      "or paste the Client ID and secret:")
        hint.setObjectName("Faint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.client_id_edit = QLineEdit()
        self.client_id_edit.setPlaceholderText("Client ID  (....apps.googleusercontent.com)")
        layout.addWidget(self.client_id_edit)

        secret_row = QHBoxLayout()
        secret_row.setSpacing(8)
        self.client_secret_edit = QLineEdit()
        self.client_secret_edit.setPlaceholderText("Client secret  (GOCSPX-...)")
        self.client_secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.client_secret_edit.returnPressed.connect(self._submit_pair)
        secret_row.addWidget(self.client_secret_edit, 1)
        save_button = QPushButton("Save")
        save_button.setCursor(Qt.CursorShape.PointingHandCursor)
        save_button.clicked.connect(self._submit_pair)
        secret_row.addWidget(save_button)
        layout.addLayout(secret_row)
        return panel

    def _browse_credentials(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose the OAuth client JSON", "", "JSON files (*.json);;All files (*)"
        )
        if path:
            self.credentials_file_chosen.emit(path)

    def _submit_pair(self) -> None:
        self.credentials_entered.emit(self.client_id_edit.text(),
                                      self.client_secret_edit.text())

    def show_setup_error(self, message: str) -> None:
        """Explain why the supplied client could not be used."""
        self.banner.show_message(message, "danger")

    # -- drag and drop -----------------------------------------------------
    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt override
        urls = event.mimeData().urls()
        if (self._credentials_missing and len(urls) == 1
                and urls[0].toLocalFile().lower().endswith(".json")):
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt override
        self.credentials_file_chosen.emit(event.mimeData().urls()[0].toLocalFile())

    # -- state -------------------------------------------------------------
    def _toggle_change_client(self) -> None:
        self._changing_client = not self._changing_client
        if self._changing_client:
            self.banner.show_message(
                "Add the new OAuth client below. It replaces the one saved now.", "info"
            )
        else:
            self.banner.hide_message()
        self._update_client_controls()

    def _update_client_controls(self) -> None:
        editing = self._credentials_missing or self._changing_client
        self.setup_panel.setVisible(editing)
        self.blurb.setVisible(not editing)
        self.logo.setVisible(not editing)
        self.change_client_button.setVisible(
            not self._credentials_missing and not self.summary.isVisible()
        )
        short_id = self._client_id.split("-", 1)[0] or "?"
        self.change_client_button.setText(
            "Cancel" if self._changing_client
            else f"Use a different OAuth client  (current: {short_id}-...)"
        )

    def set_credentials_missing(self, missing: bool, client_id: str = "") -> None:
        """Warn when no OAuth client is configured yet."""
        self._credentials_missing = missing
        self._changing_client = False
        self._client_id = client_id
        if missing:
            self.banner.show_message(
                "One-time setup: create a Desktop-app OAuth client in Google Cloud, "
                "then add it here. See the guide at the bottom for the steps.",
                "warning",
            )
            self.connect_button.setEnabled(False)
        else:
            self.banner.hide_message()
            self.connect_button.setEnabled(True)
            self.client_id_edit.clear()
            self.client_secret_edit.clear()
        self._update_client_controls()

    def set_state(self, state: AppState) -> None:
        """Reflect the application state in this page."""
        connecting = state is AppState.AUTHENTICATING
        self.progress.setVisible(connecting)
        self.progress.set_indeterminate(connecting)
        self.connect_button.setEnabled(not connecting and not self._credentials_missing)
        self.connect_button.setText(
            "  Waiting for Google..." if connecting else "  Connect Google Drive"
        )
        self.status.setText(
            "A browser window has opened. Approve access to continue."
            if connecting else ""
        )
        if state is AppState.DISCONNECTED:
            self.summary.setVisible(False)

    def show_account(self, email: str, used: int | None, limit: int | None,
                     files: int | None = None, folders: int | None = None) -> None:
        """Reveal the connected-account summary."""
        self.account_row.set_value(email)
        if used is not None:
            text = format_bytes(used)
            if limit:
                text += f" of {format_bytes(limit)}"
            self.storage_row.set_value(text)
        if files is not None:
            self.storage_row.key.setText("Your Drive")
            self.storage_row.set_value(f"{files:,} files · {folders or 0:,} folders")
        self.summary.setVisible(True)
        self.change_client_button.setVisible(False)
        self.status.setText("")
        self.progress.setVisible(False)

    def advance_animation(self) -> None:
        """Drive the indeterminate bar while connecting."""
        self.progress.advance()
