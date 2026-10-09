"""Centralised configuration: application paths and persisted user settings.

Nothing secret lives here. The OAuth client is loaded from ``credentials.json``
(or environment variables) at runtime; the user's tokens live in a separate,
permission-restricted file managed by :mod:`gdbd.auth.token_manager`.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import dataclass, field, fields
from pathlib import Path

from ..constants import (
    APP_SHORT_NAME,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_MAX_RETRIES,
    DEFAULT_WORKERS,
    MAX_CHUNK_SIZE,
    MAX_WORKERS,
    MIN_CHUNK_SIZE,
)

_ENV_DATA_DIR = "GDBD_DATA_DIR"
_ENV_CREDENTIALS = "GDBD_CREDENTIALS_FILE"
_ENV_CLIENT_ID = "GDBD_CLIENT_ID"
_ENV_CLIENT_SECRET = "GDBD_CLIENT_SECRET"

#: Export policy values for files that already exist locally.
EXISTING_SKIP = "skip"
EXISTING_REPLACE = "replace"
EXISTING_ASK = "ask"

#: Workspace export targets offered in the UI, keyed by Drive "kind".
EXPORT_CHOICES: dict[str, list[tuple[str, str]]] = {
    "document": [
        ("DOCX", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("PDF", "application/pdf"),
        ("ODT", "application/vnd.oasis.opendocument.text"),
        ("TXT", "text/plain"),
    ],
    "spreadsheet": [
        ("XLSX", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("ODS", "application/vnd.oasis.opendocument.spreadsheet"),
        ("CSV", "text/csv"),
        ("PDF", "application/pdf"),
    ],
    "presentation": [
        ("PPTX", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
        ("ODP", "application/vnd.oasis.opendocument.presentation"),
        ("PDF", "application/pdf"),
    ],
    "drawing": [
        ("PNG", "image/png"),
        ("SVG", "image/svg+xml"),
        ("PDF", "application/pdf"),
    ],
}


def default_data_dir() -> Path:
    """Per-user directory holding the database, tokens, logs and settings."""
    override = os.environ.get(_ENV_DATA_DIR)
    if override:
        return Path(override).expanduser()
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return Path(base) / APP_SHORT_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_SHORT_NAME
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / APP_SHORT_NAME


@dataclass
class AppPaths:
    """Filesystem layout for one installation."""

    data_dir: Path

    @property
    def database(self) -> Path:
        """SQLite database file."""
        return self.data_dir / "gdbd.sqlite3"

    @property
    def logs(self) -> Path:
        """Directory holding ``app.log`` and per-day backup logs."""
        return self.data_dir / "logs"

    @property
    def tokens(self) -> Path:
        """Encrypted-at-rest-by-filesystem-ACL OAuth token store."""
        return self.data_dir / "token.json"

    @property
    def settings_file(self) -> Path:
        """User settings JSON."""
        return self.data_dir / "settings.json"

    @property
    def default_credentials(self) -> Path:
        """Default location searched for the OAuth client file."""
        return self.data_dir / "credentials.json"

    def ensure(self) -> AppPaths:
        """Create every directory this layout needs."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(parents=True, exist_ok=True)
        return self


def credentials_search_paths(paths: AppPaths) -> list[Path]:
    """Ordered candidate locations for ``credentials.json``.

    An explicit ``GDBD_CREDENTIALS_FILE`` wins, then the app data directory,
    then the working directory and the directory containing the application.
    """
    candidates: list[Path] = []
    env_path = os.environ.get(_ENV_CREDENTIALS)
    if env_path:
        candidates.append(Path(env_path).expanduser())
    candidates.append(paths.default_credentials)
    candidates.append(Path.cwd() / "credentials.json")
    candidates.append(Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
                      / "credentials.json")
    seen: set[str] = set()
    unique: list[Path] = []
    for candidate in candidates:
        key = str(candidate).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return unique


def asset_path(name: str) -> Path | None:
    """Locate a bundled asset, in source layout or inside a PyInstaller bundle.

    Returns ``None`` when the asset is not present, so callers can degrade
    gracefully rather than crash.
    """
    roots = []
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        roots.append(Path(bundle) / "assets")
    roots.append(Path(__file__).resolve().parents[2] / "assets")
    for root in roots:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def client_config_from_env() -> dict | None:
    """Build an installed-app OAuth client config from environment variables.

    Returns ``None`` when the variables are not both set. This is the
    alternative to shipping a ``credentials.json`` file, and keeps secrets out
    of the repository either way.
    """
    client_id = os.environ.get(_ENV_CLIENT_ID)
    client_secret = os.environ.get(_ENV_CLIENT_SECRET)
    if not client_id or not client_secret:
        return None
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "redirect_uris": ["http://localhost"],
        }
    }


