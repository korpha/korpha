"""Tests for the small parity pack."""
from __future__ import annotations

from uuid import uuid4

import pytest

from korpha.parity import (
    SessionHandoff,
    append_subgoal,
    pick_pareto_model,
    render_active_subgoals,
)


# ---- SessionHandoff -----------------------------------------------


def test_handoff_records_history():
    h = SessionHandoff(current_alias="claude-sonnet")
    r = h.handoff_to("claude-opus")
    assert r.previous_alias == "claude-sonnet"
    assert r.new_alias == "claude-opus"
    assert h.current_alias == "claude-opus"
    assert h.history == ["claude-sonnet"]


def test_handoff_to_same_alias_is_noop():
    h = SessionHandoff(current_alias="grok")
    r = h.handoff_to("grok")
    assert "no-op" in r.notes
    assert h.history == []


def test_handoff_empty_alias_rejected():
    h = SessionHandoff(current_alias="grok")
    with pytest.raises(ValueError):
        h.handoff_to("")


def test_handoff_strips_whitespace():
    h = SessionHandoff(current_alias="grok")
    h.handoff_to("  claude  ")
    assert h.current_alias == "claude"


# ---- subgoal ------------------------------------------------------


def test_append_subgoal_basic():
    parent = uuid4()
    s = append_subgoal(parent_goal_id=parent, description="add tests")
    assert s.description == "add tests"
    assert s.parent_goal_id == parent
    assert s.active is True


def test_append_subgoal_rejects_empty():
    with pytest.raises(ValueError):
        append_subgoal(parent_goal_id=uuid4(), description="  ")


def test_render_active_subgoals_no_active():
    out = render_active_subgoals("ship landing page", [])
    assert out == "Active goal: ship landing page"


def test_render_active_subgoals_lists_each():
    pid = uuid4()
    subs = [
        append_subgoal(parent_goal_id=pid, description="form must validate"),
        append_subgoal(parent_goal_id=pid, description="mobile breakpoint"),
    ]
    out = render_active_subgoals("ship landing page", subs)
    assert "form must validate" in out
    assert "mobile breakpoint" in out
    assert "Active goal: ship landing page" in out
    assert "Acceptance criteria" in out


# ---- pareto_router -----------------------------------------------


def test_pareto_picks_cheapest_meeting_bar():
    """Default threshold 70 — DeepSeek V4 (free) is cheapest at
    score 70. Should win over Llama free (score 60, doesn't meet)
    and Sonnet (meets, but $$$)."""
    m = pick_pareto_model(min_coding_score=70.0)
    assert m is not None
    assert "free" in m.id  # picks a free model that meets the bar


def test_pareto_high_threshold_picks_top_tier():
    m = pick_pareto_model(min_coding_score=85.0)
    assert m is not None
    # GPT-5 (85) or Opus (88) — both meet the bar; cheaper wins.
    assert m.coding_score >= 85.0


def test_pareto_impossible_threshold_returns_none():
    m = pick_pareto_model(min_coding_score=99.9)
    assert m is None


def test_pareto_zero_threshold_picks_a_free_model():
    """No quality requirement → free wins outright."""
    m = pick_pareto_model(min_coding_score=0.0)
    assert m is not None
    assert m.input_per_1m_usd == 0.0
