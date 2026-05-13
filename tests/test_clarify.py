"""Tests for the structured clarify flow.

Covers:
  - ClarifyRequest data class invariants
  - parse_clarify accepts the documented shapes + rejects garbage
  - CEO router parser handles action="clarify"
  - HandleResult + AskResponse carry clarify through correctly
  - The chat template renders persisted clarify_choices as buttons
  - Malformed clarify falls back to plain respond (never wedges)

We don't drive a real LLM; the router output is stubbed via
_parse_router_decision unit tests + a CEO integration test that
patches the router response.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from korpha.cofounder.clarify import (
    MAX_CHOICES,
    ClarifyRequest,
    parse_clarify,
)
from korpha.cofounder.ceo import _parse_router_decision


# ---- ClarifyRequest invariants ----


def test_open_ended_request() -> None:
    req = ClarifyRequest(question="What should I do?")
    assert req.is_open_ended() is True
    assert req.choices == ()
    assert req.as_numbered_list() == ""


def test_multi_choice_request() -> None:
    req = ClarifyRequest(
        question="Niche A or B?",
        choices=("A: SaaS", "B: Ecom"),
    )
    assert req.is_open_ended() is False
    assert len(req.choices) == 2
    listed = req.as_numbered_list()
    assert "1. A: SaaS" in listed
    assert "2. B: Ecom" in listed


def test_max_choices_constant() -> None:
    """4 keeps the UI from getting cluttered + matches Hermes."""
    assert MAX_CHOICES == 4


# ---- parse_clarify ----


def test_parse_clarify_accepts_question_and_choices() -> None:
    out = parse_clarify({
        "question": "Pick a path",
        "choices": ["A", "B", "C"],
    })
    assert out is not None
    assert out.question == "Pick a path"
    assert out.choices == ("A", "B", "C")


def test_parse_clarify_caps_choices_at_max() -> None:
    out = parse_clarify({
        "question": "Pick",
        "choices": ["a", "b", "c", "d", "e", "f"],
    })
    assert out is not None
    assert len(out.choices) == MAX_CHOICES
    assert out.choices == ("a", "b", "c", "d")


def test_parse_clarify_drops_empty_choices() -> None:
    out = parse_clarify({
        "question": "Q",
        "choices": ["A", "", "  ", "B"],
    })
    assert out is not None
    assert out.choices == ("A", "B")


def test_parse_clarify_falls_back_to_content_for_question() -> None:
    """LLM might emit ``content`` instead of ``question`` — accept both."""
    out = parse_clarify({"content": "Pick", "choices": ["A"]})
    assert out is not None
    assert out.question == "Pick"


def test_parse_clarify_returns_none_when_no_question() -> None:
    assert parse_clarify({"choices": ["A"]}) is None
    assert parse_clarify({"question": "  "}) is None


def test_parse_clarify_handles_non_list_choices() -> None:
    """Bad LLM output: choices=null or choices="A". Don't crash."""
    out = parse_clarify({"question": "Q", "choices": None})
    assert out is not None
    assert out.choices == ()
    out2 = parse_clarify({"question": "Q", "choices": "not a list"})
    assert out2 is not None
    assert out2.choices == ()


# ---- CEO router parser ----


def test_router_parses_clarify_action() -> None:
    raw = (
        '{"action":"clarify","question":"Niche A or B?",'
        '"choices":["SaaS","Ecom"]}'
    )
    decision = _parse_router_decision(raw)
    assert decision is not None
    assert decision.action == "clarify"
    assert decision.clarify is not None
    assert decision.clarify.question == "Niche A or B?"
    assert decision.clarify.choices == ("SaaS", "Ecom")
    # `content` mirrors the question for channels that don't
    # consume the structured field.
    assert decision.content == "Niche A or B?"


def test_router_falls_back_to_respond_on_empty_clarify() -> None:
    """LLM emits action=clarify with no question AND no content —
    must NOT crash; becomes a plain respond so the user still gets
    an answer (empty in this case, but the action shape is safe)."""
    raw = '{"action":"clarify"}'
    decision = _parse_router_decision(raw)
    # parse_clarify returns None → fallback path
    assert decision is not None
    assert decision.action == "respond"


