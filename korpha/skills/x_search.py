"""``x_search`` skill — real-time X (Twitter) search via xAI's server-
side ``x_search`` tool.

Requires the xAI OAuth subscription (see
:mod:`korpha.inference.xai_oauth`). When that subscription is configured
the skill auto-registers; otherwise it stays hidden so the picker / CEO
prompt aren't polluted with a skill we can't actually run.

The skill hits the xAI Responses API directly (rather than going through
the inference cascade) because the ``x_search`` tool is xAI-server-side:
the model gets the search results inline + writes the answer in one
round trip. Falling through the cascade to, say, DeepSeek would lose
the tool entirely.

Use cases:
  - "What's the sentiment on @companyhandle this week?"
  - "Has anyone on X complained about competitor.com's pricing?"
  - "What are people saying about my product launch?"
  - "Find tweets mentioning {feature} from the last 7 days."

Inputs (args):
  - ``query`` (required) — natural-language search query.
  - ``allowed_x_handles`` (optional, ≤10) — restrict to specific
    accounts. Pass without ``@``.
  - ``excluded_x_handles`` (optional, ≤10) — exclude specific accounts.
  - ``from_date`` (optional, ISO date) — earliest tweet date.
  - ``to_date`` (optional, ISO date) — latest tweet date.
  - ``max_results`` (optional, default 20) — soft cap on tweets shown.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from korpha.audit.model import InferenceTier
from korpha.inference.xai_oauth import (
    XAI_API_BASE, XaiOAuthError, get_auth, is_configured,
)
from korpha.skills.registry import register
from korpha.skills.types import (
    Skill,
    SkillContext,
    SkillError,
    SkillProvenance,
    SkillResult,
    SkillSpec,
)

logger = logging.getLogger(__name__)


# Default model for x_search — Hermes pins to grok-4.20-reasoning
# because the tool fires search calls during reasoning. Override via
# ``args.model`` if the founder wants to A/B.
_DEFAULT_MODEL = "grok-4.20-0309-reasoning"

# Hermes caps allowed/excluded handle lists at 10 each. xAI's tool
# rejects longer lists with a 400.
_MAX_HANDLES = 10


_PROMPT_TEMPLATE = """\
The user asked: {query}

Use the x_search tool to find relevant recent tweets, then write a
concise, structured answer. For each notable result include:
- the handle (with @)
- the tweet text (first 220 chars, no embedded URLs needed)
- the post date if available

