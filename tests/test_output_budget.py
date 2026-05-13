"""Tests for the output-budget / spillover helpers.

These cover the cost-control gate: when a skill returns more than
~16KB, full content goes to disk and the model sees a preview + path.
We verify each layer independently — per-result, per-turn aggregate,
the file write, and the inline-truncation fallback when disk write
fails.

We don't test against a real ``~/.korpha`` directory; KORPHA_DATA_DIR
is set to a tmp_path in every test that touches disk.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from korpha.limits import (
    PERSISTED_OUTPUT_TAG,
    enforce_turn_budget,
    is_persisted,
    persist_if_oversized,
)
from korpha.limits.output_budget import (
    _sanitize_ref_id,
    serialize_for_prompt,
)


# ---- per-result spillover ----


def test_under_threshold_returns_content_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    out = persist_if_oversized(
        "small payload", ref_id="x", threshold=1000,
    )
    assert out == "small payload"
    # No file written
    assert not (tmp_path / "tool_results").exists()


def test_over_threshold_spills_to_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    big = "X" * 2000
    out = persist_if_oversized(
        big, ref_id="skill-foo", threshold=100, preview_chars=50,
    )
    assert PERSISTED_OUTPUT_TAG in out
    assert "skill-foo" in (
        # file path mentioned in the wrapper
        out
    )
    assert "2,000 chars" in out
    # File was written with the full content
    files = list((tmp_path / "tool_results").iterdir())
    assert len(files) == 1
    assert files[0].read_text() == big


def test_preview_includes_first_chars(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    payload = "hello world\n" * 1000  # ≈ 12K chars
    out = persist_if_oversized(
        payload, ref_id="x", threshold=200, preview_chars=100,
    )
    # Preview should contain the start of the payload
    assert "hello world" in out
    # And explicitly not the whole thing
    assert out.count("hello world") < 1000


def test_preview_cuts_at_newline_when_possible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a newline lives in the second half of the budget, prefer
    cutting there for cleaner output."""
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    # 80-char preview budget; put newline at char 60
    payload = "A" * 60 + "\n" + "B" * 4000
    out = persist_if_oversized(
        payload, ref_id="x", threshold=10, preview_chars=80,
    )
    # The 'A's run is preserved as a clean line, B's are truncated
    assert "AAA" in out
    assert "BBBBB" not in out


def test_already_persisted_content_passes_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nested or repeated calls shouldn't double-spill."""
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    persisted = (
        f"{PERSISTED_OUTPUT_TAG}\nfake preview\n</persisted-output>"
    ) + "X" * 5000
    out = persist_if_oversized(
        persisted, ref_id="x", threshold=100,
    )
    assert out == persisted
    assert not (tmp_path / "tool_results").exists()


def test_disk_write_failure_falls_back_to_inline_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read-only filesystem / disk full → inline truncate, NEVER
    crash. Better to lose the tail of one tool result than the turn."""
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))

    # Make Path.write_text fail
    original = Path.write_text

    def boom(self: Path, *_a, **_k) -> int:
        raise OSError("read-only filesystem")

    monkeypatch.setattr(Path, "write_text", boom)

    payload = "Y" * 5000
    out = persist_if_oversized(
        payload, ref_id="x", threshold=100, preview_chars=200,
    )
    # No persisted-output tag (we couldn't write the file)
    assert PERSISTED_OUTPUT_TAG not in out
    # But we still get a Truncated marker
    assert "Truncated" in out
    assert "5,000 chars" in out


# ---- ref_id sanitization (file safety) ----


@pytest.mark.parametrize("dirty,clean", [
    ("skill.foo", "skill.foo"),
    # Path-separators get replaced; dots stay (safe in filename) —
    # what matters is that the result is FLAT, not that it visually
    # erases the original characters.
    ("../etc/passwd", "..-etc-passwd"),
    ("name with spaces", "name-with-spaces"),
    ("UPPERCASE_123", "UPPERCASE_123"),
    ("multiple///slashes", "multiple-slashes"),
    ("control\x00\x01char", "control-char"),
    ("", "tool-result"),
    ("---only-dashes---", "only-dashes"),
])
def test_sanitize_ref_id_produces_safe_filename(
    dirty: str, clean: str,
) -> None:
    assert _sanitize_ref_id(dirty) == clean


def test_sanitize_ref_id_caps_length() -> None:
    out = _sanitize_ref_id("a" * 500)
    assert len(out) == 100


# ---- per-turn budget ----


def test_under_budget_returns_input_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    items = ["small", "also small"]
    out = enforce_turn_budget(items, ref_id_prefix="t", budget=1000)
    assert out == items


def test_over_budget_spills_largest_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Aggregate of 3K + 2K + 1K = 6K. Budget 4K → must spill the
    3K item, leaving 2K + 1K + (small persisted block) under budget."""
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    items = ["A" * 3000, "B" * 2000, "C" * 1000]
    out = enforce_turn_budget(
        items, ref_id_prefix="t", budget=4000, preview_chars=200,
    )
    # Item 0 (largest) was spilled
    assert PERSISTED_OUTPUT_TAG in out[0]
    assert PERSISTED_OUTPUT_TAG not in out[1]
    assert PERSISTED_OUTPUT_TAG not in out[2]
    # Total now under budget
    assert sum(len(s) for s in out) <= 4000


