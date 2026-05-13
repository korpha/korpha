"""commerce.create_payment_link — propose a real Stripe Payment Link.

Same compose → propose → approve → execute pattern as
outreach.send_cold_email. The skill creates an Approval row carrying
the (name, amount, currency, description) payload; the founder runs
``korpha execute <id>`` and the executor calls Stripe via httpx
to mint the actual link.

Why approval-gated even though no money moves at link-creation time:
the link is a public artifact tied to the founder's Stripe account,
and a misnamed product or wrong-currency price is a customer-visible
mistake. Better to confirm.
"""
from __future__ import annotations

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

_VALID_CURRENCIES = frozenset({"usd", "eur", "gbp", "cad", "aud"})


class CreatePaymentLinkSkill(Skill):
    spec = SkillSpec(
        name="commerce.create_payment_link",
        description=(
            "Propose a real Stripe Payment Link for an amount + product "
            "name. Creates a pending approval — does NOT call Stripe "
            "until the founder runs `korpha execute <id>`."
        ),
        parameters={
            "name": "Product name shown on the checkout page",
            "amount_usd": "Price in dollars (e.g. 29.00 for $29). Other "
            "currencies use the same field; we coerce to the smallest "
            "unit at API call time.",
            "currency": "ISO 4217 code: usd | eur | gbp | cad | aud "
            "(default usd)",
            "description": "Optional product description shown to the "
            "customer at checkout",
        },
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any]
    ) -> SkillResult:
        name = str(args.get("name") or "").strip()
        if not name or len(name) < 2:
            raise SkillError(
                "commerce.create_payment_link: 'name' is required (>= 2 chars)"
            )

        try:
            amount = float(args.get("amount_usd") or 0)
        except (TypeError, ValueError) as exc:
            raise SkillError(
                f"commerce.create_payment_link: 'amount_usd' must be a number, "
                f"got {args.get('amount_usd')!r}"
            ) from exc
        if amount <= 0:
            raise SkillError(
                "commerce.create_payment_link: 'amount_usd' must be > 0"
            )

        currency = str(args.get("currency") or "usd").lower().strip()
        if currency not in _VALID_CURRENCIES:
            raise SkillError(
                f"commerce.create_payment_link: currency {currency!r} not in "
                f"{sorted(_VALID_CURRENCIES)}"
            )

        description = args.get("description")
        description_str = str(description).strip() if description else None
        if description_str and len(description_str) > 500:
            description_str = description_str[:497] + "…"

        agent_role_id = ctx.invoking_agent_role_id
        if agent_role_id is None:
            agent_role_id = HiringService(ctx.session).ensure_ceo(
                ctx.business.id
            ).id

        proposal_summary = (
            f"Create Stripe payment link: {name} — {amount:.2f} {currency.upper()}"
        )
        approval = Approval(
            business_id=ctx.business.id,
            agent_role_id=agent_role_id,
            action_class=ActionClass.COMMERCE,
            platform="stripe",
            proposal_summary=proposal_summary,
            action_payload={
                "kind": "create_payment_link",
                "name": name,
                "amount_usd": amount,
                "currency": currency,
                "description": description_str,
            },
            status=ApprovalStatus.PENDING,
        )
        ctx.session.add(approval)
        ctx.session.add(
            Activity(
                business_id=ctx.business.id,
                actor_type=ActorType.AGENT,
                actor_id=agent_role_id,
                event_type="commerce.payment_link_proposed",
                payload={
                    "approval_id": str(approval.id),
                    "name": name,
                    "amount_usd": amount,
                    "currency": currency,
                },
            )
        )
        ctx.session.commit()
        ctx.session.refresh(approval)

        return SkillResult(
            skill_name=self.spec.name,
            summary=(
                f"Stripe payment link queued for approval: {name} at "
                f"{amount:.2f} {currency.upper()}. "
                f"Run `korpha execute {approval.id}` to mint it."
            ),
            payload={
                "approval_id": str(approval.id),
                "status": "pending",
                "name": name,
                "amount_usd": amount,
                "currency": currency,
                "description": description_str,
                "approve_command": f"korpha approve {approval.id}",
                "execute_command": f"korpha execute {approval.id}",
            },
            cost_usd=0.0,
        )


register(CreatePaymentLinkSkill())
