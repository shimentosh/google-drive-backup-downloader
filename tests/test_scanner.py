"""Full-Drive scanning: hierarchy reconstruction, planning and statistics."""

from __future__ import annotations

from gdbd.database.repositories import Repositories
from gdbd.database.schema import ItemStatus
from gdbd.drive.exporters import (
    STRATEGY_BINARY,
    STRATEGY_EXPORT,
    STRATEGY_SHORTCUT,
    STRATEGY_UNSUPPORTED,
    ExportPlanner,
)
from gdbd.drive.models import DriveFile
from gdbd.drive.scanner import DriveScanner, create_local_folders

from .conftest import ROOT_ID, FakeDriveClient, make_file, make_folder


def build_scanner(client, repos, account, destination, **kwargs):
    """Create a scanner wired to the fake client."""
    planner = ExportPlanner(
        client.export_formats(),
        preferred={
            "application/vnd.google-apps.document":
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.google-apps.spreadsheet":
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        },
        export_workspace=kwargs.pop("export_workspace", True),
    )
    return DriveScanner(client, repos.items, account.id, destination, planner, **kwargs)


def nested_drive() -> FakeDriveClient:
    """The example hierarchy from the specification."""
    files = [
        make_folder("videos", "Videos"),
        make_folder("youtube", "YouTube", "videos"),
        make_folder("y2025", "2025", "youtube"),
        make_folder("y2026", "2026", "youtube"),
        make_folder("courses", "Courses", "videos"),
        make_folder("projects_v", "Projects", "videos"),
        make_folder("documents", "Documents"),
        make_folder("contracts", "Contracts", "documents"),
        make_folder("reports", "Reports", "documents"),
        make_folder("images", "Images"),
        make_folder("photos", "Photos", "images"),
        make_folder("projects", "Projects"),
        make_file("v1", "video1.mp4", "y2025", b"a" * 100, "video/mp4"),
        make_file("v2", "video2.mp4", "y2025", b"b" * 200, "video/mp4"),
        make_file("v3", "video3.mp4", "y2026", b"c" * 300, "video/mp4"),
        make_file("c1", "course.mp4", "courses", b"d" * 50, "video/mp4"),
    ]
    contents = {"v1": b"a" * 100, "v2": b"b" * 200, "v3": b"c" * 300, "c1": b"d" * 50}
    return FakeDriveClient(files, contents)


class TestHierarchy:
    """The Drive folder tree is mirrored exactly."""

    def test_paths_match_the_drive_structure(self, repos, account, destination):
        client = nested_drive()
        result = build_scanner(client, repos, account, destination).scan()

        assert result.folders == 12
        assert result.files == 4
        paths = {
            item.drive_file_id: item.relative_path
            for item in repos.items.pending_files(account.id, destination)
        }
        assert paths["v1"] == "Videos/YouTube/2025/video1.mp4"
        assert paths["v2"] == "Videos/YouTube/2025/video2.mp4"
        assert paths["v3"] == "Videos/YouTube/2026/video3.mp4"
        assert paths["c1"] == "Videos/Courses/course.mp4"

    def test_folders_are_recorded_including_empty_ones(self, repos, account, destination):
        client = nested_drive()
        build_scanner(client, repos, account, destination).scan()
        folder_paths = {f.relative_path for f in repos.items.folders(account.id, destination)}
        assert "Documents/Contracts" in folder_paths
        assert "Images/Photos" in folder_paths
        assert "Projects" in folder_paths          # top-level, empty
        assert "Videos/Projects" in folder_paths   # same name, different parent

    def test_local_folders_are_created(self, repos, account, destination, tmp_path):
        client = nested_drive()
        build_scanner(client, repos, account, destination).scan()
        folders = [f.relative_path for f in repos.items.folders(account.id, destination)]
        created = create_local_folders(destination, folders)
        assert created == 12
        from pathlib import Path

        assert (Path(destination) / "Videos" / "YouTube" / "2026").is_dir()
        assert (Path(destination) / "Documents" / "Contracts").is_dir()

    def test_deeply_nested_structure(self, repos, account, destination):
        files = []
        parent = ROOT_ID
        for depth in range(25):
            folder_id = f"d{depth}"
            files.append(make_folder(folder_id, f"level{depth}", parent))
            parent = folder_id
        files.append(make_file("deep", "deep.txt", parent, b"x"))
        client = FakeDriveClient(files, {"deep": b"x"})
        build_scanner(client, repos, account, destination).scan()
        item = repos.items.get_by_file_id(account.id, destination, "deep")
        assert item.relative_path.count("/") == 25
        assert item.relative_path.startswith("level0/level1/level2/")

    def test_total_bytes_are_summed(self, repos, account, destination):
        result = build_scanner(nested_drive(), repos, account, destination).scan()
        assert result.total_bytes == 650