def test_skips_already_persisted_when_choosing_what_to_spill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The largest item is a pre-persisted block (cheap preview) —
    enforcer should leave it alone and spill the next-largest
    non-persisted item instead."""
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    pre_persisted = (
        PERSISTED_OUTPUT_TAG + "\nfake\n</persisted-output>"
    ) + "Z" * 10000
    items = [pre_persisted, "B" * 5000, "C" * 1000]
    out = enforce_turn_budget(
        items, ref_id_prefix="t", budget=2000, preview_chars=200,
    )
    # The pre-persisted item is left alone
    assert out[0] == pre_persisted
    # The next-largest non-persisted item (B) got spilled
    assert PERSISTED_OUTPUT_TAG in out[1]


def test_is_persisted_recognizes_tag() -> None:
    assert is_persisted(f"prefix\n{PERSISTED_OUTPUT_TAG}\nbody") is True
    assert is_persisted("plain text") is False


# ---- env-var overrides ----


def test_persist_threshold_env_var_changes_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Setting KORPHA_PERSIST_THRESHOLD_CHARS changes the
    module-level default at import time. Verified via re-import."""
    import importlib

    monkeypatch.setenv("KORPHA_PERSIST_THRESHOLD_CHARS", "5000")
    from korpha.limits import output_budget
    importlib.reload(output_budget)
    try:
        assert output_budget.PERSIST_THRESHOLD_CHARS == 5000
    finally:
        # Restore module state for downstream tests
        monkeypatch.delenv("KORPHA_PERSIST_THRESHOLD_CHARS", raising=False)
        importlib.reload(output_budget)


def test_invalid_env_var_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    monkeypatch.setenv("KORPHA_PERSIST_THRESHOLD_CHARS", "not-an-int")
    from korpha.limits import output_budget
    importlib.reload(output_budget)
    try:
        # Falls back to the hardcoded default (16,000)
        assert output_budget.PERSIST_THRESHOLD_CHARS == 16_000
    finally:
        monkeypatch.delenv("KORPHA_PERSIST_THRESHOLD_CHARS", raising=False)
        importlib.reload(output_budget)


def test_negative_env_var_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    monkeypatch.setenv("KORPHA_PERSIST_THRESHOLD_CHARS", "-1")
    from korpha.limits import output_budget
    importlib.reload(output_budget)
    try:
        assert output_budget.PERSIST_THRESHOLD_CHARS == 16_000
    finally:
        monkeypatch.delenv("KORPHA_PERSIST_THRESHOLD_CHARS", raising=False)
        importlib.reload(output_budget)


# ---- serialize_for_prompt ----


def test_serialize_passes_strings_through() -> None:
    assert serialize_for_prompt("already a string") == "already a string"


def test_serialize_pretty_prints_dict() -> None:
    out = serialize_for_prompt({"k": "v", "n": 1})
    assert '"k"' in out
    assert "  " in out  # indented


def test_serialize_handles_non_serializable_via_repr() -> None:
    """Datetime, custom objects, etc. → str() fallback rather than crash."""
    from datetime import datetime
    out = serialize_for_prompt({"when": datetime(2026, 5, 7)})
    assert "2026-05-07" in out


def test_serialize_falls_back_to_repr_on_typeerror() -> None:
    class _Weird:
        def __repr__(self) -> str:
            return "<weird>"
    obj = {"x": _Weird()}  # default=str will call str() on _Weird, returning the repr
    out = serialize_for_prompt(obj)
    # The default=str fallback covers most cases, so we get a string
    assert "<weird>" in out


# ---- ceo._skill_synth_prompt integration ----


def test_skill_synth_prompt_spills_oversize_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a skill returning a 50KB payload goes through
    _skill_synth_prompt and the resulting prompt has the preview
    block, not the raw 50KB."""
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    # Force a small threshold so we don't have to actually generate
    # 16KB of payload to trigger the spill path.
    monkeypatch.setenv("KORPHA_PERSIST_THRESHOLD_CHARS", "200")
    monkeypatch.setenv("KORPHA_PREVIEW_CHARS", "80")
    import importlib

    from korpha.limits import output_budget
    importlib.reload(output_budget)
    # Re-import ceo module after limits reload so the lazy
    # ``from korpha.limits import ...`` sees the new defaults.
    from korpha.cofounder import ceo as ceo_mod
    from korpha.skills.types import SkillResult

    skill = SkillResult(
        skill_name="research.scrape",
        summary="scraped one page",
        payload={"raw_html": "X" * 5000, "title": "Example"},
    )
    prompt = ceo_mod._skill_synth_prompt("Research this site", skill)
    assert PERSISTED_OUTPUT_TAG in prompt
    # The 5KB blob isn't in the prompt
    assert "X" * 1000 not in prompt
    # File was written
    files = list((tmp_path / "tool_results").iterdir())
    assert len(files) == 1
    monkeypatch.delenv("KORPHA_PERSIST_THRESHOLD_CHARS", raising=False)
    monkeypatch.delenv("KORPHA_PREVIEW_CHARS", raising=False)
    importlib.reload(output_budget)


def test_skill_synth_prompt_passes_small_payload_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    from korpha.cofounder.ceo import _skill_synth_prompt
    from korpha.skills.types import SkillResult

    skill = SkillResult(
        skill_name="niche.find_micro_niches",
        summary="found 3 niches",
        payload={"niches": [{"name": "n1"}, {"name": "n2"}]},
    )
    prompt = _skill_synth_prompt("help me find a niche", skill)
    # No spillover for small payloads
    assert PERSISTED_OUTPUT_TAG not in prompt
    # The full payload is present
    assert "n1" in prompt
    assert "n2" in prompt
