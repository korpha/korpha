"""Email inbound — Founder's reply to a digest becomes a CEO message.

Two delivery surfaces, same parser:

  1. **Resend Inbound webhook** — Resend POSTs the parsed email to a URL
     we expose. The FastAPI server's ``/api/email/inbound`` endpoint
     wraps this adapter. Recommended for production; webhook delivery
     is real-time + Resend handles SPF/DKIM upstream.

  2. **IMAP polling** — for users not on Resend (their MX runs
     elsewhere, or they don't want a public webhook). Adapter polls
     a configured IMAP mailbox every N seconds, parses each new
     message, dispatches to the channel router.

Both produce the same ``IncomingMessage`` shape so the router doesn't
care which surface fired.

Reply parsing strategy: take the full email body, strip quoted
content (`> ...` lines + a few common email-client signatures), and
treat what's left as the Founder's new message. Heuristic — not
perfect, but it gets 90% of replies right and the cofounder can ask
clarifying questions for the rest.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from korpha.channels.base import ChannelAdapter, IncomingMessage, OutgoingMessage
from korpha.cofounder.model import ThreadPlatform

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Reply body parsing
# ---------------------------------------------------------------------------


_QUOTED_LINE = re.compile(r"^\s*>", re.MULTILINE)
_REPLY_HEADER_PATTERNS = (
    # Gmail single-line: "On Mon, May 4, 2026 at 9:00 AM, Mike
    # <mike@x> wrote:"  — anchor the colon to avoid eating
    # innocent prose that happens to mention "wrote".
    re.compile(r"^On\s+.+\s+wrote:\s*$", re.MULTILINE),
    # Gmail wrapped variant: header spans 2-3 lines because the
    # mailto wrapped — match across newlines up to the colon.
    re.compile(
        r"^On\s+.+?\n.*?wrote:\s*$",
        re.MULTILINE | re.DOTALL,
    ),
    # Outlook English: "From: …\nSent: …\nTo: …" header bloc.
    # The "From:" line alone is enough to anchor the cut.
    re.compile(r"^From:\s+.+$", re.MULTILINE),
    re.compile(r"^Sent:\s+.+$", re.MULTILINE),
    re.compile(r"^Subject:\s+.+$", re.MULTILINE),
    # Outlook localized headers (Spanish / German / French /
    # Portuguese variants we've seen in the wild).
    re.compile(r"^De:\s+.+$", re.MULTILINE),
    re.compile(r"^Von:\s+.+$", re.MULTILINE),
    re.compile(r"^Enviado:\s+.+$", re.MULTILINE),
    re.compile(r"^Gesendet:\s+.+$", re.MULTILINE),
    # Apple Mail: "On <date>, at <time>, X <x@x> wrote:"
    re.compile(r"^Le\s+.+\s+a écrit\s*:?\s*$", re.MULTILINE),
    # ProtonMail attribution
    re.compile(
        r"^.*Sent with Proton Mail.*$",
        re.MULTILINE | re.IGNORECASE,
    ),
    # Resend digest signature
    re.compile(
        r"^---\s*\n.*sent by Korpha.*$",
        re.MULTILINE | re.IGNORECASE,
    ),
    # Generic "----- Original Message -----" dividers
    re.compile(
        r"^-+\s*Original Message\s*-+\s*$",
        re.MULTILINE | re.IGNORECASE,
    ),
    # "Forwarded message" headers (rarer for replies but seen
    # when Mike forwards the digest back to himself).
    re.compile(
        r"^-+\s*Forwarded message\s*-+\s*$",
        re.MULTILINE | re.IGNORECASE,
    ),
    # Yahoo Mail: "On Tuesday, May 4, 2026, X wrote:"
    re.compile(
        r"^On\s+\w+,\s+\w+\s+\d+,\s+\d{4}.+\s+wrote:\s*$",
        re.MULTILINE,
    ),
    # Outlook on Windows insert: "Get Outlook for iOS/Android"
    # signature line — common reply auto-footer.
    re.compile(
        r"^Get Outlook for (iOS|Android)\s*$",
        re.MULTILINE | re.IGNORECASE,
    ),
)
_SIGNATURE_DIVIDER = re.compile(r"^-- ?\s*$", re.MULTILINE)
_SIGNATURE_FALLBACK_DIVIDERS = (
    # "Sent from my iPhone" / "Sent from my iPad" / generic
    # mobile-client trailers — common but not RFC sig markers.
    re.compile(
        r"^Sent from (my )?(iPhone|iPad|Android phone|mobile device).*$",
        re.MULTILINE | re.IGNORECASE,
    ),
)


def parse_reply_body(raw: str) -> str:
    """Extract the Founder's new content from a reply email.

    Strategy (in order):

      1. Cut at any reply-header pattern from the (large) library
         below — Gmail, Outlook (English + ES/DE/FR), Apple Mail,
         Yahoo, ProtonMail, generic 'Original Message' dividers.
         Keeps only what's above the earliest match.
      2. Cut signature blocks. Tries the RFC-compliant ``-- ``
         divider first, then mobile-client signatures
         ('Sent from my iPhone' etc.).
      3. Drop ``> quoted`` lines (interleaved replies).
      4. Strip Outlook/Gmail "[image: foo.png]" inline-image
         placeholders that have no founder content.
      5. Whitespace normalization — collapse triple+ blank
         lines, strip outer whitespace.

    Empty result after parsing returns ``""`` — caller surfaces
    as a blocker rather than dispatching empty messages to CEO.
    """
    text = raw.replace("\r\n", "\n")

    # 1. Cut at reply-header markers
    earliest = len(text)
    for pat in _REPLY_HEADER_PATTERNS:
        m = pat.search(text)
        if m and m.start() < earliest:
            earliest = m.start()
    text = text[:earliest]

    # 2a. Cut at RFC-style signature divider
    sig = _SIGNATURE_DIVIDER.search(text)
    if sig:
        text = text[: sig.start()]

    # 2b. Cut at mobile-client / fallback signature lines
    for pat in _SIGNATURE_FALLBACK_DIVIDERS:
        m = pat.search(text)
        if m:
            text = text[: m.start()]

    # 3. Drop quoted lines
    cleaned_lines = [
        ln for ln in text.split("\n") if not _QUOTED_LINE.match(ln)
    ]
    text = "\n".join(cleaned_lines)

    # 4. Strip empty image placeholders (Gmail/Outlook insert
    # these even when forwarded-html is the only attachment).
    text = re.sub(
        r"^\[image:[^\]]*\]\s*$", "", text,
        flags=re.MULTILINE,
    )

    # 5. Whitespace normalization
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Resend webhook adapter
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ResendInboundPayload:
    """Subset of fields Resend ships in its inbound webhook event.

    Real Resend webhook is a richer envelope (see resend.com/docs);
    we only consume the fields we need so a future schema change
    can't break this adapter unless one of these specific fields
    moves."""

    from_email: str
    from_name: str | None
    to: list[str]
    subject: str
    text: str
    html: str | None = None


def parse_resend_inbound(raw: dict[str, Any]) -> ResendInboundPayload:
    """Pull the relevant fields out of Resend's inbound webhook JSON."""
    if not isinstance(raw, dict):
        raise ValueError("inbound payload must be a JSON object")

    # Resend ships either {data: {...}} or the fields directly depending
    # on event type. Handle both.
    body_candidate = raw.get("data")
    body: dict[str, Any] = body_candidate if isinstance(body_candidate, dict) else raw

    from_field = body.get("from") or {}
    if isinstance(from_field, dict):
        from_email = str(from_field.get("email", "")).strip()
        from_name = from_field.get("name")
    else:
        # Some clients send "Mike <mike@x.com>" as a single string
        m = re.match(r"^(.+?)\s*<(.+?)>$", str(from_field).strip())
        if m:
            from_name = m.group(1).strip()
            from_email = m.group(2).strip()
        else:
            from_email = str(from_field).strip()
            from_name = None

    to_raw = body.get("to") or []
    if isinstance(to_raw, str):
        to = [to_raw.strip()]
    elif isinstance(to_raw, list):
        to = [str(x).strip() for x in to_raw if x]
    else:
        to = []

    return ResendInboundPayload(
        from_email=from_email,
        from_name=from_name,
        to=to,
        subject=str(body.get("subject", "")).strip(),
        text=str(body.get("text", "")),
        html=body.get("html"),
    )


