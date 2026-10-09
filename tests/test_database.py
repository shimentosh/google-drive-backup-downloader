"""Database behaviour: items, incremental re-scan semantics, sessions, resume."""

from __future__ import annotations

import threading

from gdbd.database.repositories import ItemRepository, Repositories, normalize_destination
from gdbd.database.schema import ItemStatus, SessionStatus


def build(account_id, destination, file_id="f1", name="a.mp4", size=100,
          modified="2026-01-01T00:00:00Z", md5="abc", relative="a.mp4",
          strategy="binary", folder=False, export_mime=None, parent=None):
    """Convenience wrapper around :meth:`ItemRepository.build_row`."""
    return ItemRepository.build_row(
        account_id, destination, drive_file_id=file_id, parent_id=parent, name=name,
        mime_type="video/mp4", is_folder=folder, size=size, modified_time=modified,
        created_time=None, md5_checksum=md5, relative_path=relative,
        local_path=f"{destination}\\{relative}", strategy=strategy,
        export_mime=export_mime, drive_id=None,
    )


class TestItemInsert:
    """Scanned items land in the database with the right defaults."""

    def test_insert_and_read_back(self, repos: Repositories, account, destination):
        repos.items.bulk_upsert([build(account.id, destination)])
        item = repos.items.get_by_file_id(account.id, destination, "f1")
        assert item is not None
        assert item.name == "a.mp4"
        assert item.status == ItemStatus.PENDING
        assert item.total_bytes == 100
        assert item.downloaded_bytes == 0

    def test_destination_is_normalised(self, repos: Repositories, account):
        repos.items.bulk_upsert([build(account.id, "D:/Backup")])
        assert repos.items.get_by_file_id(account.id, "D:\\backup\\", "f1") is not None

    def test_same_file_in_two_destinations_is_independent(
        self, repos: Repositories, account
    ):
        repos.items.bulk_upsert([build(account.id, "D:/one")])
        repos.items.bulk_upsert([build(account.id, "D:/two")])
        first = repos.items.get_by_file_id(account.id, "D:/one", "f1")
        repos.items.set_status(first.id, ItemStatus.COMPLETED)
        assert repos.items.get_by_file_id(account.id, "D:/two", "f1").status == \
            ItemStatus.PENDING

    def test_bulk_insert_of_many_rows(self, repos: Repositories, account, destination):
        rows = [build(account.id, destination, f"f{i}", f"n{i}.mp4", relative=f"n{i}.mp4")
                for i in range(500)]
        repos.items.bulk_upsert(rows)
        assert repos.items.totals(account.id, destination)["files"] == 500


