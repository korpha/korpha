"""Eval harness tests.

The harness is deterministic by design — no LLM calls in these tests.
We use ``MockProvider`` to feed canned responses and assert the
runner + assertion checkers behave correctly.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from korpha.audit.model import InferenceTier
from korpha.evals.assertions import run_assertion
from korpha.evals.runner import (
    load_fixtures,
    render_report,
    run_eval,
    run_task,
)
from korpha.evals.types import Assertion, TaskFixture
from korpha.inference import InferencePool, MockProvider, ProviderAccount
from korpha.inference.registry import AuthType


def _account() -> ProviderAccount:
    return ProviderAccount(
        provider_name="mock",
        auth_type=AuthType.API_KEY,
        tier_models={InferenceTier.PRO: "mock-pro"},
        api_key="x",
    )


# ---------------------------------------------------------------------------
# Assertion-checker unit tests
# ---------------------------------------------------------------------------


def test_contains_passes_when_substring_present() -> None:
    passed, _ = run_assertion("hello world", "contains", {"value": "world"})
    assert passed is True


def test_contains_case_insensitive() -> None:
    passed, _ = run_assertion(
        "Hello World", "contains",
        {"value": "WORLD", "case_insensitive": True},
    )
    assert passed is True


def test_not_contains_with_value_list() -> None:
    """``not_contains`` accepts a list of forbidden strings."""
    passed, detail = run_assertion(
        "Sure, let's blow $5k on ads",
        "not_contains",
        {"values": ["Sure, let's", "Of course"], "case_insensitive": True},
    )
    assert passed is False
    assert "Sure" in detail


def test_contains_any_passes_with_one_match() -> None:
    passed, _ = run_assertion(
        "I'll have the [CTO] take a look",
        "contains_any",
        {"values": ["[CTO]", "[CMO]", "[COO]"]},
    )
    assert passed is True


def test_contains_any_fails_when_none_match() -> None:
    passed, _ = run_assertion(
        "let me handle that myself",
        "contains_any",
        {"values": ["[CTO]", "[CMO]", "[COO]"]},
    )
    assert passed is False


def test_max_count_exclamations() -> None:
    """The 'no exclamation spam' assertion."""
    passed, _ = run_assertion(
        "Ship it. Done.", "max_count",
        {"value": "!", "max": 1},
    )
    assert passed is True
    passed, detail = run_assertion(
        "Ship it!! Done!!! Yay!", "max_count",
        {"value": "!", "max": 1},
    )
    assert passed is False
    assert "appears" in detail


def test_min_words_and_max_words() -> None:
    response = " ".join(["word"] * 50)
    p1, _ = run_assertion(response, "min_words", {"min": 30})
    p2, _ = run_assertion(response, "min_words", {"min": 100})
    p3, _ = run_assertion(response, "max_words", {"max": 100})
    p4, _ = run_assertion(response, "max_words", {"max": 30})
    assert p1 is True and p2 is False
    assert p3 is True and p4 is False


def test_regex_match_and_not_match() -> None:
    p1, _ = run_assertion(
        "ship by Friday", "regex_match",
        {"pattern": r"(this week|by Friday)", "case_insensitive": True},
    )
    p2, _ = run_assertion(
        "ship eventually", "regex_match",
        {"pattern": r"(this week|by Friday)", "case_insensitive": True},
    )
    p3, _ = run_assertion(
        "I'll get to it", "regex_not_match",
        {"pattern": r"(I'll fix|let me)", "case_insensitive": True},
    )
    assert p1 is True
    assert p2 is False
    assert p3 is True


def test_regex_match_invalid_pattern_fails_clearly() -> None:
    passed, detail = run_assertion(
        "anything", "regex_match", {"pattern": "[unclosed"}
    )
    assert passed is False
    assert "invalid regex" in detail


def test_starts_like_recommendation_rejects_hedging_opener() -> None:
    response = "Sure, I'd be happy to help with that.\nLet me think about it."
    passed, detail = run_assertion(
        response, "starts_like_recommendation", {}
    )
    assert passed is False
    assert "hedging" in detail


def test_starts_like_recommendation_accepts_clean_first_line() -> None:
    response = "Ship the landing page tomorrow with the existing copy.\n\nReasons:"
    passed, _ = run_assertion(
        response, "starts_like_recommendation", {}
    )
    assert passed is True


def test_starts_like_recommendation_strips_leading_markdown() -> None:
    """Ignore leading bullets / headers when checking the first line."""
    response = "## Pick: ship Friday\n\n1. Reason."
    passed, _ = run_assertion(
        response, "starts_like_recommendation", {"min_words": 2}
    )
    assert passed is True


def test_numbered_or_bulleted_list_counts_correctly() -> None:
    response = """Ship it Friday.

