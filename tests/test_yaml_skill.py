"""Tests for YAML-driven skill manifests."""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from sqlmodel import Session

from korpha.audit.model import InferenceTier
from korpha.business.model import Business
from korpha.identity.model import Founder
from korpha.inference import (
    InferencePool,
    MockProvider,
    ProviderAccount,
    TierPricing,
)
from korpha.inference.cost_tracker import CostTracker
from korpha.inference.registry import AuthType
from korpha.skills import (
    SkillContext,
    YamlSkillError,
    discover_yaml_skills,
    load_user_yaml_skills,
    load_yaml_skill,
)
from korpha.skills.types import SkillError


def _account() -> ProviderAccount:
    return ProviderAccount(
        provider_name="mock",
        auth_type=AuthType.API_KEY,
        tier_models={
            InferenceTier.WORKHORSE: "mock-flash",
            InferenceTier.PRO: "mock-pro",
        },
        pricing={
            InferenceTier.WORKHORSE: TierPricing(
                input_per_1m_usd=Decimal("0.10"),
                output_per_1m_usd=Decimal("0.20"),
            ),
            InferenceTier.PRO: TierPricing(
                input_per_1m_usd=Decimal("0.50"),
                output_per_1m_usd=Decimal("1.00"),
            ),
        },
        api_key="sk-test",
        label="primary",
    )


def _ctx(
    session: Session, business: Business, founder: Founder, response: str
) -> SkillContext:
    provider = MockProvider(static_response=response)
    pool = InferencePool(providers=[provider], accounts=[_account()])
    tracker = CostTracker(pool=pool)
    return SkillContext(
        business=business,
        founder=founder,
        session=session,
        cost_tracker=tracker,
    )


def _write_manifest(dir_path: Path, body: str) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / "manifest.yaml").write_text(body, encoding="utf-8")
    return dir_path


def test_load_minimal_manifest(tmp_path: Path) -> None:
    skill_dir = _write_manifest(
        tmp_path / "demo",
        """
name: demo.echo
description: echoes the input back as JSON
default_tier: workhorse
parameters:
  message:
    description: text to echo
    default: hello
user_prompt_template: |
  Echo {message}
output:
  format: json
  required_keys: [echo, summary]
""",
    )
    skill = load_yaml_skill(skill_dir)
    assert skill.spec.name == "demo.echo"
    assert skill.spec.default_tier == InferenceTier.WORKHORSE
    assert skill.parameter_defaults == {"message": "hello"}


def test_missing_manifest_raises(tmp_path: Path) -> None:
    with pytest.raises(YamlSkillError):
        load_yaml_skill(tmp_path / "no-such")


def test_unknown_tier_errors(tmp_path: Path) -> None:
    skill_dir = _write_manifest(
        tmp_path / "x",
        """
name: x.y
description: d
default_tier: galactic
user_prompt_template: hi
output: {format: json, required_keys: [summary]}
""",
    )
    with pytest.raises(YamlSkillError) as exc:
        load_yaml_skill(skill_dir)
    assert "galactic" in str(exc.value)


def test_inline_and_file_prompt_conflict(tmp_path: Path) -> None:
    skill_dir = tmp_path / "z"
    skill_dir.mkdir()
    (skill_dir / "system.md").write_text("file body", encoding="utf-8")
    (skill_dir / "manifest.yaml").write_text(
        """
name: z.k
description: d
system_prompt: inline
system_prompt_file: system.md
user_prompt_template: hi
output: {format: json, required_keys: [summary]}
""",
        encoding="utf-8",
    )
    with pytest.raises(YamlSkillError):
        load_yaml_skill(skill_dir)


def test_prompt_from_file(tmp_path: Path) -> None:
    skill_dir = tmp_path / "f"
    skill_dir.mkdir()
    (skill_dir / "user.md").write_text(
        "User template referencing {param}", encoding="utf-8"
    )
    (skill_dir / "manifest.yaml").write_text(
        """
name: f.x
description: d
parameters:
  param: {description: thing, default: v}
user_prompt_template_file: user.md
output: {format: json, required_keys: [summary]}
""",
        encoding="utf-8",
    )
    skill = load_yaml_skill(skill_dir)
    assert "{param}" in skill.user_prompt_template