class TestNamingConflicts:
    """Drive allows duplicate names; the local layout must stay unambiguous."""

    def test_duplicate_names_in_one_folder(self, repos, account, destination):
        client = FakeDriveClient([
            make_file("a", "clip.mp4", ROOT_ID, b"1", "video/mp4"),
            make_file("b", "clip.mp4", ROOT_ID, b"22", "video/mp4"),
            make_file("c", "clip.mp4", ROOT_ID, b"333", "video/mp4"),
        ])
        build_scanner(client, repos, account, destination).scan()
        paths = sorted(i.relative_path for i in
                       repos.items.pending_files(account.id, destination))
        assert paths == ["clip (1).mp4", "clip (2).mp4", "clip.mp4"]

    def test_illegal_names_are_sanitized_and_logged(self, repos, account, destination):
        client = FakeDriveClient([
            make_file("a", "Project: Final / Version?.mp4", ROOT_ID, b"x", "video/mp4"),
        ])
        result = build_scanner(client, repos, account, destination).scan()
        item = repos.items.get_by_file_id(account.id, destination, "a")
        assert item.relative_path == "Project - Final - Version_.mp4"
        assert result.renamed_files == 1
        mapping = repos.db.query_one(
            "SELECT * FROM name_mappings WHERE drive_file_id = 'a'"
        )
        assert mapping["original_name"] == "Project: Final / Version?.mp4"
        assert mapping["local_name"] == "Project - Final - Version_.mp4"

    def test_unicode_names_survive_the_scan(self, repos, account, destination):
        client = FakeDriveClient([
            make_file("a", "বাংলা ভিডিও.mp4", ROOT_ID, b"x", "video/mp4"),
            make_file("b", "写真 🌸.jpg", ROOT_ID, b"y", "image/jpeg"),
        ])
        build_scanner(client, repos, account, destination).scan()
        names = {i.relative_path for i in repos.items.pending_files(account.id, destination)}
        assert "বাংলা ভিডিও.mp4" in names
        assert "写真 🌸.jpg" in names

    def test_scan_is_deterministic(self, repos, account, destination):
        client = nested_drive()
        build_scanner(client, repos, account, destination).scan()
        first = {i.drive_file_id: i.relative_path
                 for i in repos.items.pending_files(account.id, destination)}
        build_scanner(nested_drive(), repos, account, destination).scan()
        second = {i.drive_file_id: i.relative_path
                  for i in repos.items.pending_files(account.id, destination)}
        assert first == second


