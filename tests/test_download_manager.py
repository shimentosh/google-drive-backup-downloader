"""The download engine: streaming, resume, retry, verification, pause, cancel."""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path

import pytest

from gdbd.constants import PARTIAL_SUFFIX
from gdbd.database.repositories import ItemRepository
from gdbd.database.schema import ItemStatus
from gdbd.downloader.base import TransferControl
from gdbd.downloader.manager import DownloadManager
from gdbd.downloader.retry import RetryPolicy, classify_exception, classify_status
from gdbd.downloader.verifier import md5_of_file, verify_download
from gdbd.downloader.worker import ItemDownloader
from gdbd.filesystem.storage import partial_path
from gdbd.utils.backoff import backoff_delay

from .conftest import FakeResponse

# Retries in tests must not actually sleep for seconds.
FAST_POLICY = RetryPolicy(max_attempts=3)


def add_item(repos, account, destination, file_id="f1", name="a.mp4",
             content=b"", relative=None, strategy="binary", export_mime=None,
             md5=None):
    """Insert one scanned item and return its record."""
    relative = relative or name
    repos.items.bulk_upsert([ItemRepository.build_row(
        account.id, destination, drive_file_id=file_id, parent_id=None, name=name,
        mime_type="video/mp4", is_folder=False, size=len(content),
        modified_time="2026-01-01T00:00:00Z", created_time=None,
        md5_checksum=md5 if md5 is not None else (
            hashlib.md5(content).hexdigest() if content else None
        ),
        relative_path=relative,
        local_path=str(Path(destination) / relative.replace("/", "\\")),
        strategy=strategy, export_mime=export_mime, drive_id=None,
    )])
    return repos.items.get_by_file_id(account.id, destination, file_id)


def make_downloader(repos, control=None, **kwargs):
    """Build an :class:`ItemDownloader` with test-friendly defaults."""
    kwargs.setdefault("policy", FAST_POLICY)
    kwargs.setdefault("chunk_size", 1024)
    return ItemDownloader(repos.items, control or TransferControl(), **kwargs)


def run_one(repos, client, item, control=None, **kwargs):
    """Download one item and return the :class:`TaskResult`."""
    downloader = make_downloader(repos, control, **kwargs)
    return downloader.process(item, client, lambda _i, _n: None, lambda _n: None)


class TestSuccessfulDownload:
    """The happy path: bytes on disk, correct name, correct content."""

    def test_file_is_written_and_verified(self, repos, account, destination):
        content = b"hello world" * 500
        item = add_item(repos, account, destination, content=content)
        from .conftest import FakeDriveClient

        client = FakeDriveClient(contents={"f1": content})
        result = run_one(repos, client, item)

        assert result.status == ItemStatus.COMPLETED
        target = Path(item.local_path)
        assert target.read_bytes() == content
        assert repos.items.get(item.id).status == ItemStatus.COMPLETED

    def test_partial_file_is_removed_after_success(self, repos, account, destination):
        content = b"x" * 5000
        item = add_item(repos, account, destination, content=content)
        from .conftest import FakeDriveClient

        run_one(repos, FakeDriveClient(contents={"f1": content}), item)
        assert not partial_path(item.local_path).exists()

    def test_nested_directories_are_created(self, repos, account, destination):
        content = b"nested"
        item = add_item(repos, account, destination,
                        relative="Videos/YouTube/2026/clip.mp4", name="clip.mp4",
                        content=content)
        from .conftest import FakeDriveClient

        result = run_one(repos, FakeDriveClient(contents={"f1": content}), item)
        assert result.status == ItemStatus.COMPLETED
        assert (Path(destination) / "Videos" / "YouTube" / "2026" / "clip.mp4").exists()

    def test_large_file_is_streamed_in_chunks(self, repos, account, destination):
        content = bytes(range(256)) * 8192  # 2 MiB
        item = add_item(repos, account, destination, content=content)
        from .conftest import FakeDriveClient

        chunks: list[int] = []

        class CountingClient(FakeDriveClient):
            def open_stream(self, url, offset=0, timeout=None):
                response = super().open_stream(url, offset, timeout)
                original = response.iter_content

                def counting(chunk_size=8192):
                    for chunk in original(chunk_size):
                        chunks.append(len(chunk))
                        yield chunk

                response.iter_content = counting
                return response

        client = CountingClient(contents={"f1": content})
        result = run_one(repos, client, item, chunk_size=64 * 1024)
        assert result.status == ItemStatus.COMPLETED
        assert len(chunks) > 30                 # genuinely chunked
        assert max(chunks) <= 64 * 1024         # never one huge read
        assert Path(item.local_path).read_bytes() == content

    def test_progress_is_reported_monotonically(self, repos, account, destination):
        content = b"y" * 20_000
        item = add_item(repos, account, destination, content=content)
        from .conftest import FakeDriveClient

        seen: list[int] = []
        downloader = make_downloader(repos, chunk_size=1024)
        downloader.process(item, FakeDriveClient(contents={"f1": content}),
                           lambda _i, n: seen.append(n), lambda _n: None)
        assert seen == sorted(seen)
        assert seen[-1] == len(content)


