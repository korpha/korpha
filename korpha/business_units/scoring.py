"""Deterministic niche-fit scorer (PR7).

Drives ``niche.score_fit`` skill and the cooperation-proposal
auto-decline logic. No LLM in v1 — pure arithmetic against the
NicheProfile's core / adjacent / off-limits topic lists, with a
promo-fatigue decay penalty + density penalty.

The scorer is the audience-protection gate: it refuses off-niche work
before the agent even drafts copy, preventing the burn-the-list
disaster solopreneurs fall into constantly.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from korpha.business_units.model import NicheProfile


class FitVerdict(StrEnum):
    ACCEPT = "accept"
    DECLINE = "decline"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class FitScore:
    score: float
    verdict: FitVerdict
    base_score: float
    fatigue_penalty: float
    density_penalty: float
    off_limits_hit: bool
    reason: str


def score_fit(
    profile: NicheProfile,
    work_topics: list[str],
    *,
    now: datetime | None = None,
) -> FitScore:
    """Score a piece of work against the niche profile.

    Algorithm (per BUSINESS_UNITS.md §Niche Profile):

    1. **Base relevance** — sum core (+1.0), adjacent (+0.5),
       off-limits (-2.0) matches, normalized by topic count, clamped
       to [0.0, 1.0].
    2. **Promo fatigue** — penalty based on how recent last_promoted_at
       is: <14d = -0.30, 14-28d = -0.15, >28d = 0.
    3. **Density penalty** — promos_in_last_30_days above 2 gets
       -0.05 per extra, capped at -0.20.

    Verdict:
      - score >= 0.7 OR ALL core matches → ACCEPT
      - score <= 0.2 OR any off-limits hit → DECLINE
      - otherwise → ESCALATE (CEO decides)
    """
    now = now or datetime.now(UTC)
    work = {t.strip().lower() for t in work_topics if t.strip()}

    core = {t.lower() for t in profile.core_topics}
    adjacent = {t.lower() for t in profile.adjacent_topics}
    off_limits = {t.lower() for t in profile.off_limits_topics}

    core_hits = len(work & core)
    adj_hits = len(work & adjacent)
    off_hits = len(work & off_limits)
    total = max(len(work), 1)

    base = (1.0 * core_hits + 0.5 * adj_hits - 2.0 * off_hits) / total
    base = max(0.0, min(1.0, base))

    # Fatigue
    if profile.last_promoted_at is None:
        fatigue = 0.0
    else:
        last = profile.last_promoted_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        days = (now - last).days
        if days < 14:
            fatigue = 0.30
        elif days < 28:
            fatigue = 0.15
        else:
            fatigue = 0.0

    # Density
    extra = max(0, profile.promos_in_last_30_days - 2)
    density = min(0.20, extra * 0.05)

    score = max(0.0, min(1.0, base - fatigue - density))

    off_limits_hit = off_hits > 0
    if off_limits_hit:
        return FitScore(
            score=score, verdict=FitVerdict.DECLINE,
            base_score=base, fatigue_penalty=fatigue,
            density_penalty=density, off_limits_hit=True,
            reason=(
                f"off-limits topic hit (matched {off_hits} of "
                f"{len(off_limits)} forbidden topics)"
            ),
        )

    if score >= 0.7:
        verdict = FitVerdict.ACCEPT
        reason = (
            f"strong fit ({core_hits} core + {adj_hits} adjacent)"
        )
    elif score <= 0.2:
        verdict = FitVerdict.DECLINE
        if fatigue > 0:
            reason = (
                f"weak base ({core_hits} core + {adj_hits} adj) "
                f"+ promo fatigue ({fatigue:.2f})"
            )
        elif density > 0:
            reason = (
                f"weak base + density penalty "
                f"({profile.promos_in_last_30_days} promos/30d)"
            )
        else:
            reason = (
                f"no relevance match ({core_hits} core + "
                f"{adj_hits} adjacent in {len(work)} topics)"
            )
    else:
        verdict = FitVerdict.ESCALATE
        reason = (
            f"borderline fit (score {score:.2f}); CEO to weigh "
            f"cross-line value"
        )

    return FitScore(
        score=score, verdict=verdict,
        base_score=base, fatigue_penalty=fatigue,
        density_penalty=density, off_limits_hit=False,
        reason=reason,
    )


__all__ = ["FitScore", "FitVerdict", "score_fit"]
