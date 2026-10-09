"""Full-Drive scanning: discovery, hierarchy reconstruction and path assignment.

The scan runs in three stages:

1. **Stream** every item from ``files.list`` in one paginated pass. Folders are
   kept in memory (there are comparatively few); files go straight into an
   on-disk staging table so a Drive with a million files does not have to fit
   in RAM.
2. **Resolve** each folder's path by walking its ``parents`` chain up to
   "My Drive" (or to a shared drive root), assigning a sanitized, de-duplicated
   local folder name.
3. **Assign** each file a local path under its parent and upsert it into
   ``drive_items``, preserving the resume state of anything unchanged.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Callable, Iterable

from ..constants import (
    MY_DRIVE_FOLDER,
    SHARED_WITH_ME_FOLDER,
    UNFILED_FOLDER,
)
from ..database.repositories import ItemRepository, normalize_destination
from ..database.schema import ItemStatus
from ..filesystem.paths import PathAllocator, to_local_path
from ..utils.logging import get_logger
from ..utils.timeutil import now_iso
from .client import DriveClient
from .exporters import (
    STRATEGY_EXPORT,
    STRATEGY_FOLDER,
    STRATEGY_SHORTCUT,
    STRATEGY_UNSUPPORTED,
    ExportPlanner,
)
from .models import CATEGORY_ORDER, DriveFile, classify

log = get_logger(__name__)

_BATCH = 400
_MAX_DEPTH = 128

_STAGING_DDL = """
CREATE TABLE IF NOT EXISTS scan_staging (
    drive_file_id TEXT PRIMARY KEY,
    parent_id     TEXT,
    name          TEXT NOT NULL,
    mime_type     TEXT NOT NULL,
    size          INTEGER NOT NULL DEFAULT 0,
    modified_time TEXT,
    created_time  TEXT,
    md5_checksum  TEXT,
    drive_id      TEXT,
    owned_by_me   INTEGER NOT NULL DEFAULT 1,
    can_download  INTEGER NOT NULL DEFAULT 1,
    export_links  TEXT
);
CREATE INDEX IF NOT EXISTS idx_staging_parent ON scan_staging (parent_id);
"""


class ScanCancelled(Exception):
    """Raised inside the scanner when the user cancels."""


@dataclass
class ScanProgress:
    """Live counters emitted while a scan runs."""

    files: int = 0
    folders: int = 0
    bytes_total: int = 0
    current_path: str = ""
    stage: str = "listing"


@dataclass
class ScanResult:
    """Everything the UI needs after a scan completes."""

    files: int = 0
    folders: int = 0
    total_bytes: int = 0
    downloadable_files: int = 0
    downloadable_bytes: int = 0
    export_files: int = 0
    unsupported_files: int = 0
    shortcut_files: int = 0
    renamed_files: int = 0
    category_bytes: dict[str, int] = field(default_factory=dict)
    category_counts: dict[str, int] = field(default_factory=dict)
    removed_remotely: int = 0
    cancelled: bool = False


class DriveScanner:
    """Walks an entire Drive and records it in the database."""

    def __init__(
        self,
        client: DriveClient,
        items: ItemRepository,
        account_id: int,
        destination: str,
        planner: ExportPlanner,
        include_trashed: bool = False,
        include_shared_with_me: bool = False,
        include_shared_drives: bool = False,
        owned_only: bool = False,
    ) -> None:
        self.client = client
        self.items = items
        self.account_id = account_id
        self.destination = destination
        self.destination_key = normalize_destination(destination)
        self.planner = planner
        self.include_trashed = include_trashed
        self.include_shared_with_me = include_shared_with_me
        self.include_shared_drives = include_shared_drives
        self.owned_only = owned_only

        self._cancel = threading.Event()
        self._allocator = PathAllocator()
        self._folders: dict[str, DriveFile] = {}
        self._folder_paths: dict[str, str] = {}
        self._progress = ScanProgress()

    # -- control -----------------------------------------------------------
    def cancel(self) -> None:
        """Ask the scan to stop at the next safe point."""
        self._cancel.set()

    def _running(self) -> bool:
        return not self._cancel.is_set()

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise ScanCancelled()

    # -- entry point -------------------------------------------------------
    def scan(self, on_progress: Callable[[ScanProgress], None] | None = None) -> ScanResult:
        """Run a full scan and return its summary."""
        emit = on_progress or (lambda _p: None)
        result = ScanResult()
        stamp = now_iso()
        db = self.items.db

        self._allocator.reset()
        self._folders.clear()
        self._folder_paths.clear()
        self._progress = ScanProgress()

        db.executescript(_STAGING_DDL)
        db.execute("DELETE FROM scan_staging")

        try:
            self._stage_listing(emit)
            self._check_cancel()
            self._resolve_folders(emit, stamp, result)
            self._check_cancel()
            self._assign_files(emit, stamp, result)
        except ScanCancelled:
            result.cancelled = True
            log.info("Drive scan cancelled by user")
            return result
        finally:
            try:
                db.execute("DROP TABLE IF EXISTS scan_staging")
            except Exception:  # pragma: no cover - cleanup best effort
                pass

        result.removed_remotely = self.items.mark_missing_as_deleted(
            self.account_id, self.destination, stamp
        )
        log.info(
            "Scan complete: %s files, %s folders, %s bytes (%s exports, %s unsupported)",
            result.files, result.folders, result.total_bytes,
            result.export_files, result.unsupported_files,
        )
        return result

    # -- stage 1: stream the listing --------------------------------------
    def _stage_listing(self, emit: Callable[[ScanProgress], None]) -> None:
        self._progress.stage = "listing"
        buffer: list[tuple] = []
        corpora: list[tuple[str | None, str]] = [(None, MY_DRIVE_FOLDER)]

        if self.include_shared_drives:
            for shared in self.client.list_shared_drives():
                corpora.append((shared.id, shared.name))

        for drive_id, label in corpora:
            self._progress.current_path = label
            for item in self.client.iter_all_files(
                include_trashed=self.include_trashed,
                drive_id=drive_id,
                should_continue=self._running,
            ):
                self._check_cancel()
                if item.is_folder:
                    self._folders[item.id] = item
                    self._progress.folders += 1
                else:
                    buffer.append(self._staging_row(item))
                    self._progress.files += 1
                    self._progress.bytes_total += item.size
                    if len(buffer) >= _BATCH:
                        self._flush_staging(buffer)
                        emit(self._progress)
            emit(self._progress)

        self._flush_staging(buffer)
        emit(self._progress)
        log.info(
            "Listing finished: %s files, %s folders discovered",
            self._progress.files, self._progress.folders,
        )

    @staticmethod
    def _staging_row(item: DriveFile) -> tuple:
        return (
            item.id,
            item.parents[0] if item.parents else None,
            item.name,
            item.mime_type,
            item.size,
            item.modified_time,
            item.created_time,
            item.md5_checksum,
            item.drive_id,
            1 if item.owned_by_me else 0,
            1 if item.can_download else 0,
            json.dumps(item.export_links) if item.export_links else None,
        )

    def _flush_staging(self, buffer: list[tuple]) -> None:
        if not buffer:
            return
        self.items.db.executemany(
            "INSERT OR REPLACE INTO scan_staging (drive_file_id, parent_id, name,"
            " mime_type, size, modified_time, created_time, md5_checksum, drive_id,"
            " owned_by_me, can_download, export_links)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            buffer,
        )
        buffer.clear()

    # -- stage 2: folder hierarchy ----------------------------------------
    def _resolve_folders(
        self, emit: Callable[[ScanProgress], None], stamp: str, result: ScanResult
    ) -> None:
        self._progress.stage = "hierarchy"
        emit(self._progress)

        root_id = self.client.root_id()
        shared_names = {}
        if self.include_shared_drives:
            shared_names = {d.id: d.name for d in self.client.list_shared_drives()}

        depths: dict[str, int] = {}
        for folder_id in self._folders:
            depths[folder_id] = self._depth_of(folder_id, root_id, depths)

        ordered = sorted(
            self._folders.values(),
            key=lambda f: (depths.get(f.id, _MAX_DEPTH), f.name.casefold(), f.id),
        )

        rows: list[tuple] = []
        for folder in ordered:
            self._check_cancel()
            parent_path = self._base_path_for(folder, root_id, shared_names)
            relative = self._allocator.allocate(folder.id, folder.name, parent_path)
            self._folder_paths[folder.id] = relative
            local = str(to_local_path(self.destination, relative))
            rows.append(
                ItemRepository.build_row(
                    self.account_id, self.destination,
                    drive_file_id=folder.id,
                    parent_id=folder.parents[0] if folder.parents else None,
                    name=folder.name,
                    mime_type=folder.mime_type,
                    is_folder=True,
                    size=0,
                    modified_time=folder.modified_time,
                    created_time=folder.created_time,
                    md5_checksum=None,
                    relative_path=relative,
                    local_path=local,
                    strategy=STRATEGY_FOLDER,
                    export_mime=None,
                    drive_id=folder.drive_id,
                    stamp=stamp,
                )
            )
            if len(rows) >= _BATCH:
                self.items.bulk_upsert(rows)
                rows = []
        self.items.bulk_upsert(rows)

        result.folders = len(self._folders)
        self._progress.current_path = ""
        emit(self._progress)

    def _depth_of(self, folder_id: str, root_id: str, memo: dict[str, int]) -> int:
        """Distance from "My Drive" (or a detached root), cycle-safe."""
        if folder_id in memo:
            return memo[folder_id]
        depth = 0
        seen: set[str] = set()
        current = folder_id
        while depth < _MAX_DEPTH:
            if current in seen:
                break  # defensive: Drive should never produce a cycle
            seen.add(current)
            folder = self._folders.get(current)
            if folder is None or not folder.parents:
                break
            parent = folder.parents[0]
            if parent == root_id or parent not in self._folders:
                depth += 1
                break
            current = parent
            depth += 1
        for node in seen:
            memo.setdefault(node, depth)
        memo[folder_id] = depth
        return depth

    def _base_path_for(
        self, item: DriveFile, root_id: str, shared_names: dict[str, str]
    ) -> str:
        """Relative path of ``item``'s parent folder, creating virtual roots."""
        parent_id = item.parents[0] if item.parents else None

        if parent_id and parent_id in self._folder_paths:
            return self._folder_paths[parent_id]

        # A shared drive's top level: parent id equals the drive id.
        if item.drive_id and parent_id == item.drive_id:
            name = shared_names.get(item.drive_id, item.drive_id)
            return self._virtual_root(f"Shared drives/{name}")

        if parent_id == root_id or not parent_id:
            return ""

        # Parent exists but was not listed: the item lives outside My Drive.
        if not item.owned_by_me:
            return self._virtual_root(SHARED_WITH_ME_FOLDER)
        return self._virtual_root(UNFILED_FOLDER)

    def _virtual_root(self, path: str) -> str:
        """Register a synthetic top-level folder so names cannot collide."""
        parts = path.split("/")
        current = ""
        for part in parts:
            key = f"__virtual__:{current}/{part}"
            current = self._allocator.allocate(key, part, current)
        return current

    # -- stage 3: files ----------------------------------------------------
    def _assign_files(
        self, emit: Callable[[ScanProgress], None], stamp: str, result: ScanResult
    ) -> None:
        self._progress.stage = "planning"
        emit(self._progress)

        root_id = self.client.root_id()
        shared_names = {}
        if self.include_shared_drives:
            shared_names = {d.id: d.name for d in self.client.list_shared_drives()}

        rows: list[tuple] = []
        mappings: list[tuple] = []
        terminal: list[tuple] = []
        processed = 0
        category_bytes = {name: 0 for name in CATEGORY_ORDER}
        category_counts = {name: 0 for name in CATEGORY_ORDER}

        cursor = self.items.db.connect().execute(
            "SELECT * FROM scan_staging ORDER BY parent_id, name COLLATE NOCASE,"
            " drive_file_id"
        )
        while True:
            self._check_cancel()
            batch = cursor.fetchmany(_BATCH)
            if not batch:
                break
            for row in batch:
                item = _file_from_staging(row)

                if self.owned_only and not item.owned_by_me:
                    continue

                if not self.include_shared_with_me and not item.owned_by_me:
                    parent_known = bool(item.parents) and item.parents[0] in self._folder_paths
                    if not parent_known and item.parents and item.parents[0] != root_id:
                        continue

                plan = self.planner.plan(item)
                parent_path = self._base_path_for(item, root_id, shared_names)
                relative = self._allocator.allocate(
                    item.id, item.name, parent_path, plan.extension
                )
                local = str(to_local_path(self.destination, relative))
                leaf = relative.rsplit("/", 1)[-1]
                if leaf != item.name:
                    mappings.append(
                        (self.account_id, self.destination_key, item.id, item.name,
                         leaf, stamp)
                    )

                rows.append(
                    ItemRepository.build_row(
                        self.account_id, self.destination,
                        drive_file_id=item.id,
                        parent_id=item.parents[0] if item.parents else None,
                        name=item.name,
                        mime_type=item.mime_type,
                        is_folder=False,
                        size=item.size,
                        modified_time=item.modified_time,
                        created_time=item.created_time,
                        md5_checksum=item.md5_checksum,
                        relative_path=relative,
                        local_path=local,
                        strategy=plan.strategy,
                        export_mime=plan.export_mime,
                        drive_id=item.drive_id,
                        stamp=stamp,
                    )
                )

                category = classify(item.mime_type)
                category_counts[category] = category_counts.get(category, 0) + 1
                category_bytes[category] = category_bytes.get(category, 0) + item.size

                result.files += 1
                result.total_bytes += item.size
                if plan.strategy == STRATEGY_EXPORT:
                    result.export_files += 1
                    result.downloadable_files += 1
                elif plan.strategy == STRATEGY_UNSUPPORTED:
                    result.unsupported_files += 1
                    terminal.append(
                        (ItemStatus.UNSUPPORTED, plan.reason, stamp, item.id)
                    )
                elif plan.strategy == STRATEGY_SHORTCUT:
                    result.shortcut_files += 1
                    terminal.append((ItemStatus.SKIPPED, plan.reason, stamp, item.id))
                else:
                    result.downloadable_files += 1
                    result.downloadable_bytes += item.size

                processed += 1
                if processed % 200 == 0:
                    self._progress.current_path = parent_path.replace("/", " / ")
                    emit(self._progress)

            self.items.bulk_upsert(rows)
            rows = []

        self.items.bulk_upsert(rows)
        self._apply_terminal_states(terminal)
        if mappings:
            self._record_mappings(mappings)
        result.renamed_files = len(mappings)
        result.category_bytes = category_bytes
        result.category_counts = category_counts
        self._progress.current_path = ""
        emit(self._progress)

    def _apply_terminal_states(self, terminal: list[tuple]) -> None:
        """Give non-downloadable items their final status and specific reason.

        The reason comes from the planner, so the user sees "Google Forms cannot
        be exported" rather than a generic message.
        """
        if not terminal:
            return
        rows = [
            (status, reason, stamp, self.account_id, self.destination_key, file_id)
            for status, reason, stamp, file_id in terminal
        ]
        self.items.db.executemany(
            "UPDATE drive_items SET status = ?, error_message = ?, updated_at = ?"
            " WHERE account_id = ? AND destination = ? AND drive_file_id = ?",
            rows,
        )

    def _record_mappings(self, mappings: list[tuple]) -> None:
        self.items.db.executemany(
            "INSERT INTO name_mappings (account_id, destination, drive_file_id,"
            " original_name, local_name, created_at) VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(account_id, destination, drive_file_id) DO UPDATE SET"
            " original_name=excluded.original_name, local_name=excluded.local_name",
            mappings,
        )
        for original, local, *_ in ((m[3], m[4]) for m in mappings[:50]):
            log.info("Renamed for Windows compatibility: %r -> %r", original, local)


def _file_from_staging(row) -> DriveFile:
    """Rebuild a :class:`DriveFile` from a staging-table row."""
    links = {}
    if row["export_links"]:
        try:
            links = json.loads(row["export_links"])
        except (TypeError, ValueError):
            links = {}
    return DriveFile(
        id=row["drive_file_id"],
        name=row["name"],
        mime_type=row["mime_type"],
        parents=[row["parent_id"]] if row["parent_id"] else [],
        size=int(row["size"] or 0),
        modified_time=row["modified_time"],
        created_time=row["created_time"],
        md5_checksum=row["md5_checksum"],
        drive_id=row["drive_id"],
        owned_by_me=bool(row["owned_by_me"]),
        can_download=bool(row["can_download"]),
        export_links=links,
    )


def create_local_folders(destination: str, relative_paths: Iterable[str]) -> int:
    """Materialise the folder hierarchy under ``destination``.

    Empty Drive folders exist locally too, so the backup is a faithful mirror.
    """
    from ..filesystem.storage import ensure_directory

    created = 0
    for relative in relative_paths:
        try:
            ensure_directory(to_local_path(destination, relative))
            created += 1
        except OSError as exc:
            log.warning("Could not create folder %s: %s", relative, exc)
    return created
