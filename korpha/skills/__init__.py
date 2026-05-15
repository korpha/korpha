"""Skills — reusable, structured capabilities that agents can invoke.

A Skill is a named, parameterized procedure with a known input/output shape.
Examples:
- ``niche.find_micro_niches`` — pick promising micro-niches given Founder
  skills, time budget, and savings.
- ``outreach.draft_cold_emails`` — produce N personalized opener drafts.
- ``landing.draft_copy`` — headline + subhead + CTA for a value prop.

Skills are first-class in Korpha because they:
1. Are the BRIEF.md "Start" jobs (niche pick → validate → launch).
2. Compose: a CEO can call ``niche.find_micro_niches`` and then feed the
   winning candidate into ``landing.draft_copy``.
3. Become marketplace inventory later — community contributors publish
   their proven playbooks as installable Skills.

This module exposes the Skill base class, the SkillRegistry, and the
SkillContext that runners use to access inference / DB / business state.
"""
from __future__ import annotations

import os
from pathlib import Path

from korpha.skills.registry import (
    SkillRegistry,
    default_registry,
    register,
)
from korpha.skills.types import (
    Skill,
    SkillContext,
    SkillError,
    SkillNotFound,
    SkillProvenance,
    SkillResult,
    SkillSpec,
)
from korpha.skills.yaml_skill import (
    YamlSkill,
    YamlSkillError,
    discover_yaml_skills,
    load_yaml_skill,
)

__all__ = [
    "Skill",
    "SkillContext",
    "SkillError",
    "SkillNotFound",
    "SkillProvenance",
    "SkillRegistry",
    "SkillResult",
    "SkillSpec",
    "YamlSkill",
    "YamlSkillError",
    "default_registry",
    "discover_yaml_skills",
    "load_agent_created_python_skills",
    "load_user_yaml_skills",
    "load_yaml_skill",
    "register",
]


def _autoload_builtins() -> None:
    """Eagerly import built-in skill modules so they self-register."""
    from korpha.skills import (  # noqa: F401  -- side-effect: register
        analytics,
        bootstrap,
        business_units,
        calendar,
        channel,
        code_deploy,
        commerce,
        cooperation,
        creative,
        cron_author,
        deploy,
    )
    # AI mesh skills (image.* / audio.*) live under shared_resources
    # but self-register via decorator; importing pulls them in.
    from korpha.shared_resources import (  # noqa: F401
        ai_mesh_skills,
    )
    from korpha.skills import (  # noqa: F401
        finance,
        founder,
        geo_seo,
        growth,
        hr,
        imagery,
        kanban_skills,
        landing,
        marketing,
        memory,
        memory_notes,
        meta,
        niche,
        outreach,
        outreach_send,
        pricing,
        product,
        consultant,
        research,
        support,
        validate,
        web_search,
    )
    consultant.register_skills()
    web_search.register_skills()


def load_user_yaml_skills(
    root: os.PathLike[str] | str | None = None,
) -> list[YamlSkill]:
    """Load YAML skills from ``~/.korpha/skills/`` (or ``root`` override)
    and add them to the default registry. Skills already registered with
    the same name are skipped — built-in Python skills win on conflict so
    a third-party contributor can't accidentally clobber the canonical
    implementation.

    Returns the list of newly-loaded YAML skills.
    """
    if root is None:
        env = os.getenv("KORPHA_SKILLS_DIR")
        path = Path(env) if env else Path.home() / ".korpha" / "skills"
    else:
        path = Path(root)
    skills = discover_yaml_skills(path)
    added: list[YamlSkill] = []
    for s in skills:
        if s.spec.name in default_registry.skills:
            continue
        default_registry.add(s)
        added.append(s)
    return added


def load_agent_created_python_skills(
    root: os.PathLike[str] | str | None = None,
) -> list[str]:
    """Import Python skills authored by ``meta.author_python_skill``.

    Each previously-approved authoring writes a ``skill.py`` to:
        ``~/.korpha/skills/agent_created/python/<name>/skill.py``

    The file's bottom-of-module ``register(...)`` call adds the skill
    to ``default_registry`` as a side effect of import. We use a stable
    synthetic module name per directory so re-authoring with the same
    name overrides cleanly without polluting ``sys.modules``.

    Returns the list of module names imported. Errors on individual
    files are logged + skipped — one bad skill must not stop the
    others.
    """
    import importlib.util
    import logging
    import sys

    log = logging.getLogger(__name__)

    if root is None:
        env = os.getenv("KORPHA_SKILLS_DIR")
        base = (
            Path(env) if env
            else Path.home() / ".korpha" / "skills"
        )
    else:
        base = Path(root)
    py_root = base / "agent_created" / "python"
    if not py_root.exists():
        return []

    loaded: list[str] = []
    for skill_dir in sorted(py_root.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_path = skill_dir / "skill.py"
        if not skill_path.is_file():
            continue
        # Stable module name per directory — survives re-authoring.
        module_name = (
            f"_korpha_agent_skill_{skill_dir.name.replace('-', '_')}"
        )
        try:
            spec = importlib.util.spec_from_file_location(
                module_name, skill_path
            )
            if spec is None or spec.loader is None:
                log.warning(
                    "could not build import spec for %s", skill_path
                )
                continue
            if module_name in sys.modules:
                # Re-import path: drop the stale module so the new
                # source executes its register() again.
                del sys.modules[module_name]
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            loaded.append(module_name)
        except Exception as exc:
            log.warning(
                "failed to import agent-created Python skill %s: %s",
                skill_path, exc,
            )
    return loaded


_autoload_builtins()


# Install the post_skill_call usage tracker so the curator has data
# to work with. Idempotent — safe to call multiple times.
def _install_usage_tracking() -> None:
    try:
        from korpha.skills.curator import install_usage_hook
        install_usage_hook()
    except Exception:  # noqa: BLE001
        # Curator install failing should never block skill registration.
        # Log + move on so a buggy curator doesn't break the agent.
        import logging as _log
        _log.getLogger(__name__).debug(
            "curator usage hook install failed", exc_info=True,
        )


_install_usage_tracking()