def incoming_from_resend(payload: ResendInboundPayload) -> IncomingMessage:
    """Convert a parsed Resend payload to the channel router's
    ``IncomingMessage``. The reply body is cleaned via ``parse_reply_body``."""
    body = parse_reply_body(payload.text)
    return IncomingMessage(
        platform=ThreadPlatform.EMAIL,
        channel_user_id=payload.from_email,
        text=body,
        display_name=payload.from_name,
        raw={
            "subject": payload.subject,
            "to": payload.to,
            "html_present": payload.html is not None,
        },
    )


# ---------------------------------------------------------------------------
# IMAP polling adapter (for users not on Resend)
# ---------------------------------------------------------------------------


@dataclass
class ImapEmailAdapter(ChannelAdapter):
    """Polls an IMAP mailbox for new messages and yields them as
    ``IncomingMessage``. Most users will use the Resend webhook path
    (real-time, no polling, less infrastructure); this is the
    self-host fallback for IMAP-only setups.

    Configure via env vars:
      EMAIL_IMAP_HOST        — e.g. imap.gmail.com
      EMAIL_IMAP_PORT        — default 993 (SSL)
      EMAIL_IMAP_USER        — full email address
      EMAIL_IMAP_PASSWORD    — app password (Gmail) or account password
      EMAIL_IMAP_FOLDER      — default 'INBOX'
      EMAIL_IMAP_POLL_SECS   — default 60

    Outgoing (replies) routes through ResendEmailNotifier — this
    adapter only handles inbound. (Most users want webhook-or-IMAP
    inbound + Resend outbound; pairing both lets you receive on a
    self-hosted address while sending via Resend's deliverability.)
    """

    platform: ThreadPlatform = ThreadPlatform.EMAIL
    poll_interval_seconds: int = 60
    seen_uids: set[str] = field(default_factory=set, init=False)

    async def stream(self) -> AsyncIterator[IncomingMessage]:
        """Poll IMAP every N seconds, yield new messages.

        Errors during a single poll cycle are logged + the loop
        continues — a transient network glitch should never take down
        the channel permanently."""
        import os

        host = os.getenv("EMAIL_IMAP_HOST")
        if not host:
            raise RuntimeError(
                "ImapEmailAdapter requires EMAIL_IMAP_HOST env var. "
                "See docs/CHANNELS.md for full env var list."
            )

        while True:
            try:
                async for msg in self._poll_once():
                    yield msg
            except Exception as exc:
                logger.warning("IMAP poll cycle failed: %s", exc)
            await asyncio.sleep(self.poll_interval_seconds)

    async def _poll_once(self) -> AsyncIterator[IncomingMessage]:
        """One poll pass: connect, list UNSEEN, parse each new one,
        mark seen. Connection-per-poll keeps the implementation simple
        + handles long-lived connection drops gracefully."""
        import email
        import imaplib
        import os

        host = os.environ["EMAIL_IMAP_HOST"]
        port = int(os.getenv("EMAIL_IMAP_PORT", "993"))
        user = os.environ["EMAIL_IMAP_USER"]
        password = os.environ["EMAIL_IMAP_PASSWORD"]
        folder = os.getenv("EMAIL_IMAP_FOLDER", "INBOX")

        # Run the blocking IMAP work in a thread so we don't stall the
        # event loop.
        def _fetch_unseen() -> list[tuple[str, bytes]]:
            with imaplib.IMAP4_SSL(host, port) as conn:
                conn.login(user, password)
                conn.select(folder)
                _typ, data = conn.search(None, "UNSEEN")
                uids = data[0].split() if data and data[0] else []
                out: list[tuple[str, bytes]] = []
                for uid in uids:
                    uid_str = uid.decode()
                    if uid_str in self.seen_uids:
                        continue
                    _typ2, msg_data = conn.fetch(uid, "(RFC822)")
                    if not msg_data or not isinstance(msg_data[0], tuple):
                        continue
                    raw_bytes = msg_data[0][1]
                    out.append((uid_str, raw_bytes))
                    # Mark the IMAP-side "seen" flag too so other
                    # readers + a fresh process don't redeliver
                    conn.store(uid, "+FLAGS", "\\Seen")
                return out

        unseen = await asyncio.to_thread(_fetch_unseen)
        for uid, raw_bytes in unseen:
            self.seen_uids.add(uid)
            try:
                em = email.message_from_bytes(raw_bytes)
                yield _imap_email_to_incoming(em)
            except Exception as exc:
                logger.warning("failed to parse IMAP message uid=%s: %s", uid, exc)

    async def send(self, message: OutgoingMessage) -> None:
        """Outgoing email is intentionally NOT sent here — this adapter
        is inbound-only. Replies route through the digest pipeline OR
        a separate Resend send so the outbound deliverability story
        stays unified. Raises NotImplementedError so accidental wiring
        surfaces immediately."""
        raise NotImplementedError(
            "ImapEmailAdapter is inbound-only. Send replies via "
            "ResendEmailNotifier or the digest pipeline."
        )

    async def close(self) -> None:
        """Inbound-only adapter — there's no persistent connection to
        tear down (each ``_poll_once`` opens + closes its own IMAP4_SSL).
        We just clear the seen-UID set so a fresh start is fresh."""
        self.seen_uids.clear()


