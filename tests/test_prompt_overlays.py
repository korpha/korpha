"""Tests for the per-model prompt overlay mechanism.

Most important assertion: **open-weights models get NO overlay**.
That's how we keep DeepSeek's 96.2% score from drifting. If this
test ever fails, the overlay catalogue picked up a pattern that
matches an open-weights model id — fix the prefix to be more
specific.
"""
from __future__ import annotations

import os

import pytest

from korpha.cofounder.prompt_overlays import (
    _OVERLAYS,
    apply_overlay,
    get_overlay,
)
from korpha.inference.types import CompletionRequest, Message, Role
from korpha.audit.model import InferenceTier


def _request(system: str = "You are CEO.") -> CompletionRequest:
    return CompletionRequest(
        messages=[
            Message(role=Role.SYSTEM, content=system),
            Message(role=Role.USER, content="What should we do?"),
        ],
        tier=InferenceTier.PRO,
        session_key="test",
    )


# ---------------------------------------------------------------------------
# get_overlay — matches expected model patterns, ignores open-weights
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model_id", [
    "gpt-5.4",
    "gpt-5.4-codex",
    "GPT-5.4",
    "openai/gpt-5.4",
    "openai/gpt-5",
    "gpt-5.5-preview",
])
def test_gpt5_models_get_overlay(model_id: str) -> None:
    overlay = get_overlay(model_id)
    assert overlay
    assert "Variant" in overlay
    assert "Word caps" in overlay


@pytest.mark.parametrize("model_id", [
    "claude-opus-4-7",
    "claude-opus-4",
    "claude-opus-4-7-1m",
    "anthropic/claude-opus-4-7",
])
def test_claude_opus_models_get_overlay(model_id: str) -> None:
    overlay = get_overlay(model_id)
    assert overlay
    assert "delegate" in overlay.lower()


@pytest.mark.parametrize("model_id", [
    # open-weights — must stay no-op so existing scores hold
    "deepseek-v4-pro",
    "deepseek-v4-flash",
    "deepseek/deepseek-chat-v4",
    "kimi-k2.6",
    "moonshotai/kimi-k2.6",
    "glm-5.1",
    "qwen3.6-27b",
    "unsloth/Qwen3.5-9B",
    "unsloth/Ministral-3-14B-Instruct",
    "unsloth/Phi-4",
    "google/gemma-4-e2b-it",
    "openai/gpt-oss-120b",  # OpenAI's OPEN-weights — different from gpt-5
    "nvidia/nemotron-3-super-120b-a12b",
    "ibm/granite-4.1-8b",
    "mistral-large-3",
])
def test_open_weights_no_overlay(model_id: str) -> None:
    """The whole point of this PR: open-weights models must NOT get
    an overlay. Their prompts have been tuned to DeepSeek's habits;
    appending overlay text would break the 96.2% baseline.
    """
    assert get_overlay(model_id) == "", (
        f"open-weights model {model_id!r} unexpectedly got an overlay. "
        "Refine the prefix in _OVERLAYS to be more specific."
    )


def test_gpt_oss_is_NOT_treated_as_gpt5() -> None:
    """openai/gpt-oss-120b is OPEN-weights — must not pick up the
    gpt-5 overlay. Regression guard against the obvious 'gpt-' prefix
    collision."""
    assert get_overlay("openai/gpt-oss-120b") == ""
    assert get_overlay("openai/gpt-oss-20b") == ""


def test_empty_or_unknown_model_id() -> None:
    assert get_overlay("") == ""
    assert get_overlay("totally-fake-model") == ""
    assert get_overlay(None) == ""  # type: ignore[arg-type]


def test_env_var_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """KORPHA_DISABLE_PROMPT_OVERLAYS=1 short-circuits even known
    models — used by the eval driver to measure baseline lift."""
    monkeypatch.setenv("KORPHA_DISABLE_PROMPT_OVERLAYS", "1")
    assert get_overlay("gpt-5.4") == ""
    assert get_overlay("claude-opus-4-7") == ""


# ---------------------------------------------------------------------------
# apply_overlay — request-level merge
# ---------------------------------------------------------------------------


def test_apply_overlay_appends_to_system_message() -> None:
    req = _request(system="You are CEO. Stay terse.")
    out = apply_overlay(req, "gpt-5.4")
    assert out is not req  # new request, not mutated
    assert out.messages[0].role == Role.SYSTEM
    assert out.messages[0].content.startswith("You are CEO. Stay terse.")
    assert "Variant" in out.messages[0].content
    # User message unchanged
    assert out.messages[1].content == "What should we do?"


def test_apply_overlay_no_op_for_open_weights() -> None:
    req = _request()
    out = apply_overlay(req, "deepseek-v4-pro")
    assert out is req  # same object — no mutation, no copy
    assert out.messages[0].content == "You are CEO."


def test_apply_overlay_no_op_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_DISABLE_PROMPT_OVERLAYS", "1")
    req = _request()
    out = apply_overlay(req, "gpt-5.4")
    assert out is req
    assert "Variant" not in out.messages[0].content


def test_apply_overlay_no_op_when_no_system_message() -> None:
    """Edge case: some skill-author calls send user-only messages.
    Don't conjure a system message — just leave the request alone."""
    req = CompletionRequest(
        messages=[Message(role=Role.USER, content="hello")],
        tier=InferenceTier.PRO,
        session_key="test",
    )
    out = apply_overlay(req, "gpt-5.4")
    assert out is req


def test_apply_overlay_preserves_user_history() -> None:
    """Multi-turn requests: only system message gets the overlay,
    user/assistant history is untouched."""
    req = CompletionRequest(
        messages=[
            Message(role=Role.SYSTEM, content="You are CEO."),
            Message(role=Role.USER, content="first user msg"),
            Message(role=Role.ASSISTANT, content="first reply"),
            Message(role=Role.USER, content="follow-up"),
        ],
        tier=InferenceTier.PRO,
        session_key="test",
    )
    out = apply_overlay(req, "claude-opus-4-7")
    assert len(out.messages) == 4
    assert "delegate" in out.messages[0].content.lower()
    assert out.messages[1].content == "first user msg"
    assert out.messages[2].content == "first reply"
    assert out.messages[3].content == "follow-up"


# ---------------------------------------------------------------------------
# Catalogue invariants
# ---------------------------------------------------------------------------


def test_overlays_dict_keys_are_lowercased() -> None:
    """The matcher does case-insensitive prefix scan — catalogue keys
    must be lowercase to match the lowered model id consistently."""
    for key in _OVERLAYS:
        assert key == key.lower(), (
            f"Overlay key {key!r} must be lowercase"
        )


def test_overlays_dict_has_meaningful_content() -> None:
    """Each overlay should be more than a placeholder."""
    for key, text in _OVERLAYS.items():
        assert len(text) >= 100, (
            f"Overlay for {key!r} is suspiciously short ({len(text)} chars)"
        )
