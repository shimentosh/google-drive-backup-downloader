"""Data access for accounts, sessions, Drive items and events.

All SQL lives here. Services and UI code talk to repositories, never to
:mod:`sqlite3` directly.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from ..utils.timeutil import now_iso
from .db import Database
from .models import Account, BackupSession, DriveItemRecord
from .schema import ItemStatus, SessionStatus

#: Columns written by the scanner, in the order used by the bulk upsert.
_ITEM_COLUMNS = (
    "account_id", "destination", "drive_file_id", "parent_id", "name", "mime_type",
    "is_folder", "size", "modified_time", "created_time", "md5_checksum",
    "relative_path", "local_path", "strategy", "export_mime", "drive_id",
    "total_bytes", "seen_at", "created_at", "updated_at",
)

#: True when the remote copy differs from what we last recorded locally.
_CHANGED = (
    "(drive_items.modified_time IS NOT excluded.modified_time"
    " OR drive_items.size <> excluded.size"
    " OR IFNULL(drive_items.md5_checksum, '') <> IFNULL(excluded.md5_checksum, '')"
    " OR drive_items.local_path <> excluded.local_path"
    " OR IFNULL(drive_items.export_mime, '') <> IFNULL(excluded.export_mime, ''))"
)

_UPSERT_ITEM_SQL = f"""
INSERT INTO drive_items ({", ".join(_ITEM_COLUMNS)})
VALUES ({", ".join("?" * len(_ITEM_COLUMNS))})
ON CONFLICT(account_id, destination, drive_file_id) DO UPDATE SET
    parent_id        = excluded.parent_id,
    name             = excluded.name,
    mime_type        = excluded.mime_type,
    is_folder        = excluded.is_folder,
    size             = excluded.size,
    modified_time    = excluded.modified_time,
    created_time     = excluded.created_time,
    md5_checksum     = excluded.md5_checksum,
    relative_path    = excluded.relative_path,
    local_path       = excluded.local_path,
    strategy         = excluded.strategy,
    export_mime      = excluded.export_mime,
    drive_id         = excluded.drive_id,
    total_bytes      = excluded.total_bytes,
    seen_at          = excluded.seen_at,
    remote_deleted   = 0,
    updated_at       = excluded.updated_at,
    status           = CASE WHEN {_CHANGED} THEN '{ItemStatus.PENDING}'
                            ELSE drive_items.status END,
    downloaded_bytes = CASE WHEN {_CHANGED} THEN 0
                            ELSE drive_items.downloaded_bytes END,
    retry_count      = CASE WHEN {_CHANGED} THEN 0
                            ELSE drive_items.retry_count END,
    error_message    = CASE WHEN {_CHANGED} THEN NULL
                            ELSE drive_items.error_message END
"""


def normalize_destination(destination: str) -> str:
    """Canonical form of a destination path used as a scoping key."""
    return str(destination).replace("/", "\\").rstrip("\\").casefold()


class AccountRepository:
    """Reads and writes the ``accounts`` table."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(
        self,
        email: str,
        display_name: str | None = None,
        storage_limit: int | None = None,
        storage_used: int | None = None,
    ) -> Account:
        """Insert or refresh an account row and return it."""
        self.db.execute(
            "INSERT INTO accounts (email, display_name, storage_limit, storage_used,"
            " connected_at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(email) DO UPDATE SET display_name=excluded.display_name,"
            " storage_limit=excluded.storage_limit, storage_used=excluded.storage_used",
            (email, display_name, storage_limit, storage_used, now_iso()),
        )
        account = self.get_by_email(email)
        assert account is not None  # just inserted
        return account

    def get_by_email(self, email: str) -> Account | None:
        """Look an account up by e-mail address."""
        return Account.from_row(
            self.db.query_one("SELECT * FROM accounts WHERE email = ?", (email,))
        )

    def get(self, account_id: int) -> Account | None:
        """Look an account up by primary key."""
        return Account.from_row(
            self.db.query_one("SELECT * FROM accounts WHERE id = ?", (account_id,))
        )

    def touch_scan(self, account_id: int) -> None:
        """Record that a scan just finished for this account."""
        self.db.execute(
            "UPDATE accounts SET last_scan_at = ? WHERE id = ?", (now_iso(), account_id)
        )

    def delete(self, account_id: int) -> None:
        """Remove an account and every row that hangs off it."""
        self.db.execute("DELETE FROM accounts WHERE id = ?", (account_id,))


