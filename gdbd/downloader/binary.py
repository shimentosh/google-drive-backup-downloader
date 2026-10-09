"""Streaming download of ordinary binary files (``files.get?alt=media``).

Bytes go straight from the HTTP response to disk in fixed-size chunks, so a
40 GB video costs the same memory as a 40 KB one. ``Range`` requests let an
interrupted transfer continue from the byte it stopped at.
"""

from __future__ import annotations

from pathlib import Path

from ..database.models import DriveItemRecord
from ..database.schema import ItemStatus
from ..filesystem.paths import long_path
from ..filesystem.storage import finalize, set_modified_time
from ..utils.logging import get_logger
from ..utils.timeutil import to_epoch
from . import resume
from .base import DownloadContext, DownloadOutcome, DownloadStrategy, Interrupted
from .retry import DownloadError, classify_exception, classify_status
from .verifier import verify_download

log = get_logger(__name__)


class BinaryMediaDownloader(DownloadStrategy):
    """Downloads any Drive item that has real stored bytes."""

    name = "binary"

    @property
    def supports_resume(self) -> bool:
        """Range requests are supported for stored content."""
        return True

    def download(
        self, item: DriveItemRecord, target: Path, context: DownloadContext
    ) -> DownloadOutcome:
        """Stream ``item`` into ``target``, resuming a partial file if present."""
        expected = item.total_bytes or item.size or None
        state = resume.prepare(target, expected)
        written = state.offset

        # The file already looks complete on disk - go straight to verification.
        if expected and written >= expected:
            return self._finish(item, target, state.part_file, written, context)

        context.on_progress(written)
        acknowledge_abuse = False

        for attempt in (1, 2):
            try:
                written = self._transfer(
                    item, state.part_file, written, context, acknowledge_abuse
                )
                break
            except DownloadError as exc:
                if exc.reason == "cannotDownloadAbusiveFile" and attempt == 1:
                    # Drive requires an explicit acknowledgement for files it has
                    # flagged; the user asked for a full backup, so send it once.
                    log.warning("Retrying %s with abuse acknowledgement", item.name)
                    acknowledge_abuse = True
                    continue
                if exc.reason == "rangeNotSatisfiable" and attempt == 1 and written:
                    log.warning("Resume offset rejected for %s; restarting", item.name)
                    resume.discard(target)
                    written = 0
                    context.on_progress(0)
                    continue
                raise

        return self._finish(item, target, state.part_file, written, context)

    # -- internals ---------------------------------------------------------
    def _transfer(
        self,
        item: DriveItemRecord,
        part_file: Path,
        offset: int,
        context: DownloadContext,
        acknowledge_abuse: bool,
    ) -> int:
        url = context.client.media_url(item.drive_file_id, acknowledge_abuse)
        written = offset
        response = None
        try:
            response = context.client.open_stream(url, offset=offset)
            status = response.status_code

            if status not in (200, 206):
                body = _safe_body(response)
                raise classify_status(status, body)

            if offset > 0 and status == 200:
                # The server ignored our Range header and is resending the whole
                # file; start over so the two halves cannot be spliced together.
                log.info("Server restarted %s from byte 0", item.name)
                written = 0
                context.on_progress(0)

            mode = "r+b" if written > 0 else "wb"
            remaining = (item.total_bytes or 0) - written
            if remaining > 0:
                context.ensure_space(remaining)

            with open(long_path(part_file), mode) as handle:
                if written > 0:
                    handle.seek(written)
                    handle.truncate(written)
                for chunk in response.iter_content(chunk_size=context.chunk_size):
                    context.control.check()
                    if not chunk:
                        continue
                    handle.write(chunk)
                    written += len(chunk)
                    context.on_progress(written)
        except Interrupted:
            raise
        except DownloadError:
            raise
        except BaseException as exc:  # noqa: BLE001 - normalised immediately
            raise classify_exception(exc) from exc
        finally:
            if response is not None:
                response.close()
        return written

    def _finish(
        self,
        item: DriveItemRecord,
        target: Path,
        part_file: Path,
        written: int,
        context: DownloadContext,
    ) -> DownloadOutcome:
        expected = item.total_bytes or item.size or None
        result = verify_download(
            part_file, expected, item.md5_checksum, check_checksum=context.verify
        )
        if not result.ok:
            return DownloadOutcome(
                ItemStatus.VERIFY_FAILED, bytes_written=written, error=result.reason
            )
        try:
            finalize(part_file, target)
            set_modified_time(target, to_epoch(item.modified_time))
        except OSError as exc:
            raise classify_exception(exc) from exc
        return DownloadOutcome(
            ItemStatus.COMPLETED, bytes_written=written, checksum=result.checksum
        )


def _safe_body(response, limit: int = 2000) -> str:
    """Read a small error body without risking a huge or hung read."""
    try:
        return response.text[:limit]
    except Exception:  # pragma: no cover - defensive
        return ""