def _imap_email_to_incoming(em: Any) -> IncomingMessage:
    """Take a stdlib email.Message and produce an IncomingMessage."""
    import email.utils

    from_header = em.get("From", "")
    name, addr = email.utils.parseaddr(from_header)

    # Pull the text/plain part if multipart, else the bare payload
    body = ""
    if em.is_multipart():
        for part in em.walk():
            if part.get_content_type() == "text/plain":
                charset = part.get_content_charset() or "utf-8"
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    body = payload.decode(charset, errors="replace")
                    break
    else:
        payload = em.get_payload(decode=True)
        charset = em.get_content_charset() or "utf-8"
        if isinstance(payload, bytes):
            body = payload.decode(charset, errors="replace")
        else:
            body = str(payload or "")

    cleaned = parse_reply_body(body)
    return IncomingMessage(
        platform=ThreadPlatform.EMAIL,
        channel_user_id=addr,
        text=cleaned,
        display_name=name or None,
        raw={"subject": em.get("Subject", "")},
    )


__all__ = [
    "ImapEmailAdapter",
    "ResendInboundPayload",
    "incoming_from_resend",
    "parse_reply_body",
    "parse_resend_inbound",
]


# ---------------------------------------------------------------------------
# Self-register the IMAP variant. Resend webhook flow doesn't need a
# registry entry — it's a stateless POST handler, not a long-running
# adapter. Only IMAP fits the ChannelAdapter ABC.
# ---------------------------------------------------------------------------


