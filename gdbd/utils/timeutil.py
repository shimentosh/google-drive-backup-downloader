"""Time helpers: RFC 3339 parsing and display formatting."""

from __future__ import annotations

from datetime import datetime, timezone


def parse_rfc3339(value: str | None) -> datetime | None:
    """Parse an RFC 3339 timestamp as returned by the Drive API.

    Returns a timezone-aware :class:`datetime` in UTC, or ``None`` when the
    value is missing or unparseable.
    """
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def to_epoch(value: str | datetime | None) -> float | None:
    """Convert an RFC 3339 string or datetime to a POSIX timestamp."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    parsed = parse_rfc3339(value)
    return parsed.timestamp() if parsed else None


def now_utc() -> datetime:
    """Current time as a timezone-aware UTC datetime."""
    return datetime.now(timezone.utc)


def now_iso() -> str:
    """Current UTC time as an RFC 3339 string, to microsecond precision.

    Sub-second precision matters: two scans of a small Drive can complete inside
    the same second, and the "which items did the latest scan not see?" query
    compares these strings.
    """
    return now_utc().isoformat(timespec="microseconds")


def format_local(value: str | datetime | None, fmt: str = "%b %d, %Y  %I:%M %p") -> str:
    """Render a timestamp in the machine's local timezone for display."""
    if value is None:
        return "-"
    dt = parse_rfc3339(value) if isinstance(value, str) else value
    if dt is None:
        return "-"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime(fmt).replace(" 0", " ")


def format_relative_day(value: str | datetime | None) -> str:
    """Render a timestamp as ``Today, 8:42 PM`` / ``Yesterday, ...``."""
    if value is None:
        return "Never"
    dt = parse_rfc3339(value) if isinstance(value, str) else value
    if dt is None:
        return "Never"
    local = dt.astimezone()
    today = datetime.now().astimezone().date()
    delta = (today - local.date()).days
    time_part = local.strftime("%I:%M %p").lstrip("0")
    if delta == 0:
        return f"Today, {time_part}"
    if delta == 1:
        return f"Yesterday, {time_part}"
    return local.strftime("%b %d, %Y").replace(" 0", " ") + f", {time_part}"