class TestResume:
    """Interrupted transfers continue from the byte they stopped at."""

    def test_resumes_from_an_existing_partial_file(self, repos, account, destination):
        content = b"a" * 10_000
        item = add_item(repos, account, destination, content=content)
        part = partial_path(item.local_path)
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(content[:7300])

        from .conftest import FakeDriveClient

        client = FakeDriveClient(contents={"f1": content})
        result = run_one(repos, client, item)

        assert result.status == ItemStatus.COMPLETED
        assert client.requests == [("f1", 7300)]      # a Range request was made
        assert Path(item.local_path).read_bytes() == content

    def test_restarts_when_the_server_ignores_the_range_header(
        self, repos, account, destination
    ):
        content = b"b" * 4000
        item = add_item(repos, account, destination, content=content)
        part = partial_path(item.local_path)
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(b"garbage" * 100)

        from .conftest import FakeDriveClient

        client = FakeDriveClient(contents={"f1": content})
        # Server replies 200 with the whole body despite the Range header.
        client.scripted["f1"] = [FakeResponse(200, content)]
        result = run_one(repos, client, item)

        assert result.status == ItemStatus.COMPLETED
        assert Path(item.local_path).read_bytes() == content

    def test_oversized_partial_file_is_discarded(self, repos, account, destination):
        content = b"c" * 1000
        item = add_item(repos, account, destination, content=content)
        part = partial_path(item.local_path)
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(b"z" * 5000)

        from .conftest import FakeDriveClient

        client = FakeDriveClient(contents={"f1": content})
        result = run_one(repos, client, item)
        assert result.status == ItemStatus.COMPLETED
        assert Path(item.local_path).read_bytes() == content
        assert client.requests[0] == ("f1", 0)

    def test_complete_partial_file_is_verified_without_refetching(
        self, repos, account, destination
    ):
        content = b"d" * 2048
        item = add_item(repos, account, destination, content=content)
        part = partial_path(item.local_path)
        part.parent.mkdir(parents=True, exist_ok=True)
        part.write_bytes(content)

        from .conftest import FakeDriveClient

        client = FakeDriveClient(contents={"f1": content})
        result = run_one(repos, client, item)
        assert result.status == ItemStatus.COMPLETED
        assert client.requests == []      # nothing was downloaded again

    def test_dropped_connection_resumes_on_the_retry(self, repos, account, destination):
        content = b"e" * 8000
        item = add_item(repos, account, destination, content=content)
        from .conftest import FakeDriveClient

        client = FakeDriveClient(contents={"f1": content})
        # First attempt dies after 3000 bytes; the retry uses a Range request.
        client.scripted["f1"] = [FakeResponse(200, content, fail_after=3000)]
        result = run_one(repos, client, item, chunk_size=1000)

        assert result.status == ItemStatus.COMPLETED
        assert result.attempts == 2
        assert client.requests[1][1] == 3000
        assert Path(item.local_path).read_bytes() == content


