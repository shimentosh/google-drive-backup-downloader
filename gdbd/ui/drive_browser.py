"""My Drive: search and filter everything the scan discovered."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..database.repositories import Repositories
from ..database.schema import ItemStatus
from ..drive.models import CATEGORY_ORDER
from ..utils.formatting import format_bytes, format_count
from ..utils.timeutil import format_local
from .theme import Palette
from .widgets.common import Card, PageHeader

_STATUS_FILTERS = [
    ("All statuses", None),
    ("Downloaded", ItemStatus.COMPLETED),
    ("Pending", ItemStatus.PENDING),
    ("Failed", ItemStatus.FAILED),
    ("Skipped", ItemStatus.SKIPPED),
    ("Unsupported", ItemStatus.UNSUPPORTED),
]

_LIMIT = 1000


class DriveBrowserPage(QWidget):
    """A searchable table of every file in the scanned Drive."""

    def __init__(self, repos: Repositories, palette: Palette,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.repos = repos
        self._palette = palette
        self._account_id: int | None = None
        self._destination = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 26, 28, 24)
        root.setSpacing(14)
        self.header = PageHeader("My Drive", "Everything discovered by the last scan")
        root.addWidget(self.header)

        card = Card(padding=16, spacing=12)
        filters = QHBoxLayout()
        filters.setSpacing(10)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search files...")
        self.search.setClearButtonEnabled(True)
        filters.addWidget(self.search, 1)

        self.type_box = QComboBox()
        self.type_box.addItem("All types")
        for name in CATEGORY_ORDER:
            self.type_box.addItem(name)
        self.type_box.setFixedWidth(150)
        filters.addWidget(self.type_box)

        self.status_box = QComboBox()
        for label, _ in _STATUS_FILTERS:
            self.status_box.addItem(label)
        self.status_box.setFixedWidth(160)
        filters.addWidget(self.status_box)
        card.add_layout(filters)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["NAME", "FOLDER", "SIZE", "MODIFIED", "STATUS"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(2, 100)
        self.table.setColumnWidth(3, 170)
        self.table.setColumnWidth(4, 130)
        card.add(self.table, 1)

        self.count_label = QLabel("")
        self.count_label.setObjectName("Faint")
        card.add(self.count_label)
        root.addWidget(card, 1)

        # Debounce typing so each keystroke does not hit the database.
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(220)
        self._debounce.timeout.connect(self.refresh)
        self.search.textChanged.connect(lambda _t: self._debounce.start())
        self.type_box.currentIndexChanged.connect(lambda _i: self.refresh())
        self.status_box.currentIndexChanged.connect(lambda _i: self.refresh())

    def set_scope(self, account_id: int | None, destination: str) -> None:
        """Point the browser at one account + destination."""
        self._account_id = account_id
        self._destination = destination
        self.refresh()

    def refresh(self) -> None:
        """Re-query the database with the current filters."""
        if self._account_id is None or not self._destination:
            self.table.setRowCount(0)
            self.count_label.setText("Connect an account and scan your Drive first.")
            return

        category = None
        if self.type_box.currentIndex() > 0:
            category = self.type_box.currentText()
        status = _STATUS_FILTERS[self.status_box.currentIndex()][1]

        rows = self.repos.items.search(
            self._account_id, self._destination, self.search.text().strip(),
            status, category, limit=_LIMIT,
        )
        self.table.setRowCount(len(rows))
        total_bytes = 0
        for row, record in enumerate(rows):
            total_bytes += record.total_bytes
            folder = record.relative_path.rsplit("/", 1)[0] if "/" in record.relative_path else "/"
            name_cell = QTableWidgetItem(record.name)
            name_cell.setToolTip(record.local_path)
            self.table.setItem(row, 0, name_cell)
            self.table.setItem(row, 1, QTableWidgetItem(folder))
            self.table.setItem(row, 2, QTableWidgetItem(format_bytes(record.total_bytes)))
            self.table.setItem(row, 3, QTableWidgetItem(format_local(record.modified_time)))
            status_cell = QTableWidgetItem(record.status.replace("_", " ").title())
            status_cell.setForeground(self._colour(record.status))
            if record.error_message:
                status_cell.setToolTip(record.error_message)
            self.table.setItem(row, 4, status_cell)

        suffix = f" (showing the first {_LIMIT:,})" if len(rows) >= _LIMIT else ""
        self.count_label.setText(
            f"{format_count(len(rows))} file(s) · {format_bytes(total_bytes)}{suffix}"
        )

    def _colour(self, status: str) -> QColor:
        p = self._palette
        return QColor({
            ItemStatus.COMPLETED: p.success,
            ItemStatus.SKIPPED: p.text_muted,
            ItemStatus.FAILED: p.danger,
            ItemStatus.VERIFY_FAILED: p.danger,
            ItemStatus.UNSUPPORTED: p.warning,
            ItemStatus.DOWNLOADING: p.accent,
        }.get(status, p.text_muted))