class TestDownloadPlanning:
    """Each item is assigned the correct download strategy during the scan."""

    def test_binary_files_use_the_media_strategy(self, repos, account, destination):
        client = FakeDriveClient([make_file("a", "a.mp4", ROOT_ID, b"x", "video/mp4")])
        build_scanner(client, repos, account, destination).scan()
        assert repos.items.get_by_file_id(account.id, destination, "a").strategy == \
            STRATEGY_BINARY

    def test_google_docs_are_planned_as_exports(self, repos, account, destination):
        client = FakeDriveClient([
            DriveFile(id="doc", name="My Doc", parents=[ROOT_ID],
                      mime_type="application/vnd.google-apps.document"),
        ])
        result = build_scanner(client, repos, account, destination).scan()
        item = repos.items.get_by_file_id(account.id, destination, "doc")
        assert item.strategy == STRATEGY_EXPORT
        assert item.export_mime.endswith("wordprocessingml.document")
        assert item.relative_path == "My Doc.docx"
        assert result.export_files == 1

    def test_sheets_export_to_xlsx(self, repos, account, destination):
        client = FakeDriveClient([
            DriveFile(id="s", name="Budget", parents=[ROOT_ID],
                      mime_type="application/vnd.google-apps.spreadsheet"),
        ])
        build_scanner(client, repos, account, destination).scan()
        assert repos.items.get_by_file_id(account.id, destination, "s").relative_path == \
            "Budget.xlsx"

    def test_forms_are_marked_unsupported_with_a_reason(self, repos, account, destination):
        client = FakeDriveClient([
            DriveFile(id="form", name="Survey", parents=[ROOT_ID],
                      mime_type="application/vnd.google-apps.form"),
        ])
        result = build_scanner(client, repos, account, destination).scan()
        item = repos.items.get_by_file_id(account.id, destination, "form")
        assert item.strategy == STRATEGY_UNSUPPORTED
        assert item.status == ItemStatus.UNSUPPORTED
        assert "Forms" in (item.error_message or "")
        assert result.unsupported_files == 1
        # Unsupported items never enter the download queue.
        assert not any(i.drive_file_id == "form"
                       for i in repos.items.pending_files(account.id, destination))

    def test_google_vids_export_to_mp4(self, repos, account, destination):
        client = FakeDriveClient([
            DriveFile(id="vid", name="Team Update", parents=[ROOT_ID],
                      mime_type="application/vnd.google-apps.vids"),
        ])
        build_scanner(client, repos, account, destination).scan()
        item = repos.items.get_by_file_id(account.id, destination, "vid")
        assert item.strategy == STRATEGY_EXPORT
        assert item.export_mime == "video/mp4"
        assert item.relative_path == "Team Update.mp4"

    def test_shortcuts_are_skipped(self, repos, account, destination):
        client = FakeDriveClient([
            DriveFile(id="sc", name="Link to video", parents=[ROOT_ID],
                      mime_type="application/vnd.google-apps.shortcut",
                      shortcut_target_id="v1"),
        ])
        result = build_scanner(client, repos, account, destination).scan()
        item = repos.items.get_by_file_id(account.id, destination, "sc")
        assert item.strategy == STRATEGY_SHORTCUT
        assert item.status == ItemStatus.SKIPPED
        assert result.shortcut_files == 1

    def test_undownloadable_file_is_reported(self, repos, account, destination):
        client = FakeDriveClient([
            make_file("x", "locked.pdf", ROOT_ID, b"x", "application/pdf",
                      can_download=False),
        ])
        build_scanner(client, repos, account, destination).scan()
        item = repos.items.get_by_file_id(account.id, destination, "x")
        assert item.strategy == STRATEGY_UNSUPPORTED
        assert "disabled downloading" in item.error_message

    def test_export_can_be_disabled(self, repos, account, destination):
        client = FakeDriveClient([
            DriveFile(id="doc", name="My Doc", parents=[ROOT_ID],
                      mime_type="application/vnd.google-apps.document"),
        ])
        build_scanner(client, repos, account, destination,
                      export_workspace=False).scan()
        item = repos.items.get_by_file_id(account.id, destination, "doc")
        assert item.strategy == STRATEGY_UNSUPPORTED
        assert "turned off" in item.error_message


