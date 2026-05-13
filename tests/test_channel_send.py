"""Tests for ``channel.send_message`` skill — cross-channel send.

Validates the skill registers, parameter validation, env-var
gating, and that a successful call invokes the right backend
(Resend Notifier for email, TelegramAdapter for telegram).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from korpha.skills.channel import ChannelSendMessageSkill
from korpha.skills.types import SkillError, SkillProvenance


@pytest.fixture
def skill() -> ChannelSendMessageSkill:
    return ChannelSendMessageSkill()


# ---- registration + spec ----


def test_registered_in_default_registry() -> None:
    from korpha.skills.registry import default_registry
    assert "channel.send_message" in default_registry.skills


def test_spec_has_builtin_provenance() -> None:
    s = ChannelSendMessageSkill()
    assert s.spec.provenance == SkillProvenance.BUILTIN


def test_spec_describes_supported_platforms() -> None:
    """Description should mention 'email' and 'telegram' so the
    LLM router knows when to pick this skill."""
    s = ChannelSendMessageSkill()
    blob = s.spec.description + s.spec.parameters["platform"]
    assert "email" in blob.lower()
    assert "telegram" in blob.lower()


# ---- parameter validation ----


@pytest.mark.asyncio
async def test_missing_platform_raises(skill: ChannelSendMessageSkill) -> None:
    with pytest.raises(SkillError, match="platform"):
        await skill.run(ctx=None, args={"recipient": "x", "content": "y"})  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_unsupported_platform_raises(
    skill: ChannelSendMessageSkill,
) -> None:
    with pytest.raises(SkillError, match="not supported"):
        await skill.run(
            ctx=None,  # type: ignore[arg-type]
            args={
                "platform": "fax", "recipient": "x", "content": "y",
            },
        )


@pytest.mark.asyncio
async def test_missing_recipient_raises(
    skill: ChannelSendMessageSkill,
) -> None:
    with pytest.raises(SkillError, match="recipient"):
        await skill.run(
            ctx=None,  # type: ignore[arg-type]
            args={"platform": "email", "content": "y"},
        )


@pytest.mark.asyncio
async def test_missing_content_raises(
    skill: ChannelSendMessageSkill,
) -> None:
    with pytest.raises(SkillError, match="content"):
        await skill.run(
            ctx=None,  # type: ignore[arg-type]
            args={"platform": "email", "recipient": "x"},
        )


# ---- email path ----


@pytest.mark.asyncio
async def test_email_requires_resend_api_key(
    monkeypatch: pytest.MonkeyPatch,
    skill: ChannelSendMessageSkill,
) -> None:
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("RESEND_FROM", raising=False)
    with pytest.raises(SkillError, match="RESEND_API_KEY"):
        await skill.run(
            ctx=None,  # type: ignore[arg-type]
            args={
                "platform": "email",
                "recipient": "x@y.com",
                "content": "hello",
            },
        )


@pytest.mark.asyncio
async def test_email_requires_resend_from(
    monkeypatch: pytest.MonkeyPatch,
    skill: ChannelSendMessageSkill,
) -> None:
    monkeypatch.setenv("RESEND_API_KEY", "x")
    monkeypatch.delenv("RESEND_FROM", raising=False)
    with pytest.raises(SkillError, match="RESEND_FROM"):
        await skill.run(
            ctx=None,  # type: ignore[arg-type]
            args={
                "platform": "email",
                "recipient": "x@y.com",
                "content": "hello",
            },
        )


@pytest.mark.asyncio
async def test_email_send_invokes_notifier(
    monkeypatch: pytest.MonkeyPatch,
    skill: ChannelSendMessageSkill,
) -> None:
    monkeypatch.setenv("RESEND_API_KEY", "rk_test")
    monkeypatch.setenv("RESEND_FROM", "founder@y.com")

    sent: list[dict] = []

    class _StubNotifier:
        async def send(self, notification) -> None:
            sent.append({
                "to": notification.to,
                "subject": notification.subject,
                "body": notification.text_body,
            })
        async def close(self) -> None:
            return None

    # Patch the real source — channel.py imports it lazily so it
    # doesn't exist as an attribute on the channel module yet.
    monkeypatch.setattr(
        "korpha.notifications.email.ResendEmailNotifier",
        lambda: _StubNotifier(),
    )

    result = await skill.run(
        ctx=None,  # type: ignore[arg-type]
        args={
            "platform": "email",
            "recipient": "mike@example.com",
            "content": "Here's the landing page draft.",
            "subject": "Landing copy v1",
        },
    )
    assert result.payload["delivered"] is True
    assert result.payload["platform"] == "email"
    assert len(sent) == 1
    assert sent[0]["to"] == "mike@example.com"
    assert sent[0]["subject"] == "Landing copy v1"
    assert "landing page draft" in sent[0]["body"]


@pytest.mark.asyncio
async def test_email_default_subject_when_omitted(
    monkeypatch: pytest.MonkeyPatch,
    skill: ChannelSendMessageSkill,
) -> None:
    monkeypatch.setenv("RESEND_API_KEY", "x")
    monkeypatch.setenv("RESEND_FROM", "f@y.com")

    captured: dict = {}

    class _StubNotifier:
        async def send(self, notification) -> None:
            captured["subject"] = notification.subject
        async def close(self) -> None:
            return None

    # Patch the real source — channel.py imports it lazily so it
    # doesn't exist as an attribute on the channel module yet.
    monkeypatch.setattr(
        "korpha.notifications.email.ResendEmailNotifier",
        lambda: _StubNotifier(),
    )

    await skill.run(
        ctx=None,  # type: ignore[arg-type]
        args={
            "platform": "email",
            "recipient": "x@y.com",
            "content": "...",
        },
    )
    assert captured["subject"] == "Message from your cofounder"


# ---- telegram path ----


@pytest.mark.asyncio
async def test_telegram_requires_bot_token(
    monkeypatch: pytest.MonkeyPatch,
    skill: ChannelSendMessageSkill,
) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(SkillError, match="TELEGRAM_BOT_TOKEN"):
        await skill.run(
            ctx=None,  # type: ignore[arg-type]
            args={
                "platform": "telegram",
                "recipient": "12345",
                "content": "ping",
            },
        )


@pytest.mark.asyncio
async def test_telegram_send_invokes_adapter(
    monkeypatch: pytest.MonkeyPatch,
    skill: ChannelSendMessageSkill,
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc")

    sent: list = []

    class _StubAdapter:
        def __init__(self, *_a, **_k) -> None:
            pass
        async def send(self, msg) -> None:
            sent.append((msg.channel_user_id, msg.text))
        async def close(self) -> None:
            return None

    monkeypatch.setattr(
        "korpha.channels.TelegramAdapter", _StubAdapter,
    )

    result = await skill.run(
        ctx=None,  # type: ignore[arg-type]
        args={
            "platform": "telegram",
            "recipient": "98765",
            "content": "ping from cofounder",
        },
    )
    assert result.payload["delivered"] is True
    assert result.payload["platform"] == "telegram"
    assert sent == [("98765", "ping from cofounder")]


@pytest.mark.asyncio
async def test_send_failure_wrapped_in_skill_error(
    monkeypatch: pytest.MonkeyPatch,
    skill: ChannelSendMessageSkill,
) -> None:
    """If the underlying transport raises (network down, bad
    credentials, etc.), the skill must turn it into a SkillError so
    the CEO synthesizer doesn't crash the turn."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc")

    class _BoomAdapter:
        def __init__(self, *_a, **_k) -> None:
            pass
        async def send(self, _msg) -> None:
            raise RuntimeError("rate limited")
        async def close(self) -> None:
            return None

    monkeypatch.setattr(
        "korpha.channels.TelegramAdapter", _BoomAdapter,
    )

    with pytest.raises(SkillError, match="telegram send failed"):
        await skill.run(
            ctx=None,  # type: ignore[arg-type]
            args={
                "platform": "telegram",
                "recipient": "1",
                "content": "x",
            },
        )


@pytest.mark.asyncio
async def test_close_called_even_on_send_failure(
    monkeypatch: pytest.MonkeyPatch,
    skill: ChannelSendMessageSkill,
) -> None:
    """The transport's close() must run even when send() raises —
    otherwise we leak the underlying httpx client / file handles."""
    monkeypatch.setenv("RESEND_API_KEY", "x")
    monkeypatch.setenv("RESEND_FROM", "f@y.com")

    closed: list[bool] = []

    class _BoomNotifier:
        async def send(self, _notif) -> None:
            raise RuntimeError("boom")
        async def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(
        "korpha.notifications.email.ResendEmailNotifier",
        lambda: _BoomNotifier(),
    )

    with pytest.raises(SkillError):
        await skill.run(
            ctx=None,  # type: ignore[arg-type]
            args={
                "platform": "email",
                "recipient": "x@y.com",
                "content": "x",
            },
        )
    assert closed == [True]
