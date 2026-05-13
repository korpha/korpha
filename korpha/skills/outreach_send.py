"""outreach.send_cold_email — propose a real email send (approval-gated).

This is the first Korpha skill that produces a *real-world side effect*:
an actual email goes out to an actual recipient. Because of that, the
skill itself never sends — it creates a pending Approval row carrying
the (to, subject, body) payload, and the founder must approve before
``korpha execute <id>`` actually dispatches the send via the Resend
notifier.

The pattern (compose → propose → approve → execute) keeps the cofounder
honest: nothing leaves the system without explicit human consent the
first time, and the trust envelope auto-promotes the action class once
Mike's approved enough of them.
"""
from __future__ import annotations

import re
from typing import Any

from korpha.approvals.model import (
    ActionClass,
    Approval,
    ApprovalStatus,
)
from korpha.audit.model import Activity, ActorType
from korpha.cofounder.hiring import HiringService
from korpha.skills.registry import register
from korpha.skills.types import (
    Skill,
    SkillContext,
    SkillError,
    SkillResult,
    SkillSpec,
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SendColdEmailSkill(Skill):
    spec = SkillSpec(
        name="outreach.send_cold_email",
        description=(
            "Propose a real cold email to a specific recipient. Creates a "
            "pending approval — does NOT send until the founder runs "
            "`korpha execute <approval_id>`. Use after "
            "outreach.draft_cold_emails when the founder picks a draft."
        ),
        parameters={
            "to": "Recipient email address",
            "subject": "Subject line",
            "body": "Email body (plain text — pass the chosen draft from "
            "outreach.draft_cold_emails)",
            "from_address": "Optional override; defaults to RESEND_FROM env",
        },
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any]
    ) -> SkillResult:
        to = str(args.get("to") or "").strip()
        subject = str(args.get("subject") or "").strip()
        body = str(args.get("body") or "").strip()
        from_address = args.get("from_address")
        from_address = str(from_address).strip() if from_address else None

        if not _EMAIL_RE.match(to):
            raise SkillError(
                f"outreach.send_cold_email: 'to' must be a valid email, got {to!r}"
            )
        if not subject:
            raise SkillError("outreach.send_cold_email: 'subject' is required")
        if not body or len(body) < 20:
            raise SkillError(
                "outreach.send_cold_email: 'body' must be at least 20 chars "
                "(pick a draft from outreach.draft_cold_emails)"
            )

        agent_role_id = ctx.invoking_agent_role_id
        if agent_role_id is None:
            agent_role_id = HiringService(ctx.session).ensure_ceo(
                ctx.business.id
            ).id

        proposal_summary = f"Send email to {to} — {subject[:80]}"
        approval = Approval(
            business_id=ctx.business.id,
            agent_role_id=agent_role_id,
            action_class=ActionClass.EMAIL_OUTREACH,
            platform="email",
            proposal_summary=proposal_summary,
            action_payload={
                "to": to,
                "subject": subject,
                "body": body,
                "from_address": from_address,
            },
            status=ApprovalStatus.PENDING,
        )
        ctx.session.add(approval)
        ctx.session.add(
            Activity(
                business_id=ctx.business.id,
                actor_type=ActorType.AGENT,
                actor_id=agent_role_id,
                event_type="email.proposed",
                payload={
                    "to": to,
                    "subject_len": len(subject),
                    "body_len": len(body),
                    "approval_id": str(approval.id),
                },
            )
        )
        ctx.session.commit()
        ctx.session.refresh(approval)

        return SkillResult(
            skill_name=self.spec.name,
            summary=(
                f"Email queued for approval — to: {to}. "
                f"Run `korpha execute {approval.id}` to send."
            ),
            payload={
                "approval_id": str(approval.id),
                "status": "pending",
                "to": to,
                "subject": subject,
                "body_preview": body[:240] + ("…" if len(body) > 240 else ""),
                "approve_command": f"korpha approve {approval.id}",
                "execute_command": f"korpha execute {approval.id}",
            },
            cost_usd=0.0,
        )


register(SendColdEmailSkill())
