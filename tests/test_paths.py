"""Local path construction: hierarchy, duplicates, long paths."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gdbd.filesystem.paths import (
    WINDOWS_MAX_PATH,
    PathAllocator,
    display_path,
    is_windows,
    long_path,
    to_local_path,
)


class TestToLocalPath:
    """Relative Drive paths map onto a local root."""

    def test_nested_path(self, tmp_path: Path):
        result = to_local_path(tmp_path, "Videos/YouTube/2026/clip.mp4")
        assert result == tmp_path / "Videos" / "YouTube" / "2026" / "clip.mp4"

    def test_root_level_file(self, tmp_path: Path):
        assert to_local_path(tmp_path, "readme.txt") == tmp_path / "readme.txt"

    def test_empty_relative_path_returns_root(self, tmp_path: Path):
        assert to_local_path(tmp_path, "") == tmp_path

    def test_backslashes_are_accepted(self, tmp_path: Path):
        assert to_local_path(tmp_path, "a\\b.txt") == tmp_path / "a" / "b.txt"


@pytest.mark.skipif(not is_windows(), reason="Windows path semantics")
class TestLongPath:
    """Paths beyond MAX_PATH need the extended-length prefix."""

    def test_short_path_is_untouched(self, tmp_path: Path):
        assert not long_path(tmp_path / "a.txt").startswith("\\\\?\\")

    def test_long_path_gets_prefix(self):
        deep = "C:\\" + "\\".join("segment" + str(i) * 8 for i in range(40))
        assert len(deep) > WINDOWS_MAX_PATH
        assert long_path(deep).startswith("\\\\?\\")

    def test_prefix_is_not_applied_twice(self):
        deep = "C:\\" + "x" * 300
        once = long_path(deep)
        assert long_path(once) == once

    def test_display_path_strips_prefix(self):
        assert display_path("\\\\?\\C:\\backup\\a.txt") == "C:\\backup\\a.txt"

    def test_unc_display_path(self):
        assert display_path("\\\\?\\UNC\\server\\share\\f.txt") == "\\\\server\\share\\f.txt"

    def test_can_write_through_a_long_path(self, tmp_path: Path):
        deep = tmp_path
        for index in range(12):
            deep = deep / (f"folder{index}" + "n" * 20)
        target = deep / "file.txt"
        os.makedirs(long_path(deep), exist_ok=True)
        with open(long_path(target), "w", encoding="utf-8") as handle:
            handle.write("ok")
        assert os.path.getsize(long_path(target)) == 2


class TestPathAllocator:
    """Every Drive item gets a unique, stable relative path."""

    def test_simple_allocation(self):
        allocator = PathAllocator()
        assert allocator.allocate("id1", "video.mp4") == "video.mp4"

    def test_nested_allocation(self):
        allocator = PathAllocator()
        videos = allocator.allocate("f1", "Videos")
        youtube = allocator.allocate("f2", "YouTube", videos)
        year = allocator.allocate("f3", "2026", youtube)
        clip = allocator.allocate("v1", "clip.mp4", year)
        assert clip == "Videos/YouTube/2026/clip.mp4"

    def test_duplicate_names_in_same_folder(self):
        allocator = PathAllocator()
        first = allocator.allocate("a", "report.pdf")
        second = allocator.allocate("b", "report.pdf")
        third = allocator.allocate("c", "report.pdf")
        assert [first, second, third] == [
            "report.pdf", "report (1).pdf", "report (2).pdf"
        ]

    def test_same_name_in_different_folders_is_fine(self):
        allocator = PathAllocator()
        one = allocator.allocate("f1", "Docs")
        two = allocator.allocate("f2", "Media")
        assert allocator.allocate("a", "x.txt", one) == "Docs/x.txt"
        assert allocator.allocate("b", "x.txt", two) == "Media/x.txt"

    def test_allocation_is_idempotent_per_file_id(self):
        allocator = PathAllocator()
        first = allocator.allocate("id", "file.mp4")
        assert allocator.allocate("id", "file.mp4") == first
        assert allocator.assigned("id") == first

    def test_illegal_characters_are_sanitized(self):
        allocator = PathAllocator()
        assert allocator.allocate("id", 'bad:name?.mp4') == "bad -name_.mp4"

    def test_rename_is_recorded(self):
        allocator = PathAllocator()
        allocator.allocate("id", "a:b.txt")
        assert allocator.renames == [("a:b.txt", "a -b.txt")]

    def test_export_extension_is_appended(self):
        allocator = PathAllocator()
        assert allocator.allocate("d1", "My Doc", "", ".docx") == "My Doc.docx"

    def test_export_extension_not_doubled(self):
        allocator = PathAllocator()
        assert allocator.allocate("d1", "Sheet.xlsx", "", ".xlsx") == "Sheet.xlsx"

    def test_reserved_name_then_duplicate(self):
        allocator = PathAllocator()
        assert allocator.allocate("a", "CON.txt") == "CON_.txt"
        assert allocator.allocate("b", "CON.txt") == "CON_ (1).txt"

    def test_reset_clears_state(self):
        allocator = PathAllocator()
        allocator.allocate("a", "x.txt")
        allocator.reset()
        assert allocator.assigned("a") is None
        assert allocator.allocate("b", "x.txt") == "x.txt"

    def test_reserve_blocks_a_name(self):
        allocator = PathAllocator()
        allocator.reserve("x.txt")
        assert allocator.allocate("a", "x.txt") == "x (1).txt"

    def test_deterministic_across_runs(self):
        names = [("id%d" % i, "same.mp4") for i in range(5)]
        first = [PathAllocator().allocate(i, n) for i, n in names]
        second_allocator = PathAllocator()
        second = [second_allocator.allocate(i, n) for i, n in names]
        third_allocator = PathAllocator()
        third = [third_allocator.allocate(i, n) for i, n in names]
        assert second == third
        assert first[0] == second[0]