class TestScopeAndFilters:
    """Trash, shared-with-me and shared drives are opt-in."""

    def test_trashed_files_are_excluded_by_default(self, repos, account, destination):
        client = FakeDriveClient([
            make_file("a", "keep.mp4", ROOT_ID, b"x", "video/mp4"),
            make_file("b", "gone.mp4", ROOT_ID, b"y", "video/mp4", trashed=True),
        ])
        result = build_scanner(client, repos, account, destination).scan()
        assert result.files == 1

    def test_trashed_files_can_be_included(self, repos, account, destination):
        client = FakeDriveClient([
            make_file("a", "keep.mp4", ROOT_ID, b"x", "video/mp4"),
            make_file("b", "gone.mp4", ROOT_ID, b"y", "video/mp4", trashed=True),
        ])
        result = build_scanner(client, repos, account, destination,
                               include_trashed=True).scan()
        assert result.files == 2

    def test_shared_items_land_in_a_virtual_folder(self, repos, account, destination):
        client = FakeDriveClient([
            make_file("s", "shared.pdf", "someone-elses-folder", b"x",
                      "application/pdf", owned_by_me=False),
        ])
        build_scanner(client, repos, account, destination,
                      include_shared_with_me=True).scan()
        item = repos.items.get_by_file_id(account.id, destination, "s")
        assert item.relative_path == "Shared with me/shared.pdf"

    def test_shared_items_are_excluded_by_default(self, repos, account, destination):
        client = FakeDriveClient([
            make_file("s", "shared.pdf", "someone-elses-folder", b"x",
                      "application/pdf", owned_by_me=False),
        ])
        result = build_scanner(client, repos, account, destination).scan()
        assert result.files == 0

    def test_owned_only_skips_other_peoples_files_in_my_folders(
        self, repos, account, destination
    ):
        client = FakeDriveClient([
            make_folder("mine", "Mine"),
            make_file("own", "own.zip", "mine", b"x", "application/zip"),
            make_file("theirs", "theirs.rar", "mine", b"yy", "application/rar",
                      owned_by_me=False),
        ])
        result = build_scanner(client, repos, account, destination,
                               owned_only=True).scan()
        assert result.files == 1
        pending = {i.drive_file_id for i in repos.items.pending_files(account.id, destination)}
        assert pending == {"own"}


class TestProgressAndCancellation:
    """Scanning reports progress and stops promptly when cancelled."""

    def test_progress_callback_reports_counts(self, repos, account, destination):
        seen: list[tuple[int, int]] = []
        client = nested_drive()
        scanner = build_scanner(client, repos, account, destination)
        scanner.scan(lambda p: seen.append((p.files, p.folders)))
        assert seen
        assert seen[-1][0] == 4
        assert seen[-1][1] == 12

    def test_cancel_stops_the_scan(self, repos, account, destination):
        client = nested_drive()
        scanner = build_scanner(client, repos, account, destination)
        scanner.cancel()
        result = scanner.scan()
        assert result.cancelled is True

    def test_staging_table_is_dropped_afterwards(self, repos, account, destination):
        build_scanner(nested_drive(), repos, account, destination).scan()
        remaining = repos.db.query_one(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='scan_staging'"
        )
        assert remaining is None


class TestCategories:
    """Statistics group files the way the dashboard shows them."""

    def test_category_breakdown(self, repos, account, destination):
        client = FakeDriveClient([
            make_file("v", "a.mp4", ROOT_ID, b"x" * 1000, "video/mp4"),
            make_file("i", "b.jpg", ROOT_ID, b"y" * 500, "image/jpeg"),
            make_file("d", "c.pdf", ROOT_ID, b"z" * 200, "application/pdf"),
            make_file("z", "d.zip", ROOT_ID, b"w" * 100, "application/zip"),
        ])
        result = build_scanner(client, repos, account, destination).scan()
        assert result.category_bytes["Videos"] == 1000
        assert result.category_bytes["Images"] == 500
        assert result.category_bytes["Documents"] == 200
        assert result.category_bytes["Archives"] == 100
        assert result.category_counts["Videos"] == 1