class TestIncrementalRescan:
    """A second scan must not undo work the first one completed."""

    def test_unchanged_file_keeps_completed_status(
        self, repos: Repositories, account, destination
    ):
        row = build(account.id, destination)
        repos.items.bulk_upsert([row])
        item = repos.items.get_by_file_id(account.id, destination, "f1")
        repos.items.set_status(item.id, ItemStatus.COMPLETED, downloaded_bytes=100)

        repos.items.bulk_upsert([row])
        again = repos.items.get_by_file_id(account.id, destination, "f1")
        assert again.status == ItemStatus.COMPLETED
        assert again.downloaded_bytes == 100

    def test_changed_modified_time_resets_to_pending(
        self, repos: Repositories, account, destination
    ):
        repos.items.bulk_upsert([build(account.id, destination)])
        item = repos.items.get_by_file_id(account.id, destination, "f1")
        repos.items.set_status(item.id, ItemStatus.COMPLETED, downloaded_bytes=100)

        repos.items.bulk_upsert([build(account.id, destination,
                                       modified="2026-06-06T00:00:00Z")])
        again = repos.items.get_by_file_id(account.id, destination, "f1")
        assert again.status == ItemStatus.PENDING
        assert again.downloaded_bytes == 0

    def test_changed_size_resets_to_pending(self, repos: Repositories, account, destination):
        repos.items.bulk_upsert([build(account.id, destination)])
        item = repos.items.get_by_file_id(account.id, destination, "f1")
        repos.items.set_status(item.id, ItemStatus.COMPLETED)
        repos.items.bulk_upsert([build(account.id, destination, size=200)])
        assert repos.items.get_by_file_id(account.id, destination, "f1").status == \
            ItemStatus.PENDING

    def test_changed_checksum_resets_to_pending(
        self, repos: Repositories, account, destination
    ):
        repos.items.bulk_upsert([build(account.id, destination)])
        item = repos.items.get_by_file_id(account.id, destination, "f1")
        repos.items.set_status(item.id, ItemStatus.COMPLETED)
        repos.items.bulk_upsert([build(account.id, destination, md5="zzz")])
        assert repos.items.get_by_file_id(account.id, destination, "f1").status == \
            ItemStatus.PENDING

    def test_rename_resets_because_the_local_path_changed(
        self, repos: Repositories, account, destination
    ):
        repos.items.bulk_upsert([build(account.id, destination)])
        item = repos.items.get_by_file_id(account.id, destination, "f1")
        repos.items.set_status(item.id, ItemStatus.COMPLETED)
        repos.items.bulk_upsert([build(account.id, destination, name="b.mp4",
                                       relative="b.mp4")])
        assert repos.items.get_by_file_id(account.id, destination, "f1").status == \
            ItemStatus.PENDING

    def test_items_missing_from_a_scan_are_flagged_not_deleted(
        self, repos: Repositories, account, destination
    ):
        repos.items.bulk_upsert([
            build(account.id, destination, "f1", relative="a.mp4"),
            build(account.id, destination, "f2", name="b.mp4", relative="b.mp4"),
        ])
        later = "2027-01-01T00:00:00+00:00"
        repos.items.bulk_upsert([
            ItemRepository.build_row(
                account.id, destination, drive_file_id="f1", parent_id=None,
                name="a.mp4", mime_type="video/mp4", is_folder=False, size=100,
                modified_time="2026-01-01T00:00:00Z", created_time=None,
                md5_checksum="abc", relative_path="a.mp4",
                local_path=f"{destination}\\a.mp4", strategy="binary",
                export_mime=None, drive_id=None, stamp=later,
            )
        ])
        flagged = repos.items.mark_missing_as_deleted(account.id, destination, later)
        assert flagged == 1
        gone = repos.items.get_by_file_id(account.id, destination, "f2")
        assert gone is not None and gone.remote_deleted == 1
        assert len(repos.items.pending_files(account.id, destination)) == 1


class TestStatusTransitions:
    """Status updates, retries and interruption recovery."""

    def test_set_status_records_error_and_retry_count(
        self, repos: Repositories, account, destination
    ):
        repos.items.bulk_upsert([build(account.id, destination)])
        item = repos.items.get_by_file_id(account.id, destination, "f1")
        repos.items.set_status(item.id, ItemStatus.FAILED, error="Network timeout",
                               retry_count=3)
        again = repos.items.get(item.id)
        assert again.status == ItemStatus.FAILED
        assert again.error_message == "Network timeout"
        assert again.retry_count == 3

    def test_success_clears_a_previous_error(self, repos: Repositories, account, destination):
        repos.items.bulk_upsert([build(account.id, destination)])
        item = repos.items.get_by_file_id(account.id, destination, "f1")
        repos.items.set_status(item.id, ItemStatus.FAILED, error="boom")
        repos.items.set_status(item.id, ItemStatus.COMPLETED)
        assert repos.items.get(item.id).error_message is None

    def test_progress_is_persisted_for_resume(self, repos: Repositories, account, destination):
        repos.items.bulk_upsert([build(account.id, destination, size=10_000)])
        item = repos.items.get_by_file_id(account.id, destination, "f1")
        repos.items.set_progress(item.id, 7_300)
        assert repos.items.get(item.id).downloaded_bytes == 7_300

    def test_reset_interrupted_moves_downloading_to_paused(
        self, repos: Repositories, account, destination
    ):
        repos.items.bulk_upsert([build(account.id, destination, size=10_000)])
        item = repos.items.get_by_file_id(account.id, destination, "f1")
        repos.items.set_status(item.id, ItemStatus.DOWNLOADING, downloaded_bytes=4_000)
        assert repos.items.reset_interrupted(account.id, destination) == 1
        again = repos.items.get(item.id)
        assert again.status == ItemStatus.PAUSED
        assert again.downloaded_bytes == 4_000  # resume state survives

    def test_retry_failed_requeues_and_clears_errors(
        self, repos: Repositories, account, destination
    ):
        repos.items.bulk_upsert([build(account.id, destination)])
        item = repos.items.get_by_file_id(account.id, destination, "f1")
        repos.items.set_status(item.id, ItemStatus.FAILED, error="boom", retry_count=5)
        assert repos.items.retry_failed(account.id, destination) == 1
        again = repos.items.get(item.id)
        assert (again.status, again.retry_count, again.error_message) == \
            (ItemStatus.PENDING, 0, None)

    def test_completed_items_leave_the_pending_queue(
        self, repos: Repositories, account, destination
    ):
        repos.items.bulk_upsert([build(account.id, destination)])
        item = repos.items.get_by_file_id(account.id, destination, "f1")
        repos.items.set_status(item.id, ItemStatus.COMPLETED)
        assert repos.items.pending_files(account.id, destination) == []

    def test_failed_items_can_be_excluded_from_the_queue(
        self, repos: Repositories, account, destination
    ):
        repos.items.bulk_upsert([build(account.id, destination)])
        item = repos.items.get_by_file_id(account.id, destination, "f1")
        repos.items.set_status(item.id, ItemStatus.FAILED, error="x")
        assert repos.items.pending_files(account.id, destination, include_failed=False) == []
        assert len(repos.items.pending_files(account.id, destination,
                                             include_failed=True)) == 1


