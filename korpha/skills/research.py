"""research.* skills — pull real web content into the cofounder's context.

``research.scrape_url`` opens a URL via the configured browser service,
pulls the rendered text, and asks the LLM to summarize what's actually
on the page. Most useful when the agent needs to see a competitor's
pricing page, a product's docs, or a recent blog post.

Without browser the agent only knows what's in its prompt — adding this
skill lets it answer questions like "What does competitor.com charge?"
or "What does this signup flow look like?" with grounded, current info.
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
You are summarizing a webpage on behalf of a solo entrepreneur. Below is
the rendered text of the page they asked about. Extract:

- title: short page title
- one_line_summary: one sentence describing what this page is for
- key_facts: 3-7 specific facts the founder would want to know (prices,
  features, claims, audience, calls to action). Concrete numbers and
  exact phrases beat vague summaries.
- pricing: any prices mentioned, with currency. Empty list if none.
- ctas: any obvious call-to-action buttons or signup prompts.
- competitor_signals: anything that suggests this page targets the same
  audience as the founder's business. Empty list if none / unknown.

Respond with strict JSON only:
{{
  "title": "...",
  "one_line_summary": "...",
  "key_facts": ["...", "..."],
  "pricing": ["$29/mo Starter", "..."],
  "ctas": ["Start free trial", "..."],
  "competitor_signals": ["..."]
}}

Page URL: {url}
Founder's research goal: {goal}

--- RENDERED PAGE TEXT ---
{text}
"""


class ScrapeUrlSkill(Skill):
    spec = SkillSpec(
        name="research.scrape_url",
        description=(
            "Visit a URL with the browser, return a structured summary: title, "
            "one-line summary, key facts, prices, CTAs, competitor signals. "
            "Use when the founder asks about a specific webpage."
        ),
        parameters={
            "url": "Full URL to visit (https://…)",
            "goal": "What the founder wants to learn (free text)",
        },
        default_tier=InferenceTier.PRO,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any]
    ) -> SkillResult:
        if ctx.browser is None:
            raise SkillError(
                "research.scrape_url needs a browser provider. Wire one via "
                "BrowserService and set it on SkillContext.browser."
            )
        url = str(args.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise SkillError(
                f"research.scrape_url: 'url' must start with http(s)://, got {url!r}"
            )
        goal = str(args.get("goal") or "general research").strip()

        from korpha.browser import BrowserTask

        task = BrowserTask(
            instruction=goal,
            start_url=url,
            extract_text=True,
            timeout_seconds=30.0,
        )
        result = await ctx.browser.run(task)
        if not result.success:
            raise SkillError(
                f"research.scrape_url: browser fetch failed for {url!r}: "
                f"{result.error or 'unknown error'}"
            )

        prompt = _PROMPT.format(
            url=url,
            goal=goal,
            text=result.extracted_text or "(empty page)",
        )
        request = CompletionRequest(
            messages=[
                Message(
                    role=Role.SYSTEM,
                    content=(
                        "You are the research skill in Korpha. Be concrete, "
                        "include exact numbers and phrases, never invent."
                    ),
                ),
                Message(role=Role.USER, content=prompt),
            ],
            tier=self.spec.default_tier,
            session_key=f"skill-research-{ctx.business.id}",
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
        if parsed is None or "one_line_summary" not in parsed:
            raise SkillError(
                f"research.scrape_url: model returned unparseable output. "
                f"first 500 chars: {response.content[:500]}"
            )

        title = parsed.get("title") or result.title or url
        summary = str(parsed.get("one_line_summary", title)).strip()

        return SkillResult(
            skill_name=self.spec.name,
            summary=f"{title} — {summary}"[:200],
            payload={
                "url": url,
                "fetched_url": result.final_url,
                "title": parsed.get("title") or result.title,
                "one_line_summary": parsed.get("one_line_summary"),
                "key_facts": parsed.get("key_facts") or [],
                "pricing": parsed.get("pricing") or [],
                "ctas": parsed.get("ctas") or [],
                "competitor_signals": parsed.get("competitor_signals") or [],
                "raw_text_chars": len(result.extracted_text or ""),
            },
            cost_usd=float(response.cost_usd),
            reasoning=response.reasoning,
            raw_response=response.content,
        )


register(ScrapeUrlSkill())
