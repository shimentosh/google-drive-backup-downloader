"""The live download queue table."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QWidget,
)

from ...database.schema import ItemStatus
from ...downloader.manager import ActiveTransfer
from ...downloader.worker import TaskResult
from ...utils.formatting import format_bytes, format_speed, truncate_middle
from ..theme import Palette

#: Glyph + tone for every status the queue can display.
_STATUS_STYLE: dict[str, tuple[str, str]] = {
    ItemStatus.DOWNLOADING: ("●  Downloading", "accent"),
    ItemStatus.QUEUED: ("○  Waiting", "muted"),
    ItemStatus.PENDING: ("○  Waiting", "muted"),
    ItemStatus.COMPLETED: ("✓  Completed", "success"),
    ItemStatus.SKIPPED: ("–  Skipped", "muted"),
    ItemStatus.FAILED: ("⚠  Failed", "danger"),
    ItemStatus.VERIFY_FAILED: ("⚠  Verify failed", "danger"),
    ItemStatus.UNSUPPORTED: ("⊘  Unsupported", "warning"),
    ItemStatus.PAUSED: ("⏸  Paused", "warning"),
    ItemStatus.CANCELLED: ("–  Cancelled", "muted"),
}

_MAX_ROWS = 600


class DownloadQueueView(QTableWidget):
    """Shows active transfers at the top and finished items beneath them."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(0, 4, parent)
        self.setHorizontalHeaderLabels(["STATUS", "FILE", "PROGRESS", "DETAIL"])
        self.verticalHeader().setVisible(False)
        self.setShowGrid(False)
        self.setAlternatingRowColors(False)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setWordWrap(False)

        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.setColumnWidth(0, 140)
        self.setColumnWidth(2, 170)
        self.setColumnWidth(3, 190)
        self.verticalHeader().setDefaultSectionSize(34)

        self._palette: Palette | None = None
        self._rows: dict[int, int] = {}
        self._active_ids: set[int] = set()
        self._filter: str | None = None

    # -- theming -----------------------------------------------------------
    def apply(self, palette: Palette) -> None:
        """Bind the table to a palette."""
        self._palette = palette

    def _tone_color(self, tone: str) -> QColor:
        p = self._palette
        if p is None:
            return QColor("#9aa3b7")
        return QColor({
            "accent": p.accent, "success": p.success, "warning": p.warning,
            "danger": p.danger, "muted": p.text_muted,
        }.get(tone, p.text_muted))

    # -- content -----------------------------------------------------------
    def reset_queue(self) -> None:
        """Empty the table (new backup run)."""
        self.setRowCount(0)
        self._rows.clear()
        self._active_ids.clear()

    def set_filter(self, status: str | None) -> None:
        """Show only rows with ``status``; ``None`` shows everything."""
        self._filter = status
        for item_id, row in self._rows.items():
            cell = self.item(row, 0)
            status_value = cell.data(Qt.ItemDataRole.UserRole) if cell else None
            self.setRowHidden(row, bool(status) and status_value != status)

    def update_active(self, transfers: list[ActiveTransfer]) -> None:
        """Refresh the rows for files currently being written."""
        seen: set[int] = set()
        for transfer in transfers:
            seen.add(transfer.item_id)
            row = self._ensure_row(transfer.item_id, transfer.name, top=True)
            self._set_status(row, ItemStatus.DOWNLOADING)
            if transfer.bytes_total > 0:
                fraction = min(1.0, transfer.bytes_done / transfer.bytes_total)
                progress = (
                    f"{format_bytes(transfer.bytes_done)} / "
                    f"{format_bytes(transfer.bytes_total)}"
                )
                detail = f"{fraction * 100:.0f}%   {format_speed(transfer.speed)}"
            else:
                progress = format_bytes(transfer.bytes_done)
                detail = format_speed(transfer.speed)
            self._set_cell(row, 2, progress)
            self._set_cell(row, 3, detail)

        for stale in self._active_ids - seen:
            row = self._rows.get(stale)
            if row is not None:
                cell = self.item(row, 0)
                if cell and cell.data(Qt.ItemDataRole.UserRole) == ItemStatus.DOWNLOADING:
                    self._set_status(row, ItemStatus.QUEUED)
        self._active_ids = seen

    def mark_finished(self, result: TaskResult) -> None:
        """Move one item into its final state."""
        row = self._ensure_row(result.item.id, result.item.name, top=False)
        self._set_status(row, result.status)
        total = result.item.total_bytes
        if result.status == ItemStatus.COMPLETED and total:
            self._set_cell(row, 2, format_bytes(total))
        elif total:
            self._set_cell(row, 2, format_bytes(total))
        else:
            self._set_cell(row, 2, "-")
        self._set_cell(row, 3, _short_reason(result.error))
        self._active_ids.discard(result.item.id)
        self._trim()

    def clear_completed(self) -> int:
        """Remove finished rows, keeping active and failed ones."""
        keep = (ItemStatus.DOWNLOADING, ItemStatus.QUEUED, ItemStatus.PENDING,
                ItemStatus.FAILED, ItemStatus.VERIFY_FAILED, ItemStatus.UNSUPPORTED)
        removed = 0
        for row in range(self.rowCount() - 1, -1, -1):
            cell = self.item(row, 0)
            if cell and cell.data(Qt.ItemDataRole.UserRole) not in keep:
                self.removeRow(row)
                removed += 1
        self._reindex()
        return removed

    # -- internals ---------------------------------------------------------
    def _ensure_row(self, item_id: int, name: str, top: bool) -> int:
        row = self._rows.get(item_id)
        if row is not None and row < self.rowCount():
            return row
        row = 0 if top else self.rowCount()
        self.insertRow(row)
        for column in range(4):
            cell = QTableWidgetItem("")
            if column != 1:
                cell.setTextAlignment(Qt.AlignmentFlag.AlignVCenter)
            self.setItem(row, column, cell)
        name_cell = self.item(row, 1)
        name_cell.setText(truncate_middle(name, 80))
        name_cell.setToolTip(name)
        name_cell.setData(Qt.ItemDataRole.UserRole, item_id)
        self._reindex()
        return self._rows[item_id]

    def _reindex(self) -> None:
        self._rows.clear()
        for row in range(self.rowCount()):
            cell = self.item(row, 1)
            if cell is not None:
                item_id = cell.data(Qt.ItemDataRole.UserRole)
                if item_id is not None:
                    self._rows[int(item_id)] = row

    def _set_status(self, row: int, status: str) -> None:
        label, tone = _STATUS_STYLE.get(status, (status, "muted"))
        cell = self.item(row, 0)
        if cell is None:
            return
        cell.setText(label)
        cell.setForeground(self._tone_color(tone))
        cell.setData(Qt.ItemDataRole.UserRole, status)
        if self._filter:
            self.setRowHidden(row, status != self._filter)

    def _set_cell(self, row: int, column: int, text: str) -> None:
        cell = self.item(row, column)
        if cell is not None and cell.text() != text:
            cell.setText(text)
            if self._palette and column == 3:
                cell.setForeground(QColor(self._palette.text_faint))

    def _trim(self) -> None:
        while self.rowCount() > _MAX_ROWS:
            for row in range(self.rowCount() - 1, -1, -1):
                cell = self.item(row, 0)
                if cell and cell.data(Qt.ItemDataRole.UserRole) in (
                    ItemStatus.COMPLETED, ItemStatus.SKIPPED
                ):
                    self.removeRow(row)
                    break
            else:
                self.removeRow(self.rowCount() - 1)
        self._reindex()


def _short_reason(error: str | None) -> str:
    """First clause of an error message, for the narrow detail column."""
    if not error:
        return ""
    text = error.split(".")[0].split(" - ")[0]
    return truncate_middle(text.strip(), 34)
