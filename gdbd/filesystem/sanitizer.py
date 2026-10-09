"""Deterministic conversion of Google Drive names into safe Windows names.

Google Drive allows characters that Windows forbids in file names, allows names
that Windows reserves, and allows two siblings to share a name. Everything in
this module is pure and deterministic: the same Drive name always maps to the
same local name, so re-running a backup reuses the files it created last time.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

#: Characters Windows forbids, mapped to visually similar legal replacements.
#: The mapping is intentionally lossy-but-readable rather than a blanket "_" so
#: that names stay recognisable to the user.
_CHAR_MAP = {
    "<": "(",
    ">": ")",
    ":": " -",
    '"': "'",
    "/": "-",
    "\\": "-",
    "|": "-",
    "?": "_",
    "*": "_",
}

#: Device names Windows reserves, with or without an extension.
_RESERVED_NAMES = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")

#: Conservative per-component limit. Windows allows 255 UTF-16 units per
#: component; we stay well under so that a deep tree still fits comfortably.
DEFAULT_MAX_COMPONENT = 150


def _short_hash(text: str, length: int = 8) -> str:
    """Stable short digest used when a name must be shortened."""
    return hashlib.sha1(text.encode("utf-8", "surrogatepass")).hexdigest()[:length]


def split_extension(name: str) -> tuple[str, str]:
    """Split ``name`` into ``(stem, extension)`` keeping the dot on the suffix.

    Only a short, plausible trailing extension is split off, so a name like
    ``"Report v1.2 final"`` is not mangled into a bogus ``.2 final`` suffix.
    """
    idx = name.rfind(".")
    if idx <= 0 or idx == len(name) - 1:
        return name, ""
    ext = name[idx:]
    if len(ext) > 12 or " " in ext:
        return name, ""
    return name[:idx], ext


def sanitize_component(
    name: str,
    max_length: int = DEFAULT_MAX_COMPONENT,
    fallback: str = "unnamed",
) -> str:
    """Return a single path component that is always legal on Windows.

    Unicode (including Bengali text and emoji) is preserved as-is; only
    genuinely illegal characters, reserved device names and trailing dots or
    spaces are altered.
    """
    if name is None:
        name = ""
    # Normalise so that visually identical names collide consistently.
    text = unicodedata.normalize("NFC", str(name))
    text = _CONTROL_CHARS.sub("", text)
    text = "".join(_CHAR_MAP.get(ch, ch) for ch in text)
    text = _MULTI_SPACE.sub(" ", text).strip()

    # Windows silently strips trailing dots and spaces; do it explicitly so the
    # name we record matches the name on disk.
    text = text.rstrip(". ")
    if not text:
        text = fallback

    stem, ext = split_extension(text)
    if stem.upper() in _RESERVED_NAMES:
        stem = f"{stem}_"
        text = stem + ext

    if len(text) > max_length:
        stem, ext = split_extension(text)
        digest = _short_hash(text)
        keep = max(1, max_length - len(ext) - len(digest) - 2)
        text = f"{stem[:keep].rstrip('. ')}~{digest}{ext}"

    text = text.rstrip(". ")
    return text or fallback


def sanitize_relative_path(path: str, max_length: int = DEFAULT_MAX_COMPONENT) -> str:
    """Sanitize every component of a ``/``-separated relative path."""
    parts = [p for p in re.split(r"[\\/]+", path) if p not in ("", ".", "..")]
    return "/".join(sanitize_component(p, max_length) for p in parts)


def dedupe_name(name: str, taken: set[str], index_hint: int = 0) -> str:
    """Return a name not present in ``taken``, using ``name (1)``, ``name (2)``.

    ``taken`` holds already-used lowercase names; the chosen name is added to it
    before returning, so callers can simply keep passing the same set.
    """
    key = name.casefold()
    if key not in taken:
        taken.add(key)
        return name

    stem, ext = split_extension(name)
    counter = max(1, index_hint)
    while True:
        candidate = f"{stem} ({counter}){ext}"
        ckey = candidate.casefold()
        if ckey not in taken:
            taken.add(ckey)
            return candidate
        counter += 1


def is_reserved(name: str) -> bool:
    """True when ``name`` (ignoring extension) is a reserved Windows device."""
    stem, _ = split_extension(name)
    return stem.upper() in _RESERVED_NAMES
