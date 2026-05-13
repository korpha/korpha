"""PR7 tests — deterministic niche fit scorer.

12+ topic-mix fixtures covering core overlap, adjacent overlap,
off-limits hits, fatigue decay, density penalty, borderline escalation.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from korpha.business_units.model import NicheProfile
from korpha.business_units.scoring import (
    FitVerdict, score_fit,
)


# ---------------------------------------------------------------------------
# Core / adjacent / off-limits base
# ---------------------------------------------------------------------------


def test_all_core_hits_accept() -> None:
    profile = NicheProfile(
        core_topics=["ai_marketing", "automation"],
        adjacent_topics=["copywriting"],
    )
    out = score_fit(profile, ["ai_marketing", "automation"])
    assert out.verdict == FitVerdict.ACCEPT
    assert out.score >= 0.7


def test_off_limits_hit_decline_regardless_of_score() -> None:
    profile = NicheProfile(
        core_topics=["ai_marketing"],
        off_limits_topics=["homesteading"],
    )
    out = score_fit(profile, ["ai_marketing", "homesteading"])
    assert out.verdict == FitVerdict.DECLINE
    assert out.off_limits_hit is True


def test_no_overlap_declines() -> None:
    profile = NicheProfile(
        core_topics=["ai_marketing"],
        off_limits_topics=["finance"],
    )
    out = score_fit(profile, ["yoga", "meditation"])
    assert out.verdict == FitVerdict.DECLINE
    assert out.score == 0.0


def test_adjacent_only_escalates() -> None:
    """Adjacent topics give 0.5 weight; one adjacent in 1-topic work
    = 0.5 score → between thresholds → ESCALATE."""
    profile = NicheProfile(
        core_topics=["ai_marketing"],
        adjacent_topics=["copywriting"],
    )
    out = score_fit(profile, ["copywriting"])
    assert out.verdict == FitVerdict.ESCALATE


def test_mixed_core_and_adjacent() -> None:
    profile = NicheProfile(
        core_topics=["ai_marketing", "automation"],
        adjacent_topics=["copywriting", "analytics"],
    )
    # 2 core + 2 adj in 4 topics = (2*1 + 2*0.5)/4 = 0.75 → ACCEPT
    out = score_fit(profile, [
        "ai_marketing", "automation", "copywriting", "analytics",
    ])
    assert out.verdict == FitVerdict.ACCEPT


def test_case_insensitive_matching() -> None:
    profile = NicheProfile(
        core_topics=["AI_Marketing"],
    )
    out = score_fit(profile, ["ai_marketing"])
    assert out.verdict == FitVerdict.ACCEPT


def test_empty_work_topics_normalized_to_decline() -> None:
    profile = NicheProfile(core_topics=["x"])
    out = score_fit(profile, [])
    assert out.score == 0.0
    assert out.verdict == FitVerdict.DECLINE


def test_score_clamped_zero_to_one() -> None:
    """4 off-limits hits = -8 / 4 = -2 → clamped to 0.0."""
    profile = NicheProfile(
        off_limits_topics=["a", "b", "c", "d"],
    )
    out = score_fit(profile, ["a", "b", "c", "d"])
    assert out.score == 0.0
    assert out.verdict == FitVerdict.DECLINE


# ---------------------------------------------------------------------------
# Promo fatigue decay
# ---------------------------------------------------------------------------


def test_recent_promo_below_14d_heavy_penalty() -> None:
    """Even ideal-fit content gets penalized when last promo was <14d."""
    now = datetime(2026, 6, 1, tzinfo=UTC)
    profile = NicheProfile(
        core_topics=["ai_marketing"],
        last_promoted_at=now - timedelta(days=5),
    )
    out = score_fit(profile, ["ai_marketing"], now=now)
    # Base 1.0 - fatigue 0.30 = 0.70 → exactly at ACCEPT threshold
    assert out.fatigue_penalty == 0.30
    assert out.score == 0.70


def test_promo_14_to_28d_medium_penalty() -> None:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    profile = NicheProfile(
        core_topics=["ai_marketing"],
        last_promoted_at=now - timedelta(days=20),
    )
    out = score_fit(profile, ["ai_marketing"], now=now)
    assert out.fatigue_penalty == 0.15


def test_promo_over_28d_no_penalty() -> None:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    profile = NicheProfile(
        core_topics=["ai_marketing"],
        last_promoted_at=now - timedelta(days=60),
    )
    out = score_fit(profile, ["ai_marketing"], now=now)
    assert out.fatigue_penalty == 0.0
    assert out.verdict == FitVerdict.ACCEPT


# ---------------------------------------------------------------------------
# Density penalty
# ---------------------------------------------------------------------------


def test_low_promo_density_no_penalty() -> None:
    profile = NicheProfile(
        core_topics=["ai_marketing"],
        promos_in_last_30_days=2,
    )
    out = score_fit(profile, ["ai_marketing"])
    assert out.density_penalty == 0.0


def test_density_penalty_kicks_in_above_2() -> None:
    profile = NicheProfile(
        core_topics=["ai_marketing"],
        promos_in_last_30_days=5,  # 3 extra * 0.05 = 0.15
    )
    out = score_fit(profile, ["ai_marketing"])
    assert out.density_penalty == pytest_approx(0.15)


def test_density_penalty_capped_at_020() -> None:
    profile = NicheProfile(
        core_topics=["ai_marketing"],
        promos_in_last_30_days=20,
    )
    out = score_fit(profile, ["ai_marketing"])
    assert out.density_penalty == 0.20


def test_combined_fatigue_plus_density_can_decline() -> None:
    now = datetime(2026, 6, 1, tzinfo=UTC)
    profile = NicheProfile(
        core_topics=["ai_marketing"],
        adjacent_topics=["x"],
        last_promoted_at=now - timedelta(days=5),
        promos_in_last_30_days=10,  # 0.20 cap
    )
    # 1 adjacent in 1-topic work = 0.5 base.
    # 0.5 - 0.30 fatigue - 0.20 density = 0.0 → DECLINE
    out = score_fit(profile, ["x"], now=now)
    assert out.verdict == FitVerdict.DECLINE


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_deterministic_same_inputs_same_output() -> None:
    profile = NicheProfile(core_topics=["x"])
    a = score_fit(profile, ["x"])
    b = score_fit(profile, ["x"])
    assert a.score == b.score
    assert a.verdict == b.verdict


def pytest_approx(x: float, tol: float = 0.001):
    """Cheap approx — pytest.approx without the import."""
    class _Approx:
        def __eq__(self, other: object) -> bool:
            return isinstance(other, float) and abs(other - x) < tol
        def __repr__(self) -> str:
            return f"~{x}"
    return _Approx()
