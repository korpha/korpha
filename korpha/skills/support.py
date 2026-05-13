"""support.* skills — daily customer support batching.

Today: ``support.triage_inbox`` — given a batch of customer messages,
classifies each (refund / bug / question / feedback / spam), drafts a
reply per actionable item, and surfaces what genuinely needs Founder
attention (escalations) vs what's safe to auto-send.

Pairs with the trust envelope: once Mike has approved N replies in
``EMAIL_REPLY`` action class, the gate flips to AUTO and most messages
self-resolve.
"""
from __future__ import annotations

from typing import Any

from korpha._jsonext import extract_json_dict
from korpha.audit.model import InferenceTier
from korpha.inference.limits import agent_max_tokens, agent_timeout
from korpha.inference.types import CompletionRequest, Message, Role
from korpha.skills.registry import register
from korpha.skills.types import (
    Skill,
    SkillContext,
    SkillError,
    SkillResult,
    SkillSpec,
)

_PROMPT = """\
You are triaging the Founder's customer support inbox for the morning.

Business one-liner: {business_oneliner}
Brand voice: {voice}
Refund policy: {refund_policy}

Inbox messages (each prefixed with id):
{messages}

Classify each, draft a reply, and flag what needs Founder attention.

Respond with strict JSON only:
{{
  "items": [
    {{
      "id": "<from input>",
      "category": "refund | bug | question | feedback | spam | other",
      "severity": "low | normal | high",
      "reply": "<3-5 sentence reply in the brand voice>",
      "auto_send_safe": true | false,
      "escalation_reason": "<if not auto_send_safe: one sentence why Founder should look>"
    }}
  ],
  "summary": {{
    "total": <int>,
    "auto_safe": <int>,
    "escalations": <int>,
    "spam_dropped": <int>
  }}
}}

Rules:
- Only mark auto_send_safe=true when the reply is on-policy + low-risk
  (FAQ-shape questions, simple "thanks" acknowledgments). Refunds /
  custom requests / bugs are NOT auto-safe.
- For spam, set category=spam and reply="" (no reply).
- The brand voice + refund policy override your defaults.
- High severity = customer is angry, churning, or hit a real bug.
"""


class TriageInbox(Skill):
    spec = SkillSpec(
        name="support.triage_inbox",
        description=(
            "Triage a batch of customer support messages: classify "
            "(refund/bug/question/feedback/spam), draft a reply per item, "
            "flag which are safe to auto-send vs need Founder attention."
        ),
        parameters={
            "messages": "Inbox content. Format: '<id>: <message>' lines",
            "business_oneliner": "What the business does (one sentence)",
            "voice": "Brand voice for replies",
            "refund_policy": "One-line policy (e.g. '30 days no questions, after that case-by-case')",
        },
        default_tier=InferenceTier.PRO,
    )

    async def run(self, *, ctx: SkillContext, args: dict[str, Any]) -> SkillResult:
        prompt = _PROMPT.format(
            business_oneliner=str(
                args.get("business_oneliner") or ctx.business.description or "(unspecified)"
            ),
            voice=str(args.get("voice") or "warm, direct, founder-led"),
            refund_policy=str(args.get("refund_policy") or "30 days no questions"),
            messages=str(args.get("messages") or "(no messages)"),
        )
        request = CompletionRequest(
            messages=[
                Message(
                    role=Role.SYSTEM,
                    content=(
                        "You are the support-triage skill in Korpha. Be "
                        "concrete and on-policy. Don't over-promise. Mark "
                        "auto_send_safe conservatively — better to escalate "
                        "than to fire-and-forget the wrong reply."
                    ),
                ),
                Message(role=Role.USER, content=prompt),
            ],
            tier=self.spec.default_tier,
            session_key=f"skill-support-{ctx.business.id}",
            max_tokens=agent_max_tokens(),
            timeout_seconds=agent_timeout(),
        )
        response = await ctx.cost_tracker.complete(
            request,
            session=ctx.session,
            business_id=ctx.business.id,
            agent_role_id=ctx.invoking_agent_role_id,
        )

        parsed = extract_json_dict(response.content)
        if parsed is None or not isinstance(parsed.get("items"), list):
            raise SkillError(
                f"support.triage_inbox returned unparseable JSON. "
                f"first 500 chars: {response.content[:500]}"
            )

        items = parsed["items"]
        summary_d = parsed.get("summary") or {}
        return SkillResult(
            skill_name=self.spec.name,
            summary=(
                f"triaged {len(items)} | "
                f"auto-safe {summary_d.get('auto_safe', 0)} | "
                f"escalations {summary_d.get('escalations', 0)}"
            ),
            payload={
                "items": items,
                "summary": summary_d,
            },
            cost_usd=float(response.cost_usd),
            reasoning=response.reasoning,
            raw_response=response.content,
        )


register(TriageInbox())
