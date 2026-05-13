"""Tests for the optional Debriefeur founder-profile integration."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from korpha.identity.founder_profile import (
    FounderProfile, load_founder_profile,
)


def test_empty_profile_when_file_missing(tmp_path: Path) -> None:
    """No file → empty profile, never crashes, empty preamble."""
    fp = load_founder_profile(tmp_path)
    assert fp.is_empty()
    assert fp.as_prompt_preamble() == ""


def test_empty_profile_when_file_is_unparseable(tmp_path: Path) -> None:
    """Garbage JSON → empty profile, not a crash. Defensive."""
    (tmp_path / "founder_profile.json").write_text("not actually json {")
    fp = load_founder_profile(tmp_path)
    assert fp.is_empty()
    assert fp.as_prompt_preamble() == ""


def test_empty_profile_when_file_is_a_list_not_dict(tmp_path: Path) -> None:
    """Unexpected JSON shape (list/string) → empty, not crash."""
    (tmp_path / "founder_profile.json").write_text('["not", "a", "dict"]')
    assert load_founder_profile(tmp_path).is_empty()


def test_loads_full_profile(tmp_path: Path) -> None:
    (tmp_path / "founder_profile.json").write_text(json.dumps({
        "decision_style": "pragmatic, validate before scaling",
        "risk_tolerance": "low — reversible bets only",
        "communication_preferences": "direct, bullet points, no fluff",
        "strengths": ["shipping", "copywriting"],
        "blindspots": ["finance", "hiring"],
        "operating_rhythm": "morning deep work, afternoon ops",
    }))
    fp = load_founder_profile(tmp_path)
    assert not fp.is_empty()
    assert fp.decision_style == "pragmatic, validate before scaling"
    assert fp.strengths == ("shipping", "copywriting")
    assert fp.blindspots == ("finance", "hiring")


def test_preamble_renders_every_known_field(tmp_path: Path) -> None:
    (tmp_path / "founder_profile.json").write_text(json.dumps({
        "decision_style": "DECIDE_STYLE",
        "risk_tolerance": "RISK_LEVEL",
        "communication_preferences": "COMM_PREFS",
        "strengths": ["S1", "S2"],
        "blindspots": ["B1"],
        "operating_rhythm": "OPS_RHYTHM",
        "raw_summary": "Some longer narrative description.",
    }))
    block = load_founder_profile(tmp_path).as_prompt_preamble()
    assert "How this founder thinks" in block
    assert "DECIDE_STYLE" in block
    assert "RISK_LEVEL" in block
    assert "COMM_PREFS" in block
    assert "S1, S2" in block
    assert "B1" in block
    assert "OPS_RHYTHM" in block
    assert "Some longer narrative description." in block


def test_preamble_skips_empty_fields(tmp_path: Path) -> None:
    """Don't render '- Decision style: ' empty lines for missing fields."""
    (tmp_path / "founder_profile.json").write_text(json.dumps({
        "decision_style": "thoughtful",
        # other fields intentionally missing
    }))
    block = load_founder_profile(tmp_path).as_prompt_preamble()
    assert "thoughtful" in block
    assert "Risk tolerance" not in block
    assert "Strengths" not in block
    assert "Operating rhythm" not in block


def test_preamble_empty_when_all_fields_blank(tmp_path: Path) -> None:
    """JSON with all empty values → empty preamble (don't render header)."""
    (tmp_path / "founder_profile.json").write_text(json.dumps({
        "decision_style": "",
        "risk_tolerance": "  ",
        "strengths": [],
        "blindspots": [],
    }))
    fp = load_founder_profile(tmp_path)
    assert fp.is_empty()
    assert fp.as_prompt_preamble() == ""


def test_extra_fields_captured_but_dont_break(tmp_path: Path) -> None:
    """Debriefeur evolves — new fields should be stored in `extra`,
    not crash the loader."""
    (tmp_path / "founder_profile.json").write_text(json.dumps({
        "decision_style": "fast",
        "some_new_field_debriefeur_added": "future-shape",
        "another_one": {"nested": "structure"},
    }))
    fp = load_founder_profile(tmp_path)
    assert fp.decision_style == "fast"
    assert "some_new_field_debriefeur_added" in fp.extra
    assert fp.extra["another_one"] == {"nested": "structure"}


def test_strengths_non_list_coerces_to_empty(tmp_path: Path) -> None:
    """Defensive: if Debriefeur ever writes a string where we expect a list."""
    (tmp_path / "founder_profile.json").write_text(json.dumps({
        "strengths": "shipping",  # should be a list
    }))
    fp = load_founder_profile(tmp_path)
    assert fp.strengths == ()


def test_strengths_filters_non_scalar_items(tmp_path: Path) -> None:
    """List items that aren't strings/numbers get dropped silently."""
    (tmp_path / "founder_profile.json").write_text(json.dumps({
        "strengths": ["ok", {"nested": "object"}, ["list"], 42, None, "also-ok"],
    }))
    fp = load_founder_profile(tmp_path)
    # str, int, float kept; dict, list, None dropped
    assert "ok" in fp.strengths
    assert "also-ok" in fp.strengths
    assert "42" in fp.strengths


def test_profile_with_only_raw_summary(tmp_path: Path) -> None:
    """Sometimes Debriefeur outputs only narrative — should render."""
    (tmp_path / "founder_profile.json").write_text(json.dumps({
        "raw_summary": "Mike thinks fast, ships often, and dislikes meetings.",
    }))
    fp = load_founder_profile(tmp_path)
    assert not fp.is_empty()
    block = fp.as_prompt_preamble()
    assert "ships often" in block


def test_preamble_header_only_when_content_exists(tmp_path: Path) -> None:
    """Verify the 'How this founder thinks' header doesn't appear with empty body."""
    # No file
    assert "How this founder thinks" not in load_founder_profile(tmp_path).as_prompt_preamble()
    # All blank
    (tmp_path / "founder_profile.json").write_text(json.dumps({"strengths": []}))
    assert "How this founder thinks" not in load_founder_profile(tmp_path).as_prompt_preamble()
    # One real field → header appears
    (tmp_path / "founder_profile.json").write_text(json.dumps({"decision_style": "x"}))
    assert "How this founder thinks" in load_founder_profile(tmp_path).as_prompt_preamble()
