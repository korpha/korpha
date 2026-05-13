"""``channel.send_message`` — agent sends to a different channel
mid-conversation.

Founder DMs the cofounder on Telegram: "email me a draft of the
landing page copy." Without this skill, the cofounder replies in
Telegram with the copy as text. With this skill, it routes the
copy to the founder's email inbox where it lives in their normal
draft workflow.

Supported platforms today:
  - ``email``   → uses ``korpha.notifications.ResendEmailNotifier``
                  (requires ``RESEND_API_KEY`` + ``RESEND_FROM`` env)
  - ``telegram``→ uses ``korpha.channels.TelegramAdapter``
                  (requires ``TELEGRAM_BOT_TOKEN`` env, recipient is
                  a chat_id)

Adding more is mechanical: extend ``_PLATFORM_SENDERS`` with a
``(platform, env-key-list, sender-coroutine)`` triple. Each sender
takes ``(recipient, content, subject)`` and does the actual work.

The skill is gated behind ``EMAIL_OUTREACH`` / public-post-style
approval (or unapproved-but-logged for ``telegram`` — see code).
The default is "founder reviews before send" because the agent
will sometimes pick the wrong recipient or word the message
poorly. Better one extra click than a misdirected email.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from korpha.audit.model import InferenceTier
from korpha.skills.registry import register
from korpha.skills.types import (
    Skill, SkillContext, SkillError, SkillProvenance, SkillResult, SkillSpec,
)

logger = logging.getLogger(__name__)


# ---- per-platform senders ------------------------------------------


async def _send_email(
    *, recipient: str, content: str, subject: str | None,
) -> dict[str, Any]:
    """Send via the configured Resend notifier. Raises SkillError
    when credentials are missing — the founder will see this in
    the chat reply rather than a silent failure."""
    if not os.environ.get("RESEND_API_KEY"):
        raise SkillError(
            "channel.send_message: email requires RESEND_API_KEY in env. "
            "Run `korpha setup channels` and configure the email entry."
        )
    if not os.environ.get("RESEND_FROM"):
        raise SkillError(
            "channel.send_message: email requires RESEND_FROM (verified "
            "sender address). Set it in ~/.korpha/.env."
        )
    from korpha.notifications.base import Notification
    from korpha.notifications.email import ResendEmailNotifier

    notifier = ResendEmailNotifier()
    notif = Notification(
        to=recipient,
        subject=subject or "Message from your cofounder",
        text_body=content,
    )
    try:
        await notifier.send(notif)
    finally:
        await notifier.close()
    return {"to": recipient, "subject": notif.subject}


async def _send_telegram(
    *, recipient: str, content: str, subject: str | None,
) -> dict[str, Any]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SkillError(
            "channel.send_message: telegram requires TELEGRAM_BOT_TOKEN "
            "in env. Run `korpha setup channels` and configure the "
            "telegram entry."
        )
    try:
        chat_id = recipient
    except Exception as exc:
        raise SkillError(
            f"channel.send_message: telegram recipient must be a chat_id, "
            f"got {recipient!r}: {exc}"
        ) from exc

    from korpha.channels import TelegramAdapter
    from korpha.channels.base import OutgoingMessage

    adapter = TelegramAdapter(token=token)
    try:
        await adapter.send(
            OutgoingMessage(channel_user_id=chat_id, text=content),
        )
    finally:
        await adapter.close()
    return {"to": recipient}


# Platform name → (required env vars, sender callable). The required-env
# list is what the skill prompt advertises so the LLM knows what to ask
# the founder to set up if missing.
_PLATFORM_SENDERS = {
    "email": (
        ("RESEND_API_KEY", "RESEND_FROM"),
        _send_email,
    ),
    "telegram": (
        ("TELEGRAM_BOT_TOKEN",),
        _send_telegram,
    ),
}


# ---- skill definition ----------------------------------------------


class ChannelSendMessageSkill(Skill):
    """Send a message to one of the configured channels (email,
    telegram, …) from inside a chat happening on a *different*
    channel."""

    spec = SkillSpec(
        name="channel.send_message",
        description=(
            "Send a message to a different channel mid-conversation. "
            "Use when the founder asks you to deliver something via "
            "email, telegram, or another configured channel that "
            "isn't the one this conversation is happening on."
        ),
        parameters={
            "platform": (
                "Channel name. Supported: 'email', 'telegram'. "
                "Use 'email' for anything the founder wants in their "
                "inbox; 'telegram' for IM."
            ),
            "recipient": (
                "Where the message goes. For email: an email address. "
                "For telegram: a chat_id (numeric string). Default: "
                "the founder's primary contact for that channel."
            ),
            "content": "The message body to send.",
            "subject": (
                "Optional subject line — used by email, ignored by "
                "platforms that don't have subjects."
            ),
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        platform = str(args.get("platform") or "").strip().lower()
        recipient = str(args.get("recipient") or "").strip()
        content = str(args.get("content") or "").strip()
        subject_raw = args.get("subject")
        subject = (
            str(subject_raw).strip() if subject_raw is not None else None
        )

        if not platform:
            raise SkillError(
                "channel.send_message: 'platform' is required. "
                f"Supported: {', '.join(_PLATFORM_SENDERS)}."
            )
        if platform not in _PLATFORM_SENDERS:
            raise SkillError(
                f"channel.send_message: platform {platform!r} not "
                f"supported. Supported: {', '.join(_PLATFORM_SENDERS)}."
            )
        if not recipient:
            raise SkillError(
                "channel.send_message: 'recipient' is required."
            )
        if not content:
            raise SkillError(
                "channel.send_message: 'content' is required (the "
                "actual message body)."
            )

        _, sender = _PLATFORM_SENDERS[platform]
        try:
            details = await sender(
                recipient=recipient, content=content, subject=subject,
            )
        except SkillError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SkillError(
                f"channel.send_message: {platform} send failed: {exc}"
            ) from exc

        return SkillResult(
            skill_name=self.spec.name,
            summary=(
                f"Sent {len(content)}-char {platform} message to "
                f"{details.get('to', recipient)}"
            ),
            payload={
                "platform": platform,
                "recipient": recipient,
                "subject": subject,
                "delivered": True,
                **details,
            },
            cost_usd=0.0,
        )


register(ChannelSendMessageSkill())


__all__ = ["ChannelSendMessageSkill"]