class TestAggregates:
    """Totals feed the dashboard and the pre-flight report."""

    def test_totals(self, repos: Repositories, account, destination):
        repos.items.bulk_upsert([
            build(account.id, destination, "d1", "Videos", folder=True, size=0,
                  relative="Videos"),
            build(account.id, destination, "f1", size=100, relative="a.mp4"),
            build(account.id, destination, "f2", size=250, relative="b.mp4"),
            build(account.id, destination, "f3", size=0, relative="doc.docx",
                  strategy="export", export_mime="application/pdf"),
        ])
        totals = repos.items.totals(account.id, destination)
        assert totals["files"] == 3
        assert totals["folders"] == 1
        assert totals["total_bytes"] == 350
        assert totals["export_files"] == 1

    def test_done_bytes_track_completed_items(self, repos: Repositories, account, destination):
        repos.items.bulk_upsert([
            build(account.id, destination, "f1", size=100, relative="a.mp4"),
            build(account.id, destination, "f2", size=250, relative="b.mp4"),
        ])
        first = repos.items.get_by_file_id(account.id, destination, "f1")
        repos.items.set_status(first.id, ItemStatus.COMPLETED)
        assert repos.items.totals(account.id, destination)["done_bytes"] == 100

    def test_remaining_bytes_accounts_for_partial_downloads(
        self, repos: Repositories, account, destination
    ):
        repos.items.bulk_upsert([build(account.id, destination, size=1000)])
        item = repos.items.get_by_file_id(account.id, destination, "f1")
        repos.items.set_progress(item.id, 400)
        assert repos.items.remaining_bytes(account.id, destination) == 600

    def test_status_counts(self, repos: Repositories, account, destination):
        repos.items.bulk_upsert([
            build(account.id, destination, f"f{i}", relative=f"a{i}.mp4")
            for i in range(3)
        ])
        first = repos.items.get_by_file_id(account.id, destination, "f0")
        repos.items.set_status(first.id, ItemStatus.COMPLETED)
        counts = repos.items.status_counts(account.id, destination)
        assert counts[ItemStatus.COMPLETED] == 1
        assert counts[ItemStatus.PENDING] == 2

    def test_search_by_name(self, repos: Repositories, account, destination):
        repos.items.bulk_upsert([
            build(account.id, destination, "f1", name="holiday.mp4", relative="holiday.mp4"),
            build(account.id, destination, "f2", name="invoice.pdf", relative="invoice.pdf"),
        ])
        assert len(repos.items.search(account.id, destination, "holiday")) == 1

    def test_search_by_status(self, repos: Repositories, account, destination):
        repos.items.bulk_upsert([build(account.id, destination)])
        item = repos.items.get_by_file_id(account.id, destination, "f1")
        repos.items.set_status(item.id, ItemStatus.FAILED, error="x")
        assert len(repos.items.search(account.id, destination,
                                      status=ItemStatus.FAILED)) == 1
        assert repos.items.search(account.id, destination,
                                  status=ItemStatus.COMPLETED) == []