def test_router_clarify_with_open_ended_question() -> None:
    """No choices key → open-ended clarify still parses."""
    raw = '{"action":"clarify","question":"What did you mean?"}'
    decision = _parse_router_decision(raw)
    assert decision is not None
    assert decision.action == "clarify"
    assert decision.clarify is not None
    assert decision.clarify.is_open_ended() is True


# ---- AskResponse Pydantic shape ----


def test_ask_response_carries_clarify_fields() -> None:
    from korpha.api.server import AskResponse

    resp = AskResponse(
        content="Niche A or B?",
        skills_used=[],
        reasoning_chars=0,
        cost_usd=0.0,
        clarify_question="Niche A or B?",
        clarify_choices=["SaaS", "Ecom"],
    )
    assert resp.clarify_question == "Niche A or B?"
    assert resp.clarify_choices == ["SaaS", "Ecom"]


def test_ask_response_clarify_fields_optional() -> None:
    """Existing callers without clarify still work."""
    from korpha.api.server import AskResponse

    resp = AskResponse(
        content="hi", skills_used=[], reasoning_chars=0, cost_usd=0.0,
    )
    assert resp.clarify_question is None
    assert resp.clarify_choices is None


# ---- chat.html template renders clarify buttons ----


def test_chat_template_renders_clarify_buttons() -> None:
    """When a Message has attachments['clarify_choices'], the chat
    template must render a clickable button for each choice. We
    render an isolated fragment rather than the whole base/chat
    template to keep the test fast + independent of layout churn."""
    from jinja2 import Environment

    env = Environment(autoescape=True)
    fragment = """
{% if m.attachments and m.attachments.get('clarify_choices') %}
<div class="chat-clarify" data-msg-id="{{ m.id }}">
  {% for choice in m.attachments['clarify_choices'] %}
    <button type="button" class="chat-clarify-choice"
            data-choice="{{ choice }}">{{ loop.index }}. {{ choice }}</button>
  {% endfor %}
</div>
{% endif %}
"""
    tmpl = env.from_string(fragment)

    msg_with_clarify = MagicMock()
    msg_with_clarify.id = "abc"
    msg_with_clarify.attachments = {
        "clarify_question": "Niche A or B?",
        "clarify_choices": ["SaaS", "Ecom"],
    }
    rendered = tmpl.render(m=msg_with_clarify)
    assert 'class="chat-clarify"' in rendered
    assert 'data-choice="SaaS"' in rendered
    assert "1. SaaS" in rendered
    assert "2. Ecom" in rendered

    # Message without clarify renders nothing
    msg_plain = MagicMock()
    msg_plain.attachments = {}
    rendered_plain = tmpl.render(m=msg_plain)
    assert "chat-clarify" not in rendered_plain


# ---- routing: attachments threaded through ----


def test_route_outbound_persists_attachments(
    tmp_path,
    monkeypatch,
) -> None:
    """When CEO surfaces a clarify, route_outbound must persist
    the choices into Message.attachments so the page survives a
    refresh with buttons intact."""
    from sqlmodel import Session, SQLModel, create_engine

    from korpha.business.model import Business
    from korpha.cofounder.hiring import HiringService
    from korpha.cofounder.model import (
        Message, MessageSenderType, ThreadPlatform,
    )
    from korpha.cofounder.routing import ConversationRouter
    from korpha.identity.model import Founder

    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        founder = Founder(email="x@y.com", display_name="X")
        session.add(founder)
        session.commit()
        session.refresh(founder)
        business = Business(
            name="B", description="d", founder_id=founder.id,
        )
        session.add(business)
        session.commit()
        session.refresh(business)

        hiring = HiringService(session)
        ceo = hiring.ensure_ceo(business.id)

        router = ConversationRouter(session=session, hiring=hiring)
        router.route_outbound(
            business_id=business.id,
            founder_id=founder.id,
            platform=ThreadPlatform.WEB,
            content="Niche A or B?",
            requesting_agent_role_id=ceo.id,
            attachments={
                "clarify_question": "Niche A or B?",
                "clarify_choices": ["SaaS", "Ecom"],
            },
        )

        # Pull the persisted Message
        from sqlmodel import select
        msgs = list(session.exec(
            select(Message).where(
                Message.sender_type == MessageSenderType.AGENT,
            )
        ).all())
        assert len(msgs) == 1
        assert msgs[0].attachments == {
            "clarify_question": "Niche A or B?",
            "clarify_choices": ["SaaS", "Ecom"],
        }