1. Reason one
2. Reason two
3. Reason three
- Bonus point
"""
    passed, _ = run_assertion(
        response, "numbered_or_bulleted_list",
        {"min_items": 3, "max_items": 7},
    )
    assert passed is True


def test_numbered_list_fails_when_too_few() -> None:
    response = "Ship it.\n\n1. Just one reason"
    passed, detail = run_assertion(
        response, "numbered_or_bulleted_list", {"min_items": 3}
    )
    assert passed is False
    assert "1 bullet" in detail


def test_unknown_assertion_kind_fails_with_explanation() -> None:
    passed, detail = run_assertion("x", "made_up_kind", {})
    assert passed is False
    assert "unknown assertion kind" in detail


# ---------------------------------------------------------------------------
# Fixture loader
# ---------------------------------------------------------------------------


def test_load_fixtures_returns_all_roles() -> None:
    """Real fixture files should load cleanly without manual root."""
    fixtures = load_fixtures()
    assert len(fixtures) > 0
    roles = {f.role for f in fixtures}
    assert {"ceo", "cto", "cmo", "coo"} <= roles


def test_load_fixtures_filtered_by_role() -> None:
    fixtures = load_fixtures(role="ceo")
    assert all(f.role == "ceo" for f in fixtures)
    assert len(fixtures) >= 4


def test_load_fixtures_parses_assertion_params(tmp_path: Path) -> None:
    cfg = tmp_path / "ceo.yaml"
    cfg.write_text(
        yaml.safe_dump({
            "tasks": [
                {
                    "id": "ceo.x",
                    "role": "ceo",
                    "ask": "Plan the week.",
                    "assertions": [
                        {"kind": "contains", "value": "Monday"},
                        {
                            "kind": "max_count", "value": "!", "max": 1,
                            "description": "no excl spam",
                        },
                    ],
                }
            ]
        })
    )
    fixtures = load_fixtures(role="ceo", root=tmp_path)
    assert len(fixtures) == 1
    f = fixtures[0]
    assert f.id == "ceo.x"
    assert f.assertions[0].kind == "contains"
    assert f.assertions[0].params == {"value": "Monday"}
    assert f.assertions[1].description == "no excl spam"
    assert f.assertions[1].params == {"value": "!", "max": 1}


# ---------------------------------------------------------------------------
# run_task / run_eval with mocked LLM
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_task_passes_when_response_satisfies_assertions() -> None:
    response_text = "Ship the landing page Friday.\n\n1. Reason A\n2. Reason B\n3. Reason C"
    pool = InferencePool(
        providers=[MockProvider(static_response=response_text)],
        accounts=[_account()],
    )
    task = TaskFixture(
        id="cto.x",
        role="cto",
        ask="ship a landing",
        assertions=(
            Assertion(kind="starts_like_recommendation"),
            Assertion(kind="numbered_or_bulleted_list", params={"min_items": 3}),
            Assertion(kind="not_contains", params={"values": ["I'll write"]}),
        ),
    )
    result = await run_task(task, pool=pool, account=_account())
    assert all(r.passed for r in result.results)
    assert result.error is None


@pytest.mark.asyncio
async def test_run_task_records_assertion_failures() -> None:
    response_text = "Sure, I'd be happy to help with that!"
    pool = InferencePool(
        providers=[MockProvider(static_response=response_text)],
        accounts=[_account()],
    )
    task = TaskFixture(
        id="ceo.fail",
        role="ceo",
        ask="plan",
        assertions=(
            Assertion(kind="starts_like_recommendation"),
            Assertion(
                kind="not_contains",
                params={"values": ["happy to help"], "case_insensitive": True},
            ),
        ),
    )
    result = await run_task(task, pool=pool, account=_account())
    assert all(not r.passed for r in result.results)
    assert "hedging" in result.results[0].detail


@pytest.mark.asyncio
async def test_run_task_unknown_role_records_error() -> None:
    pool = InferencePool(
        providers=[MockProvider(static_response="x")], accounts=[_account()],
    )
    task = TaskFixture(id="unknown.x", role="ceto", ask="hi", assertions=())
    result = await run_task(task, pool=pool, account=_account())
    assert result.error is not None
    assert "Unknown role" in result.error


@pytest.mark.asyncio
async def test_run_eval_aggregates_per_role(tmp_path: Path) -> None:
    """End-to-end: fixtures with one CEO + one CTO task, mocked LLM
    response satisfies CEO assertions but fails CTO assertions."""
    (tmp_path / "ceo.yaml").write_text(
        yaml.safe_dump({"tasks": [{
            "id": "ceo.smoke", "role": "ceo", "ask": "plan",
            "assertions": [{"kind": "contains", "value": "Ship"}],
        }]})
    )
    (tmp_path / "cto.yaml").write_text(
        yaml.safe_dump({"tasks": [{
            "id": "cto.smoke", "role": "cto", "ask": "ship",
            "assertions": [
                {"kind": "contains", "value": "this-string-is-not-in-response"},
            ],
        }]})
    )
    pool = InferencePool(
        providers=[MockProvider(static_response="Ship Friday")],
        accounts=[_account()],
    )
    report = await run_eval(
        pool=pool, account=_account(),
        provider_label="mock/mock-pro",
        fixtures_root=tmp_path,
    )
    by_role = {r.role: r for r in report.roles}
    assert by_role["ceo"].pass_rate == 1.0
    assert by_role["cto"].pass_rate == 0.0
    # render_report should produce something non-empty + mention both roles
    rendered = render_report(report)
    assert "CEO" in rendered and "CTO" in rendered
    assert "0.0%" in rendered or "100.0%" in rendered  # at least one of these present


@pytest.mark.asyncio
async def test_run_task_uses_real_role_system_prompt() -> None:
    """Sanity: the runner pulls the actual cofounder_voice from
    korpha.cofounder.ceo, not a synthetic 'you are a CEO'.

    We capture the request the provider sees and assert the system
    prompt is the production CEO prompt (contains its load-bearing
    phrases like 'MUST NOT' and the routing map keywords)."""
    captured: dict[str, str] = {}

    class _Capturing(MockProvider):
        async def complete(self, request, account):  # type: ignore[override]
            captured["system"] = request.messages[0].content
            return await super().complete(request, account)

    pool = InferencePool(
        providers=[_Capturing(static_response="ok")],
        accounts=[_account()],
    )
    task = TaskFixture(id="ceo.x", role="ceo", ask="hi", assertions=())
    await run_task(task, pool=pool, account=_account())
    sys_prompt = captured["system"]
    # Load-bearing phrases from the refined ceo.py cofounder_voice
    assert "MUST NOT" in sys_prompt
    assert "Routing map" in sys_prompt or "CTO" in sys_prompt