class TestRetryAndFailure:
    """Transient errors retry with backoff; permanent errors do not."""

    def test_transient_server_error_is_retried(self, repos, account, destination):
        content = b"f" * 500
        item = add_item(repos, account, destination, content=content)
        from .conftest import FakeDriveClient

        client = FakeDriveClient(contents={"f1": content})
        client.scripted["f1"] = [FakeResponse(503, b"backend error")]
        result = run_one(repos, client, item)
        assert result.status == ItemStatus.COMPLETED
        assert result.attempts == 2

    def test_rate_limit_is_retried(self, repos, account, destination):
        content = b"g" * 500
        item = add_item(repos, account, destination, content=content)
        from .conftest import FakeDriveClient

        client = FakeDriveClient(contents={"f1": content})
        client.scripted["f1"] = [FakeResponse(429, b"rateLimitExceeded")]
        assert run_one(repos, client, item).status == ItemStatus.COMPLETED

    def test_permanent_error_is_not_retried(self, repos, account, destination):
        item = add_item(repos, account, destination, content=b"h" * 10)
        from .conftest import FakeDriveClient

        client = FakeDriveClient(contents={})     # 404 for everything
        result = run_one(repos, client, item)
        assert result.status == ItemStatus.FAILED
        assert result.attempts == 1
        assert "unavailable" in result.error.lower()

    def test_permission_denied_is_reported_clearly(self, repos, account, destination):
        item = add_item(repos, account, destination, content=b"i" * 10)
        from .conftest import FakeDriveClient

        client = FakeDriveClient(contents={"f1": b"i" * 10})
        client.scripted["f1"] = [FakeResponse(403, b'{"error":"forbidden"}')]
        result = run_one(repos, client, item)
        assert result.status == ItemStatus.FAILED
        assert "Permission denied" in result.error

    def test_retries_are_exhausted_then_the_item_fails(self, repos, account, destination):
        content = b"j" * 100
        item = add_item(repos, account, destination, content=content)
        from .conftest import FakeDriveClient

        client = FakeDriveClient(contents={"f1": content})
        client.scripted["f1"] = [FakeResponse(503, b"x") for _ in range(6)]
        result = run_one(repos, client, item, policy=RetryPolicy(max_attempts=2))
        assert result.status == ItemStatus.FAILED
        assert result.attempts == 2
        assert repos.items.get(item.id).retry_count == 2

    def test_abuse_flag_is_acknowledged_once(self, repos, account, destination):
        content = b"k" * 200
        item = add_item(repos, account, destination, content=content)
        from .conftest import FakeDriveClient

        client = FakeDriveClient(contents={"f1": content})
        client.scripted["f1"] = [FakeResponse(403, b'{"reason":"cannotDownloadAbusiveFile"}')]
        result = run_one(repos, client, item)
        assert result.status == ItemStatus.COMPLETED


class TestVerification:
    """Nothing is renamed to its final name until it verifies."""

    def test_checksum_mismatch_fails_verification(self, repos, account, destination):
        content = b"l" * 1000
        item = add_item(repos, account, destination, content=content,
                        md5="0" * 32)   # deliberately wrong
        from .conftest import FakeDriveClient

        client = FakeDriveClient(contents={"f1": content})
        result = run_one(repos, client, item, policy=RetryPolicy(max_attempts=1))
        assert result.status in (ItemStatus.VERIFY_FAILED, ItemStatus.FAILED)
        assert not Path(item.local_path).exists()

    def test_verification_failure_is_retried(self, repos, account, destination):
        content = b"m" * 1000
        item = add_item(repos, account, destination, content=content, md5="0" * 32)
        from .conftest import FakeDriveClient

        client = FakeDriveClient(contents={"f1": content})
        result = run_one(repos, client, item, policy=RetryPolicy(max_attempts=2))
        assert result.attempts == 2
        assert len(client.requests) == 2

    def test_truncated_response_fails_the_size_check(self, repos, account, destination):
        content = b"n" * 4000
        item = add_item(repos, account, destination, content=content)
        from .conftest import FakeDriveClient

        client = FakeDriveClient(contents={"f1": content})
        # Server returns only half the promised bytes, cleanly.
        client.scripted["f1"] = [FakeResponse(200, content[:2000])]
        result = run_one(repos, client, item, policy=RetryPolicy(max_attempts=1))
        assert result.status in (ItemStatus.VERIFY_FAILED, ItemStatus.FAILED)
        assert not Path(item.local_path).exists()

    def test_verify_download_helper(self, tmp_path: Path):
        target = tmp_path / "f.bin"
        target.write_bytes(b"content")
        digest = hashlib.md5(b"content").hexdigest()
        assert verify_download(target, 7, digest).ok
        assert not verify_download(target, 8, digest).ok
        assert not verify_download(target, 7, "0" * 32).ok
        assert md5_of_file(target) == digest


