"""``knowledge.*`` skills — agents fetch SKILL.md packs on demand.

The Director prompt only carries a compact directory of available
packs (see :mod:`korpha.cofounder.knowledge_inject`). When the agent
actually needs the playbook content for one of them (e.g. to call the
Notion API correctly), it invokes ``knowledge.get_pack`` and the full
content lands in the next turn's context.
"""
from __future__ import annotations

import logging
from typing import Any

from korpha.skills.registry import register
from korpha.skills.types import (
    Skill,
    SkillContext,
    SkillError,
    SkillProvenance,
    SkillResult,
    SkillSpec,
)

logger = logging.getLogger(__name__)


class KnowledgeGetPackSkill(Skill):
    """Return the full content of a knowledge pack by slug."""

    spec = SkillSpec(
        name="knowledge.get_pack",
        description=(
            "Fetch the full SKILL.md content of a knowledge pack by "
            "slug (e.g. 'productivity/notion'). Call this when you "
            "need detailed playbook info to operate a third-party "
            "tool — the directory in your prompt lists what's available."
        ),
        parameters={
            "slug": (
                "Pack slug in '<category>/<name>' form. Example: "
                "'productivity/notion'."
            ),
        },
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        from korpha.knowledge_packs import get_pack

        slug = str(args.get("slug") or "").strip()
        if not slug:
            raise SkillError("knowledge.get_pack: slug required")
        pack = get_pack(slug)
        if pack is None:
            raise SkillError(
                f"knowledge.get_pack: no pack at {slug!r}. Call "
                "knowledge.list to see what's available."
            )
        return SkillResult(
            skill_name="knowledge.get_pack",
            summary=(
                f"Fetched {pack.slug} ({pack.char_length} chars)"
            ),
            payload={
                "slug": pack.slug,
                "category": pack.category,
                "name": pack.name,
                "content": pack.content,
            },
        )


class KnowledgeListSkill(Skill):
    """List all knowledge packs, optionally filtered by category."""

    spec = SkillSpec(
        name="knowledge.list",
        description=(
            "List available knowledge packs. Optional 'category' arg "
            "filters to one category (productivity / developer / "
            "creative / communication and their underlying source "
            "categories like github / devops / mlops). Returns each "
            "pack's slug + first content line."
        ),
        parameters={
            "category": (
                "Optional. Filter to one category. Leave blank for "
                "everything."
            ),
        },
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        from korpha.knowledge_packs import available_packs

        cat = str(args.get("category") or "").strip().lower()
        packs = available_packs()
        if cat:
            packs = [p for p in packs if p.category.lower() == cat]
        rows = []
        for p in packs:
            first_line = next(
                (ln.strip().lstrip("#").strip()
                 for ln in p.content.splitlines()
                 if ln.strip() and not ln.strip().startswith("---")),
                "",
            )[:120]
            rows.append({
                "slug": p.slug,
                "category": p.category,
                "name": p.name,
                "char_length": p.char_length,
                "first_line": first_line,
            })
        return SkillResult(
            skill_name="knowledge.list",
            summary=(
                f"{len(rows)} knowledge pack(s)"
                + (f" in {cat!r}" if cat else "")
            ),
            payload={"packs": rows},
        )


def register_skills() -> None:
    register(KnowledgeGetPackSkill())
    register(KnowledgeListSkill())


__all__ = [
    "KnowledgeGetPackSkill",
    "KnowledgeListSkill",
    "register_skills",
]
