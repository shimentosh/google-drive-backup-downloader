"""Thread-safe SQLite access layer.

Every thread gets its own connection (SQLite connections are not safely shared
across threads). WAL journalling lets the scanner write while download workers
read, and a generous busy timeout absorbs the short lock waits that a handful
of concurrent writers produce.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from ..utils.logging import get_logger
from .schema import DDL, SCHEMA_VERSION

log = get_logger(__name__)


class Database:
    """Owns the SQLite file and hands out per-thread connections."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        self._write_lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialise()

    # -- connection management --------------------------------------------
    def connect(self) -> sqlite3.Connection:
        """Return this thread's connection, creating it on first use."""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            return conn
        conn = sqlite3.connect(
            str(self.path),
            timeout=30.0,
            isolation_level=None,  # explicit transaction control
            check_same_thread=True,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        self._local.conn = conn
        with self._connections_lock:
            self._connections.append(conn)
        return conn

    def close(self) -> None:
        """Close every connection this database handed out."""
        with self._connections_lock:
            for conn in self._connections:
                try:
                    conn.close()
                except sqlite3.Error:  # pragma: no cover - defensive
                    pass
            self._connections.clear()
        self._local = threading.local()

    # -- schema ------------------------------------------------------------
    def _initialise(self) -> None:
        self.executescript(DDL)
        self.execute(
            "INSERT INTO schema_info (key, value) VALUES ('version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )
        log.debug("Database ready at %s (schema v%s)", self.path, SCHEMA_VERSION)

    def executescript(self, script: str) -> None:
        """Run a multi-statement DDL script.

        ``executescript`` commits any open transaction before it runs, so it
        must not be nested inside :meth:`transaction`.
        """
        with self._write_lock:
            self.connect().executescript(script)

    # -- execution helpers -------------------------------------------------
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block inside one immediate transaction.

        Writers are additionally serialised in-process, which keeps SQLite's
        ``database is locked`` retries rare when 16 download workers report
        progress at once.
        """
        conn = self.connect()
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        """Run a single write statement in its own transaction."""
        with self.transaction() as conn:
            return conn.execute(sql, params)

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        """Run a batched write inside one transaction."""
        with self.transaction() as conn:
            conn.executemany(sql, seq)

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        """Run a read query and return all rows."""
        return self.connect().execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        """Run a read query and return the first row, if any."""
        return self.connect().execute(sql, params).fetchone()

    def scalar(self, sql: str, params: Sequence[Any] = (), default: Any = None) -> Any:
        """Run a read query and return the first column of the first row."""
        row = self.query_one(sql, params)
        if row is None:
            return default
        value = row[0]
        return default if value is None else value

    # -- key/value settings table -----------------------------------------
    def get_setting(self, key: str, default: str | None = None) -> str | None:
        """Read an application-level value from the ``settings`` table."""
        row = self.query_one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str | None) -> None:
        """Write an application-level value into the ``settings`` table."""
        from ..utils.timeutil import now_iso

        self.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (key, value, now_iso()),
        )
