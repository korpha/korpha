"""Tests for the natural-language date-range parser."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from korpha.memory.date_parse import DateRange, parse_natural_date


# Fix "now" for deterministic testing
_NOW = datetime(2026, 5, 19, 14, 30, 0, tzinfo=UTC)  # Tuesday


def _parse(text: str) -> DateRange | None:
    return parse_natural_date(text, now=_NOW)


# ---------------------------------------------------------------------------
# Single-keyword
# ---------------------------------------------------------------------------


def test_today() -> None:
    r = _parse("today")
    assert r.start == datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC)
    assert r.end == datetime(2026, 5, 20, 0, 0, 0, tzinfo=UTC)
    assert r.label == "today"


def test_yesterday() -> None:
    r = _parse("yesterday")
    assert r.start == datetime(2026, 5, 18, 0, 0, 0, tzinfo=UTC)
    assert r.end == datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC)


def test_this_week() -> None:
    # 2026-05-19 is Tuesday. ISO week starts Monday → 2026-05-18.
    r = _parse("this week")
    assert r.start == datetime(2026, 5, 18, 0, 0, 0, tzinfo=UTC)
    assert r.end == datetime(2026, 5, 25, 0, 0, 0, tzinfo=UTC)


def test_last_week() -> None:
    r = _parse("last week")
    assert r.start == datetime(2026, 5, 11, 0, 0, 0, tzinfo=UTC)
    assert r.end == datetime(2026, 5, 18, 0, 0, 0, tzinfo=UTC)


def test_this_month() -> None:
    r = _parse("this month")
    assert r.start == datetime(2026, 5, 1, 0, 0, 0, tzinfo=UTC)
    assert r.end == datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)


def test_last_month() -> None:
    r = _parse("last month")
    assert r.start == datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)
    assert r.end == datetime(2026, 5, 1, 0, 0, 0, tzinfo=UTC)


def test_last_month_january_rolls_year() -> None:
    jan_now = datetime(2026, 1, 15, tzinfo=UTC)
    r = parse_natural_date("last month", now=jan_now)
    assert r.start == datetime(2025, 12, 1, 0, 0, 0, tzinfo=UTC)
    assert r.end == datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Weekday names
# ---------------------------------------------------------------------------


def test_last_thursday() -> None:
    # 2026-05-19 is Tuesday. Last Thursday = 2026-05-14.
    r = _parse("last Thursday")
    assert r.start == datetime(2026, 5, 14, 0, 0, 0, tzinfo=UTC)
    assert r.end == datetime(2026, 5, 15, 0, 0, 0, tzinfo=UTC)


def test_bare_weekday_is_last() -> None:
    """A bare 'Thursday' should resolve to the most-recent past Thursday."""
    r = _parse("Thursday")
    assert r.start == datetime(2026, 5, 14, 0, 0, 0, tzinfo=UTC)


def test_same_weekday_as_today_means_one_week_ago() -> None:
    """'Last Tuesday' when today is Tuesday means 7 days back, not today."""
    r = _parse("last Tuesday")
    assert r.start == datetime(2026, 5, 12, 0, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# N <units> ago
# ---------------------------------------------------------------------------


def test_n_days_ago() -> None:
    r = _parse("3 days ago")
    assert r.start == datetime(2026, 5, 16, 0, 0, 0, tzinfo=UTC)
    assert r.end == datetime(2026, 5, 17, 0, 0, 0, tzinfo=UTC)


def test_n_weeks_ago() -> None:
    """'2 weeks ago' returns the whole week 14 days back."""
    r = _parse("2 weeks ago")
    # 2026-05-19 → 14 days back = 2026-05-05 (Tuesday) → that week's Monday is 2026-05-04
    assert r.start == datetime(2026, 5, 4, 0, 0, 0, tzinfo=UTC)
    assert r.end == datetime(2026, 5, 11, 0, 0, 0, tzinfo=UTC)


def test_n_months_ago() -> None:
    r = _parse("2 months ago")
    assert r.start == datetime(2026, 3, 1, 0, 0, 0, tzinfo=UTC)
    assert r.end == datetime(2026, 4, 1, 0, 0, 0, tzinfo=UTC)


def test_n_months_ago_year_rollback() -> None:
    r = _parse("6 months ago")
    assert r.start == datetime(2025, 11, 1, 0, 0, 0, tzinfo=UTC)
    assert r.end == datetime(2025, 12, 1, 0, 0, 0, tzinfo=UTC)


def test_one_day_ago_singular() -> None:
    r = _parse("1 day ago")
    assert r.start == datetime(2026, 5, 18, 0, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Date literals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phrasing", [
    "May 10",
    "May 10th",
    "may 10",
    "May 10, 2026",
])
def test_month_day_forms(phrasing: str) -> None:
    r = _parse(phrasing)
    assert r.start == datetime(2026, 5, 10, 0, 0, 0, tzinfo=UTC)


def test_month_day_no_year_future_assumes_last_year() -> None:
    """'December 25' when it's May → last year's Christmas."""
    r = _parse("December 25")
    assert r.start == datetime(2025, 12, 25, 0, 0, 0, tzinfo=UTC)


def test_iso_date() -> None:
    r = _parse("2026-05-10")
    assert r.start == datetime(2026, 5, 10, 0, 0, 0, tzinfo=UTC)


def test_numeric_md_form() -> None:
    r = _parse("5/10")
    assert r.start == datetime(2026, 5, 10, 0, 0, 0, tzinfo=UTC)


def test_numeric_mdy_form() -> None:
    r = _parse("5/10/2025")
    assert r.start == datetime(2025, 5, 10, 0, 0, 0, tzinfo=UTC)


def test_invalid_date_returns_none() -> None:
    assert _parse("foo bar") is None
    assert _parse("") is None
    assert _parse("February 30") is None  # invalid date


# ---------------------------------------------------------------------------
# Ranges
# ---------------------------------------------------------------------------


def test_between_two_dates() -> None:
    r = _parse("between May 10 and May 14")
    assert r.start == datetime(2026, 5, 10, 0, 0, 0, tzinfo=UTC)
    assert r.end == datetime(2026, 5, 15, 0, 0, 0, tzinfo=UTC)


def test_between_relative_phrases() -> None:
    """Mike says 'between Monday and Wednesday last week'.

    Our simple parser handles 'between X and Y' but won't compose all
    the relative phrases — should still gracefully fall through.
    """
    # Simpler form that we DO handle
    r = _parse("between last Monday and last Friday")
    # last Monday from Tuesday 2026-05-19 = 2026-05-18 (yesterday)
    # last Friday = 2026-05-15
    assert r is not None
    assert r.start == datetime(2026, 5, 15, 0, 0, 0, tzinfo=UTC)
    assert r.end == datetime(2026, 5, 19, 0, 0, 0, tzinfo=UTC)