End with a 2-3 sentence "what this means" summary tailored to a solo
founder who's deciding whether to act on this signal.
"""


class XSearchSkill(Skill):
    spec = SkillSpec(
        name="research.x_search",
        description=(
            "Real-time X (Twitter) search via xAI's server-side "
            "tool. Pulls recent tweets matching a query (optionally "
            "scoped to specific handles + date range) and summarizes "
            "what people are saying. Requires X Premium+ / SuperGrok "
            "subscription."
        ),
        parameters={
            "query": "Natural-language search query (required).",
            "allowed_x_handles": (
                "Optional list of up to 10 X handles (no @) to "
                "restrict the search to."
            ),
            "excluded_x_handles": (
                "Optional list of up to 10 X handles to exclude."
            ),
            "from_date": "Optional ISO date (YYYY-MM-DD) — earliest tweet.",
            "to_date": "Optional ISO date (YYYY-MM-DD) — latest tweet.",
            "max_results": "Soft cap on tweets shown (default 20).",
            "model": (
                "Override the xAI model. Default: "
                f"{_DEFAULT_MODEL}."
            ),
        },
        default_tier=InferenceTier.PRO,
        provenance=SkillProvenance.HERMES_PORT,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any]
    ) -> SkillResult:
        if not is_configured(
            str(ctx.business_unit_id) if ctx.business_unit_id else None,
        ) and not is_configured():
            raise SkillError(
                "research.x_search needs an X Premium+ / SuperGrok "
                "subscription. Sign in with `aigenteur auth add "
                "xai-oauth` or click the button on /app/credentials.",
            )

        query = str(args.get("query") or "").strip()
        if not query:
            raise SkillError(
                "research.x_search: 'query' is required and non-empty.",
            )
        model = str(args.get("model") or _DEFAULT_MODEL)

        # Build the server-side x_search tool config.
        tool_params: dict[str, Any] = {"query": query}
        for key in ("allowed_x_handles", "excluded_x_handles"):
            raw = args.get(key) or []
            if isinstance(raw, str):
                raw = [h.strip() for h in raw.split(",") if h.strip()]
            cleaned = [
                str(h).strip().lstrip("@") for h in raw if str(h).strip()
            ]
            if len(cleaned) > _MAX_HANDLES:
                raise SkillError(
                    f"research.x_search: {key} must be ≤{_MAX_HANDLES} "
                    f"handles, got {len(cleaned)}.",
                )
            if cleaned:
                tool_params[key] = cleaned
        for date_key in ("from_date", "to_date"):
            v = args.get(date_key)
            if v:
                tool_params[date_key] = str(v)
        max_results = int(args.get("max_results") or 20)

        try:
            auth = get_auth(
                str(ctx.business_unit_id)
                if ctx.business_unit_id else None,
            )
        except XaiOAuthError as exc:
            raise SkillError(f"research.x_search: xAI auth: {exc}") from exc

        payload: dict[str, Any] = {
            "model": model,
            "store": False,
            "stream": False,
            "instructions": (
                "You are a research assistant for a solo founder. "
                "When you call the x_search tool, summarize what you "
                "find faithfully — never invent tweets."
            ),
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": _PROMPT_TEMPLATE.format(query=query),
                        },
                    ],
                },
            ],
            "tools": [{"type": "x_search", **tool_params}],
        }

        headers = {
            "Authorization": f"Bearer {auth.access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    f"{XAI_API_BASE}/responses",
                    json=payload,
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            raise SkillError(
                f"research.x_search: transport failed: "
                f"{type(exc).__name__}: {exc}",
            ) from exc

        if resp.status_code != 200:
            raise SkillError(
                f"research.x_search: xAI returned {resp.status_code}: "
                f"{resp.text[:400]}",
            )

        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise SkillError(
                f"research.x_search: xAI returned non-JSON: {exc}",
            ) from exc

        # Pull the answer text + any structured tool results out of the
        # Responses-API shape. xAI mirrors OpenAI's response.output array.
        answer_text = ""
        tweets: list[dict[str, Any]] = []
        for item in data.get("output") or []:
            if item.get("type") == "message":
                for chunk in item.get("content") or []:
                    if chunk.get("type") in ("output_text", "text"):
                        answer_text += chunk.get("text", "")
            elif item.get("type") in ("x_search_results", "tool_result"):
                results = item.get("results") or item.get("output") or []
                for r in results[:max_results]:
                    tweets.append({
                        "handle": (
                            r.get("handle")
                            or r.get("author_handle")
                            or r.get("username")
                            or ""
                        ),
                        "text": (r.get("text") or "")[:400],
                        "created_at": r.get("created_at"),
                        "id": r.get("id") or r.get("tweet_id"),
                        "url": r.get("url"),
                    })

        usage = (data.get("usage") or {})
        return SkillResult(
            skill_name=self.spec.name,
            summary=(answer_text.strip().split("\n", 1)[0] or
                     f"x_search: {query}")[:200],
            payload={
                "query": query,
                "allowed_x_handles": tool_params.get("allowed_x_handles") or [],
                "excluded_x_handles": tool_params.get("excluded_x_handles") or [],
                "from_date": tool_params.get("from_date"),
                "to_date": tool_params.get("to_date"),
                "answer": answer_text,
                "tweets": tweets,
                "model": model,
                "input_tokens": int(usage.get("input_tokens") or 0),
                "output_tokens": int(usage.get("output_tokens") or 0),
            },
            cost_usd=0.0,  # subscription-paid
            reasoning=None,
            raw_response=answer_text,
        )


def register_skills() -> None:
    """Register x_search ONLY if the xAI subscription is configured.

    Without auth the skill would 401 on first call — better to omit
    from the picker entirely so the CEO doesn't suggest a skill the
    founder can't run."""
    if is_configured():
        register(XSearchSkill())
        logger.info("x_search skill registered")
    else:
        logger.debug("x_search skill not registered — xAI OAuth not configured")
