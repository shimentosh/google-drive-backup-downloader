"""Backup history: past sessions and their per-file outcomes."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..database.models import BackupSession
from ..database.repositories import Repositories
from ..database.schema import SessionStatus
from ..utils.formatting import format_bytes, format_count, format_duration
from ..utils.timeutil import format_local
from .theme import Palette
from .widgets.common import Card, Divider, KeyValue, PageHeader

_TONE = {
    SessionStatus.COMPLETED: "success",
    SessionStatus.COMPLETED_WITH_ERRORS: "warning",
    SessionStatus.CANCELLED: "muted",
    SessionStatus.FAILED: "danger",
    SessionStatus.PAUSED: "warning",
    SessionStatus.RUNNING: "info",
}


class HistoryPage(QWidget):
    """Lists previous backup runs and drills into one when selected."""

    def __init__(self, repos: Repositories, palette: Palette,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.repos = repos
        self._palette = palette
        self._sessions: list[BackupSession] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(28, 26, 28, 24)
        root.setSpacing(14)
        root.addWidget(PageHeader("History", "Every backup run recorded on this PC"))

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        left = Card(padding=14, spacing=10)
        left_title = QLabel("Backup runs")
        left_title.setObjectName("SectionTitle")
        left.add(left_title)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["DATE", "STATUS", "FILES", "SIZE", "DURATION"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.table.itemSelectionChanged.connect(self._on_selection)
        left.add(self.table, 1)
        splitter.addWidget(left)

        right = Card(padding=18, spacing=8)
        detail_title = QLabel("Run details")
        detail_title.setObjectName("SectionTitle")
        right.add(detail_title)
        right.add(Divider())
        self.detail_when = KeyValue("Started", "-")
        self.detail_finished = KeyValue("Finished", "-")
        self.detail_dest = KeyValue("Destination", "-")
        self.detail_status = KeyValue("Status", "-")
        self.detail_files = KeyValue("Files", "-")
        self.detail_ok = KeyValue("Successful", "-")
        self.detail_failed = KeyValue("Failed", "-")
        self.detail_unsupported = KeyValue("Unsupported", "-")
        self.detail_bytes = KeyValue("Downloaded", "-")
        self.detail_duration = KeyValue("Duration", "-")
        for row in (self.detail_when, self.detail_finished, self.detail_dest,
                    self.detail_status, self.detail_files, self.detail_ok,
                    self.detail_failed, self.detail_unsupported, self.detail_bytes,
                    self.detail_duration):
            right.add(row)
        right.add(Divider())
        errors_title = QLabel("Reported problems")
        errors_title.setObjectName("CardLabel")
        right.add(errors_title)
        self.events = QTableWidget(0, 2)
        self.events.setHorizontalHeaderLabels(["LEVEL", "DETAIL"])
        self.events.verticalHeader().setVisible(False)
        self.events.setShowGrid(False)
        self.events.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.events.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.events.setColumnWidth(0, 80)
        right.add(self.events, 1)
        splitter.addWidget(right)
        splitter.setSizes([520, 460])
        root.addWidget(splitter, 1)

        self.empty = QLabel("No backups have been run yet.")
        self.empty.setObjectName("Faint")
        root.addWidget(self.empty)

    def refresh(self, account_id: int | None) -> None:
        """Reload the session list for one account."""
        self._sessions = self.repos.sessions.list(account_id, limit=200)
        self.table.setRowCount(len(self._sessions))
        for row, session in enumerate(self._sessions):
            self._set(row, 0, format_local(session.started_at))
            status_cell = QTableWidgetItem(session.status.replace("_", " ").title())
            tone = _TONE.get(session.status, "muted")
            status_cell.setForeground(self._colour(tone))
            self.table.setItem(row, 1, status_cell)
            self._set(row, 2, format_count(session.total_files))
            self._set(row, 3, format_bytes(session.completed_bytes))
            self._set(row, 4, format_duration(session.duration_seconds))
        self.empty.setVisible(not self._sessions)
        if self._sessions:
            self.table.selectRow(0)

    def _set(self, row: int, column: int, text: str) -> None:
        self.table.setItem(row, column, QTableWidgetItem(text))

    def _colour(self, tone: str):
        from PySide6.QtGui import QColor

        p = self._palette
        return QColor({
            "success": p.success, "warning": p.warning, "danger": p.danger,
            "info": p.accent, "muted": p.text_muted,
        }.get(tone, p.text_muted))

    def _on_selection(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        index = rows[0].row()
        if index >= len(self._sessions):
            return
        session = self._sessions[index]
        self.detail_when.set_value(format_local(session.started_at))
        self.detail_finished.set_value(format_local(session.finished_at))
        self.detail_dest.set_value(session.destination)
        self.detail_status.set_value(session.status.replace("_", " ").title())
        self.detail_files.set_value(format_count(session.total_files))
        self.detail_ok.set_value(format_count(session.successful_files))
        self.detail_failed.set_value(format_count(session.failed_files))
        self.detail_unsupported.set_value(format_count(session.unsupported_files))
        self.detail_bytes.set_value(format_bytes(session.completed_bytes))
        self.detail_duration.set_value(format_duration(session.duration_seconds))

        events = [
            event for event in self.repos.sessions.events(session.id, limit=300)
            if event["level"] in ("ERROR", "WARNING")
        ]
        self.events.setRowCount(len(events))
        for row, event in enumerate(events):
            level = QTableWidgetItem(event["level"])
            level.setForeground(
                self._colour("danger" if event["level"] == "ERROR" else "warning")
            )
            self.events.setItem(row, 0, level)
            detail = QTableWidgetItem(event["message"] or event["event"])
            detail.setToolTip(event["message"] or "")
            self.events.setItem(row, 1, detail)