@dataclass
class Settings:
    """User-adjustable settings, persisted as JSON.

    Every field has a production-safe default so a fresh install works with no
    configuration beyond the OAuth client.
    """

    # --- Backup -----------------------------------------------------------
    destination: str = ""
    workers: int = DEFAULT_WORKERS
    retry_count: int = DEFAULT_MAX_RETRIES
    chunk_size: int = DEFAULT_CHUNK_SIZE
    existing_files: str = EXISTING_SKIP
    verify_downloads: bool = True
    retry_failed_automatically: bool = True
    preserve_structure: bool = True

    # --- Google Workspace -------------------------------------------------
    export_workspace: bool = True
    export_document: str = EXPORT_CHOICES["document"][0][1]
    export_spreadsheet: str = EXPORT_CHOICES["spreadsheet"][0][1]
    export_presentation: str = EXPORT_CHOICES["presentation"][0][1]
    export_drawing: str = EXPORT_CHOICES["drawing"][0][1]

    # --- Scope ------------------------------------------------------------
    include_shared_with_me: bool = False
    include_shared_drives: bool = False
    include_trashed: bool = False
    #: Skip files other people own, even inside the user's own folders.
    owned_only: bool = False

    # --- Appearance -------------------------------------------------------
    theme: str = "system"

    # --- While a backup runs ------------------------------------------------
    #: Stop Windows from going to sleep in the middle of a long backup.
    prevent_sleep: bool = True
    #: Show a desktop notification when a backup finishes.
    notify_on_finish: bool = True

    # --- Advanced ---------------------------------------------------------
    debug_logging: bool = False
    credentials_path: str = ""

    # --- Remembered state -------------------------------------------------
    last_account_email: str = ""

    _path: Path | None = field(default=None, repr=False, compare=False)
    _lock: threading.RLock = field(
        default_factory=threading.RLock, repr=False, compare=False
    )

    # -- persistence -------------------------------------------------------
    @classmethod
    def load(cls, path: Path) -> Settings:
        """Load settings from ``path``, falling back to defaults on any error."""
        instance = cls()
        instance._path = path
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = {}
            valid = {f.name for f in fields(cls) if not f.name.startswith("_")}
            for key, value in raw.items():
                if key in valid:
                    setattr(instance, key, value)
        instance.normalize()
        return instance

    def save(self) -> None:
        """Write settings atomically. Never raises on I/O failure."""
        if self._path is None:
            return
        with self._lock:
            self.normalize()
            # Built field by field rather than with ``asdict``: the private
            # fields hold a lock and a Path, neither of which can be deep-copied
            # or serialised to JSON.
            payload = {
                f.name: getattr(self, f.name)
                for f in fields(self)
                if not f.name.startswith("_")
            }
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                temp = self._path.with_suffix(".json.tmp")
                temp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                os.replace(temp, self._path)
            except OSError:
                pass

    def normalize(self) -> None:
        """Clamp every numeric/enum field into its supported range."""
        self.workers = max(1, min(MAX_WORKERS, int(self.workers or DEFAULT_WORKERS)))
        self.retry_count = max(0, min(20, int(self.retry_count)))
        self.chunk_size = max(
            MIN_CHUNK_SIZE, min(MAX_CHUNK_SIZE, int(self.chunk_size or DEFAULT_CHUNK_SIZE))
        )
        if self.existing_files not in (EXISTING_SKIP, EXISTING_REPLACE, EXISTING_ASK):
            self.existing_files = EXISTING_SKIP
        if self.theme not in ("system", "dark", "light"):
            self.theme = "system"

    # -- helpers -----------------------------------------------------------
    def export_map(self) -> dict[str, str]:
        """Map Google Workspace source MIME types to the chosen export MIME."""
        return {
            "application/vnd.google-apps.document": self.export_document,
            "application/vnd.google-apps.spreadsheet": self.export_spreadsheet,
            "application/vnd.google-apps.presentation": self.export_presentation,
            "application/vnd.google-apps.drawing": self.export_drawing,
        }

    def update(self, **values: object) -> None:
        """Set several fields at once and persist."""
        with self._lock:
            for key, value in values.items():
                if hasattr(self, key) and not key.startswith("_"):
                    setattr(self, key, value)
        self.save()


@dataclass
class AppContext:
    """Everything the services need that is decided at startup."""

    paths: AppPaths
    settings: Settings

    @classmethod
    def create(cls, data_dir: Path | None = None) -> AppContext:
        """Build the runtime context, creating directories as needed."""
        paths = AppPaths(Path(data_dir) if data_dir else default_data_dir()).ensure()
        settings = Settings.load(paths.settings_file)
        return cls(paths=paths, settings=settings)
