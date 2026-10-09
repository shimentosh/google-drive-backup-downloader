"""Logs page: tail the application log without blocking the UI."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QFont, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..filesystem.storage import open_in_file_manager
from ..utils.formatting import format_bytes
from .theme import Palette
from .widgets.common import Card, PageHeader

#: Only the tail is read, so a multi-megabyte log never stalls the UI.
_TAIL_BYTES = 256 * 1024


class LogsPage(QWidget):
    """Shows the current log file and refreshes it while the page is visible."""

    def __init__(self, log_dir: Path, palette: Palette,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.log_dir = Path(log_dir)
        self._palette = palette
        self._offset = 0
        self._current: Path | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 26, 28, 24)
        root.setSpacing(14)
        header = PageHeader("Logs", str(self.log_dir))
        root.addWidget(header)

        card = Card(padding=16, spacing=10)
        controls = QHBoxLayout()
        controls.setSpacing(10)
        self.file_box = QComboBox()
        self.file_box.setMinimumWidth(260)
        self.file_box.currentIndexChanged.connect(self._on_file_changed)
        controls.addWidget(self.file_box)
        self.follow = QCheckBox("Follow")
        self.follow.setChecked(True)
        controls.addWidget(self.follow)
        controls.addStretch(1)
        refresh = QPushButton("Refresh list")
        refresh.clicked.connect(self.refresh_files)
        controls.addWidget(refresh)
        open_button = QPushButton("Open log folder")
        open_button.clicked.connect(lambda: open_in_file_manager(self.log_dir))
        controls.addWidget(open_button)
        card.add_layout(controls)

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(6000)
        font = QFont("Cascadia Mono")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(9)
        self.view.setFont(font)
        card.add(self.view, 1)

        self.status = QLabel("")
        self.status.setObjectName("Faint")
        card.add(self.status)
        root.addWidget(card, 1)

        self._timer = QTimer(self)
        self._timer.setInterval(1500)
        self._timer.timeout.connect(self._tail)
        self.refresh_files()

    # -- lifecycle ---------------------------------------------------------
    def showEvent(self, event) -> None:  # noqa: D102, N802 - Qt override
        super().showEvent(event)
        self.refresh_files()
        self._timer.start()

    def hideEvent(self, event) -> None:  # noqa: D102, N802 - Qt override
        super().hideEvent(event)
        self._timer.stop()

    # -- content -----------------------------------------------------------
    def refresh_files(self) -> None:
        """Rebuild the file list, keeping the current selection when possible."""
        current = self.file_box.currentData()
        files = sorted(
            (p for p in self.log_dir.glob("*.log") if p.is_file()),
            key=lambda p: (p.name != "app.log", p.name), reverse=False,
        )
        self.file_box.blockSignals(True)
        self.file_box.clear()
        for path in files:
            self.file_box.addItem(path.name, str(path))
        if current:
            index = self.file_box.findData(current)
            if index >= 0:
                self.file_box.setCurrentIndex(index)
        self.file_box.blockSignals(False)
        if not files:
            self.view.setPlainText("No log files yet.")
            self.status.setText("")
            return
        self._on_file_changed()

    def _on_file_changed(self) -> None:
        data = self.file_box.currentData()
        if not data:
            return
        path = Path(data)
        if path != self._current:
            self._current = path
            self._offset = 0
            self.view.clear()
        self._tail(force=True)

    def _tail(self, force: bool = False) -> None:
        path = self._current
        if path is None or not path.exists():
            return
        try:
            size = path.stat().st_size
            if size < self._offset:  # rotated
                self._offset = 0
                self.view.clear()
            if size == self._offset and not force:
                return
            start = max(self._offset, size - _TAIL_BYTES) if self._offset == 0 else self._offset
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                handle.seek(start)
                text = handle.read()
            self._offset = size
        except OSError as exc:
            self.status.setText(f"Could not read log: {exc}")
            return

        if text:
            self.view.appendPlainText(text.rstrip("\n"))
            if self.follow.isChecked():
                self.view.moveCursor(QTextCursor.MoveOperation.End)
        self.status.setText(f"{path.name} · {format_bytes(self._offset)}")
