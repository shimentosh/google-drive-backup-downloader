"""Structured application logging.

Two sinks are configured:

* ``logs/app.log``          - rotating application log (all runs)
* ``logs/backup_<date>.log`` - one file per day, attached while a backup runs

A redaction filter guarantees that OAuth tokens and client secrets never reach
either sink even if a third-party library tries to log them.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
import sys
from pathlib import Path

_LOG_FORMAT = "%(asctime)s  %(levelname)-8s  %(name)-28s  %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

#: Patterns whose *values* must never be written to disk.
_SECRET_PATTERNS = [
    re.compile(r"(access_token[\"'\s:=]+)([A-Za-z0-9._\-]+)", re.I),
    re.compile(r"(refresh_token[\"'\s:=]+)([A-Za-z0-9._\-/]+)", re.I),
    re.compile(r"(client_secret[\"'\s:=]+)([A-Za-z0-9._\-]+)", re.I),
    re.compile(r"(id_token[\"'\s:=]+)([A-Za-z0-9._\-]+)", re.I),
    re.compile(r"(Bearer\s+)([A-Za-z0-9._\-]+)", re.I),
    re.compile(r"(ya29\.)([A-Za-z0-9._\-]+)"),
]

_configured = False
_backup_handler: logging.Handler | None = None


class RedactingFilter(logging.Filter):
    """Replace anything that looks like a credential with ``***REDACTED***``."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: D102
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            return True
        redacted = self.redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True

    @staticmethod
    def redact(text: str) -> str:
        """Return ``text`` with credential-shaped substrings masked."""
        for pattern in _SECRET_PATTERNS:
            text = pattern.sub(lambda m: f"{m.group(1)}***REDACTED***", text)
        return text


def configure_logging(log_dir: Path, debug: bool = False) -> Path:
    """Install console + rotating file handlers. Returns the app log path.

    Safe to call more than once; later calls only adjust the level.
    """
    global _configured
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    app_log = log_dir / "app.log"

    root = logging.getLogger()
    level = logging.DEBUG if debug else logging.INFO
    root.setLevel(logging.DEBUG)

    if not _configured:
        formatter = logging.Formatter(_LOG_FORMAT, _DATE_FORMAT)
        redactor = RedactingFilter()

        file_handler = logging.handlers.RotatingFileHandler(
            app_log, maxBytes=8 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(redactor)
        file_handler.set_name("app_file")
        root.addHandler(file_handler)

        # A windowed build (PyInstaller --noconsole) has no stderr, and a
        # StreamHandler bound to None fails on every single log call.
        if sys.stderr is not None:
            console = logging.StreamHandler()
            console.setFormatter(formatter)
            console.addFilter(redactor)
            console.set_name("console")
            root.addHandler(console)

        # These libraries are extremely chatty at DEBUG and can echo headers.
        for noisy in ("googleapiclient", "google_auth_httplib2", "urllib3",
                      "google.auth", "oauthlib", "requests_oauthlib",
                      "keyring", "win32ctypes", "PIL"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        _configured = True

    for handler in root.handlers:
        if handler.get_name() in ("app_file", "console"):
            handler.setLevel(level)
    return app_log


def start_backup_log(log_dir: Path, date_str: str) -> Path:
    """Attach a per-day backup log file and return its path."""
    global _backup_handler
    stop_backup_log()
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"backup_{date_str}.log"
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, _DATE_FORMAT))
    handler.addFilter(RedactingFilter())
    handler.set_name("backup_file")
    logging.getLogger().addHandler(handler)
    _backup_handler = handler
    return path


def stop_backup_log() -> None:
    """Detach the per-backup log file if one is attached."""
    global _backup_handler
    if _backup_handler is not None:
        logging.getLogger().removeHandler(_backup_handler)
        try:
            _backup_handler.close()
        except Exception:  # pragma: no cover - defensive
            pass
        _backup_handler = None


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced application logger."""
    return logging.getLogger(name)
