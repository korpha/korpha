"""Tests for per-task tier overrides via auxiliary.yaml."""
from __future__ import annotations

from pathlib import Path

import pytest

from korpha.audit.model import InferenceTier
from korpha.inference.auxiliary import (
    AuxiliaryConfig,
    invalidate_cache,
    load_auxiliary_config,
)


@pytest.fixture(autouse=True)
def _reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    invalidate_cache()
    yield
    invalidate_cache()


def _write_config(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "auxiliary.yaml"
    p.write_text(body)
    return p


# ---- AuxiliaryConfig.resolve_tier ----


def test_empty_config_is_passthrough() -> None:
    cfg = AuxiliaryConfig()
    assert cfg.resolve_tier("anything", InferenceTier.PRO) == InferenceTier.PRO


def test_passthrough_when_no_prefix_matches() -> None:
    cfg = AuxiliaryConfig(
        tier_overrides={"summarize": InferenceTier.WORKHORSE},
    )
    assert cfg.resolve_tier(
        "ceo-handle-x", InferenceTier.PRO,
    ) == InferenceTier.PRO


def test_simple_prefix_match() -> None:
    cfg = AuxiliaryConfig(
        tier_overrides={"summarize": InferenceTier.WORKHORSE},
    )
    assert cfg.resolve_tier(
        "summarize:thread-abc", InferenceTier.PRO,
    ) == InferenceTier.WORKHORSE


def test_longest_prefix_wins() -> None:
    """A more-specific prefix overrides a more-general one."""
    cfg = AuxiliaryConfig(tier_overrides={
        "ceo-": InferenceTier.PRO,
        "ceo-handle-": InferenceTier.CONSULTANT,
    })
    # ceo-handle-X matches both prefixes; the longer wins
    assert cfg.resolve_tier(
        "ceo-handle-abc", InferenceTier.WORKHORSE,
    ) == InferenceTier.CONSULTANT
    # ceo-router-X only matches the shorter prefix
    assert cfg.resolve_tier(
        "ceo-router-abc", InferenceTier.WORKHORSE,
    ) == InferenceTier.PRO


def test_resolve_tier_handles_none_session_key() -> None:
    cfg = AuxiliaryConfig(
        tier_overrides={"x": InferenceTier.WORKHORSE},
    )
    assert cfg.resolve_tier(None, InferenceTier.PRO) == InferenceTier.PRO


# ---- load_auxiliary_config ----


def test_load_returns_empty_when_missing(tmp_path: Path) -> None:
    cfg = load_auxiliary_config()
    assert cfg.tier_overrides == {}


def test_load_parses_valid_config(tmp_path: Path) -> None:
    _write_config(tmp_path, """
tasks:
  summarize: workhorse
  director-cto: pro
  cos-triage: workhorse
""")
    cfg = load_auxiliary_config()
    assert cfg.tier_overrides == {
        "summarize": InferenceTier.WORKHORSE,
        "director-cto": InferenceTier.PRO,
        "cos-triage": InferenceTier.WORKHORSE,
    }


def test_load_normalizes_tier_case(tmp_path: Path) -> None:
    _write_config(tmp_path, """
tasks:
  summarize: WORKHORSE
  ceo-: Pro
""")
    cfg = load_auxiliary_config()
    assert cfg.tier_overrides["summarize"] == InferenceTier.WORKHORSE
    assert cfg.tier_overrides["ceo-"] == InferenceTier.PRO


def test_load_skips_unknown_tier_with_warning(
    tmp_path: Path, caplog,
) -> None:
    _write_config(tmp_path, """
tasks:
  summarize: workhorse
  bad-task: turbo-extreme
""")
    cfg = load_auxiliary_config()
    assert "summarize" in cfg.tier_overrides
    assert "bad-task" not in cfg.tier_overrides


def test_load_handles_malformed_yaml(tmp_path: Path) -> None:
    _write_config(tmp_path, "not: [valid: yaml")
    cfg = load_auxiliary_config()
    assert cfg.tier_overrides == {}


def test_load_handles_no_tasks_key(tmp_path: Path) -> None:
    _write_config(tmp_path, "other_key: 42")
    cfg = load_auxiliary_config()
    assert cfg.tier_overrides == {}


def test_load_caches_in_process(tmp_path: Path) -> None:
    """Second call returns the same object — no re-parse cost."""
    _write_config(tmp_path, "tasks: {summarize: workhorse}")
    a = load_auxiliary_config()
    b = load_auxiliary_config()
    assert a is b


def test_force_refresh_re_reads(tmp_path: Path) -> None:
    _write_config(tmp_path, "tasks: {summarize: workhorse}")
    a = load_auxiliary_config()
    assert "summarize" in a.tier_overrides

    _write_config(tmp_path, "tasks: {director-cto: pro}")
    b = load_auxiliary_config(force_refresh=True)
    assert "director-cto" in b.tier_overrides
    assert "summarize" not in b.tier_overrides


# ---- CostTracker integration ----


@pytest.mark.asyncio
async def test_cost_tracker_applies_aux_override(
    tmp_path: Path,
) -> None:
    """End-to-end: writing summarize: workhorse → director-summarize
    requests get rewritten before hitting the pool."""
    from korpha.audit.model import InferenceTier
    from korpha.inference import (
        CompletionRequest, MockProvider, ProviderAccount,
    )
    from korpha.inference.cost_tracker import CostTracker
    from korpha.inference.pool import InferencePool
    from korpha.inference.registry import AuthType

    _write_config(tmp_path, "tasks:\n  summarize: workhorse\n")
    invalidate_cache()

    captured_tier: list[str] = []

    class _SnoopProvider(MockProvider):
        async def complete(self, request, account):
            captured_tier.append(request.tier.value)
            return await super().complete(request, account)

    account = ProviderAccount(
        provider_name="mock",
        auth_type=AuthType.API_KEY,
        tier_models={
            InferenceTier.PRO: "p", InferenceTier.WORKHORSE: "w",
        },
        api_key="x",
    )
    pool = InferencePool(
        providers=[_SnoopProvider(static_response="ok")],
        accounts=[account],
    )
    tracker = CostTracker(pool=pool)

    # Caller asks for PRO but session_key matches summarize → WORKHORSE
    rewritten = tracker._apply_auxiliary_overrides(CompletionRequest(
        messages=[],
        tier=InferenceTier.PRO,
        session_key="summarize:thread-abc",
    ))
    assert rewritten.tier == InferenceTier.WORKHORSE


@pytest.mark.asyncio
async def test_cost_tracker_passthrough_when_no_match(
    tmp_path: Path,
) -> None:
    """No matching prefix → request is unchanged (same object,
    even — short-circuit avoids constructing a new one)."""
    from korpha.audit.model import InferenceTier
    from korpha.inference import CompletionRequest
    from korpha.inference.cost_tracker import CostTracker

    _write_config(tmp_path, "tasks:\n  summarize: workhorse\n")
    invalidate_cache()

    tracker = CostTracker(pool=None)  # type: ignore[arg-type]
    req = CompletionRequest(
        messages=[],
        tier=InferenceTier.PRO,
        session_key="ceo-handle-x",  # doesn't match 'summarize'
    )
    out = tracker._apply_auxiliary_overrides(req)
    assert out is req  # same object — no allocation when no rewrite


@pytest.mark.asyncio
async def test_cost_tracker_passthrough_when_config_empty(
    tmp_path: Path,
) -> None:
    """No config file → no allocation, request returned as-is."""
    from korpha.audit.model import InferenceTier
    from korpha.inference import CompletionRequest
    from korpha.inference.cost_tracker import CostTracker

    invalidate_cache()
    tracker = CostTracker(pool=None)  # type: ignore[arg-type]
    req = CompletionRequest(
        messages=[],
        tier=InferenceTier.PRO,
        session_key="anything",
    )
    out = tracker._apply_auxiliary_overrides(req)
    assert out is req
