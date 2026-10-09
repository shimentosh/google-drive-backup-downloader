"""SQLite schema and migrations.

The schema is intentionally small and denormalised enough to answer the two
questions the application asks constantly:

1. "What still needs downloading for this account + destination?"  -> ``drive_items``
2. "What happened during backup run N?"                            -> ``backup_sessions`` / ``download_tasks``

``drive_items`` is keyed on ``(account_id, destination, drive_file_id)`` so the
same Drive can be backed up to several destinations, each keeping its own
independent resume state.
"""

from __future__ import annotations

SCHEMA_VERSION = 1


class ItemStatus:
    """Lifecycle of one Drive item within one destination."""

    PENDING = "PENDING"          #: discovered, not yet attempted
    QUEUED = "QUEUED"            #: handed to the download manager
    DOWNLOADING = "DOWNLOADING"  #: bytes are moving
    PAUSED = "PAUSED"            #: partially downloaded, user paused
    COMPLETED = "COMPLETED"      #: downloaded and verified
    SKIPPED = "SKIPPED"          #: already present and unchanged
    FAILED = "FAILED"            #: retries exhausted
    VERIFY_FAILED = "VERIFY_FAILED"  #: bytes arrived but did not verify
    UNSUPPORTED = "UNSUPPORTED"  #: Drive offers no way to download this item
    CANCELLED = "CANCELLED"      #: user cancelled before completion

    TERMINAL_OK = (COMPLETED, SKIPPED)
    TERMINAL_BAD = (FAILED, VERIFY_FAILED, UNSUPPORTED)
    RESUMABLE = (PENDING, QUEUED, DOWNLOADING, PAUSED, CANCELLED, VERIFY_FAILED, FAILED)


class SessionStatus:
    """Lifecycle of a backup run."""

    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"

    OPEN = (RUNNING, PAUSED)


#: Executed in order inside one transaction on first run.
DDL = """
CREATE TABLE IF NOT EXISTS schema_info (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE,
    display_name  TEXT,
    storage_limit INTEGER,
    storage_used  INTEGER,
    connected_at  TEXT NOT NULL,
    last_scan_at  TEXT
);

CREATE TABLE IF NOT EXISTS backup_sessions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id        INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    destination       TEXT NOT NULL,
    status            TEXT NOT NULL,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    total_files       INTEGER NOT NULL DEFAULT 0,
    total_folders     INTEGER NOT NULL DEFAULT 0,
    total_bytes       INTEGER NOT NULL DEFAULT 0,
    completed_files   INTEGER NOT NULL DEFAULT 0,
    completed_bytes   INTEGER NOT NULL DEFAULT 0,
    skipped_files     INTEGER NOT NULL DEFAULT 0,
    failed_files      INTEGER NOT NULL DEFAULT 0,
    unsupported_files INTEGER NOT NULL DEFAULT 0,
    duration_seconds  INTEGER,
    message           TEXT
);

CREATE TABLE IF NOT EXISTS drive_items (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id        INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    destination       TEXT NOT NULL,
    drive_file_id     TEXT NOT NULL,
    parent_id         TEXT,
    name              TEXT NOT NULL,
    mime_type         TEXT NOT NULL,
    is_folder         INTEGER NOT NULL DEFAULT 0,
    size              INTEGER NOT NULL DEFAULT 0,
    modified_time     TEXT,
    created_time      TEXT,
    md5_checksum      TEXT,
    relative_path     TEXT NOT NULL,
    local_path        TEXT NOT NULL,
    strategy          TEXT NOT NULL DEFAULT 'binary',
    export_mime       TEXT,
    drive_id          TEXT,
    status            TEXT NOT NULL DEFAULT 'PENDING',
    downloaded_bytes  INTEGER NOT NULL DEFAULT 0,
    total_bytes       INTEGER NOT NULL DEFAULT 0,
    error_message     TEXT,
    retry_count       INTEGER NOT NULL DEFAULT 0,
    last_session_id   INTEGER,
    seen_at           TEXT,
    remote_deleted    INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE (account_id, destination, drive_file_id)
);

CREATE INDEX IF NOT EXISTS idx_items_file_id  ON drive_items (drive_file_id);
CREATE INDEX IF NOT EXISTS idx_items_parent   ON drive_items (parent_id);
CREATE INDEX IF NOT EXISTS idx_items_status   ON drive_items (status);
CREATE INDEX IF NOT EXISTS idx_items_local    ON drive_items (local_path);
CREATE INDEX IF NOT EXISTS idx_items_scope    ON drive_items (account_id, destination, status);
CREATE INDEX IF NOT EXISTS idx_items_folder   ON drive_items (account_id, destination, is_folder);

CREATE TABLE IF NOT EXISTS download_tasks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   INTEGER NOT NULL REFERENCES backup_sessions(id) ON DELETE CASCADE,
    item_id      INTEGER NOT NULL REFERENCES drive_items(id) ON DELETE CASCADE,
    status       TEXT NOT NULL,
    bytes_moved  INTEGER NOT NULL DEFAULT 0,
    attempts     INTEGER NOT NULL DEFAULT 0,
    started_at   TEXT,
    finished_at  TEXT,
    error_message TEXT,
    UNIQUE (session_id, item_id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_session ON download_tasks (session_id, status);

CREATE TABLE IF NOT EXISTS download_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER REFERENCES backup_sessions(id) ON DELETE CASCADE,
    item_id    INTEGER,
    level      TEXT NOT NULL DEFAULT 'INFO',
    event      TEXT NOT NULL,
    message    TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_session ON download_events (session_id, id);

CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS name_mappings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    INTEGER NOT NULL,
    destination   TEXT NOT NULL,
    drive_file_id TEXT NOT NULL,
    original_name TEXT NOT NULL,
    local_name    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    UNIQUE (account_id, destination, drive_file_id)
);
"""
