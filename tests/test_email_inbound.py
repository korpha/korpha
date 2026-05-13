"""Email inbound — reply-body parser + Resend webhook adapter tests.

The parser is the high-leverage code: gets it wrong and Mike's reply
"yes ship it" gets mixed with the quoted digest below + the CEO sees
500 words of stale context. Heavy table-driven tests below.

Webhook payload parsing tested in isolation; full HTTP roundtrip
exercised in tests/test_api*.py.
"""
from __future__ import annotations

import pytest

from korpha.channels.email_inbound import (
    incoming_from_resend,
    parse_reply_body,
    parse_resend_inbound,
)
from korpha.cofounder.model import ThreadPlatform

# ---------------------------------------------------------------------------
# parse_reply_body — the body extractor
# ---------------------------------------------------------------------------


def test_simple_reply_no_quote() -> None:
    """No quoted content — return whole body verbatim."""
    body = "yes, ship it.\n\nthanks"
    assert parse_reply_body(body) == "yes, ship it.\n\nthanks"


def test_reply_with_on_wrote_header() -> None:
    """Most common case: 'On … wrote:' divider — keep only top."""
    body = (
        "yes, ship it.\n\n"
        "On Mon, May 4, 2026 at 9:00 AM, Korpha <cofounder@x.com> wrote:\n"
        "> here's the digest\n"
        "> ...\n"
    )
    assert parse_reply_body(body) == "yes, ship it."


def test_reply_with_outlook_style_header() -> None:
    body = (
        "approve all of these\n\n"
        "From: Korpha Cofounder\n"
        "Sent: Monday, May 4, 2026 9:00 AM\n"
        "To: mike@x.com\n"
        "Subject: Daily digest\n\n"
        "[full digest body]\n"
    )
    assert parse_reply_body(body) == "approve all of these"


def test_reply_with_original_message_divider() -> None:
    body = (
        "deny — let me think\n\n"
        "----- Original Message -----\n"
        "[stuff]\n"
    )
    assert parse_reply_body(body) == "deny — let me think"


def test_reply_strips_quoted_lines() -> None:
    """Even without a header, drop any > prefixed lines (some clients
    quote inline without an 'On … wrote:' marker)."""
    body = (
        "let's go with the second variant\n"
        "> 1. Variant A\n"
        "> 2. Variant B  ← my pick\n"
        "> 3. Variant C\n"
    )
    assert parse_reply_body(body) == "let's go with the second variant"


def test_reply_strips_signature_block() -> None:
    """`--` standalone divider marks signature start — drop everything
    after it. (Common email convention.)"""
    body = (
        "approve\n\n"
        "--\n"
        "Mike\n"
        "founder@widgetco.com\n"
    )
    assert parse_reply_body(body) == "approve"


def test_reply_collapses_whitespace() -> None:
    """Triple+ blank lines collapse to one — keeps the cleaned-up
    output tight when stripping quotes leaves gaps."""
    body = "first line\n\n\n\nsecond line"
    assert parse_reply_body(body) == "first line\n\nsecond line"


def test_reply_korpha_signature_dropped() -> None:
    body = (
        "approve\n\n"
        "---\n"
        "sent by Korpha cofounder\n"
        "[unsubscribe link]\n"
    )
    assert parse_reply_body(body) == "approve"


def test_empty_body_returns_empty() -> None:
    assert parse_reply_body("") == ""


def test_only_quoted_returns_empty() -> None:
    """A reply with NOTHING but quoted content — that's a fwd / accidental
    empty reply. Caller surfaces as a no-op rather than dispatching
    a blank message to CEO."""
    body = "> first\n> second\n> third"
    assert parse_reply_body(body) == ""


def test_multiple_headers_take_earliest() -> None:
    """When both 'On … wrote:' and 'From:' appear, cut at the earliest
    one — don't accidentally retain content between them."""
    body = (
        "real reply\n\n"
        "On Mon wrote:\n"
        "> A\n\n"
        "From: someone\n"
        "Sent: somewhere\n"
    )
    assert parse_reply_body(body) == "real reply"


