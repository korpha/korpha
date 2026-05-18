"""Pareto-optimal model picker for cost-conscious routing.

Given a quality threshold (min_coding_score), pick the cheapest model
that meets or exceeds it. Saves $ on tasks where any-reasonably-good
model would do, vs always routing to the best-quality option.

Scores are sourced from a small static table — the LiveBench-style
public benchmark numbers (coding category). Updated when major model
families release. Plugins can override / extend by registering
additional ``ParetoModel`` entries.

Use case: 50 boilerplate doc-comment writes don't need Opus; pick a
model that scores >= 70 on coding and let cost optimize.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ParetoModel:
    """One model + its public-benchmark quality scores + price."""

    id: str
    """OpenRouter / canonical model id."""

    coding_score: float
    """Coding-category score, 0-100. Sourced from public benchmarks
    (LiveBench, SWE-Bench, HumanEval averaged when no LiveBench).
    Update when frontier models ship."""

    input_per_1m_usd: float
    output_per_1m_usd: float

    label: str = ""
    """Display name."""

    def is_cheaper_than(self, other: "ParetoModel") -> bool:
        """Comparable on a 1:4 input:output mix (typical agent
        workload). Cheaper = lower total cost per typical 1M-token
        round trip."""
        a = self.input_per_1m_usd + 4 * self.output_per_1m_usd
        b = other.input_per_1m_usd + 4 * other.output_per_1m_usd
        return a < b


# Static catalog. Update as new models land. Sources: LiveBench
# coding category (Jan 2026 snapshot) + OpenRouter pricing.
_CATALOG: tuple[ParetoModel, ...] = (
    ParetoModel(
        id="anthropic/claude-opus-4-7",
        coding_score=88.0,
        input_per_1m_usd=15.00,
        output_per_1m_usd=75.00,
        label="Claude Opus 4.7",
    ),
    ParetoModel(
        id="anthropic/claude-sonnet-4-7",
        coding_score=82.0,
        input_per_1m_usd=3.00,
        output_per_1m_usd=15.00,
        label="Claude Sonnet 4.7",
    ),
    ParetoModel(
        id="openai/gpt-5.4",
        coding_score=85.0,
        input_per_1m_usd=10.00,
        output_per_1m_usd=40.00,
        label="GPT-5.4",
    ),
    ParetoModel(
        id="deepseek/deepseek-v4-pro",
        coding_score=80.0,
        input_per_1m_usd=0.27,
        output_per_1m_usd=1.10,
        label="DeepSeek V4 Pro",
    ),
    ParetoModel(
        id="deepseek/deepseek-v4-flash",
        coding_score=72.0,
        input_per_1m_usd=0.07,
        output_per_1m_usd=0.27,
        label="DeepSeek V4 Flash",
    ),
    ParetoModel(
        id="x-ai/grok-4.20-reasoning",
        coding_score=83.0,
        input_per_1m_usd=2.00,
        output_per_1m_usd=10.00,
        label="Grok 4.20 Reasoning",
    ),
    ParetoModel(
        id="moonshotai/kimi-k2.6",
        coding_score=78.0,
        input_per_1m_usd=0.60,
        output_per_1m_usd=2.50,
        label="Kimi K2.6",
    ),
    ParetoModel(
        id="meta-llama/llama-3.3-70b-instruct:free",
        coding_score=60.0,
        input_per_1m_usd=0.0,
        output_per_1m_usd=0.0,
        label="Llama 3.3 70B (free)",
    ),
    ParetoModel(
        id="deepseek/deepseek-chat-v4:free",
        coding_score=70.0,
        input_per_1m_usd=0.0,
        output_per_1m_usd=0.0,
        label="DeepSeek V4 (free)",
    ),
)


def pick_pareto_model(
    *, min_coding_score: float = 70.0,
    catalog: tuple[ParetoModel, ...] = _CATALOG,
) -> ParetoModel | None:
    """Return the cheapest model whose coding_score >= threshold.
    None when nothing meets the bar (caller falls back to default
    routing). Ties broken by score (higher wins) — operator gets
    a bit more headroom for the same dollar.
    """
    eligible = [m for m in catalog if m.coding_score >= min_coding_score]
    if not eligible:
        return None
    # Sort by cheapness (asc) then by score (desc) for tiebreak.
    eligible.sort(
        key=lambda m: (
            m.input_per_1m_usd + 4 * m.output_per_1m_usd,
            -m.coding_score,
        ),
    )
    return eligible[0]


def all_pareto_models() -> tuple[ParetoModel, ...]:
    """Exposed for the dashboard / debug. Returns the catalog
    in registration order."""
    return _CATALOG


__all__ = [
    "ParetoModel",
    "all_pareto_models",
    "pick_pareto_model",
]
