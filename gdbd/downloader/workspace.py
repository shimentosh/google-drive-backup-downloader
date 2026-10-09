"""Export of native Google Workspace files (Docs, Sheets, Slides, ...).

These items have no stored bytes, so ``alt=media`` does not apply; Drive
generates a converted representation on demand via ``files.export``. Generated
content cannot be resumed - there is no stable byte range to continue from - so
an interrupted export restarts, which is cheap because exports are small.

The ``files.export`` endpoint refuses documents whose export would exceed 10 MB.
When that happens the item's own ``exportLinks`` URL is tried, which Google
serves without that limit for large documents.
"""

from __future__ import annotations

from pathlib import Path

from ..database.models import DriveItemRecord
from ..database.schema import ItemStatus
from ..filesystem.paths import long_path
from ..filesystem.storage import finalize, remove, set_modified_time
from ..utils.logging import get_logger
from ..utils.timeutil import to_epoch
from . import resume
from .base import DownloadContext, DownloadOutcome, DownloadStrategy, Interrupted
from .retry import DownloadError, classify_exception, classify_status

log = get_logger(__name__)


class WorkspaceExportDownloader(DownloadStrategy):
    """Converts a Google editor file into a real local document."""

    name = "export"

    @property
    def supports_resume(self) -> bool:
        """Exports are generated on demand and always restart."""
        return False

    def download(
        self, item: DriveItemRecord, target: Path, context: DownloadContext
    ) -> DownloadOutcome:
        """Export ``item`` to ``target`` in the configured format."""
        if not item.export_mime:
            return DownloadOutcome(
                ItemStatus.UNSUPPORTED,
                error="No export format was selected for this file type.",
            )

        # Generated content has no resumable byte range: always start clean.
        state = resume.prepare(target, None, allow_resume=False)
        remove(state.part_file)
        context.on_progress(0)

        url = context.client.export_url(item.drive_file_id, item.export_mime)
        try:
            written = self._stream(url, state.part_file, context)
        except DownloadError as exc:
            if exc.reason != "exportSizeLimitExceeded":
                raise
            log.info("Export of %s exceeds the API limit; trying exportLinks", item.name)
            fallback = self._export_link(item, context)
            if not fallback:
                return DownloadOutcome(
                    ItemStatus.FAILED,
                    error=(
                        "Export not supported: this document is too large for the "
                        "Drive export API and Google offers no direct export link "
                        "for it. Choose a different export format in Settings "
                        "(PDF is often smaller) or download it manually."
                    ),
                )
            remove(state.part_file)
            written = self._stream(fallback, state.part_file, context)

        if written <= 0:
            return DownloadOutcome(
                ItemStatus.VERIFY_FAILED, error="The export returned no content."
            )

        try:
            finalize(state.part_file, target)
            set_modified_time(target, to_epoch(item.modified_time))
        except OSError as exc:
            raise classify_exception(exc) from exc
        return DownloadOutcome(ItemStatus.COMPLETED, bytes_written=written)

    # -- internals ---------------------------------------------------------
    def _stream(self, url: str, part_file: Path, context: DownloadContext) -> int:
        written = 0
        response = None
        try:
            response = context.client.open_stream(url, offset=0)
            if response.status_code not in (200, 206):
                raise classify_status(response.status_code, _safe_body(response))

            declared = response.headers.get("Content-Length")
            if declared:
                try:
                    context.ensure_space(int(declared))
                except ValueError:
                    pass

            with open(long_path(part_file), "wb") as handle:
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

    def _export_link(self, item: DriveItemRecord, context: DownloadContext) -> str | None:
        """Fetch the item's own export URL for the configured MIME type."""
        try:
            fresh = context.client.get_file(item.drive_file_id, fields="id,exportLinks")
        except Exception as exc:  # pragma: no cover - network dependent
            log.debug("Could not read exportLinks for %s: %s", item.name, exc)
            return None
        return fresh.export_links.get(item.export_mime or "")


def _safe_body(response, limit: int = 2000) -> str:
    """Read a small error body without risking a huge or hung read."""
    try:
        return response.text[:limit]
    except Exception:  # pragma: no cover - defensive
        return ""