def test_crlf_normalized() -> None:
    """Outlook + some Windows clients send \\r\\n line endings — must
    handle the same as \\n."""
    body = "approve\r\n\r\nOn Mon wrote:\r\n> stale"
    assert parse_reply_body(body) == "approve"


# ---------------------------------------------------------------------------
# parse_reply_body — extended real-world patterns (#214)
# ---------------------------------------------------------------------------


def test_reply_outlook_spanish_header() -> None:
    """Spanish-locale Outlook header: 'De: …'"""
    body = (
        "aprobado, adelante\n\n"
        "De: Korpha Cofounder <cofounder@x.com>\n"
        "Enviado: lunes 4 de mayo de 2026 9:00\n"
        "Para: mike@x.com\n"
        "Asunto: Resumen diario\n\n"
        "[contenido del resumen]\n"
    )
    assert parse_reply_body(body) == "aprobado, adelante"


def test_reply_outlook_german_header() -> None:
    body = (
        "ja, weitermachen\n\n"
        "Von: Korpha Cofounder\n"
        "Gesendet: Montag, 4. Mai 2026 09:00\n"
        "An: mike@x.com\n"
    )
    assert parse_reply_body(body) == "ja, weitermachen"


def test_reply_apple_mail_french_header() -> None:
    """Apple Mail French: 'Le 4 mai 2026 à 09:00, X a écrit:'"""
    body = (
        "d'accord, on lance\n\n"
        "Le 4 mai 2026 à 09:00, Korpha <cofounder@x.com> a écrit :\n"
        "> contenu du résumé\n"
    )
    assert parse_reply_body(body) == "d'accord, on lance"


def test_reply_yahoo_mail_attribution() -> None:
    body = (
        "ship variant B\n\n"
        "On Tuesday, May 4, 2026, Korpha <cofounder@x.com> wrote:\n"
        "> details\n"
    )
    assert parse_reply_body(body) == "ship variant B"


def test_reply_protonmail_attribution() -> None:
    body = (
        "approve\n\n"
        "Sent with Proton Mail secure email.\n\n"
        "On Monday, May 4th, 2026 at 09:00, Korpha wrote:\n"
        "> stale\n"
    )
    assert parse_reply_body(body) == "approve"


def test_reply_forwarded_message_divider() -> None:
    body = (
        "thoughts on this?\n\n"
        "---------- Forwarded message ---------\n"
        "From: someone\n"
    )
    assert parse_reply_body(body) == "thoughts on this?"


def test_reply_strips_iphone_signature() -> None:
    body = (
        "yes go\n\n"
        "Sent from my iPhone\n"
    )
    assert parse_reply_body(body) == "yes go"


def test_reply_strips_ipad_signature() -> None:
    body = (
        "approve all\n\n"
        "Sent from my iPad\n"
    )
    assert parse_reply_body(body) == "approve all"


def test_reply_strips_android_signature() -> None:
    body = (
        "looks good\n\n"
        "Sent from my Android phone\n"
    )
    assert parse_reply_body(body) == "looks good"


def test_reply_strips_outlook_mobile_footer() -> None:
    body = (
        "yep\n\n"
        "Get Outlook for iOS\n\n"
        "From: Korpha\n"
    )
    assert parse_reply_body(body) == "yep"


def test_reply_strips_image_placeholders() -> None:
    """Gmail/Outlook insert '[image: foo.png]' placeholder lines
    where the original image was attached. Drop them to avoid
    feeding the CEO an empty-but-not-empty token."""
    body = (
        "looks great, ship it\n"
        "[image: chart.png]\n"
        "[image: screenshot-2026-05-04.png]\n"
    )
    assert parse_reply_body(body) == "looks great, ship it"


def test_reply_gmail_wrapped_attribution() -> None:
    """Gmail wraps long 'On … wrote:' attributions across lines
    when the recipient's name+address overflows."""
    body = (
        "let's go\n\n"
        "On Mon, May 4, 2026 at 9:00 AM Korpha Cofounder <\n"
        "cofounder@korpha.example.com> wrote:\n"
        "> stale digest content\n"
    )
    assert parse_reply_body(body) == "let's go"


