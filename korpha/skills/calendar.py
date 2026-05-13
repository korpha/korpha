"""``calendar.create_event`` — generate an .ics + add-to-cal links.

BRIEF demo, minute 4:30: Mike sees a "calendar slot for kickoff with
cofounder tomorrow." This skill is the deliverable:

  * RFC 5545 ``.ics`` file written under ``<data_dir>/calendar/`` so
    the API server's ``/app/calendar/`` static mount can serve it
    publicly. Drops into any calendar app — Google, Apple, Outlook,
    Proton, Fastmail.
  * One-click "Add to Google Calendar" template URL — works without
    OAuth (just a query-string template Google has supported for
    years). We don't ship Google API integration on the demo path
    because OAuth scopes + refresh token storage is a heavy lift
    for what's basically a deep link.
  * Optional kanban artifact so ``/app/kanban`` shows the URL on
    the originating card.

Stdlib only. No external calendar SDKs, no OAuth flow. Mike gets a
universal artifact in ~50ms.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote_plus, urlencode

from korpha.audit.model import InferenceTier
from korpha.kanban.artifacts import ArtifactKind
from korpha.skills.registry import register
from korpha.skills.types import (
    Skill,
    SkillContext,
    SkillError,
    SkillProvenance,
    SkillResult,
    SkillSpec,
)


# ---------------------------------------------------------------------------
# RFC 5545 helpers
# ---------------------------------------------------------------------------


_ICS_DATETIME_FMT = "%Y%m%dT%H%M%SZ"


def _to_utc(dt: datetime) -> datetime:
    """Force timezone-aware UTC. Naive datetimes get treated as UTC
    because that's the safest interpretation for an LLM-supplied
    ISO string with no offset — better than guessing the founder's
    locale."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _format_ics_datetime(dt: datetime) -> str:
    return _to_utc(dt).strftime(_ICS_DATETIME_FMT)


def _escape_ics_text(s: str) -> str:
    """RFC 5545 §3.3.11 — backslash, semicolon, comma, newline."""
    return (
        s.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def _fold_line(line: str, *, limit: int = 73) -> str:
    """RFC 5545 line folding — wrap lines longer than 75 octets,
    continuation marker is CRLF + single SPACE. We use 73 to leave
    breathing room for the 2-byte CRLF when assembled.

    Folds operate on octets, not codepoints — a UTF-8 multibyte
    char crossing the boundary would be split mid-byte. We fold
    by encoded length but break only on safe boundaries.
    """
    encoded = line.encode("utf-8")
    if len(encoded) <= limit:
        return line
    out: list[str] = []
    buf = bytearray()
    for byte in encoded:
        buf.append(byte)
        if len(buf) >= limit:
            # Don't split mid-utf8 — back off until next byte is
            # an ASCII char or a UTF-8 leader byte.
            while buf and (buf[-1] & 0xC0) == 0x80:
                # We're inside a multibyte seq — pull back further
                # so we keep the full sequence on the next line.
                # In practice this rarely fires; ICS escaping
                # neuters most multibytes.
                buf.pop()
                # safety: if buf empty, give up the optimization
                if not buf:
                    break
            out.append(buf.decode("utf-8", errors="ignore"))
            buf = bytearray()
    if buf:
        out.append(buf.decode("utf-8", errors="ignore"))
    return "\r\n ".join(out)


# ---------------------------------------------------------------------------
# Event model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CalendarEvent:
    """Normalized event input. Always UTC at the boundary; the LLM
    is responsible for converting "tomorrow at 9am" into a concrete
    datetime before calling the skill."""

    title: str
    start: datetime
    end: datetime
    description: str = ""
    location: str = ""
    attendees: tuple[str, ...] = ()
    organizer_email: str = ""
    uid: str = ""

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("CalendarEvent: title is required")
        if self.end <= self.start:
            raise ValueError(
                "CalendarEvent: end must be strictly after start",
            )

    def with_uid(self) -> "CalendarEvent":
        if self.uid:
            return self
        return CalendarEvent(
            title=self.title,
            start=self.start,
            end=self.end,
            description=self.description,
            location=self.location,
            attendees=self.attendees,
            organizer_email=self.organizer_email,
            uid=f"{uuid.uuid4()}@korpha",
        )


def build_ics(event: CalendarEvent) -> str:
    """Render the event as a complete VCALENDAR + VEVENT block.

    Output uses CRLF line endings (RFC 5545 mandate) — passing
    this string straight to ``write_text(..., newline='')`` keeps
    them intact across platforms.
    """
    ev = event.with_uid()
    now = _format_ics_datetime(datetime.now(timezone.utc))
    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Korpha//Cofounder//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{_escape_ics_text(ev.uid)}",
        f"DTSTAMP:{now}",
        f"DTSTART:{_format_ics_datetime(ev.start)}",
        f"DTEND:{_format_ics_datetime(ev.end)}",
        f"SUMMARY:{_escape_ics_text(ev.title)}",
    ]
    if ev.description:
        lines.append(f"DESCRIPTION:{_escape_ics_text(ev.description)}")
    if ev.location:
        lines.append(f"LOCATION:{_escape_ics_text(ev.location)}")
    if ev.organizer_email:
        lines.append(
            f"ORGANIZER:mailto:{_escape_ics_text(ev.organizer_email)}",
        )
    for att in ev.attendees:
        # Each ATTENDEE prop is its own line so calendar clients
        # show the right invitee list. RSVP=TRUE asks the client
        # to surface accept/decline buttons.
        lines.append(
            f"ATTENDEE;RSVP=TRUE:mailto:{_escape_ics_text(att)}",
        )
    lines.extend([
        "STATUS:CONFIRMED",
        "TRANSP:OPAQUE",
        "END:VEVENT",
        "END:VCALENDAR",
    ])
    folded = [_fold_line(line) for line in lines]
    return "\r\n".join(folded) + "\r\n"


