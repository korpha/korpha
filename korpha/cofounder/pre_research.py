"""Pre-research injection for Director.attempt.

When the assigned task looks research-heavy ("KDP categories",
"competitor pricing", "trending niches", "current best practices",
etc.), run a web search before the Director drafts and inject the
top results as inline context. The Director then drafts with real
data instead of guessing from its training cutoff.

This is the simple short-term path. Long-term: when the Codex
Responses API transport lands, the Director can invoke web.search
mid-attempt as a real tool call.

Heuristic to avoid wasting search calls on every dispatch — only
triggers when the task contains research-shaped keywords.
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from korpha.web.types import SearchResult

logger = logging.getLogger(__name__)


# Words / phrases that signal the task benefits from current data.
# Conservative — false negatives are fine (skip search, draft from
# training); false positives cost ~1 search call.
_RESEARCH_TRIGGERS = (
    "categor", "tag", "keyword", "trend", "compet",
    "best practic", "current", "latest", "this year",
    "2024", "2025", "2026", "2027",
    "price", "pricing", "fee", "rate",
    "research", "analy", "benchmark", "popular",
    "top ", " top-", "review", "compare",
    "niche", "market", "audience size", "demand",
    "seo", "rank", "search volume",
    "amazon best", "etsy popular",
)


def _task_wants_research(task: str) -> bool:
    """Cheap regex-free keyword scan. Returns True when the task body
    matches any research trigger."""
    if not task:
        return False
    low = task.lower()
    return any(t in low for t in _RESEARCH_TRIGGERS)


def _build_query(task: str) -> str:
    """Strip role tags, pick a focused query out of the task text."""
    # Drop [CTO] / [CMO] / [WORKER:xxx] tags
    stripped = re.sub(r"\[[A-Z_][A-Z0-9_:-]*\]\s*", "", task).strip()
    # Drop common test-prefix markers
    stripped = re.sub(r"^_test fixture\s*[—-]\s*", "", stripped)
    # Bound length so DDG / Brave don't choke
    return stripped[:300]


async def build_pre_research_block(task: str, *, max_results: int = 4) -> str:
    """Return a markdown block to prepend to the Director's prompt, or
    "" if the task doesn't look research-needing or search returned
    nothing. Never raises."""
    if not _task_wants_research(task):
        return ""
    query = _build_query(task)
    if not query:
        return ""
    try:
        from korpha.web.search import web_search
        results: list[SearchResult] = await web_search(
            query, max_results=max_results,
        )
    except Exception:  # noqa: BLE001
        logger.warning("pre_research: web_search raised", exc_info=True)
        return ""
    if not results:
        return ""
    lines = [
        "Live web research (run by the chain layer for you — use as "
        "authoritative current data, cite urls in the deliverable):",
    ]
    for r in results:
        snippet = r.snippet[:240]
        if len(r.snippet) > 240:
            snippet += "…"
        url = r.url if r.url else "(no url)"
        lines.append(f"- **{r.title}** ({url})")
        if snippet:
            lines.append(f"  {snippet}")
    return "\n".join(lines)


__all__ = ["build_pre_research_block"]