def test_reply_combines_multiple_strippers() -> None:
    """Realistic Mike-on-iPhone reply: short answer + image
    placeholder + mobile signature + quoted digest."""
    body = (
        "approve A and C, drop B\n"
        "[image: digest-preview.png]\n\n"
        "Sent from my iPhone\n\n"
        "On Mon, May 4, 2026, Korpha wrote:\n"
        "> Variant A: …\n"
        "> Variant B: …\n"
        "> Variant C: …\n"
    )
    assert parse_reply_body(body) == "approve A and C, drop B"


# ---------------------------------------------------------------------------
# parse_resend_inbound — webhook payload parser
# ---------------------------------------------------------------------------


def test_resend_object_from() -> None:
    payload = {
        "from": {"email": "mike@x.com", "name": "Mike"},
        "to": ["cofounder@korpha.com"],
        "subject": "Re: Daily digest",
        "text": "yes, ship it",
    }
    parsed = parse_resend_inbound(payload)
    assert parsed.from_email == "mike@x.com"
    assert parsed.from_name == "Mike"
    assert parsed.to == ["cofounder@korpha.com"]
    assert parsed.subject == "Re: Daily digest"
    assert parsed.text == "yes, ship it"


def test_resend_string_from_with_display_name() -> None:
    """Some clients send the From: header as a single string like
    'Mike <mike@x.com>'. Parse out the address + name."""
    payload = {
        "from": "Mike Founder <mike@x.com>",
        "to": "cofounder@x.com",
        "subject": "ok",
        "text": "go ahead",
    }
    parsed = parse_resend_inbound(payload)
    assert parsed.from_email == "mike@x.com"
    assert parsed.from_name == "Mike Founder"


def test_resend_string_from_address_only() -> None:
    payload = {
        "from": "mike@x.com",
        "to": ["cofounder@x.com"],
        "subject": "ok",
        "text": "go",
    }
    parsed = parse_resend_inbound(payload)
    assert parsed.from_email == "mike@x.com"
    assert parsed.from_name is None


def test_resend_event_envelope() -> None:
    """Resend wraps real events in {data: {...}} for some webhook types.
    Handle both shapes."""
    payload = {
        "type": "email.received",
        "data": {
            "from": {"email": "mike@x.com"},
            "to": ["cofounder@x.com"],
            "subject": "ok",
            "text": "approve",
        },
    }
    parsed = parse_resend_inbound(payload)
    assert parsed.from_email == "mike@x.com"
    assert parsed.text == "approve"


def test_resend_non_dict_rejected() -> None:
    with pytest.raises(ValueError, match=r"JSON object"):
        parse_resend_inbound([])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# incoming_from_resend — the bridge to the channel framework
# ---------------------------------------------------------------------------


def test_incoming_from_resend_strips_quote() -> None:
    """End-to-end: webhook → parser → IncomingMessage with cleaned body."""
    payload = parse_resend_inbound(
        {
            "from": {"email": "mike@x.com", "name": "Mike"},
            "to": ["cofounder@x.com"],
            "subject": "Re: Daily digest",
            "text": (
                "yes, ship it\n\n"
                "On Mon, May 4, 2026 at 9:00 AM, Korpha wrote:\n"
                "> [digest body]\n"
            ),
        }
    )
    incoming = incoming_from_resend(payload)
    assert incoming.platform == ThreadPlatform.EMAIL
    assert incoming.channel_user_id == "mike@x.com"
    assert incoming.text == "yes, ship it"
    assert incoming.display_name == "Mike"
    assert incoming.raw["subject"] == "Re: Daily digest"


def test_incoming_from_resend_preserves_to_in_raw() -> None:
    """The To: address is preserved in raw — useful when Korpha
    has multiple inbound aliases (per-business, per-channel)."""
    payload = parse_resend_inbound(
        {
            "from": "mike@x.com",
            "to": ["cofounder@biz1.com", "noisy-list@whatever.com"],
            "subject": "ok",
            "text": "go",
        }
    )
    incoming = incoming_from_resend(payload)
    assert incoming.raw["to"] == ["cofounder@biz1.com", "noisy-list@whatever.com"]
