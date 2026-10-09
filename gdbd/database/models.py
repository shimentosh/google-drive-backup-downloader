"""Typed row objects for the persisted tables."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, fields
from typing import Any, TypeVar

T = TypeVar("T", bound="Row")


@dataclass
class Row:
    """Base class that builds a dataclass from a :class:`sqlite3.Row`."""

    @classmethod
    def from_row(cls: type[T], row: sqlite3.Row | dict[str, Any] | None) -> T | None:
        """Convert a database row into this dataclass, ignoring extra columns."""
        if row is None:
            return None
        data = dict(row)
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})  # type: ignore[arg-type]

    @classmethod
    def from_rows(cls: type[T], rows: list[sqlite3.Row]) -> list[T]:
        """Convert a list of rows, dropping any that fail to convert."""
        out: list[T] = []
        for row in rows:
            item = cls.from_row(row)
            if item is not None:
                out.append(item)
        return out


@dataclass
class Account(Row):
    """A connected Google account."""

    id: int
    email: str
    display_name: str | None = None
    storage_limit: int | None = None
    storage_used: int | None = None
    connected_at: str = ""
    last_scan_at: str | None = None


@dataclass
class BackupSession(Row):
    """One backup run against one destination."""

    id: int
    account_id: int
    destination: str
    status: str
    started_at: str
    finished_at: str | None = None
    total_files: int = 0
    total_folders: int = 0
    total_bytes: int = 0
    completed_files: int = 0
    completed_bytes: int = 0
    skipped_files: int = 0
    failed_files: int = 0
    unsupported_files: int = 0
    duration_seconds: int | None = None
    message: str | None = None

    @property
    def successful_files(self) -> int:
        """Files that finished cleanly (downloaded or skipped as unchanged)."""
        return self.completed_files + self.skipped_files


@dataclass
class DriveItemRecord(Row):
    """Durable per-destination state for one Drive file or folder."""

    id: int
    account_id: int
    destination: str
    drive_file_id: str
    name: str
    mime_type: str
    relative_path: str
    local_path: str
    parent_id: str | None = None
    is_folder: int = 0
    size: int = 0
    modified_time: str | None = None
    created_time: str | None = None
    md5_checksum: str | None = None
    strategy: str = "binary"
    export_mime: str | None = None
    drive_id: str | None = None
    status: str = "PENDING"
    downloaded_bytes: int = 0
    total_bytes: int = 0
    error_message: str | None = None
    retry_count: int = 0
    last_session_id: int | None = None
    seen_at: str | None = None
    remote_deleted: int = 0
    created_at: str = ""
    updated_at: str = ""

    @property
    def is_directory(self) -> bool:
        """True when this record represents a folder."""
        return bool(self.is_folder)


@dataclass
class ScanSummary:
    """Aggregate statistics produced by a Drive scan."""

    files: int = 0
    folders: int = 0
    total_bytes: int = 0
    downloadable_bytes: int = 0
    export_files: int = 0
    unsupported_files: int = 0
    by_category: dict[str, int] = None  # type: ignore[assignment]
    by_category_count: dict[str, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.by_category is None:
            self.by_category = {}
        if self.by_category_count is None:
            self.by_category_count = {}