class ItemRepository:
    """Reads and writes ``drive_items`` - the backup's source of truth."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # -- scan writes -------------------------------------------------------
    def bulk_upsert(self, rows: Sequence[Sequence[Any]]) -> None:
        """Insert or update a batch of scanned items in one transaction.

        Rows whose remote metadata is unchanged keep their existing status and
        resume state; rows that changed remotely are reset to ``PENDING``.
        """
        if not rows:
            return
        self.db.executemany(_UPSERT_ITEM_SQL, rows)

    @staticmethod
    def build_row(
        account_id: int,
        destination: str,
        *,
        drive_file_id: str,
        parent_id: str | None,
        name: str,
        mime_type: str,
        is_folder: bool,
        size: int,
        modified_time: str | None,
        created_time: str | None,
        md5_checksum: str | None,
        relative_path: str,
        local_path: str,
        strategy: str,
        export_mime: str | None,
        drive_id: str | None,
        stamp: str | None = None,
    ) -> tuple[Any, ...]:
        """Assemble one tuple in the exact order :data:`_ITEM_COLUMNS` expects."""
        stamp = stamp or now_iso()
        return (
            account_id, normalize_destination(destination), drive_file_id, parent_id,
            name, mime_type, 1 if is_folder else 0, int(size or 0), modified_time,
            created_time, md5_checksum, relative_path, local_path, strategy,
            export_mime, drive_id, int(size or 0), stamp, stamp, stamp,
        )

    def mark_missing_as_deleted(self, account_id: int, destination: str, stamp: str) -> int:
        """Flag items not seen in the latest scan as removed from Drive.

        Local files are never touched - this only records that the remote copy
        is gone so the UI can report it.
        """
        cursor = self.db.execute(
            "UPDATE drive_items SET remote_deleted = 1, updated_at = ?"
            " WHERE account_id = ? AND destination = ?"
            " AND (seen_at IS NULL OR seen_at < ?) AND remote_deleted = 0",
            (now_iso(), account_id, normalize_destination(destination), stamp),
        )
        return cursor.rowcount or 0

    # -- work queue --------------------------------------------------------
    def pending_files(
        self,
        account_id: int,
        destination: str,
        include_failed: bool = True,
        limit: int | None = None,
    ) -> list[DriveItemRecord]:
        """Files still needing work, in stable tree order."""
        statuses = [
            ItemStatus.PENDING, ItemStatus.QUEUED, ItemStatus.DOWNLOADING,
            ItemStatus.PAUSED, ItemStatus.CANCELLED, ItemStatus.VERIFY_FAILED,
        ]
        if include_failed:
            statuses.append(ItemStatus.FAILED)
        placeholders = ", ".join("?" * len(statuses))
        sql = (
            "SELECT * FROM drive_items WHERE account_id = ? AND destination = ?"
            f" AND is_folder = 0 AND remote_deleted = 0 AND status IN ({placeholders})"
            " ORDER BY relative_path COLLATE NOCASE, id"
        )
        params: list[Any] = [account_id, normalize_destination(destination), *statuses]
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return DriveItemRecord.from_rows(self.db.query(sql, params))

    def failed_files(self, account_id: int, destination: str) -> list[DriveItemRecord]:
        """Items whose last outcome was a failure the user can retry."""
        return DriveItemRecord.from_rows(
            self.db.query(
                "SELECT * FROM drive_items WHERE account_id = ? AND destination = ?"
                " AND is_folder = 0 AND status IN (?, ?)"
                " ORDER BY relative_path COLLATE NOCASE",
                (account_id, normalize_destination(destination),
                 ItemStatus.FAILED, ItemStatus.VERIFY_FAILED),
            )
        )

    def folders(self, account_id: int, destination: str) -> list[DriveItemRecord]:
        """Every folder discovered for this account + destination."""
        return DriveItemRecord.from_rows(
            self.db.query(
                "SELECT * FROM drive_items WHERE account_id = ? AND destination = ?"
                " AND is_folder = 1 AND remote_deleted = 0"
                " ORDER BY relative_path COLLATE NOCASE",
                (account_id, normalize_destination(destination)),
            )
        )

    def get(self, item_id: int) -> DriveItemRecord | None:
        """Load one item by primary key."""
        return DriveItemRecord.from_row(
            self.db.query_one("SELECT * FROM drive_items WHERE id = ?", (item_id,))
        )

    def get_by_file_id(
        self, account_id: int, destination: str, drive_file_id: str
    ) -> DriveItemRecord | None:
        """Load one item by its Drive file id within a destination."""
        return DriveItemRecord.from_row(
            self.db.query_one(
                "SELECT * FROM drive_items WHERE account_id = ? AND destination = ?"
                " AND drive_file_id = ?",
                (account_id, normalize_destination(destination), drive_file_id),
            )
        )

    # -- status updates ----------------------------------------------------
    def set_status(
        self,
        item_id: int,
        status: str,
        *,
        error: str | None = None,
        downloaded_bytes: int | None = None,
        retry_count: int | None = None,
        session_id: int | None = None,
    ) -> None:
        """Update one item's status and any related bookkeeping fields."""
        sets = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status, now_iso()]
        # Clearing the error on success keeps the failed-file view accurate.
        sets.append("error_message = ?")
        params.append(error)
        if downloaded_bytes is not None:
            sets.append("downloaded_bytes = ?")
            params.append(int(downloaded_bytes))
        if retry_count is not None:
            sets.append("retry_count = ?")
            params.append(int(retry_count))
        if session_id is not None:
            sets.append("last_session_id = ?")
            params.append(session_id)
        params.append(item_id)
        self.db.execute(f"UPDATE drive_items SET {', '.join(sets)} WHERE id = ?", params)

    def set_progress(self, item_id: int, downloaded_bytes: int) -> None:
        """Persist resume progress for one item (called sparingly)."""
        self.db.execute(
            "UPDATE drive_items SET downloaded_bytes = ?, updated_at = ? WHERE id = ?",
            (int(downloaded_bytes), now_iso(), item_id),
        )

    def bulk_set_status(self, item_ids: Iterable[int], status: str) -> None:
        """Move several items to the same status in one transaction."""
        stamp = now_iso()
        rows = [(status, stamp, item_id) for item_id in item_ids]
        if rows:
            self.db.executemany(
                "UPDATE drive_items SET status = ?, updated_at = ? WHERE id = ?", rows
            )

    def reset_interrupted(self, account_id: int, destination: str) -> int:
        """After a crash, move ``DOWNLOADING``/``QUEUED`` rows back to ``PAUSED``.

        Their ``.part`` files and byte counts are preserved, so the next run
        resumes rather than restarting.
        """
        cursor = self.db.execute(
            "UPDATE drive_items SET status = ?, updated_at = ?"
            " WHERE account_id = ? AND destination = ? AND status IN (?, ?)",
            (ItemStatus.PAUSED, now_iso(), account_id,
             normalize_destination(destination), ItemStatus.DOWNLOADING,
             ItemStatus.QUEUED),
        )
        return cursor.rowcount or 0

    def retry_failed(self, account_id: int, destination: str) -> int:
        """Reset failed items to ``PENDING`` so the next run picks them up."""
        cursor = self.db.execute(
            "UPDATE drive_items SET status = ?, retry_count = 0, error_message = NULL,"
            " updated_at = ? WHERE account_id = ? AND destination = ? AND status IN (?, ?)",
            (ItemStatus.PENDING, now_iso(), account_id,
             normalize_destination(destination), ItemStatus.FAILED,
             ItemStatus.VERIFY_FAILED),
        )
        return cursor.rowcount or 0

    def clear_scope(self, account_id: int, destination: str) -> None:
        """Delete every item row for one account + destination (fresh start)."""
        self.db.execute(
            "DELETE FROM drive_items WHERE account_id = ? AND destination = ?",
            (account_id, normalize_destination(destination)),
        )

    # -- aggregates --------------------------------------------------------
    def status_counts(self, account_id: int, destination: str) -> dict[str, int]:
        """File counts grouped by status."""
        rows = self.db.query(
            "SELECT status, COUNT(*) AS n FROM drive_items"
            " WHERE account_id = ? AND destination = ? AND is_folder = 0"
            " GROUP BY status",
            (account_id, normalize_destination(destination)),
        )
        return {row["status"]: row["n"] for row in rows}

    def totals(self, account_id: int, destination: str) -> dict[str, int]:
        """Headline totals for the dashboard and the setup screen."""
        dest = normalize_destination(destination)
        row = self.db.query_one(
            "SELECT"
            "  SUM(CASE WHEN is_folder = 0 THEN 1 ELSE 0 END) AS files,"
            "  SUM(CASE WHEN is_folder = 1 THEN 1 ELSE 0 END) AS folders,"
            "  SUM(CASE WHEN is_folder = 0 THEN total_bytes ELSE 0 END) AS total_bytes,"
            "  SUM(CASE WHEN is_folder = 0 AND status IN (?, ?) THEN total_bytes ELSE 0 END)"
            "    AS done_bytes,"
            "  SUM(CASE WHEN is_folder = 0 AND strategy = 'export' THEN 1 ELSE 0 END)"
            "    AS export_files,"
            "  SUM(CASE WHEN is_folder = 0 AND strategy = 'unsupported' THEN 1 ELSE 0 END)"
            "    AS unsupported_files"
            " FROM drive_items WHERE account_id = ? AND destination = ? AND remote_deleted = 0",
            (ItemStatus.COMPLETED, ItemStatus.SKIPPED, account_id, dest),
        )
        if row is None:
            return {}
        return {key: int(row[key] or 0) for key in row.keys()}

    def remaining_bytes(self, account_id: int, destination: str) -> int:
        """Bytes still to transfer, accounting for partially downloaded files."""
        return int(
            self.db.scalar(
                "SELECT SUM(MAX(total_bytes - downloaded_bytes, 0)) FROM drive_items"
                " WHERE account_id = ? AND destination = ? AND is_folder = 0"
                " AND remote_deleted = 0 AND status NOT IN (?, ?, ?)",
                (account_id, normalize_destination(destination), ItemStatus.COMPLETED,
                 ItemStatus.SKIPPED, ItemStatus.UNSUPPORTED),
                default=0,
            )
            or 0
        )

    def category_breakdown(self, account_id: int, destination: str) -> list[tuple[str, int, int]]:
        """``(mime_type, file_count, bytes)`` rows for statistics."""
        rows = self.db.query(
            "SELECT mime_type, COUNT(*) AS n, SUM(total_bytes) AS bytes"
            " FROM drive_items WHERE account_id = ? AND destination = ?"
            " AND is_folder = 0 AND remote_deleted = 0 GROUP BY mime_type",
            (account_id, normalize_destination(destination)),
        )
        return [(row["mime_type"], int(row["n"]), int(row["bytes"] or 0)) for row in rows]

    def search(
        self,
        account_id: int,
        destination: str,
        text: str = "",
        status: str | None = None,
        category: str | None = None,
        limit: int = 500,
    ) -> list[DriveItemRecord]:
        """Filtered listing used by the Drive browser."""
        sql = [
            "SELECT * FROM drive_items WHERE account_id = ? AND destination = ?"
            " AND is_folder = 0"
        ]
        params: list[Any] = [account_id, normalize_destination(destination)]
        if text:
            sql.append(" AND (name LIKE ? OR relative_path LIKE ?)")
            pattern = f"%{text}%"
            params.extend([pattern, pattern])
        if status:
            sql.append(" AND status = ?")
            params.append(status)
        if category:
            from ..drive.models import category_mime_clause

            clause, cat_params = category_mime_clause(category)
            if clause:
                sql.append(f" AND {clause}")
                params.extend(cat_params)
        sql.append(" ORDER BY relative_path COLLATE NOCASE LIMIT ?")
        params.append(limit)
        return DriveItemRecord.from_rows(self.db.query("".join(sql), params))

    def record_name_mapping(
        self, account_id: int, destination: str, drive_file_id: str,
        original: str, local: str
    ) -> None:
        """Record a local name that differs from the Drive name.

        Covers Windows-illegal characters, de-duplication suffixes and
        export extensions, so the mapping back to Drive is never lost.
        """
        self.db.execute(
            "INSERT INTO name_mappings (account_id, destination, drive_file_id,"
            " original_name, local_name, created_at) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(account_id, destination, drive_file_id) DO UPDATE SET"
            " original_name=excluded.original_name, local_name=excluded.local_name",
            (account_id, normalize_destination(destination), drive_file_id,
             original, local, now_iso()),
        )


