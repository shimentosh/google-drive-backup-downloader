"""Formatting, time helpers, settings persistence and log redaction."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from gdbd.config.settings import (
    EXISTING_SKIP,
    AppContext,
    AppPaths,
    Settings,
    client_config_from_env,
    credentials_search_paths,
)
from gdbd.constants import MAX_CHUNK_SIZE, MAX_WORKERS, MIN_CHUNK_SIZE
from gdbd.drive.models import (
    CATEGORY_ARCHIVE,
    CATEGORY_DOCUMENT,
    CATEGORY_IMAGE,
    CATEGORY_OTHER,
    CATEGORY_VIDEO,
    DriveFile,
    classify,
)
from gdbd.filesystem.storage import disk_space, is_writable, partial_path
from gdbd.utils.formatting import (
    format_bytes,
    format_count,
    format_duration,
    format_eta,
    format_speed,
    percent,
    truncate_middle,
)
from gdbd.utils.logging import RedactingFilter
from gdbd.utils.timeutil import format_relative_day, parse_rfc3339, to_epoch


class TestFormatting:
    """Numbers shown to the user."""

    @pytest.mark.parametrize("value,expected", [
        (0, "0 B"), (512, "512 B"), (1024, "1.0 KB"), (1536, "1.5 KB"),
        (1024 ** 2, "1.0 MB"), (1024 ** 3, "1.0 GB"),
        (int(684.7 * 1024 ** 3), "684.7 GB"), (1024 ** 4, "1.0 TB"),
    ])
    def test_format_bytes(self, value, expected):
        assert format_bytes(value) == expected

    def test_format_bytes_handles_none(self):
        assert format_bytes(None) == "-"

    def test_format_speed(self):
        assert format_speed(18.4 * 1024 ** 2) == "18.4 MB/s"
        assert format_speed(0) == "-"

    @pytest.mark.parametrize("seconds,expected", [
        (45, "45s"), (750, "12m 30s"), (4080, "1h 08m"), (0, "0s"),
    ])
    def test_format_duration(self, seconds, expected):
        assert format_duration(seconds) == expected

    def test_format_eta(self):
        assert format_eta(1000, 100) == "10s"
        assert format_eta(1000, 0) == "-"

    def test_format_count(self):
        assert format_count(12483) == "12,483"

    def test_percent_is_clamped(self):
        assert percent(50, 100) == 50.0
        assert percent(150, 100) == 100.0
        assert percent(1, 0) == 0.0

    def test_truncate_middle(self):
        assert truncate_middle("short", 20) == "short"
        result = truncate_middle("a" * 100, 20)
        assert len(result) == 20 and "..." in result


class TestTime:
    """RFC 3339 parsing matches what the Drive API returns."""

    def test_parse_drive_timestamp(self):
        parsed = parse_rfc3339("2026-09-06T20:42:11.123Z")
        assert parsed == datetime(2026, 9, 6, 20, 42, 11, 123000, tzinfo=timezone.utc)

    def test_parse_offset_timestamp(self):
        assert parse_rfc3339("2026-09-06T20:42:11+02:00").hour == 18

    def test_parse_invalid_returns_none(self):
        assert parse_rfc3339("not a date") is None
        assert parse_rfc3339(None) is None

    def test_to_epoch_round_trip(self):
        epoch = to_epoch("2026-09-06T00:00:00Z")
        assert epoch == datetime(2026, 9, 6, tzinfo=timezone.utc).timestamp()

    def test_relative_day_for_never(self):
        assert format_relative_day(None) == "Never"


class TestClassification:
    """MIME types map to the dashboard's categories."""

    @pytest.mark.parametrize("mime,category", [
        ("video/mp4", CATEGORY_VIDEO),
        ("video/x-matroska", CATEGORY_VIDEO),
        ("image/jpeg", CATEGORY_IMAGE),
        ("image/webp", CATEGORY_IMAGE),
        ("application/pdf", CATEGORY_DOCUMENT),
        ("text/csv", CATEGORY_DOCUMENT),
        ("application/vnd.google-apps.document", CATEGORY_DOCUMENT),
        ("application/zip", CATEGORY_ARCHIVE),
        ("application/x-7z-compressed", CATEGORY_ARCHIVE),
        ("application/octet-stream", CATEGORY_OTHER),
        ("image/vnd.adobe.photoshop", CATEGORY_IMAGE),
    ])
    def test_classify(self, mime, category):
        assert classify(mime) == category

    def test_drive_file_from_api(self):
        item = DriveFile.from_api({
            "id": "abc", "name": "clip.mp4", "mimeType": "video/mp4",
            "size": "1048576", "parents": ["p1"], "md5Checksum": "deadbeef",
            "modifiedTime": "2026-01-01T00:00:00.000Z",
            "capabilities": {"canDownload": True},
        })
        assert item.size == 1048576
        assert item.category == CATEGORY_VIDEO
        assert not item.is_folder

    def test_drive_file_tolerates_missing_size(self):
        item = DriveFile.from_api({"id": "a", "name": "Doc",
                                   "mimeType": "application/vnd.google-apps.document"})
        assert item.size == 0
        assert item.is_google_app


