"""Skills system tests."""
from __future__ import annotations

import pytest
from sqlmodel import Session

from korpha.audit.model import InferenceTier
from korpha.business.model import Business
from korpha.identity.model import Founder
from korpha.inference import (
    InferencePool,
    MockProvider,
    ProviderAccount,
)
from korpha.inference.cost_tracker import CostTracker
from korpha.inference.registry import AuthType
from korpha.skills import (
    SkillContext,
    SkillError,
    SkillNotFound,
    SkillRegistry,
    default_registry,
)
from korpha.skills.niche import FindMicroNichesSkill


def _account() -> ProviderAccount:
    return ProviderAccount(
        provider_name="mock",
        auth_type=AuthType.API_KEY,
        tier_models={
            InferenceTier.WORKHORSE: "mock-flash",
            InferenceTier.PRO: "mock-pro",
        },
        api_key="x",
    )


def _make_ctx(
    session: Session, business: Business, founder: Founder, *, response: str
) -> SkillContext:
    pool = InferencePool(
        providers=[MockProvider(static_response=response)], accounts=[_account()]
    )
    tracker = CostTracker(pool=pool)
    return SkillContext(
        business=business,
        founder=founder,
        session=session,
        cost_tracker=tracker,
    )


def test_default_registry_has_niche_skill() -> None:
    skill = default_registry.get("niche.find_micro_niches")
    assert skill.spec.name == "niche.find_micro_niches"
    assert "skills" in skill.spec.parameters


def test_registry_unknown_skill_raises() -> None:
    with pytest.raises(SkillNotFound):
        default_registry.get("nonexistent.skill")


def test_registry_duplicate_add_raises() -> None:
    reg = SkillRegistry()
    reg.add(FindMicroNichesSkill())
    with pytest.raises(ValueError):
        reg.add(FindMicroNichesSkill())


def test_registry_list_specs() -> None:
    specs = default_registry.list_specs()
    names = {s.name for s in specs}
    assert "niche.find_micro_niches" in names


@pytest.mark.asyncio
async def test_niche_skill_parses_candidates(
    session: Session, business: Business, founder: Founder
) -> None:
    response = (
        '{"candidates":['
        '{"name":"Deployment automation for solo Python devs",'
        '"target_avatar":"indie hackers shipping side SaaS",'
        '"value_prop":"removes 5h/wk of devops",'
        '"price_band":"$29-99/mo","competition":"render/fly cover ops",'
        '"validation_experiment":"5 interviews + 1-page Carrd landing",'
        '"fit_score":9},'
        '{"name":"Cron monitoring for solo Pythonistas","target_avatar":"x",'
        '"value_prop":"y","price_band":"$15-49/mo","competition":"z",'
        '"validation_experiment":"q","fit_score":6}'
        '],"recommended_index":0,"rationale":"highest fit + biggest pain"}'
    )
    ctx = _make_ctx(session, business, founder, response=response)
    result = await default_registry.run(
        "niche.find_micro_niches",
        ctx=ctx,
        args={
            "skills": "Python, FastAPI, Docker",
            "time_budget_hours": 5,
            "savings_usd": 2000,
            "goal": "$5k MRR in 6 months",
        },
    )
    assert "Deployment automation" in result.summary
    assert len(result.payload["candidates"]) == 2
    assert result.payload["recommended_index"] == 0
    assert "highest fit" in result.payload["rationale"]


@pytest.mark.asyncio
async def test_niche_skill_raises_on_unparseable(
    session: Session, business: Business, founder: Founder
) -> None:
    ctx = _make_ctx(session, business, founder, response="not json at all")
    with pytest.raises(SkillError):
        await default_registry.run(
            "niche.find_micro_niches",
            ctx=ctx,
            args={"skills": "x", "time_budget_hours": 5, "savings_usd": 1000},
        )


@pytest.mark.asyncio
async def test_niche_skill_clamps_recommended_index(
    session: Session, business: Business, founder: Founder
) -> None:
    response = (
        '{"candidates":[{"name":"A","target_avatar":"x","value_prop":"y",'
        '"price_band":"$","competition":"z","validation_experiment":"q",'
        '"fit_score":7}],"recommended_index":99,"rationale":"r"}'
    )
    ctx = _make_ctx(session, business, founder, response=response)
    result = await default_registry.run(
        "niche.find_micro_niches",
        ctx=ctx,
        args={"skills": "x", "time_budget_hours": 5, "savings_usd": 1000},
    )
    assert result.payload["recommended_index"] == 0


@pytest.mark.asyncio
async def test_niche_skill_handles_markdown_fences(
    session: Session, business: Business, founder: Founder
) -> None:
    response = (
        "```json\n"
        '{"candidates":[{"name":"X","target_avatar":"a","value_prop":"b",'
        '"price_band":"c","competition":"d","validation_experiment":"e",'
        '"fit_score":5}],"recommended_index":0,"rationale":"r"}\n'
        "```"
    )
    ctx = _make_ctx(session, business, founder, response=response)
    result = await default_registry.run(
        "niche.find_micro_niches",
        ctx=ctx,
        args={"skills": "x", "time_budget_hours": 5, "savings_usd": 1000},
    )
    assert result.payload["candidates"][0]["name"] == "X"