class TestExistingFiles:
    """Repeated backups skip what is already there."""

    def test_unchanged_file_is_skipped(self, repos, account, destination):
        content = b"o" * 300
        item = add_item(repos, account, destination, content=content)
        Path(item.local_path).parent.mkdir(parents=True, exist_ok=True)
        Path(item.local_path).write_bytes(content)

        from .conftest import FakeDriveClient

        client = FakeDriveClient(contents={"f1": content})
        result = run_one(repos, client, item)
        assert result.status == ItemStatus.SKIPPED
        assert client.requests == []

    def test_size_mismatch_is_downloaded_again(self, repos, account, destination):
        content = b"p" * 300
        item = add_item(repos, account, destination, content=content)
        Path(item.local_path).parent.mkdir(parents=True, exist_ok=True)
        Path(item.local_path).write_bytes(b"stale")

        from .conftest import FakeDriveClient

        client = FakeDriveClient(contents={"f1": content})
        result = run_one(repos, client, item)
        assert result.status == ItemStatus.COMPLETED
        assert Path(item.local_path).read_bytes() == content

    def test_replace_policy_always_downloads(self, repos, account, destination):
        content = b"q" * 300
        item = add_item(repos, account, destination, content=content)
        Path(item.local_path).parent.mkdir(parents=True, exist_ok=True)
        Path(item.local_path).write_bytes(content)

        from .conftest import FakeDriveClient

        client = FakeDriveClient(contents={"f1": content})
        result = run_one(repos, client, item, existing_policy="replace")
        assert result.status == ItemStatus.COMPLETED
        assert client.requests

    def test_ask_policy_consults_the_resolver(self, repos, account, destination):
        content = b"r" * 300
        item = add_item(repos, account, destination, content=content)
        Path(item.local_path).parent.mkdir(parents=True, exist_ok=True)
        Path(item.local_path).write_bytes(content)

        from .conftest import FakeDriveClient

        asked: list[str] = []

        def resolver(record):
            asked.append(record.name)
            return "replace"

        client = FakeDriveClient(contents={"f1": content})
        result = run_one(repos, client, item, existing_policy="ask",
                         conflict_resolver=resolver)
        assert asked == ["a.mp4"]
        assert result.status == ItemStatus.COMPLETED


class TestWorkspaceExport:
    """Google editor files go through ``files.export``."""

    def test_document_is_exported(self, repos, account, destination):
        item = add_item(repos, account, destination, name="My Doc.docx",
                        relative="My Doc.docx", strategy="export",
                        export_mime="application/vnd.openxmlformats-officedocument"
                                    ".wordprocessingml.document")
        from .conftest import FakeDriveClient

        client = FakeDriveClient(exports={"f1": b"PK\x03\x04 fake docx"})
        result = run_one(repos, client, item)
        assert result.status == ItemStatus.COMPLETED
        assert Path(item.local_path).read_bytes() == b"PK\x03\x04 fake docx"

    def test_export_without_a_format_is_unsupported(self, repos, account, destination):
        item = add_item(repos, account, destination, strategy="export")
        from .conftest import FakeDriveClient

        result = run_one(repos, FakeDriveClient(), item)
        assert result.status == ItemStatus.UNSUPPORTED

    def test_export_size_limit_is_explained(self, repos, account, destination):
        item = add_item(repos, account, destination, name="Huge.docx",
                        relative="Huge.docx", strategy="export",
                        export_mime="application/pdf")
        from .conftest import FakeDriveClient

        client = FakeDriveClient(
            files=[], exports={"f1": b"x"},
        )
        client.scripted["f1"] = [
            FakeResponse(403, b'{"reason":"exportSizeLimitExceeded"}')
        ]
        result = run_one(repos, client, item)
        assert result.status == ItemStatus.FAILED
        assert "too large" in result.error

    def test_empty_export_is_not_accepted(self, repos, account, destination):
        item = add_item(repos, account, destination, name="Doc.pdf",
                        relative="Doc.pdf", strategy="export",
                        export_mime="application/pdf")
        from .conftest import FakeDriveClient

        client = FakeDriveClient(exports={"f1": b""})
        result = run_one(repos, client, item, policy=RetryPolicy(max_attempts=1))
        assert result.status in (ItemStatus.VERIFY_FAILED, ItemStatus.FAILED)
        assert not Path(item.local_path).exists()


