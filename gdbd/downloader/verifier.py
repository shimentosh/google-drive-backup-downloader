"""Post-download verification.

A file is only renamed from ``<name>.gdbd.part`` to ``<name>`` after it passes
verification, so a partial or corrupted transfer can never masquerade as a
completed backup.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from ..filesystem.paths import long_path
from ..filesystem.storage import file_size


@dataclass(frozen=True)
class VerificationResult:
    """Outcome of verifying one downloaded file."""

    ok: bool
    reason: str = ""
    checksum: str | None = None


def md5_of_file(path: str | Path, chunk_size: int = 4 * 1024 * 1024) -> str:
    """Stream a file through MD5 without loading it into memory."""
    digest = hashlib.md5()  # noqa: S324 - matches the checksum Drive publishes
    with open(long_path(path), "rb") as handle:
        while True:
            block = handle.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def verify_download(
    path: str | Path,
    expected_size: int | None,
    expected_md5: str | None = None,
    check_checksum: bool = True,
) -> VerificationResult:
    """Verify a downloaded file against the metadata Drive reported.

    Size is always checked. The MD5 is checked as well when Drive published one
    (binary uploads have it; Workspace exports do not, because the bytes are
    generated on demand and have no stored checksum).
    """
    actual_size = file_size(path)
    if actual_size == 0 and (expected_size or 0) > 0:
        return VerificationResult(False, "Downloaded file is empty.")

    if expected_size is not None and expected_size > 0 and actual_size != expected_size:
        return VerificationResult(
            False,
            f"Size mismatch: expected {expected_size:,} bytes, got {actual_size:,}.",
        )

    if check_checksum and expected_md5:
        try:
            actual_md5 = md5_of_file(path)
        except OSError as exc:
            return VerificationResult(False, f"Could not read file for checksum: {exc}")
        if actual_md5.lower() != expected_md5.lower():
            return VerificationResult(
                False, "Checksum mismatch - the downloaded bytes do not match Drive.",
                actual_md5,
            )
        return VerificationResult(True, checksum=actual_md5)

    return VerificationResult(True)


def looks_unchanged(
    local_path: str | Path,
    expected_size: int | None,
    expected_md5: str | None = None,
    verify_checksum: bool = False,
) -> bool:
    """Cheap check for "this file is already backed up and still matches".

    Size is compared first because it costs one ``stat``; the checksum is only
    computed when the caller explicitly asks, since hashing a 20 GB video to
    decide whether to skip it would defeat the purpose.
    """
    actual = file_size(local_path)
    if actual == 0:
        return False
    if expected_size is not None and expected_size > 0 and actual != expected_size:
        return False
    if verify_checksum and expected_md5:
        try:
            return md5_of_file(local_path).lower() == expected_md5.lower()
        except OSError:
            return False
    return True