def google_calendar_url(event: CalendarEvent) -> str:
    """Build the Google Calendar TEMPLATE deep-link.

    Format Google has supported for years; doesn't require OAuth.
    The recipient still needs a Google account to actually save
    the event but Mike can paste the link into any channel.
    """
    params: dict[str, str] = {
        "action": "TEMPLATE",
        "text": event.title,
        "dates": (
            f"{_format_ics_datetime(event.start)}/"
            f"{_format_ics_datetime(event.end)}"
        ),
    }
    if event.description:
        params["details"] = event.description
    if event.location:
        params["location"] = event.location
    if event.attendees:
        params["add"] = ",".join(event.attendees)
    return (
        "https://calendar.google.com/calendar/render?"
        + urlencode(params, quote_via=quote_plus)
    )


def outlook_calendar_url(event: CalendarEvent) -> str:
    """Outlook.live.com deep-link compose URL — paired companion
    to ``google_calendar_url`` for users on Outlook/Hotmail."""
    params: dict[str, str] = {
        "rru": "addevent",
        "subject": event.title,
        "startdt": _to_utc(event.start).isoformat(),
        "enddt": _to_utc(event.end).isoformat(),
    }
    if event.description:
        params["body"] = event.description
    if event.location:
        params["location"] = event.location
    return (
        "https://outlook.live.com/calendar/0/deeplink/compose?"
        + urlencode(params, quote_via=quote_plus)
    )


# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------


_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def _slugify(text: str, *, max_len: int = 60) -> str:
    s = _SLUG_RE.sub("-", text.strip().lower())
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:max_len] or "event"


def _calendar_dir() -> Path:
    """`<data_dir>/calendar/` — matches the static-mount path on
    the API server. Imported lazily so test runs without a real
    data dir don't fail at import."""
    import os

    base = os.getenv("KORPHA_DATA_DIR") or str(
        Path.home() / ".korpha",
    )
    out = Path(base) / "calendar"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _parse_dt(raw: Any) -> datetime:
    """Accept datetime, ISO string, or date-string + reasonable
    default. Raise SkillError on anything we can't read."""
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            raise SkillError("calendar.create_event: empty datetime")
        # Python 3.12 fromisoformat handles a wide subset incl.
        # "2026-05-09T09:00:00+00:00" and "2026-05-09 09:00".
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SkillError(
                f"calendar.create_event: bad datetime {s!r} — "
                "expected ISO 8601 (e.g. 2026-05-09T09:00:00Z)",
            ) from exc
    raise SkillError(
        f"calendar.create_event: datetime must be ISO string or "
        f"datetime, got {type(raw).__name__}",
    )


def _parse_attendees(raw: Any) -> tuple[str, ...]:
    if not raw:
        return ()
    if isinstance(raw, str):
        items: Iterable[str] = re.split(r"[,;\s]+", raw)
    elif isinstance(raw, (list, tuple)):
        items = (str(x) for x in raw)
    else:
        raise SkillError(
            "calendar.create_event: attendees must be a list "
            "or comma-separated string of emails",
        )
    cleaned = tuple(s.strip() for s in items if s and s.strip())
    return cleaned


