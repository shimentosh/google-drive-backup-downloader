"""Human-readable formatting helpers."""

from __future__ import annotations

_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def format_bytes(num: float | int | None, decimals: int = 1) -> str:
    """Format a byte count using binary multiples with decimal-style labels.

    ``format_bytes(1536)`` -> ``'1.5 KB'``.
    """
    if num is None:
        return "-"
    value = float(num)
    if value < 0:
        return "-"
    for unit in _UNITS:
        if value < 1024.0 or unit == _UNITS[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.{decimals}f} {unit}"
        value /= 1024.0
    return f"{value:.{decimals}f} PB"


def format_speed(bytes_per_second: float | None) -> str:
    """Format a transfer rate, e.g. ``'18.4 MB/s'``."""
    if not bytes_per_second or bytes_per_second <= 0:
        return "-"
    return f"{format_bytes(bytes_per_second)}/s"


def format_duration(seconds: float | int | None) -> str:
    """Format a duration as ``1h 08m``, ``12m 30s`` or ``45s``."""
    if seconds is None or seconds < 0:
        return "-"
    if 0 < seconds < 1:
        return "<1s"
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def format_eta(remaining_bytes: float, bytes_per_second: float) -> str:
    """Estimate remaining time from a byte backlog and a current rate."""
    if bytes_per_second <= 0 or remaining_bytes <= 0:
        return "-"
    return format_duration(remaining_bytes / bytes_per_second)


def format_count(value: int | None) -> str:
    """Format an integer with thousands separators."""
    if value is None:
        return "-"
    return f"{value:,}"


def percent(done: float, total: float) -> float:
    """Return ``done/total`` as a percentage clamped to ``0..100``."""
    if total <= 0:
        return 0.0
    return max(0.0, min(100.0, (done / total) * 100.0))


def truncate_middle(text: str, max_len: int = 60) -> str:
    """Shorten ``text`` to ``max_len`` characters, eliding the middle."""
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    keep = max_len - 3
    head = keep // 2
    tail = keep - head
    return f"{text[:head]}...{text[-tail:]}"