# --- founder.intake_brief ----------------------------------------------------

_INTAKE_RESPONSE = (
    '{"goal":"$5k MRR in 6 months","timeline_months":6,'
    '"time_per_week_hours":10,"savings_usd":2000,'
    '"skills":"Python, B2B SaaS","niches_considered":["devops","cron"],'
    '"constraints":["cant quit job"],'
    '"summary":"You want $5k MRR in 6 months while keeping your day job. '
    'Next: pick a niche from your two ideas and validate one this week."}'
)


@pytest.mark.asyncio
async def test_intake_brief_persists_to_business(
    session: Session, business: Business, founder: Founder
) -> None:
    ctx = _make_ctx(session, business, founder, response=_INTAKE_RESPONSE)
    result = await default_registry.run(
        "founder.intake_brief",
        ctx=ctx,
        args={
            "answer": (
                "I'm a Python dev, want $5k/mo side income in 6 months, "
                "have ~10h/week and $2k savings. Considered devops and cron."
            )
        },
    )
    assert result.summary.startswith("Captured: $5k MRR")
    assert result.payload["timeline_months"] == 6
    assert result.payload["time_per_week_hours"] == 10
    assert "cron" in result.payload["niches_considered"]

    session.refresh(business)
    assert business.founder_brief["goal"] == "$5k MRR in 6 months"
    assert business.founder_brief["raw_answer"].startswith("I'm a Python dev")


@pytest.mark.asyncio
async def test_intake_brief_requires_answer(
    session: Session, business: Business, founder: Founder
) -> None:
    ctx = _make_ctx(session, business, founder, response=_INTAKE_RESPONSE)
    with pytest.raises(SkillError, match="requires `answer`"):
        await default_registry.run(
            "founder.intake_brief", ctx=ctx, args={"answer": ""}
        )


@pytest.mark.asyncio
async def test_intake_brief_uses_defaults_when_fields_missing(
    session: Session, business: Business, founder: Founder
) -> None:
    minimal = (
        '{"goal":"unclear","summary":"You want something. Next: '
        'try `korpha skill run niche.find_micro_niches`."}'
    )
    ctx = _make_ctx(session, business, founder, response=minimal)
    result = await default_registry.run(
        "founder.intake_brief", ctx=ctx, args={"answer": "uhh"}
    )
    assert result.payload["timeline_months"] == 6
    assert result.payload["time_per_week_hours"] == 5
    assert result.payload["savings_usd"] == 1000
    assert result.payload["niches_considered"] == []


def _capturing_ctx(
    session: Session,
    business: Business,
    founder: Founder,
    *,
    response: str,
    captured: dict[str, str],
) -> SkillContext:
    class _CapturingProvider(MockProvider):
        async def complete(self, request, account):  # type: ignore[override]
            captured["prompt"] = request.messages[-1].content
            return await super().complete(request, account)

    pool = InferencePool(
        providers=[_CapturingProvider(static_response=response)],
        accounts=[_account()],
    )
    tracker = CostTracker(pool=pool)
    return SkillContext(
        business=business, founder=founder, session=session, cost_tracker=tracker,
    )


@pytest.mark.asyncio
async def test_validate_skill_defaults_skills_and_constraints_from_brief(
    session: Session, business: Business, founder: Founder
) -> None:
    """validate.score_idea reads brief.skills + brief.constraints when
    the caller doesn't pass them explicitly."""
    business.founder_brief = {
        "skills": "Python, Django, FastAPI",
        "time_per_week_hours": 10,
        "savings_usd": 2000,
        "constraints": ["can't quit job"],
    }
    session.add(business)
    session.commit()

    captured: dict[str, str] = {}
    response = (
        '{"scores":{"demand_signal":7,"willingness_to_pay":6,'
        '"founder_fit":8,"distribution_path":5},"overall":7,'
        '"verdict":"go","strengths":["x"],"concerns":["y"],'
        '"kill_test":"z","improvement_path":""}'
    )
    ctx = _capturing_ctx(
        session, business, founder, response=response, captured=captured,
    )
    await default_registry.run(
        "validate.score_idea", ctx=ctx, args={"idea": "X", "avatar": "Y"},
    )
    prompt = captured["prompt"]
    assert "Python, Django, FastAPI" in prompt
    assert "10h/week" in prompt
    assert "$2000 cash" in prompt
    assert "can't quit job" in prompt