class TestSessions:
    """Backup sessions record history and survive crashes."""

    def test_create_and_finish(self, repos: Repositories, account, destination):
        session = repos.sessions.create(account.id, destination, 10, 2, 5000)
        assert session.status == SessionStatus.RUNNING
        repos.sessions.update_progress(session.id, completed_files=8, completed_bytes=4000,
                                       skipped_files=1, failed_files=1,
                                       unsupported_files=0)
        repos.sessions.finish(session.id, SessionStatus.COMPLETED_WITH_ERRORS, 120, "done")
        again = repos.sessions.get(session.id)
        assert again.status == SessionStatus.COMPLETED_WITH_ERRORS
        assert again.completed_files == 8
        assert again.successful_files == 9
        assert again.duration_seconds == 120
        assert again.finished_at is not None

    def test_stale_sessions_are_closed_on_next_launch(
        self, repos: Repositories, account, destination
    ):
        repos.sessions.create(account.id, destination, 1, 0, 1)
        assert repos.sessions.close_stale() == 1
        assert repos.sessions.latest_open(account.id) is None
        assert repos.sessions.latest_finished(account.id).status == SessionStatus.FAILED

    def test_events_are_recorded_and_filtered(self, repos: Repositories, account, destination):
        session = repos.sessions.create(account.id, destination, 1, 0, 1)
        repos.sessions.add_event(session.id, "backup.start", "started")
        repos.sessions.add_event(session.id, "item.failed", "boom", level="ERROR")
        events = repos.sessions.events(session.id)
        assert len(events) == 2
        assert any(event["level"] == "ERROR" for event in events)

    def test_tasks_are_upserted_per_item(self, repos: Repositories, account, destination):
        repos.items.bulk_upsert([build(account.id, destination)])
        item = repos.items.get_by_file_id(account.id, destination, "f1")
        session = repos.sessions.create(account.id, destination, 1, 0, 100)
        repos.sessions.upsert_task(session.id, item.id, ItemStatus.DOWNLOADING, 50, 1)
        repos.sessions.upsert_task(session.id, item.id, ItemStatus.COMPLETED, 100, 1,
                                   finished=True)
        tasks = repos.sessions.session_tasks(session.id)
        assert len(tasks) == 1
        assert tasks[0]["status"] == ItemStatus.COMPLETED
        assert tasks[0]["bytes_moved"] == 100

    def test_history_is_newest_first(self, repos: Repositories, account, destination):
        first = repos.sessions.create(account.id, destination, 1, 0, 1)
        second = repos.sessions.create(account.id, destination, 1, 0, 1)
        listed = repos.sessions.list(account.id)
        assert [s.id for s in listed] == [second.id, first.id]


class TestConcurrency:
    """Several worker threads write progress at once."""

    def test_parallel_status_updates(self, repos: Repositories, account, destination):
        rows = [build(account.id, destination, f"f{i}", relative=f"a{i}.mp4")
                for i in range(40)]
        repos.items.bulk_upsert(rows)
        items = repos.items.pending_files(account.id, destination)
        errors: list[BaseException] = []

        def worker(subset):
            try:
                for item in subset:
                    repos.items.set_progress(item.id, 10)
                    repos.items.set_status(item.id, ItemStatus.COMPLETED,
                                           downloaded_bytes=100)
            except BaseException as exc:  # pragma: no cover - failure path
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(items[i::4],)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert not errors
        counts = repos.items.status_counts(account.id, destination)
        assert counts[ItemStatus.COMPLETED] == 40


def test_normalize_destination():
    """Destination keys ignore case, separators and trailing slashes."""
    assert normalize_destination("D:/Backup/") == normalize_destination("D:\\backup")
    assert normalize_destination("D:\\Backup\\\\") == "d:\\backup"


def test_settings_kv_table(repos: Repositories):
    """The settings table stores small application values."""
    repos.db.set_setting("last_page", "overview")
    assert repos.db.get_setting("last_page") == "overview"
    repos.db.set_setting("last_page", "backup")
    assert repos.db.get_setting("last_page") == "backup"
    assert repos.db.get_setting("missing", "fallback") == "fallback"
