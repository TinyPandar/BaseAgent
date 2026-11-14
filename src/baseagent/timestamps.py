"""One time convention for everything BaseAgent shows: UTC, always marked.

- JSON strings: ISO 8601 with an explicit ``+00:00`` offset (``2026-11-13T08:30:00+00:00``).
- Text for people: ``2026-11-13 08:30:00 UTC``.
- JSON numbers named ``timestamp`` / ``*_at``: Unix epoch seconds, which are UTC by definition.
  Where people read such a number (``--inspect-session``, ``--task-tree``, chat status), a sibling
  ``*_utc`` string carries the same instant in ISO form.

Storage is unchanged: SQLite ``CURRENT_TIMESTAMP`` text (UTC without a marker) and epoch numbers are
converted only on output, so values written by older versions display correctly.
"""

from __future__ import annotations

from datetime import UTC, datetime

TIME_ZONE = "UTC"


def iso_utc(seconds):
    """Epoch seconds -> ``YYYY-MM-DDTHH:MM:SS+00:00``; None (or a non-number) stays None."""
    if seconds is None or isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=UTC).isoformat(timespec="seconds")
    except (OverflowError, OSError, ValueError):
        return None


def text_utc(seconds):
    """Epoch seconds -> ``YYYY-MM-DD HH:MM:SS UTC`` for text output; None stays None."""
    value = iso_utc(seconds)
    return None if value is None else value.replace("T", " ").replace("+00:00", " UTC")
