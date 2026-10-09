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

from ..constants import APP_NAME, APP_SUBTITLE, SETUP_GUIDE_URL
from ..services.event_bus import AppState
from ..utils.formatting import format_bytes
from .icons import icon
from .theme import Palette
from .widgets.common import Banner, Card, Divider, KeyValue
from .widgets.progress import ProgressBar


#: The one-time Google Cloud setup, in the order a first-time user does it.
#: Each step opens the exact console page, so nobody has to hunt through menus.
#: (title, what to do there, anchor in the online guide, console URL)
SETUP_STEPS = [
    ("Create a Google Cloud project",
     "Sign in with the Gmail you want to back up. Name it anything, then Create.",
     "step-1", "https://console.cloud.google.com/projectcreate"),
    ("Turn on the Google Drive API",
     "Check your new project is selected, then click Enable.",
     "step-2",
     "https://console.cloud.google.com/flows/enableapi?apiid=drive.googleapis.com"),
    ("Set up the sign-in screen",
     "Get Started \u2192 any app name + your email \u2192 Audience: External "
     "\u2192 your email \u2192 tick the policy box \u2192 Create.",
     "step-3", "https://console.cloud.google.com/auth/branding"),
    ("Add yourself as a test user",
     "Under Test users, click Add users, type your Gmail, Save. "
     "Skipping this causes \"Access blocked\".",
     "step-4", "https://console.cloud.google.com/auth/audience"),
    ("Create a Desktop app key",
     "Create Client \u2192 Application type: Desktop app \u2192 Create. "
     "Click Download JSON straight away: Google shows the secret only once.",
     "step-5", "https://console.cloud.google.com/auth/clients"),
]

#: Shown while the browser is open, because Google's screens surprise people.
_CONNECT_HINT = (
    "Your browser is open. Choose your Google account, then:\n"
    "\u2022 \"Google hasn't verified this app\" \u2192 click Continue "
    "(it's your own private key, so this is expected)\n"
    "\u2022 Tick \"See and download all your Google Drive files\" \u2192 Continue"
)


def _open(url: str) -> None:
    QDesktopServices.openUrl(QUrl(url))


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
        self.card.setFixedWidth(580)
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
        self.status.setWordWrap(True)
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

        help_button = QPushButton("Step-by-step guide with pictures (English & \u09ac\u09be\u0982\u09b2\u09be)")
        help_button.setObjectName("Ghost")
        help_button.setCursor(Qt.CursorShape.PointingHandCursor)
        help_button.clicked.connect(lambda: _open(SETUP_GUIDE_URL))
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

        heading = QLabel("One-time setup \u00b7 about 5 minutes")
        heading.setObjectName("SectionTitle")
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(heading)
        intro = QLabel(
            "This app connects to Google with your own free key, so your files never "
            "pass through anyone else's server. No credit card needed. Do each step "
            "in your browser, then come back here."
        )
        intro.setObjectName("Faint")
        intro.setWordWrap(True)
        intro.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(intro)
        layout.addSpacing(4)

        self.step_rows: list[QWidget] = []
        for number, (title, detail, anchor, url) in enumerate(SETUP_STEPS, start=1):
            row = self._step_row(number, title, detail, url,
                                 f"{SETUP_GUIDE_URL}#{anchor}")
            self.step_rows.append(row)
            layout.addWidget(row)

        final = Card(padding=12, spacing=8, sub=True)
        final_title = QLabel(f"{len(SETUP_STEPS) + 1}.  Add the downloaded file here")
        final_title.setObjectName("CardValueSmall")
        final.add(final_title)
        browse_button = QPushButton("  Choose downloaded JSON file...")
        browse_button.setObjectName("Primary")
        browse_button.setIcon(icon("folder", self._palette.on_accent, 16))
        browse_button.setCursor(Qt.CursorShape.PointingHandCursor)
        browse_button.clicked.connect(self._browse_credentials)
        final.add(browse_button)
        drop_hint = QLabel("Or drag the .json file onto this window.")
        drop_hint.setObjectName("Faint")
        drop_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        final.add(drop_hint)
        layout.addWidget(final)

        # Pasting is an advanced fallback (e.g. the JSON was lost but the ID was
        # copied), so it stays folded away to keep the main path obvious.
        self.paste_toggle = QPushButton("Prefer to paste the Client ID and secret instead?")
        self.paste_toggle.setObjectName("Ghost")
        self.paste_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(self.paste_toggle)
        self.paste_box = QWidget()
        paste_layout = QVBoxLayout(self.paste_box)
        paste_layout.setContentsMargins(0, 0, 0, 0)
        paste_layout.setSpacing(8)
        self.paste_box.setVisible(False)
        self.paste_toggle.clicked.connect(
            lambda: self.paste_box.setVisible(not self.paste_box.isVisible())
        )
        layout.addWidget(self.paste_box)

        self.client_id_edit = QLineEdit()
        self.client_id_edit.setPlaceholderText("Client ID  (....apps.googleusercontent.com)")
        paste_layout.addWidget(self.client_id_edit)

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
        paste_layout.addLayout(secret_row)
        return panel

    def _step_row(self, number: int, title: str, detail: str, url: str,
                  guide_url: str) -> QWidget:
        """One numbered setup step with a button that opens its console page."""
        p = self._palette
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(12)

        badge = QLabel(str(number))
        badge.setFixedSize(26, 26)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setStyleSheet(
            f"background: {p.accent_soft}; color: {p.accent}; border-radius: 13px;"
            " font-weight: 600;"
        )
        layout.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)

        text = QVBoxLayout()
        text.setSpacing(2)
        name = QLabel(title)
        name.setObjectName("CardValueSmall")
        text.addWidget(name)
        what = QLabel(f'{detail}  <a href="{guide_url}">Show me</a>')
        what.setObjectName("Faint")
        what.setWordWrap(True)
        what.setOpenExternalLinks(True)
        text.addWidget(what)
        layout.addLayout(text, 1)

        button = QPushButton("Open")
        button.setIcon(icon("external", p.text, 14))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setToolTip(url)
        button.clicked.connect(lambda _c=False, u=url: _open(u))
        layout.addWidget(button, 0, Qt.AlignmentFlag.AlignTop)
        return row

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
            self.banner.hide_message()
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
        self.status.setText(_CONNECT_HINT if connecting else "")
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