@pytest.mark.asyncio
async def test_run_returns_parsed_json(
    tmp_path: Path, session: Session, business: Business, founder: Founder
) -> None:
    skill_dir = _write_manifest(
        tmp_path / "echo",
        """
name: demo.echo
description: returns JSON
parameters:
  word: {description: word, default: hi}
user_prompt_template: 'Output JSON for: {word}'
output:
  format: json
  required_keys: [echo, summary]
""",
    )
    skill = load_yaml_skill(skill_dir)
    ctx = _ctx(
        session,
        business,
        founder,
        '{"echo": "hi", "summary": "echoed hi"}',
    )
    result = await skill.run(ctx=ctx, args={"word": "hi"})
    assert result.skill_name == "demo.echo"
    assert result.summary == "echoed hi"
    assert result.payload["echo"] == "hi"


@pytest.mark.asyncio
async def test_text_output_returns_raw_content(
    tmp_path: Path, session: Session, business: Business, founder: Founder
) -> None:
    skill_dir = _write_manifest(
        tmp_path / "txt",
        """
name: demo.text
description: text output
user_prompt_template: 'Write a haiku.'
output:
  format: text
""",
    )
    skill = load_yaml_skill(skill_dir)
    ctx = _ctx(session, business, founder, "Cherry blossoms fall\nSilent stream below the bridge\nSpring is everywhere")
    result = await skill.run(ctx=ctx, args={})
    assert result.payload["text"].startswith("Cherry blossoms")
    assert result.summary.startswith("Cherry blossoms")


@pytest.mark.asyncio
async def test_missing_required_key_raises(
    tmp_path: Path, session: Session, business: Business, founder: Founder
) -> None:
    skill_dir = _write_manifest(
        tmp_path / "r",
        """
name: r.s
description: d
user_prompt_template: hi
output:
  format: json
  required_keys: [must_be_present, summary]
""",
    )
    skill = load_yaml_skill(skill_dir)
    ctx = _ctx(session, business, founder, '{"summary": "ok"}')
    with pytest.raises(SkillError) as exc:
        await skill.run(ctx=ctx, args={})
    assert "must_be_present" in str(exc.value)


@pytest.mark.asyncio
async def test_unparseable_json_raises(
    tmp_path: Path, session: Session, business: Business, founder: Founder
) -> None:
    skill_dir = _write_manifest(
        tmp_path / "bad",
        """
name: bad.json
description: d
user_prompt_template: hi
output:
  format: json
  required_keys: [summary]
""",
    )
    skill = load_yaml_skill(skill_dir)
    ctx = _ctx(session, business, founder, "not json at all")
    with pytest.raises(SkillError):
        await skill.run(ctx=ctx, args={})


@pytest.mark.asyncio
async def test_unknown_template_var_raises(
    tmp_path: Path, session: Session, business: Business, founder: Founder
) -> None:
    skill_dir = _write_manifest(
        tmp_path / "u",
        """
name: u.x
description: d
user_prompt_template: 'Hello {nonexistent_param}'
output: {format: json, required_keys: [summary]}
""",
    )
    skill = load_yaml_skill(skill_dir)
    ctx = _ctx(session, business, founder, '{"summary": "ok"}')
    with pytest.raises(YamlSkillError):
        await skill.run(ctx=ctx, args={})


def test_discover_skips_dirs_without_manifest(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "manifest.yaml").write_text(
        """
name: real.skill
description: d
user_prompt_template: hi
output: {format: text}
""",
        encoding="utf-8",
    )
    (tmp_path / "not-a-skill").mkdir()  # no manifest
    skills = discover_yaml_skills(tmp_path)
    assert len(skills) == 1
    assert skills[0].spec.name == "real.skill"


def test_discover_returns_empty_for_missing_root(tmp_path: Path) -> None:
    assert discover_yaml_skills(tmp_path / "missing") == []


def test_load_user_yaml_skills_does_not_overwrite_builtins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    (skill_dir / "manifest.yaml").write_text(
        """
name: niche.find_micro_niches
description: tries to clobber builtin
user_prompt_template: hi
output: {format: json, required_keys: [summary]}
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("KORPHA_SKILLS_DIR", str(tmp_path))
    added = load_user_yaml_skills()
    # Built-in already registered → user skill is rejected silently.
    assert added == []


def test_example_manifest_in_repo_loads() -> None:
    """The shipped example must always parse cleanly."""
    here = Path(__file__).resolve().parent.parent
    example_dir = here / "examples" / "yaml_skills" / "example_micro_niches"
    skill = load_yaml_skill(example_dir)
    assert skill.spec.name == "example.find_micro_niches"
    assert skill.spec.default_tier == InferenceTier.PRO
