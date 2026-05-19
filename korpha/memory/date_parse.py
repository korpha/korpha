"""Natural-language date-range parser — stdlib only.

Maps common phrases the founder might type into a (start, end) UTC
datetime tuple. Used by ``memory.recall_by_date`` so Mike can ask
"what did we do last Thursday?" without us hitting an LLM to parse
the date out of the query (the Hermes "no tokens for indexing"
property).

Handled forms:
  - "today" / "yesterday"
  - "last <weekday>"   → most recent past occurrence of that weekday
  - "<weekday>"        → most recent past occurrence (same as "last")
  - "N days ago"       → that single day
  - "N weeks ago"      → that whole week (Monday → Sunday)
  - "N months ago"     → that whole calendar month
  - "May 10" / "May 10th" / "5/10" / "5/10/2026" → that single day
  - "2026-05-10"       → that single day (ISO)
  - "between X and Y"  → range from start of X to end of Y
  - "this week" / "this month" / "last week" / "last month"

Returns ``None`` for unparseable input — caller surfaces a helpful
error to the user.
"""
from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from typing import NamedTuple


class DateRange(NamedTuple):
    """Half-open UTC datetime range [start, end).

    ``start`` is the first second of the matching window; ``end`` is
    the first second AFTER the window (exclusive). So a single day
    "May 10" → start=2026-05-10 00:00:00, end=2026-05-11 00:00:00.
    """

    start: datetime
    end: datetime
    label: str  # human-readable summary of what was parsed


_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _day_range(d: date, label: str) -> DateRange:
    start = datetime.combine(d, datetime.min.time(), tzinfo=UTC)
    end = start + timedelta(days=1)
    return DateRange(start=start, end=end, label=label)


def _week_range(any_day_in_week: date, label: str) -> DateRange:
    """ISO week starting Monday."""
    monday = any_day_in_week - timedelta(days=any_day_in_week.weekday())
    start = datetime.combine(monday, datetime.min.time(), tzinfo=UTC)
    end = start + timedelta(days=7)
    return DateRange(start=start, end=end, label=label)


def _month_range(year: int, month: int, label: str) -> DateRange:
    start = datetime(year, month, 1, tzinfo=UTC)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=UTC)
    else:
        end = datetime(year, month + 1, 1, tzinfo=UTC)
    return DateRange(start=start, end=end, label=label)


def parse_natural_date(
    text: str, *, now: datetime | None = None,
) -> DateRange | None:
    """Parse one natural-language date phrase into a UTC DateRange.

    Returns None for unparseable input. ``now`` defaults to current UTC
    time — pass an explicit ``now`` for deterministic tests.
    """
    if not text:
        return None
    now = now or datetime.now(UTC)
    today = now.date()
    s = text.strip().lower()
    if not s:
        return None

    # --- single-keyword shortcuts ---
    if s == "today":
        return _day_range(today, "today")
    if s == "yesterday":
        return _day_range(today - timedelta(days=1), "yesterday")
    if s == "this week":
        return _week_range(today, "this week")
    if s == "last week":
        return _week_range(today - timedelta(days=7), "last week")
    if s == "this month":
        return _month_range(today.year, today.month, "this month")
    if s == "last month":
        ly, lm = (today.year - 1, 12) if today.month == 1 else (
            today.year, today.month - 1
        )
        return _month_range(ly, lm, "last month")

    # --- "last <weekday>" or bare "<weekday>" ---
    bare_weekday = _WEEKDAYS.get(s)
    last_weekday_match = re.match(r"^last\s+(\w+)$", s)
    if last_weekday_match:
        wd = _WEEKDAYS.get(last_weekday_match.group(1))
    else:
        wd = bare_weekday
    if wd is not None:
        # Most recent past occurrence (strict — today doesn't count as
        # "last Thursday" if today IS Thursday; that becomes 7 days ago)
        delta = (today.weekday() - wd) % 7
        if delta == 0:
            delta = 7
        target = today - timedelta(days=delta)
        return _day_range(target, f"last {target.strftime('%A')} ({target.isoformat()})")

    # --- "N days/weeks/months ago" ---
    m = re.match(r"^(\d+)\s+(day|days|week|weeks|month|months)\s+ago$", s)
    if m:
        n = int(m.group(1))
        unit = m.group(2).rstrip("s")
        if unit == "day":
            target = today - timedelta(days=n)
            return _day_range(target, f"{n} day{'s' if n != 1 else ''} ago ({target.isoformat()})")
        if unit == "week":
            target = today - timedelta(weeks=n)
            return _week_range(target, f"{n} week{'s' if n != 1 else ''} ago")
        if unit == "month":
            tot = today.month - n - 1 + today.year * 12
            ny, nm = divmod(tot, 12)
            nm += 1
            return _month_range(ny, nm, f"{n} month{'s' if n != 1 else ''} ago ({ny}-{nm:02d})")

    # --- ISO date ---
    iso_m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if iso_m:
        try:
            d = date(int(iso_m.group(1)), int(iso_m.group(2)), int(iso_m.group(3)))
            return _day_range(d, d.isoformat())
        except ValueError:
            return None

    # --- "Month Dayth" / "Month Day" + optional year ---
    month_day = re.match(
        r"^(\w+)\s+(\d{1,2})(?:st|nd|rd|th)?(?:[,\s]+(\d{4}))?$", s,
    )
    if month_day:
        month = _MONTHS.get(month_day.group(1))
        if month:
            day = int(month_day.group(2))
            year = int(month_day.group(3)) if month_day.group(3) else today.year
            try:
                d = date(year, month, day)
                # If the resulting date is in the future + year wasn't
                # given explicitly, assume they meant last year.
                if d > today and not month_day.group(3):
                    d = date(year - 1, month, day)
                return _day_range(d, d.isoformat())
            except ValueError:
                return None

    # --- "M/D" or "M/D/Y" numeric ---
    md = re.match(r"^(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?$", s)
    if md:
        month = int(md.group(1))
        day = int(md.group(2))
        year_raw = md.group(3)
        if year_raw is None:
            year = today.year
        else:
            year = int(year_raw)
            if year < 100:
                year += 2000
        try:
            d = date(year, month, day)
            if d > today and md.group(3) is None:
                d = date(year - 1, month, day)
            return _day_range(d, d.isoformat())
        except ValueError:
            return None

    # --- "between X and Y" ---
    bw = re.match(r"^between\s+(.+?)\s+and\s+(.+)$", s)
    if bw:
        left = parse_natural_date(bw.group(1), now=now)
        right = parse_natural_date(bw.group(2), now=now)
        if left and right:
            start = min(left.start, right.start)
            end = max(left.end, right.end)
            return DateRange(
                start=start, end=end,
                label=f"{left.label} → {right.label}",
            )

    return None


__all__ = ["DateRange", "parse_natural_date"]
