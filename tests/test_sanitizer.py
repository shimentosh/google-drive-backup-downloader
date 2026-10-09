"""Filename sanitization: Windows-illegal names, Unicode, duplicates, length."""

from __future__ import annotations

import pytest

from gdbd.filesystem.sanitizer import (
    DEFAULT_MAX_COMPONENT,
    dedupe_name,
    is_reserved,
    sanitize_component,
    sanitize_relative_path,
    split_extension,
)

_ILLEGAL = '<>:"/\\|?*'


class TestIllegalCharacters:
    """Windows forbids nine characters plus control codes."""

    def test_documented_example(self):
        assert (sanitize_component("Project: Final / Version?.mp4")
                == "Project - Final - Version_.mp4")

    @pytest.mark.parametrize("char", list(_ILLEGAL))
    def test_no_illegal_character_survives(self, char):
        result = sanitize_component(f"a{char}b.txt")
        assert not any(bad in result for bad in _ILLEGAL)
        assert result

    def test_control_characters_are_removed(self):
        assert sanitize_component("tab\there\x00.txt") == "tabhere.txt"

    def test_trailing_dots_and_spaces_are_trimmed(self):
        assert sanitize_component("report...") == "report"
        assert sanitize_component("report   ") == "report"
        assert sanitize_component("folder. . ") == "folder"

    def test_empty_name_gets_a_fallback(self):
        assert sanitize_component("") == "unnamed"
        assert sanitize_component("///") == "---"
        assert sanitize_component("...") == "unnamed"


class TestReservedNames:
    """CON, PRN, LPT1 and friends cannot be used, even with an extension."""

    @pytest.mark.parametrize("name", ["CON", "con", "PRN.txt", "aux", "NUL",
                                      "COM1", "LPT9.mp4"])
    def test_reserved_names_are_escaped(self, name):
        result = sanitize_component(name)
        assert result != name
        assert not is_reserved(result)

    def test_similar_but_legal_names_are_untouched(self):
        assert sanitize_component("CONTRACT.pdf") == "CONTRACT.pdf"
        assert sanitize_component("com10.txt") == "com10.txt"


class TestUnicode:
    """Non-ASCII names are legal on NTFS and must be preserved."""

    @pytest.mark.parametrize("name", [
        "বাংলা নথি.pdf",                 # Bengali
        "प्रोजेक्ट.docx",                    # Devanagari
        "写真 2026.jpg",                  # Japanese
        "Отчёт.xlsx",                    # Cyrillic
        "vacation 🏖️ photos.zip",        # emoji
        "café-naïve.txt",                # accents
    ])
    def test_unicode_is_preserved(self, name):
        assert sanitize_component(name) == name

    def test_emoji_only_name(self):
        assert sanitize_component("🎬🎥") == "🎬🎥"

    def test_unicode_with_illegal_character(self):
        assert sanitize_component("বাংলা: নথি.pdf") == "বাংলা - নথি.pdf"


class TestLongNames:
    """Components are shortened deterministically, keeping the extension."""

    def test_long_name_is_truncated(self):
        name = "x" * 400 + ".mp4"
        result = sanitize_component(name)
        assert len(result) <= DEFAULT_MAX_COMPONENT
        assert result.endswith(".mp4")

    def test_truncation_is_deterministic(self):
        name = "y" * 300 + ".mkv"
        assert sanitize_component(name) == sanitize_component(name)

    def test_different_long_names_do_not_collide(self):
        first = sanitize_component("a" * 300 + "one.mp4")
        second = sanitize_component("a" * 300 + "two.mp4")
        assert first != second


class TestExtensionSplit:
    """The extension split must not mangle names containing dots."""

    @pytest.mark.parametrize("name,stem,ext", [
        ("video.mp4", "video", ".mp4"),
        ("archive.tar.gz", "archive.tar", ".gz"),
        ("noextension", "noextension", ""),
        (".hidden", ".hidden", ""),
        ("Report v1.2 final", "Report v1.2 final", ""),
        ("trailing.", "trailing.", ""),
    ])
    def test_split(self, name, stem, ext):
        assert split_extension(name) == (stem, ext)


class TestDeduplication:
    """Two Drive files may share a name inside one folder."""

    def test_first_name_is_unchanged(self):
        taken: set[str] = set()
        assert dedupe_name("file.mp4", taken) == "file.mp4"

    def test_duplicates_get_numbered(self):
        taken: set[str] = set()
        names = [dedupe_name("file.mp4", taken) for _ in range(3)]
        assert names == ["file.mp4", "file (1).mp4", "file (2).mp4"]

    def test_duplicate_detection_is_case_insensitive(self):
        taken: set[str] = set()
        assert dedupe_name("File.MP4", taken) == "File.MP4"
        assert dedupe_name("file.mp4", taken) == "file (1).mp4"

    def test_extensionless_duplicates(self):
        taken: set[str] = set()
        assert dedupe_name("Reports", taken) == "Reports"
        assert dedupe_name("Reports", taken) == "Reports (1)"


class TestRelativePaths:
    """Every component of a path is sanitized independently."""

    def test_each_component_is_cleaned(self):
        assert (sanitize_relative_path("Videos/YouTube: 2026/clip?.mp4")
                == "Videos/YouTube - 2026/clip_.mp4")

    def test_traversal_segments_are_dropped(self):
        assert sanitize_relative_path("a/../b/./c") == "a/b/c"

    def test_backslashes_are_treated_as_separators(self):
        assert sanitize_relative_path("a\\b\\c.txt") == "a/b/c.txt"