class TestSettings:
    """Preferences round-trip and are clamped to safe ranges."""

    def test_defaults(self, tmp_path: Path):
        settings = Settings.load(tmp_path / "s.json")
        assert settings.workers == 4
        assert settings.existing_files == EXISTING_SKIP
        assert settings.verify_downloads is True

    def test_round_trip(self, tmp_path: Path):
        path = tmp_path / "s.json"
        first = Settings.load(path)
        first.update(destination="D:/Backup", workers=8, theme="dark")
        second = Settings.load(path)
        assert second.destination == "D:/Backup"
        assert second.workers == 8
        assert second.theme == "dark"

    def test_out_of_range_values_are_clamped(self, tmp_path: Path):
        settings = Settings.load(tmp_path / "s.json")
        settings.workers = 999
        settings.chunk_size = 1
        settings.retry_count = -5
        settings.theme = "neon"
        settings.normalize()
        assert settings.workers == MAX_WORKERS
        assert settings.chunk_size == MIN_CHUNK_SIZE
        assert settings.retry_count == 0
        assert settings.theme == "system"

    def test_huge_chunk_size_is_capped(self, tmp_path: Path):
        settings = Settings.load(tmp_path / "s.json")
        settings.chunk_size = 10 ** 12
        settings.normalize()
        assert settings.chunk_size == MAX_CHUNK_SIZE

    def test_corrupt_file_falls_back_to_defaults(self, tmp_path: Path):
        path = tmp_path / "s.json"
        path.write_text("{ this is not json", encoding="utf-8")
        assert Settings.load(path).workers == 4

    def test_unknown_keys_are_ignored(self, tmp_path: Path):
        path = tmp_path / "s.json"
        path.write_text('{"workers": 6, "made_up": true}', encoding="utf-8")
        settings = Settings.load(path)
        assert settings.workers == 6
        assert not hasattr(settings, "made_up")

    def test_export_map(self, tmp_path: Path):
        settings = Settings.load(tmp_path / "s.json")
        mapping = settings.export_map()
        assert mapping["application/vnd.google-apps.document"].endswith(
            "wordprocessingml.document"
        )
        assert mapping["application/vnd.google-apps.spreadsheet"].endswith(
            "spreadsheetml.sheet"
        )


class TestAppPaths:
    """The application's on-disk layout."""

    def test_paths_are_derived_from_the_data_dir(self, tmp_path: Path):
        paths = AppPaths(tmp_path).ensure()
        assert paths.database.parent == tmp_path
        assert paths.logs.is_dir()
        assert paths.settings_file.name == "settings.json"

    def test_context_creates_everything(self, tmp_path: Path):
        context = AppContext.create(tmp_path / "data")
        assert context.paths.data_dir.is_dir()
        assert context.paths.logs.is_dir()
        assert isinstance(context.settings, Settings)

    def test_credentials_search_order(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("GDBD_CREDENTIALS_FILE", raising=False)
        paths = AppPaths(tmp_path)
        candidates = credentials_search_paths(paths)
        assert candidates[0] == paths.default_credentials
        assert all(p.name == "credentials.json" for p in candidates)

    def test_env_credentials_take_priority(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("GDBD_CREDENTIALS_FILE", str(tmp_path / "custom.json"))
        assert credentials_search_paths(AppPaths(tmp_path))[0].name == "custom.json"

    def test_client_config_from_env(self, monkeypatch):
        monkeypatch.setenv("GDBD_CLIENT_ID", "id.apps.googleusercontent.com")
        monkeypatch.setenv("GDBD_CLIENT_SECRET", "secret")
        config = client_config_from_env()
        assert config["installed"]["client_id"] == "id.apps.googleusercontent.com"
        assert config["installed"]["token_uri"].startswith("https://")

    def test_client_config_needs_both_variables(self, monkeypatch):
        monkeypatch.setenv("GDBD_CLIENT_ID", "id")
        monkeypatch.delenv("GDBD_CLIENT_SECRET", raising=False)
        assert client_config_from_env() is None


class TestStorage:
    """Destination checks."""

    def test_writable_directory(self, tmp_path: Path):
        ok, reason = is_writable(tmp_path)
        assert ok and reason == ""

    def test_missing_directory_is_created(self, tmp_path: Path):
        target = tmp_path / "new" / "deeper"
        ok, _ = is_writable(target)
        assert ok and target.is_dir()

    def test_file_as_destination_is_rejected(self, tmp_path: Path):
        target = tmp_path / "afile"
        target.write_text("x", encoding="utf-8")
        ok, reason = is_writable(target)
        assert not ok and reason

    def test_disk_space_reports_positive_values(self, tmp_path: Path):
        space = disk_space(tmp_path)
        assert space.total > 0 and space.free >= 0

    def test_disk_space_walks_up_to_an_existing_parent(self, tmp_path: Path):
        assert disk_space(tmp_path / "does" / "not" / "exist").total > 0

    def test_partial_path_naming(self):
        assert str(partial_path("D:/b/video.mp4")).endswith("video.mp4.gdbd.part")


class TestRedaction:
    """Credentials must never reach a log file."""

    @pytest.mark.parametrize("text", [
        'access_token: ya29.a0AfH6SMBexample',
        '{"refresh_token": "1//04abcDEF_ghi"}',
        "client_secret=GOCSPX-verysecretvalue",
        "Authorization: Bearer ya29.token.value",
        '"id_token": "eyJhbGciOi.payload.sig"',
    ])
    def test_secrets_are_masked(self, text):
        redacted = RedactingFilter.redact(text)
        assert "REDACTED" in redacted
        for secret in ("ya29.a0AfH6SMBexample", "1//04abcDEF_ghi",
                       "GOCSPX-verysecretvalue", "eyJhbGciOi.payload.sig"):
            assert secret not in redacted

    def test_ordinary_text_is_untouched(self):
        message = "Downloaded Videos/2026/clip.mp4 (1048576 bytes)"
        assert RedactingFilter.redact(message) == message