class TestPauseAndCancel:
    """The user can stop the engine at any time without losing progress."""

    def test_cancel_stops_a_transfer_and_keeps_the_partial_file(
        self, repos, account, destination
    ):
        content = b"s" * 50_000
        item = add_item(repos, account, destination, content=content)
        control = TransferControl()
        from .conftest import FakeDriveClient

        class SlowClient(FakeDriveClient):
            def open_stream(self, url, offset=0, timeout=None):
                response = super().open_stream(url, offset, timeout)
                original = response.iter_content

                def slow(chunk_size=8192):
                    for index, chunk in enumerate(original(chunk_size)):
                        if index == 2:
                            control.cancel_event.set()
                        yield chunk

                response.iter_content = slow
                return response

        result = run_one(repos, SlowClient(contents={"f1": content}), item,
                         control=control, chunk_size=1024)
        assert result.status == ItemStatus.CANCELLED
        assert partial_path(item.local_path).exists()
        assert not Path(item.local_path).exists()

    def test_pause_marks_the_item_paused(self, repos, account, destination):
        content = b"t" * 50_000
        item = add_item(repos, account, destination, content=content)
        control = TransferControl()
        from .conftest import FakeDriveClient

        class PausingClient(FakeDriveClient):
            def open_stream(self, url, offset=0, timeout=None):
                response = super().open_stream(url, offset, timeout)
                original = response.iter_content

                def pausing(chunk_size=8192):
                    for index, chunk in enumerate(original(chunk_size)):
                        if index == 3:
                            control.pause_event.set()
                        yield chunk

                response.iter_content = pausing
                return response

        result = run_one(repos, PausingClient(contents={"f1": content}), item,
                         control=control, chunk_size=1024)
        assert result.status == ItemStatus.PAUSED
        assert repos.items.get(item.id).status == ItemStatus.PAUSED
        assert partial_path(item.local_path).stat().st_size > 0


class TestManager:
    """Concurrency, aggregate progress and queue behaviour."""

    def make(self, repos, destination, client, **kwargs):
        """Build a manager for the fake client."""
        kwargs.setdefault("workers", 4)
        kwargs.setdefault("chunk_size", 1024)
        kwargs.setdefault("retry_count", 2)
        return DownloadManager(client, repos.items, destination, **kwargs)

    def test_downloads_every_queued_file(self, repos, account, destination):
        contents = {f"f{i}": bytes([i % 256]) * (500 + i) for i in range(12)}
        items = [add_item(repos, account, destination, f"f{i}", f"file{i}.bin",
                          contents[f"f{i}"], relative=f"file{i}.bin")
                 for i in range(12)]
        from .conftest import FakeDriveClient

        client = FakeDriveClient(contents=contents)
        total = sum(len(v) for v in contents.values())
        manager = self.make(repos, destination, client, total_bytes=total,
                            total_files=12)
        outcome = manager.run(items)

        assert outcome.completed == 12
        assert outcome.failed == 0
        assert outcome.bytes_downloaded == total
        for index in range(12):
            assert (Path(destination) / f"file{index}.bin").read_bytes() == \
                contents[f"f{index}"]

    def test_workers_run_in_parallel(self, repos, account, destination):
        contents = {f"f{i}": b"z" * 2000 for i in range(8)}
        items = [add_item(repos, account, destination, f"f{i}", f"f{i}.bin",
                          contents[f"f{i}"], relative=f"f{i}.bin") for i in range(8)]
        from .conftest import FakeDriveClient

        concurrent = 0
        peak = 0
        lock = threading.Lock()

        class SlowClient(FakeDriveClient):
            def open_stream(self, url, offset=0, timeout=None):
                nonlocal concurrent, peak
                with lock:
                    concurrent += 1
                    peak = max(peak, concurrent)
                time.sleep(0.03)
                with lock:
                    concurrent -= 1
                return super().open_stream(url, offset, timeout)

        manager = self.make(repos, destination, SlowClient(contents=contents),
                            workers=4)
        outcome = manager.run(items)
        assert outcome.completed == 8
        assert peak > 1

    def test_progress_snapshots_are_consistent(self, repos, account, destination):
        contents = {f"f{i}": b"p" * 4000 for i in range(6)}
        items = [add_item(repos, account, destination, f"f{i}", f"f{i}.bin",
                          contents[f"f{i}"], relative=f"f{i}.bin") for i in range(6)]
        from .conftest import FakeDriveClient

        snapshots = []
        total = sum(len(v) for v in contents.values())
        manager = self.make(
            repos, destination, FakeDriveClient(contents=contents),
            total_bytes=total, total_files=6,
            on_progress=snapshots.append,
        )
        manager.run(items)

        assert snapshots
        final = snapshots[-1]
        assert final.finished_files == 6
        assert final.done_bytes == total
        assert final.byte_fraction == pytest.approx(1.0)
        # The overall fraction must never run backwards.
        fractions = [s.byte_fraction for s in snapshots]
        assert fractions == sorted(fractions)

    def test_one_failure_does_not_stop_the_run(self, repos, account, destination):
        contents = {"f0": b"a" * 100, "f2": b"c" * 100}
        items = [
            add_item(repos, account, destination, "f0", "a.bin", b"a" * 100,
                     relative="a.bin"),
            add_item(repos, account, destination, "f1", "b.bin", b"b" * 100,
                     relative="b.bin"),      # missing from the fake Drive -> 404
            add_item(repos, account, destination, "f2", "c.bin", b"c" * 100,
                     relative="c.bin"),
        ]
        from .conftest import FakeDriveClient

        manager = self.make(repos, destination, FakeDriveClient(contents=contents),
                            workers=2)
        outcome = manager.run(items)
        assert outcome.completed == 2
        assert outcome.failed == 1
        assert (Path(destination) / "a.bin").exists()
        assert (Path(destination) / "c.bin").exists()

    def test_cancel_stops_the_queue(self, repos, account, destination):
        contents = {f"f{i}": b"x" * 3000 for i in range(20)}
        items = [add_item(repos, account, destination, f"f{i}", f"f{i}.bin",
                          contents[f"f{i}"], relative=f"f{i}.bin") for i in range(20)]
        from .conftest import FakeDriveClient

        started = threading.Event()

        class SlowClient(FakeDriveClient):
            """Each transfer takes long enough for a cancel to land mid-queue."""

            def open_stream(self, url, offset=0, timeout=None):
                started.set()
                time.sleep(0.05)
                return super().open_stream(url, offset, timeout)

        manager = self.make(repos, destination, SlowClient(contents=contents),
                            workers=2)

        def cancel_when_running():
            started.wait(5.0)
            time.sleep(0.05)
            manager.cancel()

        canceller = threading.Thread(target=cancel_when_running, daemon=True)
        canceller.start()
        outcome = manager.run(items)
        canceller.join(5.0)

        assert outcome.cancelled is True
        assert outcome.completed < 20
        # Nothing already downloaded is thrown away.
        assert repos.items.status_counts(account.id, destination).get(
            ItemStatus.COMPLETED, 0
        ) == outcome.completed

    def test_insufficient_space_stops_the_run(self, repos, account, destination):
        content = b"y" * 5000
        items = [add_item(repos, account, destination, "f1", "big.bin", content,
                          relative="big.bin")]
        from .conftest import FakeDriveClient

        manager = self.make(repos, destination, FakeDriveClient(contents={"f1": content}))
        manager._space_free = 0
        manager._space_checked = time.monotonic() + 10_000  # freeze the cache
        outcome = manager.run(items)
        assert outcome.failed == 1
        assert outcome.fatal_error and "space" in outcome.fatal_error.lower()

    def test_finished_callback_fires_per_item(self, repos, account, destination):
        contents = {f"f{i}": b"w" * 200 for i in range(4)}
        items = [add_item(repos, account, destination, f"f{i}", f"f{i}.bin",
                          contents[f"f{i}"], relative=f"f{i}.bin") for i in range(4)]
        from .conftest import FakeDriveClient

        seen = []
        manager = self.make(repos, destination, FakeDriveClient(contents=contents),
                            on_item_finished=seen.append)
        manager.run(items)
        assert len(seen) == 4
        assert all(r.status == ItemStatus.COMPLETED for r in seen)


