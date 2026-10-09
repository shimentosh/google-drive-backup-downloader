"""End-to-end backup flows against a fake Drive: scan, download, rescan, resume."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from gdbd.database.schema import ItemStatus, SessionStatus
from gdbd.drive.exporters import ExportPlanner
from gdbd.drive.models import DriveFile
from gdbd.drive.scanner import DriveScanner
from gdbd.filesystem.storage import partial_path
from gdbd.services.backup_service import BackupRunner

from .conftest import ROOT_ID, FakeDriveClient, FakeResponse, make_file, make_folder


def scan(client, repos, account, settings, destination):
    """Run a full scan with the fake client."""
    planner = ExportPlanner(client.export_formats(), settings.export_map(),
                            settings.export_workspace)
    scanner = DriveScanner(client, repos.items, account.id, destination, planner,
                           include_trashed=settings.include_trashed,
                           include_shared_with_me=settings.include_shared_with_me)
    return scanner.scan()


def runner(client, repos, settings, account, destination) -> BackupRunner:
    """Build a backup runner."""
    return BackupRunner(client, repos, settings, account.id, destination)


def sample_drive():
    """A small Drive with a nested folder and three files."""
    contents = {
        "v1": b"video-one" * 200,
        "v2": b"video-two" * 300,
        "d1": b"document-bytes" * 50,
    }
    files = [
        make_folder("videos", "Videos"),
        make_folder("y2026", "2026", "videos"),
        make_file("v1", "clip1.mp4", "y2026", contents["v1"], "video/mp4"),
        make_file("v2", "clip2.mp4", "y2026", contents["v2"], "video/mp4"),
        make_file("d1", "notes.pdf", ROOT_ID, contents["d1"], "application/pdf"),
    ]
    # The client gets its own copy so a test can remove a file from the fake
    # Drive without also losing the reference bytes it asserts against.
    return FakeDriveClient(files, dict(contents)), contents


class TestFirstBackup:
    """A fresh destination gets every file, in the right place."""

    def test_full_backup(self, repos, account, settings, destination):
        client, contents = sample_drive()
        scan(client, repos, account, settings, destination)
        outcome = runner(client, repos, settings, account, destination).run()

        assert outcome.completed == 3
        assert outcome.failed == 0
        assert outcome.bytes_downloaded == sum(len(v) for v in contents.values())

        root = Path(destination)
        assert (root / "Videos" / "2026" / "clip1.mp4").read_bytes() == contents["v1"]
        assert (root / "Videos" / "2026" / "clip2.mp4").read_bytes() == contents["v2"]
        assert (root / "notes.pdf").read_bytes() == contents["d1"]

    def test_empty_folders_are_created(self, repos, account, settings, destination):
        client = FakeDriveClient([
            make_folder("empty", "Empty Folder"),
            make_folder("nested", "Inner", "empty"),
        ])
        scan(client, repos, account, settings, destination)
        runner(client, repos, settings, account, destination).run()
        assert (Path(destination) / "Empty Folder" / "Inner").is_dir()

    def test_session_is_recorded(self, repos, account, settings, destination):
        client, _ = sample_drive()
        scan(client, repos, account, settings, destination)
        runner(client, repos, settings, account, destination).run()

        session = repos.sessions.latest_finished(account.id)
        assert session is not None
        assert session.status == SessionStatus.COMPLETED
        assert session.completed_files == 3
        assert session.destination == destination
        assert session.duration_seconds is not None

    def test_events_are_logged(self, repos, account, settings, destination):
        client, _ = sample_drive()
        scan(client, repos, account, settings, destination)
        runner(client, repos, settings, account, destination).run()
        session = repos.sessions.latest_finished(account.id)
        events = {e["event"] for e in repos.sessions.events(session.id)}
        assert "backup.start" in events
        assert "backup.finish" in events


class TestIncrementalBackup:
    """Re-running against the same destination only fetches what changed."""

    def test_unchanged_files_are_skipped(self, repos, account, settings, destination):
        client, _ = sample_drive()
        scan(client, repos, account, settings, destination)
        runner(client, repos, settings, account, destination).run()

        client.requests.clear()
        scan(client, repos, account, settings, destination)
        second = runner(client, repos, settings, account, destination).run()

        assert second.completed == 0
        assert second.skipped == 0        # nothing was even queued
        assert client.requests == []

    def test_modified_file_is_downloaded_again(self, repos, account, settings, destination):
        client, contents = sample_drive()
        scan(client, repos, account, settings, destination)
        runner(client, repos, settings, account, destination).run()

        # The file changed in Drive: new bytes, new size, new timestamp.
        updated = b"brand new content" * 40
        client.files = [
            make_file("v1", "clip1.mp4", "y2026", updated, "video/mp4",
                      modified="2026-08-08T00:00:00.000Z")
            if item.id == "v1" else item
            for item in client.files
        ]
        client.contents["v1"] = updated

        scan(client, repos, account, settings, destination)
        second = runner(client, repos, settings, account, destination).run()

        assert second.completed == 1
        assert (Path(destination) / "Videos" / "2026" / "clip1.mp4").read_bytes() == updated

    def test_locally_deleted_file_is_fetched_again(
        self, repos, account, settings, destination
    ):
        client, contents = sample_drive()
        scan(client, repos, account, settings, destination)
        runner(client, repos, settings, account, destination).run()

        target = Path(destination) / "notes.pdf"
        target.unlink()
        # Re-queue it the way the retry action would.
        item = repos.items.get_by_file_id(account.id, destination, "d1")
        repos.items.set_status(item.id, ItemStatus.PENDING)

        second = runner(client, repos, settings, account, destination).run()
        assert second.completed == 1
        assert target.read_bytes() == contents["d1"]

    def test_new_file_is_added_without_touching_the_others(
        self, repos, account, settings, destination
    ):
        client, contents = sample_drive()
        scan(client, repos, account, settings, destination)
        runner(client, repos, settings, account, destination).run()

        extra = b"a new arrival" * 30
        client.files.append(make_file("v3", "clip3.mp4", "y2026", extra, "video/mp4"))
        client.contents["v3"] = extra

        scan(client, repos, account, settings, destination)
        client.requests.clear()
        second = runner(client, repos, settings, account, destination).run()

        assert second.completed == 1
        assert [file_id for file_id, _ in client.requests] == ["v3"]
        assert (Path(destination) / "Videos" / "2026" / "clip3.mp4").read_bytes() == extra

    def test_remote_deletion_never_deletes_the_local_copy(
        self, repos, account, settings, destination
    ):
        client, contents = sample_drive()
        scan(client, repos, account, settings, destination)
        runner(client, repos, settings, account, destination).run()

        target = Path(destination) / "notes.pdf"
        assert target.exists()

        client.files = [f for f in client.files if f.id != "d1"]
        result = scan(client, repos, account, settings, destination)
        runner(client, repos, settings, account, destination).run()

        assert result.removed_remotely == 1
        assert target.exists()          # the whole point: local data is never destroyed
        record = repos.items.get_by_file_id(account.id, destination, "d1")
        assert record.remote_deleted == 1


class TestFailureHandling:
    """One bad file must not spoil the run."""

    def test_failed_file_is_recorded_and_others_continue(
        self, repos, account, settings, destination
    ):
        client, contents = sample_drive()
        del client.contents["v2"]           # this one now 404s
        scan(client, repos, account, settings, destination)
        outcome = runner(client, repos, settings, account, destination).run()

        assert outcome.completed == 2
        assert outcome.failed == 1
        assert (Path(destination) / "notes.pdf").exists()

        failed = repos.items.failed_files(account.id, destination)
        assert len(failed) == 1
        assert failed[0].drive_file_id == "v2"
        assert "unavailable" in failed[0].error_message.lower()

    def test_retry_failed_downloads_them(self, repos, account, settings, destination):
        client, contents = sample_drive()
        del client.contents["v2"]
        scan(client, repos, account, settings, destination)
        runner(client, repos, settings, account, destination).run()

        client.contents["v2"] = contents["v2"]      # the file came back
        assert repos.items.retry_failed(account.id, destination) == 1
        second = runner(client, repos, settings, account, destination).run()

        assert second.completed == 1
        assert second.failed == 0
        assert repos.items.failed_files(account.id, destination) == []

    def test_unsupported_items_are_reported_not_retried(
        self, repos, account, settings, destination
    ):
        client = FakeDriveClient([
            DriveFile(id="form", name="Survey", parents=[ROOT_ID],
                      mime_type="application/vnd.google-apps.form"),
            make_file("v1", "clip.mp4", ROOT_ID, b"x" * 100, "video/mp4"),
        ], {"v1": b"x" * 100})
        scan(client, repos, account, settings, destination)
        outcome = runner(client, repos, settings, account, destination).run()

        assert outcome.completed == 1
        assert outcome.failed == 0
        record = repos.items.get_by_file_id(account.id, destination, "form")
        assert record.status == ItemStatus.UNSUPPORTED

    def test_session_reports_completed_with_errors(
        self, repos, account, settings, destination
    ):
        client, _ = sample_drive()
        del client.contents["v1"]
        scan(client, repos, account, settings, destination)
        runner(client, repos, settings, account, destination).run()
        session = repos.sessions.latest_finished(account.id)
        assert session.status == SessionStatus.COMPLETED_WITH_ERRORS
        assert session.failed_files == 1


class TestCrashRecovery:
    """State left by an interrupted run is picked up on the next launch."""

    def test_interrupted_item_resumes_from_its_partial_file(
        self, repos, account, settings, destination
    ):
        client, contents = sample_drive()
        scan(client, repos, account, settings, destination)

        # Simulate a crash mid-transfer: partial file plus DOWNLOADING status.
        item = repos.items.get_by_file_id(account.id, destination, "v1")
        part = partial_path(item.local_path)
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(contents["v1"][:900])
        repos.items.set_status(item.id, ItemStatus.DOWNLOADING, downloaded_bytes=900)

        client.requests.clear()
        outcome = runner(client, repos, settings, account, destination).run()

        assert outcome.completed == 3
        assert ("v1", 900) in client.requests        # a Range request was issued
        assert Path(item.local_path).read_bytes() == contents["v1"]

    def test_stale_sessions_are_closed(self, repos, account, settings, destination):
        client, _ = sample_drive()
        scan(client, repos, account, settings, destination)
        repos.sessions.create(account.id, destination, 3, 2, 100)   # never finished
        assert repos.sessions.close_stale() >= 1
        assert repos.sessions.latest_open(account.id) is None

    def test_plan_reports_previous_progress(self, repos, account, settings, destination):
        client, contents = sample_drive()
        scan(client, repos, account, settings, destination)
        runner(client, repos, settings, account, destination).run()

        plan = runner(client, repos, settings, account, destination).plan()
        assert plan.has_previous_backup
        assert plan.pending_files == 0
        assert plan.already_done_bytes == sum(len(v) for v in contents.values())


class TestPreflight:
    """The plan blocks impossible runs before a single byte moves."""

    def test_plan_totals(self, repos, account, settings, destination):
        client, contents = sample_drive()
        scan(client, repos, account, settings, destination)
        plan = runner(client, repos, settings, account, destination).plan()

        assert plan.total_files == 3
        assert plan.total_folders == 2
        assert plan.total_bytes == sum(len(v) for v in contents.values())
        assert plan.pending_files == 3
        assert plan.can_start
        assert plan.free_space > 0

    def test_missing_destination_is_reported(self, repos, account, settings, tmp_path):
        client, _ = sample_drive()
        missing = str(tmp_path / "not-created-yet")
        scan(client, repos, account, settings, missing)
        plan = runner(client, repos, settings, account, missing).plan()
        # The folder is created on demand, so this must succeed rather than fail.
        assert plan.writable
        assert Path(missing).is_dir()

    def test_insufficient_space_blocks_the_run(
        self, repos, account, settings, destination, monkeypatch
    ):
        client, _ = sample_drive()
        scan(client, repos, account, settings, destination)

        from gdbd.filesystem.storage import DiskSpace

        monkeypatch.setattr(
            "gdbd.services.backup_service.disk_space",
            lambda _p: DiskSpace(total=10**12, used=10**12, free=1024),
        )
        plan = runner(client, repos, settings, account, destination).plan()
        assert not plan.can_start
        assert "disk space" in plan.problems[0].lower()

    def test_unsupported_items_produce_a_warning(
        self, repos, account, settings, destination
    ):
        client = FakeDriveClient([
            DriveFile(id="form", name="Survey", parents=[ROOT_ID],
                      mime_type="application/vnd.google-apps.form"),
        ])
        scan(client, repos, account, settings, destination)
        plan = runner(client, repos, settings, account, destination).plan()
        assert any("unsupported" in w.lower() for w in plan.warnings)


class TestPauseResume:
    """Pausing keeps partial work; resuming continues it."""

    def test_pause_then_resume_completes_the_backup(
        self, repos, account, settings, destination
    ):
        contents = {f"f{i}": b"z" * 4000 for i in range(10)}
        files = [make_file(f"f{i}", f"file{i}.bin", ROOT_ID, contents[f"f{i}"])
                 for i in range(10)]
        started = threading.Event()

        class SlowClient(FakeDriveClient):
            def open_stream(self, url, offset=0, timeout=None):
                started.set()
                time.sleep(0.04)
                return super().open_stream(url, offset, timeout)

        client = SlowClient(files, contents)
        settings.workers = 2
        scan(client, repos, account, settings, destination)

        job = runner(client, repos, settings, account, destination)
        outcomes: list = []
        worker = threading.Thread(target=lambda: outcomes.append(job.run()), daemon=True)
        worker.start()

        # Pause once transfers are genuinely under way.
        started.wait(5.0)
        time.sleep(0.08)
        job.pause()

        # A paused run parks rather than exiting, so the thread is still alive
        # and no further files finish while it waits.
        time.sleep(0.25)
        assert worker.is_alive()
        paused_count = repos.items.status_counts(account.id, destination).get(
            ItemStatus.COMPLETED, 0
        )
        assert paused_count < 10

        job.resume()
        worker.join(30.0)
        assert not worker.is_alive()

        outcome = outcomes[0]
        assert outcome.completed + outcome.skipped == 10
        assert repos.items.status_counts(account.id, destination)[
            ItemStatus.COMPLETED
        ] == 10
        for index in range(10):
            assert (Path(destination) / f"file{index}.bin").read_bytes() == \
                contents[f"f{index}"]

    def test_cancelled_run_keeps_finished_files_and_can_be_resumed(
        self, repos, account, settings, destination
    ):
        contents = {f"f{i}": b"q" * 3000 for i in range(8)}
        files = [make_file(f"f{i}", f"c{i}.bin", ROOT_ID, contents[f"f{i}"])
                 for i in range(8)]
        started = threading.Event()

        class SlowClient(FakeDriveClient):
            def open_stream(self, url, offset=0, timeout=None):
                started.set()
                time.sleep(0.04)
                return super().open_stream(url, offset, timeout)

        client = SlowClient(files, contents)
        settings.workers = 2
        scan(client, repos, account, settings, destination)

        job = runner(client, repos, settings, account, destination)
        outcomes: list = []
        worker = threading.Thread(target=lambda: outcomes.append(job.run()), daemon=True)
        worker.start()
        started.wait(5.0)
        time.sleep(0.06)
        job.cancel()
        worker.join(30.0)

        assert outcomes[0].cancelled
        session = repos.sessions.get(job.session_id)
        assert session.status == SessionStatus.CANCELLED

        # Everything downloaded before the cancel is still on disk...
        finished = repos.items.status_counts(account.id, destination).get(
            ItemStatus.COMPLETED, 0
        )
        assert all((Path(destination) / f"c{i}.bin").exists()
                   for i in range(8)
                   if repos.items.get_by_file_id(account.id, destination,
                                                 f"f{i}").status
                   == ItemStatus.COMPLETED)
        # ...and a fresh run finishes the rest.
        second = runner(client, repos, settings, account, destination).run()
        assert finished + second.completed >= 8
        assert repos.items.status_counts(account.id, destination)[
            ItemStatus.COMPLETED
        ] == 8


class TestWorkspaceEndToEnd:
    """Google editor files are exported into real local documents."""

    def test_docs_sheets_and_slides(self, repos, account, settings, destination):
        client = FakeDriveClient(
            files=[
                DriveFile(id="doc", name="Report", parents=[ROOT_ID],
                          mime_type="application/vnd.google-apps.document"),
                DriveFile(id="sheet", name="Budget", parents=[ROOT_ID],
                          mime_type="application/vnd.google-apps.spreadsheet"),
                DriveFile(id="deck", name="Pitch", parents=[ROOT_ID],
                          mime_type="application/vnd.google-apps.presentation"),
            ],
            exports={"doc": b"docx-bytes", "sheet": b"xlsx-bytes", "deck": b"pptx-bytes"},
        )
        scan(client, repos, account, settings, destination)
        outcome = runner(client, repos, settings, account, destination).run()

        assert outcome.completed == 3
        root = Path(destination)
        assert (root / "Report.docx").read_bytes() == b"docx-bytes"
        assert (root / "Budget.xlsx").read_bytes() == b"xlsx-bytes"
        assert (root / "Pitch.pptx").read_bytes() == b"pptx-bytes"

    def test_pdf_export_preference_is_honoured(self, repos, account, settings, destination):
        settings.export_document = "application/pdf"
        client = FakeDriveClient(
            files=[DriveFile(id="doc", name="Report", parents=[ROOT_ID],
                             mime_type="application/vnd.google-apps.document")],
            exports={"doc": b"%PDF-1.7 fake"},
        )
        scan(client, repos, account, settings, destination)
        runner(client, repos, settings, account, destination).run()
        assert (Path(destination) / "Report.pdf").read_bytes() == b"%PDF-1.7 fake"


class TestUnicodeEndToEnd:
    """Non-ASCII names round-trip through scan, download and verification."""

    def test_bengali_and_emoji_filenames(self, repos, account, settings, destination):
        contents = {"a": "নথি".encode(), "b": b"photo-bytes"}
        client = FakeDriveClient(
            files=[
                make_folder("f", "বাংলা ফোল্ডার"),
                make_file("a", "গুরুত্বপূর্ণ নথি.pdf", "f", contents["a"],
                          "application/pdf"),
                make_file("b", "trip 🏔️.jpg", ROOT_ID, contents["b"], "image/jpeg"),
            ],
            contents=contents,
        )
        scan(client, repos, account, settings, destination)
        outcome = runner(client, repos, settings, account, destination).run()

        assert outcome.completed == 2
        root = Path(destination)
        assert (root / "বাংলা ফোল্ডার" / "গুরুত্বপূর্ণ নথি.pdf").read_bytes() == contents["a"]
        assert (root / "trip 🏔️.jpg").read_bytes() == contents["b"]

    def test_illegal_windows_name_is_backed_up_under_a_safe_name(
        self, repos, account, settings, destination
    ):
        content = b"final cut"
        client = FakeDriveClient(
            files=[make_file("a", "Project: Final / Version?.mp4", ROOT_ID, content,
                             "video/mp4")],
            contents={"a": content},
        )
        scan(client, repos, account, settings, destination)
        outcome = runner(client, repos, settings, account, destination).run()

        assert outcome.completed == 1
        assert (Path(destination) / "Project - Final - Version_.mp4").read_bytes() == content


class TestDuplicateNamesEndToEnd:
    """Two Drive files with one name become two distinct local files."""

    def test_duplicates_are_both_kept(self, repos, account, settings, destination):
        contents = {"a": b"first copy", "b": b"second copy"}
        client = FakeDriveClient(
            files=[
                make_file("a", "report.pdf", ROOT_ID, contents["a"], "application/pdf"),
                make_file("b", "report.pdf", ROOT_ID, contents["b"], "application/pdf"),
            ],
            contents=contents,
        )
        scan(client, repos, account, settings, destination)
        outcome = runner(client, repos, settings, account, destination).run()

        assert outcome.completed == 2
        root = Path(destination)
        written = {p.name: p.read_bytes() for p in root.glob("report*.pdf")}
        assert len(written) == 2
        assert set(written.values()) == {contents["a"], contents["b"]}
