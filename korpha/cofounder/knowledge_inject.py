"""Inject knowledge-pack directory into Director / Worker prompts.

The full SKILL.md packs are too big to dump into every prompt (avg
~10 KB, 69 packs total = 700 KB). Instead we inject a compact
**directory** listing the packs relevant to this role's capability,
and expose a ``knowledge.get_pack`` skill the agent can call to
fetch the full content of any pack on demand.

Cheap-prompt directory + deep-dive on demand is the same pattern
Hermes uses for its own SKILL.md set — it scales as the pack count
grows without exploding context size.
"""
from __future__ import annotations

from collections.abc import Iterable


# Map role_type / specialty → capability tags that activate which
# pack categories. A CMO gets communication + productivity packs;
# a CTO gets the developer set; CEO gets everything because they
# plan across the whole business.
_ROLE_CAPABILITIES: dict[str, tuple[str, ...]] = {
    # CEO plans across the whole company — gets every category PLUS the
    # agent_design meta-packs (Hermes's autonomous-ai-agents playbook
    # on delegation, attempt structuring, when to escalate).
    "ceo": (
        "productivity", "developer", "creative", "communication",
        "agent_design",
    ),
    "cto": ("developer", "productivity"),
    "cmo": ("creative", "communication", "productivity"),
    "coo": ("productivity", "communication"),
    # Line VPs delegate to specialists + sequence multi-step Line plans,
    # so they benefit from agent_design too (smaller-scope planning).
    "vp": (
        "productivity", "creative", "communication", "agent_design",
    ),
    # Workers execute, don't plan — productivity packs are enough.
    # Specialty overrides handled by _SPECIALTY_CAPABILITIES below.
    "worker": ("productivity",),
}

_SPECIALTY_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "designer": ("creative",),
    "copywriter": ("creative", "communication"),
    "support": ("communication",),
    "developer": ("developer",),
    "researcher": ("productivity", "developer"),
}


def capabilities_for_role(
    *,
    role_type: str | None,
    specialty: str | None = None,
) -> tuple[str, ...]:
    """Resolve the capability tags for a role. ``specialty`` overrides
    when present (a 'designer' worker gets creative packs even though
    workers default to productivity)."""
    if specialty:
        spec = (specialty or "").strip().lower()
        if spec in _SPECIALTY_CAPABILITIES:
            return _SPECIALTY_CAPABILITIES[spec]
    rt = (role_type or "").strip().lower()
    return _ROLE_CAPABILITIES.get(rt, ("productivity",))


def build_knowledge_directory_block(
    *,
    role_type: str | None,
    specialty: str | None = None,
    extra_slugs: Iterable[str] = (),
    max_packs: int = 30,
) -> str:
    """Render a compact directory of available knowledge packs.

    Returns ``""`` when no packs are loaded or the role's capability
    set is empty (don't pad the prompt with empty headers)."""
    try:
        from korpha.knowledge_packs import select_packs_for_capability
    except Exception:  # noqa: BLE001
        return ""
    capabilities = capabilities_for_role(
        role_type=role_type, specialty=specialty,
    )
    if not capabilities:
        return ""
    packs = select_packs_for_capability(
        capabilities, extra_slugs=extra_slugs,
    )
    if not packs:
        return ""
    # Trim to max — packs are sorted by slug, so this drops the tail
    # of less-relevant categories first.
    trimmed = packs[:max_packs]

    lines: list[str] = [
        "<knowledge_packs_available>",
        (
            "Tool / domain playbooks you can pull on demand. To read the "
            "full content of any pack, call the ``knowledge.get_pack`` "
            "skill with the slug. Don't dump these into your output — "
            "they're for YOU to reason from when you need tool details."
        ),
        "",
    ]
    for p in trimmed:
        lines.append(f"- {p.slug} — {_first_content_line(p.content)}")
    if len(packs) > max_packs:
        lines.append(
            f"... +{len(packs) - max_packs} more packs available; "
            f"call knowledge.list to see them all."
        )
    lines.append("</knowledge_packs_available>")
    return "\n".join(lines)


def _first_content_line(content: str) -> str:
    """Pull a useful 1-line summary out of a SKILL.md, skipping YAML
    front-matter and markdown headings. Hermes packs have the shape:

        ---
        name: notion
        description: "Notion API via curl: pages, databases, blocks..."
        ...
        ---
        # Notion API
        Use the Notion API via curl to...

    The most useful summary line is the ``description:`` front-matter
    value; falling back to the first real paragraph if that's missing.
    """
    in_frontmatter = False
    fm_done = False
    description_from_fm = ""
    first_body_para = ""
    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped == "---":
            if not in_frontmatter and not fm_done:
                in_frontmatter = True
                continue
            if in_frontmatter:
                in_frontmatter = False
                fm_done = True
                continue
        if in_frontmatter:
            if stripped.lower().startswith("description:"):
                description_from_fm = stripped.split(":", 1)[1].strip()
                description_from_fm = description_from_fm.strip('"\'')
            continue
        # Body — first non-heading line is usually the gist.
        if stripped.startswith("#"):
            continue
        first_body_para = stripped
        break
    pick = description_from_fm or first_body_para
    return pick[:140]


__all__ = [
    "build_knowledge_directory_block",
    "capabilities_for_role",
]