class SessionRepository:
    """Reads and writes ``backup_sessions``, ``download_tasks`` and events."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def create(
        self,
        account_id: int,
        destination: str,
        total_files: int,
        total_folders: int,
        total_bytes: int,
    ) -> BackupSession:
        """Open a new backup session and return it."""
        cursor = self.db.execute(
            "INSERT INTO backup_sessions (account_id, destination, status, started_at,"
            " total_files, total_folders, total_bytes) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (account_id, str(destination), SessionStatus.RUNNING, now_iso(),
             total_files, total_folders, total_bytes),
        )
        session = self.get(int(cursor.lastrowid))
        assert session is not None
        return session

    def get(self, session_id: int) -> BackupSession | None:
        """Load one session by id."""
        return BackupSession.from_row(
            self.db.query_one("SELECT * FROM backup_sessions WHERE id = ?", (session_id,))
        )

    def latest_open(self, account_id: int) -> BackupSession | None:
        """The most recent session that was never finished (crash recovery)."""
        return BackupSession.from_row(
            self.db.query_one(
                "SELECT * FROM backup_sessions WHERE account_id = ? AND status IN (?, ?)"
                " ORDER BY id DESC LIMIT 1",
                (account_id, SessionStatus.RUNNING, SessionStatus.PAUSED),
            )
        )

    def latest_finished(self, account_id: int) -> BackupSession | None:
        """The most recent completed session, for the "Last backup" tile."""
        return BackupSession.from_row(
            self.db.query_one(
                "SELECT * FROM backup_sessions WHERE account_id = ? AND finished_at"
                " IS NOT NULL ORDER BY id DESC LIMIT 1",
                (account_id,),
            )
        )

    def list(self, account_id: int | None = None, limit: int = 100) -> list[BackupSession]:
        """Recent sessions, newest first."""
        if account_id is None:
            rows = self.db.query(
                "SELECT * FROM backup_sessions ORDER BY id DESC LIMIT ?", (limit,)
            )
        else:
            rows = self.db.query(
                "SELECT * FROM backup_sessions WHERE account_id = ? ORDER BY id DESC"
                " LIMIT ?",
                (account_id, limit),
            )
        return BackupSession.from_rows(rows)

    def update_progress(
        self,
        session_id: int,
        *,
        completed_files: int,
        completed_bytes: int,
        skipped_files: int,
        failed_files: int,
        unsupported_files: int,
    ) -> None:
        """Persist the running counters (called on a timer, not per chunk)."""
        self.db.execute(
            "UPDATE backup_sessions SET completed_files = ?, completed_bytes = ?,"
            " skipped_files = ?, failed_files = ?, unsupported_files = ? WHERE id = ?",
            (completed_files, completed_bytes, skipped_files, failed_files,
             unsupported_files, session_id),
        )

    def set_status(self, session_id: int, status: str, message: str | None = None) -> None:
        """Change a session's status without closing it."""
        self.db.execute(
            "UPDATE backup_sessions SET status = ?, message = COALESCE(?, message)"
            " WHERE id = ?",
            (status, message, session_id),
        )

    def finish(
        self, session_id: int, status: str, duration_seconds: int, message: str | None = None
    ) -> None:
        """Close a session with a final status."""
        self.db.execute(
            "UPDATE backup_sessions SET status = ?, finished_at = ?,"
            " duration_seconds = ?, message = COALESCE(?, message) WHERE id = ?",
            (status, now_iso(), duration_seconds, message, session_id),
        )

    def close_stale(self) -> int:
        """Mark sessions left open by a crash as ``FAILED`` on next launch."""
        cursor = self.db.execute(
            "UPDATE backup_sessions SET status = ?, finished_at = ?,"
            " message = COALESCE(message, 'Interrupted - application closed unexpectedly')"
            " WHERE status IN (?, ?)",
            (SessionStatus.FAILED, now_iso(), SessionStatus.RUNNING, SessionStatus.PAUSED),
        )
        return cursor.rowcount or 0

    # -- tasks -------------------------------------------------------------
    def upsert_task(
        self,
        session_id: int,
        item_id: int,
        status: str,
        bytes_moved: int = 0,
        attempts: int = 0,
        error: str | None = None,
        finished: bool = False,
    ) -> None:
        """Record what happened to one item inside one session."""
        stamp = now_iso()
        self.db.execute(
            "INSERT INTO download_tasks (session_id, item_id, status, bytes_moved,"
            " attempts, started_at, finished_at, error_message)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(session_id, item_id) DO UPDATE SET status=excluded.status,"
            " bytes_moved=excluded.bytes_moved, attempts=excluded.attempts,"
            " finished_at=excluded.finished_at, error_message=excluded.error_message",
            (session_id, item_id, status, bytes_moved, attempts, stamp,
             stamp if finished else None, error),
        )

    def session_tasks(self, session_id: int, limit: int = 1000) -> list[dict[str, Any]]:
        """Tasks belonging to a session joined with their item names."""
        rows = self.db.query(
            "SELECT t.*, i.name, i.relative_path, i.total_bytes FROM download_tasks t"
            " JOIN drive_items i ON i.id = t.item_id WHERE t.session_id = ?"
            " ORDER BY t.id LIMIT ?",
            (session_id, limit),
        )
        return [dict(row) for row in rows]

    # -- events ------------------------------------------------------------
    def add_event(
        self,
        session_id: int | None,
        event: str,
        message: str = "",
        level: str = "INFO",
        item_id: int | None = None,
    ) -> None:
        """Append one structured event to the session log."""
        self.db.execute(
            "INSERT INTO download_events (session_id, item_id, level, event, message,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, item_id, level, event, message[:2000], now_iso()),
        )

    def events(self, session_id: int | None = None, limit: int = 500) -> list[dict[str, Any]]:
        """Recent events, newest first."""
        if session_id is None:
            rows = self.db.query(
                "SELECT * FROM download_events ORDER BY id DESC LIMIT ?", (limit,)
            )
        else:
            rows = self.db.query(
                "SELECT * FROM download_events WHERE session_id = ? ORDER BY id DESC"
                " LIMIT ?",
                (session_id, limit),
            )
        return [dict(row) for row in rows]


class Repositories:
    """Convenience bundle so callers pass one object around."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.accounts = AccountRepository(db)
        self.items = ItemRepository(db)
        self.sessions = SessionRepository(db)
