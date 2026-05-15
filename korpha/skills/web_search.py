"""``web.search`` and ``web.extract`` skills — agent-callable wrappers
around :mod:`korpha.web.search`.

Lets the CEO router (and any chain step) pull live web data when
training-cutoff knowledge isn't enough. Cascade picks the best
configured provider; ``[]`` when nothing's wired."""
from __future__ import annotations

from typing import Any

from korpha.audit.model import InferenceTier
from korpha.skills.registry import register
from korpha.skills.types import (
    Skill, SkillContext, SkillProvenance, SkillResult, SkillSpec,
)


class WebSearchSkill(Skill):
    """Search the web via the configured provider cascade."""

    spec = SkillSpec(
        name="web.search",
        description=(
            "Search the web and return up to N results with title, "
            "URL, and a short snippet. Cascade tries paid providers "
            "(Tavily/Exa/Perplexity/Gemini/etc.) first when keyed, "
            "falls back to Brave free tier, then SearXNG if self-"
            "hosted, then DuckDuckGo (always available, no key). "
            "Use when you need current information beyond training "
            "cutoff: trends, prices, competitor moves, fresh "
            "categories/tags, or to verify a claim."
        ),
        parameters={
            "query": (
                "What to search for. Plain text question or keyword "
                "string. ~3-12 words tends to work best."
            ),
            "max_results": (
                "Optional. How many hits to return (default 5, max 20)."
            ),
            "site": (
                "Optional. Restrict to one domain — e.g. "
                "'amazon.com', 'etsy.com'. Most providers respect this."
            ),
            "recency_days": (
                "Optional. Filter to results from the last N days. "
                "Use for trends / breaking news. Some providers map "
                "this to coarser buckets (day/week/month/year)."
            ),
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        from korpha.web.search import web_search

        query = str(args.get("query") or "").strip()
        if not query:
            return SkillResult(
                summary="empty query — nothing to search",
                payload={"results": []},
            )
        try:
            max_results = int(args.get("max_results") or 5)
        except (TypeError, ValueError):
            max_results = 5
        site = (args.get("site") or None)
        site = str(site).strip() if site else None
        try:
            recency_days = (
                int(args.get("recency_days"))
                if args.get("recency_days") is not None else None
            )
        except (TypeError, ValueError):
            recency_days = None

        results = await web_search(
            query,
            max_results=max(1, min(max_results, 20)),
            site=site,
            recency_days=recency_days,
        )
        payload = {
            "query": query,
            "results": [
                {
                    "title": r.title,
                    "url": r.url,
                    "snippet": r.snippet,
                    "provider": r.provider,
                    "published_at": r.published_at,
                    "score": r.score,
                }
                for r in results
            ],
        }
        summary = (
            f"web.search({query!r}) → {len(results)} result(s)"
            + (
                f" via {results[0].provider}"
                if results and results[0].provider
                else ""
            )
        )
        return SkillResult(summary=summary, payload=payload)


class WebExtractSkill(Skill):
    """Pull the full content of a URL via the configured provider."""

    spec = SkillSpec(
        name="web.extract",
        description=(
            "Fetch and return the readable content of a URL as text "
            "or markdown. Cascade tries providers that support "
            "extraction (Tavily/Exa/Firecrawl). Returns empty when "
            "no extract-capable provider is configured."
        ),
        parameters={
            "url": "The URL to fetch.",
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        from korpha.web.search import web_extract

        url = str(args.get("url") or "").strip()
        if not url:
            return SkillResult(
                summary="empty url — nothing to extract",
                payload={"content": ""},
            )
        result = await web_extract(url)
        if result is None:
            return SkillResult(
                summary=f"web.extract({url}) → no extract-capable provider configured",
                payload={"content": "", "url": url},
            )
        return SkillResult(
            summary=f"web.extract({url}) → {len(result.content)} chars via {result.provider}",
            payload={
                "content": result.content,
                "title": result.title,
                "url": result.url,
                "provider": result.provider,
            },
        )


def register_skills() -> None:
    register(WebSearchSkill())
    register(WebExtractSkill())


__all__ = [
    "WebExtractSkill", "WebSearchSkill", "register_skills",
]