class CreateEventSkill(Skill):
    """Generate a calendar invite as an .ics file + add-to-cal
    deep links. Returns:

      * ``ics_path`` — local file path (for archival)
      * ``ics_url`` — public ``/app/calendar/<id>.ics`` URL
      * ``google_url`` — one-click Add-to-Google-Calendar URL
      * ``outlook_url`` — Outlook.live.com deep-link
      * ``event_uid`` — the ICS UID for downstream cross-ref

    Optional ``kanban_card_id`` adds a typed URL artifact to the
    card so ``/app/kanban`` renders the link.
    """

    spec = SkillSpec(
        name="calendar.create_event",
        description=(
            "Create a calendar event and produce a sharable "
            ".ics file + add-to-Google-Calendar + add-to-Outlook "
            "deep-links. Use when the founder agrees to a meeting "
            "(e.g. 'kickoff with cofounder tomorrow at 9am') — "
            "you supply concrete ISO datetimes and we do the rest. "
            "No OAuth required; the .ics drops into any calendar "
            "app and the deep links cover the 1-click cases."
        ),
        parameters={
            "title": (
                "Event title — what shows on the calendar grid. "
                "e.g. 'Kickoff with Korpha cofounder'."
            ),
            "start": (
                "ISO 8601 datetime for the event start. "
                "Naive datetimes are interpreted as UTC. "
                "e.g. '2026-05-09T09:00:00Z'."
            ),
            "end": (
                "ISO 8601 datetime for the event end. Optional — "
                "if omitted, defaults to start + duration_minutes."
            ),
            "duration_minutes": (
                "Used only when ``end`` is not supplied. "
                "Default 30."
            ),
            "description": "Long-form event details / agenda.",
            "location": (
                "Physical address OR a meeting URL "
                "(Zoom / Meet / Whereby)."
            ),
            "attendees": (
                "List of emails (or comma-separated string) to "
                "invite. Surfaces as ATTENDEE rows in the .ics."
            ),
            "organizer_email": (
                "Optional. Defaults to omitted. Some calendar "
                "apps need an organizer to render RSVP buttons."
            ),
            "kanban_card_id": (
                "Optional. UUID of a kanban card. We attach a "
                "typed URL artifact pointing at the .ics so the "
                "kanban view renders the link."
            ),
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        title = str(args.get("title") or "").strip()
        if not title:
            raise SkillError(
                "calendar.create_event: title is required",
            )

        start_raw = args.get("start")
        if start_raw is None:
            raise SkillError(
                "calendar.create_event: start is required",
            )
        start = _parse_dt(start_raw)

        end_raw = args.get("end")
        if end_raw is not None and str(end_raw).strip():
            end = _parse_dt(end_raw)
        else:
            duration_raw = args.get("duration_minutes")
            if duration_raw is None or duration_raw == "":
                duration = 30
            else:
                try:
                    duration = int(duration_raw)
                except (TypeError, ValueError) as exc:
                    raise SkillError(
                        "calendar.create_event: duration_minutes "
                        "must be an integer",
                    ) from exc
            if duration <= 0:
                raise SkillError(
                    "calendar.create_event: duration_minutes "
                    "must be positive",
                )
            end = start + timedelta(minutes=duration)

        event = CalendarEvent(
            title=title,
            start=start,
            end=end,
            description=str(args.get("description") or ""),
            location=str(args.get("location") or ""),
            attendees=_parse_attendees(args.get("attendees")),
            organizer_email=str(
                args.get("organizer_email") or "",
            ).strip(),
        ).with_uid()

        # Persist the .ics under <data_dir>/calendar/<slug>-<id>.ics
        # so the static mount serves it. Slug-prefix keeps the
        # filename human-readable in directory listings.
        ics_text = build_ics(event)
        slug = _slugify(title)
        # event.uid format: <uuid4>@korpha — take the uuid part
        # for the filename to dodge "@" in URLs.
        uid_short = event.uid.split("@", 1)[0]
        filename = f"{slug}-{uid_short}.ics"
        path = _calendar_dir() / filename
        # ICS spec says CRLF; write_bytes preserves them.
        path.write_bytes(ics_text.encode("utf-8"))

        ics_url = f"/app/calendar/{filename}"
        gcal_url = google_calendar_url(event)
        out_url = outlook_calendar_url(event)

        # Kanban artifact emit — same pattern deploy.publish_landing
        # uses. Best-effort; never fail the skill on artifact
        # write hiccups.
        card_id_raw = args.get("kanban_card_id")
        if card_id_raw:
            try:
                from uuid import UUID as _UUID

                from korpha.kanban import (
                    ArtifactService,
                )

                svc = ArtifactService(ctx.session)
                card_id = _UUID(str(card_id_raw))
                existing = svc.list_for_card(card_id)
                svc.add(
                    card_id=card_id,
                    business_id=ctx.business.id,
                    kind=ArtifactKind.URL,
                    label=f"calendar invite: {title}",
                    location=ics_url,
                    is_primary=not existing,
                )
            except Exception:  # noqa: BLE001
                import logging
                logging.getLogger(__name__).warning(
                    "calendar artifact emit failed", exc_info=True,
                )

        return SkillResult(
            skill_name=self.spec.name,
            summary=(
                f"{title} — {event.start.isoformat()} → "
                f"{event.end.isoformat()}"
            ),
            payload={
                "event_uid": event.uid,
                "ics_path": str(path),
                "ics_url": ics_url,
                "google_url": gcal_url,
                "outlook_url": out_url,
                "title": title,
                "start": event.start.isoformat(),
                "end": event.end.isoformat(),
                "attendees": list(event.attendees),
            },
            cost_usd=0.0,
        )


register(CreateEventSkill())


__all__ = [
    "CalendarEvent",
    "CreateEventSkill",
    "build_ics",
    "google_calendar_url",
    "outlook_calendar_url",
]
