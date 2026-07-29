"""Minimal RFC 5545 calendar output.

Written by hand rather than pulled from a library: the subset of iCalendar
needed for all-day multi-day events is small, and a dependency-free
implementation is one less thing to break in CI three years from now.

The browser-side exporter in `site/assets/js/app.js` produces byte-identical
output from the same rules, so a file downloaded from the dashboard matches one
produced by `campradar export`.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone

from .models import CampSession

__all__ = ["render_calendar"]

_PRODID = "-//Camp Radar//EN"

# Characters that carry structural meaning in iCalendar text values.
_ESCAPES = str.maketrans({"\\": "\\\\", ";": "\\;", ",": "\\,", "\n": "\\n"})


def _escape(text: str) -> str:
    return text.translate(_ESCAPES)


def _fold(line: str) -> str:
    """Wrap to 75 octets per RFC 5545, continuation lines starting with a space.

    Unfolded long lines are the most common reason a calendar file imports
    into Google Calendar but not Apple Calendar.
    """
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line
    chunks: list[str] = []
    current = b""
    for char in line:
        char_bytes = char.encode("utf-8")
        limit = 75 if not chunks else 74  # continuation lines lose one octet to the space
        if len(current) + len(char_bytes) > limit:
            chunks.append(current.decode("utf-8"))
            current = b""
        current += char_bytes
    chunks.append(current.decode("utf-8"))
    return "\r\n ".join(chunks)


def _all_day_event(session: CampSession, stamp: str) -> list[str]:
    """One VEVENT.

    DTEND is exclusive for all-day events, so a camp running Mon–Fri needs a
    DTEND of Saturday. Getting this wrong silently drops the final day, which
    is exactly the day a parent would have no childcare.
    """
    summary = session.title
    if session.daily_start and session.daily_end:
        summary = f"{summary} ({session.daily_start}-{session.daily_end})"

    lines = [
        "BEGIN:VEVENT",
        f"UID:{session.key}@camp-radar",
        f"DTSTAMP:{stamp}",
        f"DTSTART;VALUE=DATE:{session.start_date.strftime('%Y%m%d')}",
        f"DTEND;VALUE=DATE:{(session.end_date + timedelta(days=1)).strftime('%Y%m%d')}",
        f"SUMMARY:{_escape(summary)}",
    ]

    details: list[str] = []
    if session.price_usd is not None:
        details.append(f"${session.price_usd:,.0f}")
    if session.min_age is not None and session.max_age is not None:
        details.append(f"ages {session.min_age}-{session.max_age}")
    if session.description:
        details.append(session.description[:400])
    if details:
        lines.append(f"DESCRIPTION:{_escape(' | '.join(details))}")

    if session.url:
        lines.append(f"URL:{session.url}")

    lines.append("END:VEVENT")
    return lines


def render_calendar(sessions: Iterable[CampSession], *, name: str = "Camp Radar") -> str:
    """Render sessions as an iCalendar document with CRLF line endings."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{_PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape(name)}",
    ]
    for session in sessions:
        lines.extend(_all_day_event(session, stamp))
    lines.append("END:VCALENDAR")

    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def days_in_range(start: date, end: date) -> list[date]:
    """Every date from start to end inclusive. Small helper used by coverage."""
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