class TestErrorClassification:
    """HTTP statuses and exceptions map to actionable messages."""

    @pytest.mark.parametrize("status,retryable", [
        (429, True), (500, True), (502, True), (503, True), (504, True),
        (403, False), (404, False), (401, False),
    ])
    def test_status_retryability(self, status, retryable):
        assert classify_status(status, "").retryable is retryable

    def test_rate_limited_403_is_retryable(self):
        assert classify_status(403, '{"reason":"rateLimitExceeded"}').retryable

    def test_disk_full_is_detected(self):
        error = OSError(28, "No space left on device")
        error.errno = 28
        assert "disk space" in classify_exception(error).message.lower()

    def test_network_errors_are_retryable(self):
        import requests.exceptions as rex

        assert classify_exception(rex.ConnectionError("dns")).retryable
        assert classify_exception(rex.ReadTimeout("slow")).retryable
        assert classify_exception(rex.ChunkedEncodingError("reset")).retryable

    def test_backoff_grows_and_is_capped(self):
        delays = [backoff_delay(attempt, jitter=False) for attempt in range(1, 9)]
        assert delays[:4] == [2.0, 4.0, 8.0, 16.0]
        assert max(delays) <= 64.0
        assert delays == sorted(delays)

    def test_backoff_jitter_stays_in_range(self):
        for _ in range(50):
            assert 1.6 <= backoff_delay(1) <= 2.4