@pytest.mark.asyncio
async def test_product_skill_defaults_constraints_from_brief(
    session: Session, business: Business, founder: Founder
) -> None:
    business.founder_brief = {"time_per_week_hours": 15, "savings_usd": 3000}
    session.add(business)
    session.commit()

    captured: dict[str, str] = {}
    response = (
        '{"candidates":[{"name":"X","buy_trigger":"t",'
        '"smallest_shippable_unit":"u","build_hours":8,'
        '"trigger_strength":7,"why":"w"}],"recommended_index":0,'
        '"rationale":"r","do_not_build":[]}'
    )
    ctx = _capturing_ctx(
        session, business, founder, response=response, captured=captured,
    )
    await default_registry.run(
        "product.first_feature",
        ctx=ctx,
        args={"niche": "n", "audience": "a", "value_prop": "v"},
    )
    prompt = captured["prompt"]
    assert "15h/week" in prompt
    assert "$3000 cash" in prompt
    # Falls back to the hardcoded default only when brief is empty
    assert "5h/week, $500" not in prompt


@pytest.mark.asyncio
async def test_growth_skill_cadence_scales_with_time_budget(
    session: Session, business: Business, founder: Founder
) -> None:
    """5h/week → ~2 posts; 30h/week → 7 (capped). The plan should match
    the Founder's reality."""
    business.founder_brief = {"time_per_week_hours": 30}
    session.add(business)
    session.commit()

    captured: dict[str, str] = {}
    response = (
        '{"posts":[{"day":"Mon","channel":"x","theme":"y","hook":"z",'
        '"cta":"q"}],"summary":"s"}'
    )
    ctx = _capturing_ctx(
        session, business, founder, response=response, captured=captured,
    )
    await default_registry.run(
        "growth.draft_content_plan", ctx=ctx, args={"audience": "a"},
    )
    prompt = captured["prompt"]
    assert "7 posts/week" in prompt  # capped at 7

    # Now check the small-budget case
    business.founder_brief = {"time_per_week_hours": 4}
    session.add(business)
    session.commit()
    captured.clear()
    ctx = _capturing_ctx(
        session, business, founder, response=response, captured=captured,
    )
    await default_registry.run(
        "growth.draft_content_plan", ctx=ctx, args={"audience": "a"},
    )
    prompt = captured["prompt"]
    assert "2 posts/week" in prompt


@pytest.mark.asyncio
async def test_outreach_skill_defaults_bio_from_brief_skills(
    session: Session, business: Business, founder: Founder
) -> None:
    """outreach.draft_cold_emails uses brief.skills as founder_bio when
    none is supplied — beats a generic "indie developer" credibility line."""
    business.founder_brief = {"skills": "10y backend dev, ex-Stripe"}
    session.add(business)
    session.commit()

    captured: dict[str, str] = {}
    response = (
        '{"variants":[{"angle":"a","subject":"s","body":"b"}],'
        '"personalization_template":"p","follow_up_subject":"f"}'
    )
    ctx = _capturing_ctx(
        session, business, founder, response=response, captured=captured,
    )
    await default_registry.run(
        "outreach.draft_cold_emails",
        ctx=ctx,
        args={"avatar": "x", "value_prop": "y"},
    )
    prompt = captured["prompt"]
    assert "10y backend dev, ex-Stripe" in prompt
    # No fallback bleeds through
    assert "indie developer" not in prompt


@pytest.mark.asyncio
async def test_niche_skill_defaults_from_founder_brief(
    session: Session, business: Business, founder: Founder
) -> None:
    """When the niche skill is run with no args, it should pull defaults
    from the previously-captured founder_brief instead of the hardcoded
    fallbacks ("(unspecified)", 5h/week, $1000)."""
    business.founder_brief = {
        "goal": "$10k MRR in 12 months",
        "skills": "Rust, audio DSP",
        "time_per_week_hours": 15,
        "savings_usd": 5000,
    }
    session.add(business)
    session.commit()

    response = (
        '{"candidates":[{"name":"DSP plugins for indie producers",'
        '"target_avatar":"x","value_prop":"y","price_band":"$","competition":"z",'
        '"validation_experiment":"q","fit_score":8}],'
        '"recommended_index":0,"rationale":"r"}'
    )

    captured: dict[str, str] = {}

    class _CapturingProvider(MockProvider):
        async def complete(self, request, account):  # type: ignore[override]
            captured["prompt"] = request.messages[-1].content
            return await super().complete(request, account)

    pool = InferencePool(
        providers=[_CapturingProvider(static_response=response)],
        accounts=[_account()],
    )
    tracker = CostTracker(pool=pool)
    ctx = SkillContext(
        business=business,
        founder=founder,
        session=session,
        cost_tracker=tracker,
    )
    await default_registry.run("niche.find_micro_niches", ctx=ctx, args={})
    prompt = captured["prompt"]
    assert "Rust, audio DSP" in prompt
    assert "15 hours" in prompt or "Weekly time budget: 15" in prompt
    assert "5000" in prompt
    assert "$10k MRR" in prompt
