"""Tests that CEO + Directors actually consume the founder profile."""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from korpha.business.model import Business
from korpha.cofounder.director import Director, DirectorPersonality
from korpha.cofounder.model import RoleType
from korpha.identity.model import Founder
from korpha.audit.model import InferenceTier


def _make_director(session=None) -> Director:
    """Minimal director with the bare-minimum personality fields."""
    p = DirectorPersonality(
        role_type=RoleType.CTO,
        title="CTO",
        system_prompt="You are the CTO.",
        domains=["coding"],
        default_tier=InferenceTier.PRO,
    )
    return Director(
        personality=p,
        session=session or MagicMock(),
        cost_tracker=MagicMock(),
        queue=MagicMock(),
        hiring=MagicMock(),
    )


def test_director_system_prompt_includes_profile_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a profile JSON is on disk, the Director's system prompt
    must contain the 'How this founder thinks' preamble."""
    (tmp_path / "founder_profile.json").write_text(json.dumps({
        "decision_style": "ship-fast-then-iterate",
        "blindspots": ["finance"],
    }))
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))

    director = _make_director()
    biz = Business(name="Marketro", founder_id=uuid4())
    founder = Founder(name="Mike", email="mike@example.com")

    prompt = director._system_prompt(biz, founder)
    assert "You are the CTO." in prompt
    assert "Marketro" in prompt
    assert "How this founder thinks" in prompt
    assert "ship-fast-then-iterate" in prompt
    assert "finance" in prompt


def test_director_system_prompt_clean_when_no_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No profile file → no preamble. Original prompt unchanged."""
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))

    director = _make_director()
    biz = Business(name="Marketro", founder_id=uuid4())
    founder = Founder(name="Mike", email="mike@example.com")

    prompt = director._system_prompt(biz, founder)
    assert "How this founder thinks" not in prompt
    assert "You are the CTO." in prompt
    assert "Marketro" in prompt


def test_director_system_prompt_survives_bad_profile_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Garbage profile file must not break the system prompt."""
    (tmp_path / "founder_profile.json").write_text("garbage {")
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))

    director = _make_director()
    biz = Business(name="Marketro", founder_id=uuid4())
    founder = Founder(name="Mike", email="mike@example.com")

    # Must not raise
    prompt = director._system_prompt(biz, founder)
    assert "You are the CTO." in prompt
    # No preamble injected from bad file
    assert "How this founder thinks" not in prompt


def test_director_system_prompt_uses_only_canonical_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Future debriefeur fields in `extra` shouldn't leak into the prompt
    (the preamble renders known fields only). Defensive against schema drift."""
    (tmp_path / "founder_profile.json").write_text(json.dumps({
        "decision_style": "thoughtful",
        "some_future_field": "FUTURE_VALUE_DO_NOT_LEAK",
    }))
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))

    director = _make_director()
    biz = Business(name="Marketro", founder_id=uuid4())
    founder = Founder(name="Mike", email="mike@example.com")

    prompt = director._system_prompt(biz, founder)
    assert "thoughtful" in prompt
    # We deliberately do not render extra fields — they're stored on
    # the profile object for inspection but not rendered to the LLM.
    assert "FUTURE_VALUE_DO_NOT_LEAK" not in prompt


def test_profile_picked_up_without_server_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each _system_prompt call should re-read the profile from disk —
    so when Mike finishes `korpha debrief`, the next message already
    benefits without a restart."""
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))

    director = _make_director()
    biz = Business(name="Marketro", founder_id=uuid4())
    founder = Founder(name="Mike", email="mike@example.com")

    # Before profile exists
    p1 = director._system_prompt(biz, founder)
    assert "How this founder thinks" not in p1

    # Mike runs korpha debrief — profile lands
    (tmp_path / "founder_profile.json").write_text(json.dumps({
        "decision_style": "post-debrief-style",
    }))

    # Same director instance, no restart — next call must see it
    p2 = director._system_prompt(biz, founder)
    assert "post-debrief-style" in p2
