"""Helpers for surfacing BusinessUnit state into agent context.

PR-INT-4 — the CEO's system prompt builder + the onboarding chain
both call ``render_unit_summary`` to give the agent visibility into
which Lines exist. Without this, the CEO has no way to know that
``hr.start_business_line`` is a thing it should call.

Also surfaces the canonical line-kind options for the onboarding
form's optional "what kind of business is this?" picker.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlmodel import Session

from korpha.business_units.board import BusinessUnitBoard
from korpha.business_units.model import (
    BusinessUnit, BusinessUnitKind, NicheProfile,
)


CANONICAL_LINE_KINDS: list[dict[str, str]] = [
    {"value": "default", "label": "Single business (default)"},
    {"value": "pod", "label": "Print on Demand"},
    {"value": "kdp", "label": "Amazon KDP / books"},
    {"value": "info", "label": "Info products (courses / ebooks)"},
    {"value": "saas", "label": "SaaS app"},
    {"value": "affiliate", "label": "Affiliate marketing"},
    {"value": "agency", "label": "Agency / services"},
]


@dataclass(frozen=True)
class UnitContextSummary:
    """Compact summary of a unit's state for agent context."""

    unit_id: UUID
    name: str
    kind: str
    status: str
    namespace_id: UUID
    parent_id: UUID | None
    playbook_skill_pack: str | None
    has_niche_profile: bool
    owner_agent_role_id: UUID | None

    def to_dict(self) -> dict:
        return {
            "unit_id": str(self.unit_id),
            "name": self.name,
            "kind": self.kind,
            "status": self.status,
            "parent_id": str(self.parent_id) if self.parent_id else None,
            "playbook": self.playbook_skill_pack,
            "owner_agent": str(self.owner_agent_role_id) if self.owner_agent_role_id else None,
        }


def list_units_for_context(
    session: Session, business_id: UUID,
) -> list[UnitContextSummary]:
    """Compact list of all units for inclusion in agent context.

    Filters out archived units. Returned in tree order (root first,
    then BFS) so the CEO sees the org chart shape at a glance.
    """
    board = BusinessUnitBoard(session)
    all_units = board.list_for_business(
        business_id, include_archived=False,
    )
    # Sort: root (parent=None) first, then by depth approximation
    all_units.sort(
        key=lambda u: (u.parent_id is not None, u.created_at)
    )
    return [
        UnitContextSummary(
            unit_id=u.id,
            name=u.name,
            kind=u.kind.value,
            status=u.status,
            namespace_id=u.memory_namespace_id,
            parent_id=u.parent_id,
            playbook_skill_pack=u.playbook_skill_pack,
            has_niche_profile=u.niche_profile is not None,
            owner_agent_role_id=u.owner_agent_role_id,
        )
        for u in all_units
    ]


def render_unit_summary_for_prompt(
    session: Session, business_id: UUID,
) -> str:
    """Markdown table-style summary of the unit tree for the CEO's
    system prompt. Empty string if no units exist (CEO knows it can
    start one via hr.start_business_line).

    Includes unit UUIDs so the CEO can pass them to skills that take
    business_unit_id / from_unit_id / to_unit_id without an extra
    lookup turn. Without this, the LLM either hallucinates UUIDs or
    fails to call cross-unit skills entirely.
    """
    units = list_units_for_context(session, business_id)
    if not units:
        return (
            "**Business org:** No business units configured yet. "
            "Call `hr.start_business_line(kind=<pod|kdp|info|saas|"
            "affiliate|agency>)` when the founder commits to a line."
        )
    lines = [
        "**Business org tree:**",
        "",
        "| Unit | Kind | Status | UUID |",
        "|------|------|--------|------|",
    ]
    for u in units:
        indent = "  " if u.parent_id else ""
        lines.append(
            f"| {indent}{u.name} | {u.kind} | {u.status} "
            f"| `{u.unit_id}` |"
        )
    lines.append("")
    lines.append(
        "**Unit-scoped skill use:** any skill that takes "
        "`business_unit_id`, `from_unit_id`, or `to_unit_id` accepts "
        "either the UUID from the table above OR the unit name "
        "(case-insensitive) — both resolve to the same row. When the "
        "founder says \"remember this for KDP\" or \"ask POD if...\", "
        "pass the unit name; the skill handles the lookup. Skills that "
        "auto-scope on caller context (memory.remember, "
        "cooperation.ask_about) infer `business_unit_id` / "
        "`from_unit_id` from your active unit unless overridden."
    )
    lines.append("")
    lines.append(
        "Available skills: `hr.start_business_line` / "
        "`hr.spawn_type_manager` / `hr.spawn_audience_manager` / "
        "`hr.spawn_product_vp` / `niche.score_fit` / "
        "`cooperation.propose` / `cooperation.ask_about`."
    )
    return "\n".join(lines)


def resolve_unit_id(
    session: Session,
    business_id: UUID,
    name_or_uuid: str | UUID | None,
) -> UUID | None:
    """Translate ``name_or_uuid`` to a BusinessUnit.id within ``business_id``.

    Accepts:
      * None — returns None (caller should fall back to ctx.business_unit_id)
      * a UUID instance or 36-char hex UUID string — passes through
        after verifying the row exists in this business
      * a unit name (case-insensitive) — returns the matching unit's id

    Raises ``ValueError`` when the lookup fails so the skill can surface
    a helpful "no unit named X" message to the agent."""
    from korpha.business_units.model import BusinessUnit
    from sqlmodel import select as _select

    if name_or_uuid is None:
        return None

    # UUID instance — verify ownership
    if isinstance(name_or_uuid, UUID):
        row = session.get(BusinessUnit, name_or_uuid)
        if row is None or row.business_id != business_id:
            raise ValueError(
                f"BusinessUnit {name_or_uuid} not found in this business"
            )
        return row.id

    s = str(name_or_uuid).strip()
    if not s:
        return None

    # Looks like a UUID string?
    try:
        as_uuid = UUID(s)
    except ValueError:
        as_uuid = None
    if as_uuid is not None:
        return resolve_unit_id(session, business_id, as_uuid)

    # Otherwise treat as a name (case-insensitive)
    rows = list(session.exec(
        _select(BusinessUnit).where(BusinessUnit.business_id == business_id)
    ).all())
    matches = [r for r in rows if r.name.lower() == s.lower()]
    if len(matches) == 1:
        return matches[0].id
    if not matches:
        # Try fuzzy prefix match as a courtesy
        starts = [
            r for r in rows if r.name.lower().startswith(s.lower())
        ]
        if len(starts) == 1:
            return starts[0].id
        raise ValueError(
            f"No BusinessUnit named {name_or_uuid!r} in this business "
            f"(available: {', '.join(sorted(r.name for r in rows)) or '<none>'})"
        )
    raise ValueError(
        f"Multiple BusinessUnits named {name_or_uuid!r} — pass a UUID instead"
    )


__all__ = [
    "CANONICAL_LINE_KINDS",
    "UnitContextSummary",
    "list_units_for_context",
    "render_unit_summary_for_prompt",
    "resolve_unit_id",
]
