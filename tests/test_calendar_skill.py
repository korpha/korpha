"""Tests for calendar.create_event — ICS generation + Google/Outlook
add-link URLs + the skill that ties it together."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from sqlmodel import Session

from korpha.business.model import Business
from korpha.identity.model import Founder
from korpha.skills import default_registry
from korpha.skills.calendar import (
    CalendarEvent,
    build_ics,
    google_calendar_url,
    outlook_calendar_url,
)
from korpha.skills.types import SkillContext, SkillError


# ---------------------------------------------------------------------------
# CalendarEvent validation
# ---------------------------------------------------------------------------


def _utc(y: int, mo: int, d: int, h: int = 9, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def test_event_requires_title() -> None:
    with pytest.raises(ValueError, match="title"):
        CalendarEvent(
            title="   ",
            start=_utc(2026, 5, 9),
            end=_utc(2026, 5, 9, 9, 30),
        )


def test_event_end_after_start() -> None:
    with pytest.raises(ValueError, match="end"):
        CalendarEvent(
            title="Kickoff",
            start=_utc(2026, 5, 9, 9, 30),
            end=_utc(2026, 5, 9, 9),
        )


def test_event_with_uid_assigns_when_missing() -> None:
    ev = CalendarEvent(
        title="Kickoff",
        start=_utc(2026, 5, 9),
        end=_utc(2026, 5, 9, 9, 30),
    ).with_uid()
    assert ev.uid.endswith("@korpha")
    # Idempotent — second call doesn't re-roll.
    assert ev.with_uid().uid == ev.uid


# ---------------------------------------------------------------------------
# build_ics — RFC 5545 shape
# ---------------------------------------------------------------------------


def test_ics_has_envelope() -> None:
    ev = CalendarEvent(
        title="Kickoff",
        start=_utc(2026, 5, 9),
        end=_utc(2026, 5, 9, 9, 30),
    )
    ics = build_ics(ev)
    assert ics.startswith("BEGIN:VCALENDAR\r\n")
    assert "BEGIN:VEVENT\r\n" in ics
    assert "END:VEVENT\r\n" in ics
    assert ics.rstrip("\r\n").endswith("END:VCALENDAR")


def test_ics_uses_crlf_line_endings() -> None:
    """RFC 5545 mandates CRLF — non-CRLF parsers exist but the
    standard ones (Apple, Google) reject mixed."""
    ev = CalendarEvent(
        title="Kickoff",
        start=_utc(2026, 5, 9),
        end=_utc(2026, 5, 9, 9, 30),
    )
    ics = build_ics(ev)
    # No bare LF — every \n must be preceded by \r.
    bare_lf = [
        i for i in range(1, len(ics))
        if ics[i] == "\n" and ics[i - 1] != "\r"
    ]
    assert not bare_lf, f"bare LFs at offsets {bare_lf[:5]}"


def test_ics_has_required_fields() -> None:
    ev = CalendarEvent(
        title="Kickoff",
        start=_utc(2026, 5, 9, 9),
        end=_utc(2026, 5, 9, 9, 30),
        description="day-1 plan",
        location="https://meet.google.com/abc",
        attendees=("mike@x.com", "co@korpha.example"),
    )
    ics = build_ics(ev)
    assert "DTSTART:20260509T090000Z" in ics
    assert "DTEND:20260509T093000Z" in ics
    assert "SUMMARY:Kickoff" in ics
    assert "DESCRIPTION:day-1 plan" in ics
    assert "LOCATION:https://meet.google.com/abc" in ics
    assert "ATTENDEE;RSVP=TRUE:mailto:mike@x.com" in ics
    assert "ATTENDEE;RSVP=TRUE:mailto:co@korpha.example" in ics


def test_ics_escapes_special_chars() -> None:
    """RFC 5545 §3.3.11 — backslash, semicolon, comma, newline."""
    ev = CalendarEvent(
        title="Kick;off, planning\\day",
        start=_utc(2026, 5, 9),
        end=_utc(2026, 5, 9, 9, 30),
        description="line one\nline two",
    )
    ics = build_ics(ev)
    # Semicolon, comma, backslash, newline all escaped
    assert r"SUMMARY:Kick\;off\, planning\\day" in ics
    assert r"DESCRIPTION:line one\nline two" in ics


def test_ics_naive_datetime_treated_as_utc() -> None:
    """Naive datetime input — most LLM-supplied ISO strings — gets
    treated as UTC rather than rejected. Safer than guessing locale."""
    ev = CalendarEvent(
        title="Kickoff",
        start=datetime(2026, 5, 9, 9, 0),  # naive
        end=datetime(2026, 5, 9, 9, 30),  # naive
    )
    ics = build_ics(ev)
    assert "DTSTART:20260509T090000Z" in ics


def test_ics_handles_non_utc_input() -> None:
    """Aware datetime in another offset gets converted to UTC."""
    pst = timezone(timedelta(hours=-8))
    ev = CalendarEvent(
        title="Kickoff",
        # 09:00 PST → 17:00 UTC
        start=datetime(2026, 5, 9, 9, 0, tzinfo=pst),
        end=datetime(2026, 5, 9, 9, 30, tzinfo=pst),
    )
    ics = build_ics(ev)
    assert "DTSTART:20260509T170000Z" in ics
    assert "DTEND:20260509T173000Z" in ics


def test_ics_omits_optional_when_blank() -> None:
    ev = CalendarEvent(
        title="Kickoff",
        start=_utc(2026, 5, 9),
        end=_utc(2026, 5, 9, 9, 30),
    )
    ics = build_ics(ev)
    assert "DESCRIPTION" not in ics
    assert "LOCATION" not in ics
    assert "ATTENDEE" not in ics
    assert "ORGANIZER" not in ics


def test_ics_folds_long_lines() -> None:
    """RFC 5545 §3.1 — lines >75 octets must fold. Calendar
    parsers reject overlong lines outright."""
    ev = CalendarEvent(
        title="Kickoff",
        start=_utc(2026, 5, 9),
        end=_utc(2026, 5, 9, 9, 30),
        description="x" * 300,
    )
    ics = build_ics(ev)
    for raw in ics.split("\r\n"):
        # Continuation lines start with a space — those are allowed
        # to be long because they're inside a folded sequence.
        assert len(raw.encode("utf-8")) <= 200, raw


# ---------------------------------------------------------------------------
# Add-to-cal deep links
# ---------------------------------------------------------------------------


def test_google_url_carries_required_params() -> None:
    ev = CalendarEvent(
        title="Kickoff with cofounder",
        start=_utc(2026, 5, 9, 9),
        end=_utc(2026, 5, 9, 9, 30),
        description="day-1 plan",
        location="https://meet.google.com/abc",
        attendees=("mike@x.com",),
    )
    url = google_calendar_url(ev)
    parsed = urlparse(url)
    assert parsed.netloc == "calendar.google.com"
    assert parsed.path == "/calendar/render"
    qs = parse_qs(parsed.query)
    assert qs["action"] == ["TEMPLATE"]
    assert qs["text"] == ["Kickoff with cofounder"]
    assert qs["dates"] == ["20260509T090000Z/20260509T093000Z"]
    assert qs["details"] == ["day-1 plan"]
    assert qs["location"] == ["https://meet.google.com/abc"]
    assert qs["add"] == ["mike@x.com"]


def test_google_url_omits_optional() -> None:
    ev = CalendarEvent(
        title="Kickoff",
        start=_utc(2026, 5, 9),
        end=_utc(2026, 5, 9, 9, 30),
    )
    url = google_calendar_url(ev)
    qs = parse_qs(urlparse(url).query)
    assert "details" not in qs
    assert "location" not in qs
    assert "add" not in qs


def test_outlook_url_uses_iso_datetimes() -> None:
    """Outlook expects ISO 8601 with offset, not the compact
    RFC 5545 form Google takes."""
    ev = CalendarEvent(
        title="Kickoff",
        start=_utc(2026, 5, 9, 9),
        end=_utc(2026, 5, 9, 9, 30),
    )
    url = outlook_calendar_url(ev)
    qs = parse_qs(urlparse(url).query)
    assert qs["rru"] == ["addevent"]
    assert qs["subject"] == ["Kickoff"]
    assert qs["startdt"] == ["2026-05-09T09:00:00+00:00"]
    assert qs["enddt"] == ["2026-05-09T09:30:00+00:00"]


# ---------------------------------------------------------------------------
# CreateEventSkill — skill end-to-end
# ---------------------------------------------------------------------------


def _ctx(session: Session, business: Business, founder: Founder) -> SkillContext:
    from korpha.inference.cost_tracker import CostTracker
    from korpha.inference.pool import InferencePool

    return SkillContext(
        business=business, founder=founder, session=session,
        cost_tracker=CostTracker(pool=InferencePool(
            providers=[], accounts=[],
        )),
    )


@pytest.fixture
def calendar_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Path:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    return tmp_path / "calendar"


@pytest.mark.asyncio
async def test_skill_writes_ics_file(
    session: Session, business: Business, founder: Founder,
    calendar_root: Path,
) -> None:
    skill = default_registry.skills["calendar.create_event"]
    result = await skill.run(
        ctx=_ctx(session, business, founder),
        args={
            "title": "Kickoff with cofounder",
            "start": "2026-05-09T09:00:00Z",
            "duration_minutes": 30,
            "description": "Day-1 plan review",
            "attendees": ["mike@x.com"],
        },
    )
    ics_path = Path(result.payload["ics_path"])
    assert ics_path.is_file()
    content = ics_path.read_bytes().decode("utf-8")
    assert "BEGIN:VCALENDAR" in content
    assert "SUMMARY:Kickoff with cofounder" in content
    assert "DTSTART:20260509T090000Z" in content
    # The static-mount URL is what dashboards / channels use.
    assert result.payload["ics_url"].startswith("/app/calendar/")
    assert result.payload["ics_url"].endswith(".ics")
    assert ics_path.parent == calendar_root


@pytest.mark.asyncio
async def test_skill_returns_addto_links(
    session: Session, business: Business, founder: Founder,
    calendar_root: Path,
) -> None:
    skill = default_registry.skills["calendar.create_event"]
    result = await skill.run(
        ctx=_ctx(session, business, founder),
        args={
            "title": "Kickoff",
            "start": "2026-05-09T09:00:00Z",
            "end": "2026-05-09T09:30:00Z",
        },
    )
    assert result.payload["google_url"].startswith(
        "https://calendar.google.com/calendar/render?",
    )
    assert result.payload["outlook_url"].startswith(
        "https://outlook.live.com/calendar/0/deeplink/compose?",
    )


@pytest.mark.asyncio
async def test_skill_defaults_duration_to_30min(
    session: Session, business: Business, founder: Founder,
    calendar_root: Path,
) -> None:
    skill = default_registry.skills["calendar.create_event"]
    result = await skill.run(
        ctx=_ctx(session, business, founder),
        args={
            "title": "Kickoff",
            "start": "2026-05-09T09:00:00Z",
        },
    )
    # 30 min from 09:00 → 09:30
    assert result.payload["end"].endswith("09:30:00+00:00")


@pytest.mark.asyncio
async def test_skill_rejects_missing_title(
    session: Session, business: Business, founder: Founder,
    calendar_root: Path,
) -> None:
    skill = default_registry.skills["calendar.create_event"]
    with pytest.raises(SkillError, match="title"):
        await skill.run(
            ctx=_ctx(session, business, founder),
            args={"start": "2026-05-09T09:00:00Z"},
        )


@pytest.mark.asyncio
async def test_skill_rejects_missing_start(
    session: Session, business: Business, founder: Founder,
    calendar_root: Path,
) -> None:
    skill = default_registry.skills["calendar.create_event"]
    with pytest.raises(SkillError, match="start"):
        await skill.run(
            ctx=_ctx(session, business, founder),
            args={"title": "Kickoff"},
        )


@pytest.mark.asyncio
async def test_skill_rejects_bad_iso(
    session: Session, business: Business, founder: Founder,
    calendar_root: Path,
) -> None:
    skill = default_registry.skills["calendar.create_event"]
    with pytest.raises(SkillError, match="ISO 8601"):
        await skill.run(
            ctx=_ctx(session, business, founder),
            args={
                "title": "Kickoff",
                "start": "next tuesday at 9",
            },
        )


@pytest.mark.asyncio
async def test_skill_rejects_zero_duration(
    session: Session, business: Business, founder: Founder,
    calendar_root: Path,
) -> None:
    skill = default_registry.skills["calendar.create_event"]
    with pytest.raises(SkillError, match="positive"):
        await skill.run(
            ctx=_ctx(session, business, founder),
            args={
                "title": "Kickoff",
                "start": "2026-05-09T09:00:00Z",
                "duration_minutes": 0,
            },
        )


@pytest.mark.asyncio
async def test_skill_attaches_kanban_artifact(
    session: Session, business: Business, founder: Founder,
    calendar_root: Path,
) -> None:
    """When ``kanban_card_id`` is given, a URL artifact is added to
    the card so /app/kanban renders the .ics link."""
    from korpha.kanban import (
        ArtifactService, CreateCardInput, KanbanBoard,
    )

    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id,
        title="Set up day-1 kickoff",
    ))
    skill = default_registry.skills["calendar.create_event"]
    result = await skill.run(
        ctx=_ctx(session, business, founder),
        args={
            "title": "Kickoff with cofounder",
            "start": "2026-05-09T09:00:00Z",
            "kanban_card_id": str(card.id),
        },
    )
    artifacts = ArtifactService(session).list_for_card(card.id)
    assert len(artifacts) == 1
    art = artifacts[0]
    assert art.location == result.payload["ics_url"]
    assert "calendar invite" in art.label.lower()


@pytest.mark.asyncio
async def test_skill_attendees_accept_csv_string(
    session: Session, business: Business, founder: Founder,
    calendar_root: Path,
) -> None:
    """LLMs sometimes pass attendees as 'a@x, b@x'; we accept it."""
    skill = default_registry.skills["calendar.create_event"]
    result = await skill.run(
        ctx=_ctx(session, business, founder),
        args={
            "title": "Kickoff",
            "start": "2026-05-09T09:00:00Z",
            "attendees": "mike@x.com, co@korpha.example",
        },
    )
    assert result.payload["attendees"] == [
        "mike@x.com", "co@korpha.example",
    ]
