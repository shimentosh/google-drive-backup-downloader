"""Shared fixtures and a fake Google Drive, so tests need no real credentials."""

from __future__ import annotations

import hashlib
import sys
import threading
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gdbd.config.settings import Settings  # noqa: E402
from gdbd.constants import FOLDER_MIME  # noqa: E402
from gdbd.database.db import Database  # noqa: E402
from gdbd.database.repositories import Repositories  # noqa: E402
from gdbd.drive.models import DriveFile  # noqa: E402

ROOT_ID = "root-id"


# --------------------------------------------------------------------------
# Fake HTTP layer
# --------------------------------------------------------------------------
@dataclass
class FakeResponse:
    """Minimal stand-in for a streaming ``requests`` response."""

    status_code: int
    body: bytes = b""
    headers: dict = field(default_factory=dict)
    #: Fail after this many bytes have been yielded (simulates a dropped link).
    fail_after: int | None = None
    closed: bool = False

    @property
    def text(self) -> str:
        """Error bodies are read as text by the download strategies."""
        return self.body.decode("utf-8", "replace")

    def iter_content(self, chunk_size: int = 8192):
        """Yield the body in chunks, optionally breaking part-way through."""
        sent = 0
        for start in range(0, len(self.body), chunk_size):
            chunk = self.body[start:start + chunk_size]
            if self.fail_after is not None and sent + len(chunk) > self.fail_after:
                keep = max(0, self.fail_after - sent)
                if keep:
                    yield chunk[:keep]
                import requests.exceptions as rex

                raise rex.ChunkedEncodingError("connection reset by peer")
            sent += len(chunk)
            yield chunk

    def close(self) -> None:
        """Mark the response closed so tests can assert cleanup."""
        self.closed = True


class FakeDriveClient:
    """Implements exactly the surface :mod:`gdbd` uses from :class:`DriveClient`."""

    def __init__(
        self,
        files: list[DriveFile] | None = None,
        contents: dict[str, bytes] | None = None,
        exports: dict[str, bytes] | None = None,
    ) -> None:
        self.files = files or []
        self.contents = contents or {}
        self.exports = exports or {}
        #: Queue of responses to return instead of the default, per file id.
        self.scripted: dict[str, list[FakeResponse]] = {}
        self.requests: list[tuple[str, int]] = []
        self.shared_drives: list = []
        self._lock = threading.Lock()

    # -- metadata ----------------------------------------------------------
    def root_id(self) -> str:
        """Id of My Drive."""
        return ROOT_ID

    def export_formats(self) -> dict[str, list[str]]:
        """The export map the real API would return."""
        return {
            "application/vnd.google-apps.document": [
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/pdf", "text/plain",
            ],
            "application/vnd.google-apps.spreadsheet": [
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "text/csv",
            ],
            "application/vnd.google-apps.presentation": [
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                "application/pdf",
            ],
            "application/vnd.google-apps.vids": ["video/mp4"],
        }

    def list_shared_drives(self) -> list:
        """Shared drives visible to the account."""
        return list(self.shared_drives)

    def iter_all_files(self, include_trashed=False, drive_id=None,
                       should_continue=None, on_page=None, page_size=1000):
        """Stream the fake corpus the way a paginated listing would."""
        for index, item in enumerate(self.files):
            if should_continue is not None and not should_continue():
                return
            if on_page is not None and index % page_size == 0:
                on_page(index // page_size + 1)
            if item.trashed and not include_trashed:
                continue
            if drive_id and item.drive_id != drive_id:
                continue
            if not drive_id and item.drive_id:
                continue
            yield item

    def get_file(self, file_id: str, fields: str = "") -> DriveFile:
        """Fetch one item's metadata."""
        for item in self.files:
            if item.id == file_id:
                return item
        raise KeyError(file_id)

    # -- content -----------------------------------------------------------
    def media_url(self, file_id: str, acknowledge_abuse: bool = False) -> str:
        """URL for binary content."""
        suffix = "&acknowledgeAbuse=true" if acknowledge_abuse else ""
        return f"https://fake/drive/v3/files/{file_id}?alt=media{suffix}"

    def export_url(self, file_id: str, mime_type: str) -> str:
        """URL for an export."""
        return (f"https://fake/drive/v3/files/{file_id}/export?"
                f"mimeType={urllib.parse.quote(mime_type)}")

    def open_stream(self, url: str, offset: int = 0, timeout=None) -> FakeResponse:
        """Serve bytes for a media or export URL, honouring ``Range``."""
        file_id = url.split("/files/")[1].split("?")[0].split("/")[0]
        with self._lock:
            self.requests.append((file_id, offset))
            queued = self.scripted.get(file_id)
            if queued:
                return queued.pop(0)

        if "/export?" in url:
            body = self.exports.get(file_id)
            if body is None:
                return FakeResponse(404, b'{"error": {"message": "not found"}}')
            return FakeResponse(200, body, {"Content-Length": str(len(body))})

        body = self.contents.get(file_id)
        if body is None:
            return FakeResponse(404, b'{"error": {"message": "not found"}}')
        if offset:
            if offset >= len(body):
                return FakeResponse(416, b"range not satisfiable")
            return FakeResponse(206, body[offset:],
                                {"Content-Range": f"bytes {offset}-{len(body) - 1}/{len(body)}"})
        return FakeResponse(200, body, {"Content-Length": str(len(body))})


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------
def make_folder(file_id: str, name: str, parent: str = ROOT_ID) -> DriveFile:
    """Build a folder resource."""
    return DriveFile(id=file_id, name=name, mime_type=FOLDER_MIME, parents=[parent],
                     modified_time="2026-01-01T00:00:00.000Z")


def make_file(
    file_id: str,
    name: str,
    parent: str = ROOT_ID,
    content: bytes = b"",
    mime_type: str = "application/octet-stream",
    modified: str = "2026-01-01T00:00:00.000Z",
    **kwargs,
) -> DriveFile:
    """Build a binary file resource whose checksum matches ``content``."""
    return DriveFile(
        id=file_id, name=name, mime_type=mime_type, parents=[parent],
        size=len(content), modified_time=modified,
        md5_checksum=hashlib.md5(content).hexdigest() if content else None,
        **kwargs,
    )


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
@pytest.fixture()
def db(tmp_path: Path) -> Database:
    """A fresh database on disk."""
    database = Database(tmp_path / "test.sqlite3")
    yield database
    database.close()


@pytest.fixture()
def repos(db: Database) -> Repositories:
    """Repositories bound to the temporary database."""
    return Repositories(db)


@pytest.fixture()
def account(repos: Repositories):
    """A stored account row."""
    return repos.accounts.upsert("tester@example.com", "Tester", 10**12, 10**9)


@pytest.fixture()
def destination(tmp_path: Path) -> str:
    """An empty backup destination directory."""
    target = tmp_path / "backup"
    target.mkdir()
    return str(target)


@pytest.fixture()
def settings(tmp_path: Path, destination: str) -> Settings:
    """Settings tuned for fast, deterministic tests."""
    cfg = Settings.load(tmp_path / "settings.json")
    cfg.destination = destination
    cfg.workers = 2
    cfg.retry_count = 3
    cfg.chunk_size = 1024
    cfg.verify_downloads = True
    return cfg