def _register_email_imap() -> None:
    import os

    from korpha.channels.registry import (
        PlatformEntry,
        platform_registry,
    )

    def _factory(cfg: Any) -> ImapEmailAdapter:
        # Config could be a dataclass or an env-driven dict; pull
        # the fields ImapEmailAdapter needs. Defaults match the
        # docstring above.
        get = cfg.get if isinstance(cfg, dict) else lambda k, d=None: getattr(cfg, k, d)
        adapter = ImapEmailAdapter(
            poll_interval_seconds=int(
                get("poll_interval_seconds", os.getenv("EMAIL_IMAP_POLL_SECS", "60"))
            ),
        )
        return adapter

    def _validate(cfg: Any) -> bool:
        # IMAP is fully env-driven today; we only require host + user.
        return bool(
            os.getenv("EMAIL_IMAP_HOST") and os.getenv("EMAIL_IMAP_USER")
        )

    platform_registry.register(PlatformEntry(
        name=ThreadPlatform.EMAIL.value,
        label="Email (IMAP)",
        adapter_factory=_factory,
        check_fn=lambda: True,  # imaplib is stdlib
        validate_config=_validate,
        required_env=[
            "EMAIL_IMAP_HOST",
            "EMAIL_IMAP_USER",
            "EMAIL_IMAP_PASSWORD",
        ],
        install_hint="",
        source="builtin",
        emoji="✉️",
    ))


_register_email_imap()
