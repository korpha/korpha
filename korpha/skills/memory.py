"""``memory.remember`` + ``memory.recall`` — agent-facing skills
for the long-term memory backend.

Together with the ABC + DB-backed default they give the agent
cross-session recall: "remember that Mike is targeting freelance
designers" and later, "what niche are we focused on?".
"""
from __future__ import annotations

from typing import Any

from korpha.audit.model import InferenceTier
from korpha.memory import (
    LongTermMemory, MemoryQuery, NoopLongTermMemory,
    active_long_term_memory,
)
from korpha.memory.db_backend import DbLongTermMemory
from korpha.skills.registry import register
from korpha.skills.types import (
    Skill, SkillContext, SkillError, SkillProvenance, SkillResult, SkillSpec,
)


def _resolve_memory(ctx: SkillContext) -> LongTermMemory:
    """Use the registry's active provider, but fall through to the
    DB-backed default when nothing is registered (saves the founder
    from "you have no memory provider" errors out of the box).
    Plugin-supplied providers still win when registered."""
    active = active_long_term_memory()
    if isinstance(active, NoopLongTermMemory):
        # No plugin → use the DB default. Don't permanently swap
        # the active provider — that would surprise plugins that
        # register later.
        return DbLongTermMemory(ctx.session)
    return active


class MemoryRememberSkill(Skill):
    """Store a fact for cross-session recall."""

    spec = SkillSpec(
        name="memory.remember",
        description=(
            "Store a fact about the founder / business for future "
            "recall. Use when the founder says 'remember that...' "
            "or when you uncover a stable preference (their niche, "
            "their target customer, their budget cap) that should "
            "survive across chat sessions. Do NOT use for transient "
            "details — those belong in the per-thread message log."
        ),
        parameters={
            "text": (
                "The fact to remember, in plain English. Keep it "
                "self-contained — the agent will read this without "
                "the surrounding conversation context."
            ),
            "tags": (
                "Optional comma-separated labels (e.g. "
                "'niche,target-customer'). Helps with later "
                "retrieval."
            ),
            "business_unit_id": (
                "Optional unit name OR UUID to scope this memory to. "
                "When the founder says 'remember for KDP that...' or "
                "'note in the POD line that...', pass the line name "
                "here so the entry partitions correctly. Defaults to "
                "the caller's own unit context."
            ),
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        text = str(args.get("text") or "").strip()
        if not text:
            raise SkillError(
                "memory.remember: 'text' is required."
            )
        tags_raw = str(args.get("tags") or "").strip()
        tags = [
            t.strip() for t in tags_raw.split(",") if t.strip()
        ] if tags_raw else []

        mem = _resolve_memory(ctx)
        business_id = getattr(ctx.business, "id", None)
        founder_id = getattr(ctx.founder, "id", None)
        if business_id is None or founder_id is None:
            raise SkillError(
                "memory.remember: missing business / founder context."
            )

        # PR-INT-8 / completes PR-INT-2: stamp the entry with the
        # caller unit's memory namespace so the recall-side partition
        # actually has data to filter on. Pre-PR9 writes (no unit
        # context) stay namespace=None — read as company-wide.
        #
        # PR-INT-9: also honor an explicit ``business_unit_id`` arg so
        # the CEO at root can scope a memory to a specific Line without
        # delegating to that Line's VP. Accepts unit name or UUID.
        namespace_id = None
        explicit_unit_arg = args.get("business_unit_id")
        target_unit_id = ctx.business_unit_id
        if explicit_unit_arg:
            from korpha.business_units.context import resolve_unit_id
            try:
                target_unit_id = resolve_unit_id(
                    ctx.session, business_id, explicit_unit_arg,
                )
            except ValueError as exc:
                raise SkillError(f"memory.remember: {exc}") from exc
        scoped_unit_name: str | None = None
        if target_unit_id is not None:
            from korpha.business_units.model import BusinessUnit
            unit = ctx.session.get(BusinessUnit, target_unit_id)
            if unit is not None:
                namespace_id = unit.memory_namespace_id
                scoped_unit_name = unit.name

        try:
            entry = await mem.add(
                business_id=business_id,
                founder_id=founder_id,
                text=text,
                tags=tags,
                namespace_id=namespace_id,
            )
        except Exception as exc:  # noqa: BLE001
            raise SkillError(f"memory.remember: {exc}") from exc

        summary_suffix = ""
        if tags:
            summary_suffix += f" (tags: {', '.join(tags)})"
        if scoped_unit_name:
            summary_suffix += f" [scope: {scoped_unit_name}]"
        return SkillResult(
            skill_name=self.spec.name,
            summary=(
                f"Remembered: {text[:120]}" + summary_suffix
            ),
            payload={
                "memory_id": entry.id,
                "text": entry.text,
                "tags": list(entry.tags),
                "provider": mem.name,
                "scoped_to_unit": scoped_unit_name,
                "namespace_id": (
                    str(namespace_id) if namespace_id else None
                ),
            },
            cost_usd=0.0,
        )


class MemoryRecallSkill(Skill):
    """Retrieve previously-stored facts for the current founder."""

    spec = SkillSpec(
        name="memory.recall",
        description=(
            "Retrieve previously-stored facts about the founder / "
            "business. Use when the founder asks something like "
            "'what niche are we focused on?' or before drafting a "
            "response that depends on standing preferences. Returns "
            "the most-relevant matches; empty when nothing matches."
        ),
        parameters={
            "query": (
                "What you're looking for, in plain English. "
                "The provider tokenizes + scores against stored "
                "memory text."
            ),
            "limit": (
                "Max results to return (default 5). Higher = more "
                "context but more tokens — keep small unless you "
                "need a wide cast."
            ),
            "tags": (
                "Optional comma-separated tag filter — only return "
                "memories matching at least one of these tags."
            ),
            "namespace_id": (
                "Optional foreign BusinessUnit memory namespace OR "
                "unit name/UUID to read from. Defaults to the caller's "
                "own unit namespace. Cross-namespace reads require an "
                "active CrossNamespaceRecallGrant (from an accepted "
                "CooperationProposal with cross_namespace_recall=True)."
            ),
            "business_unit_id": (
                "Alias for namespace_id — pass a unit name or UUID "
                "and the skill resolves the namespace."
            ),
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        query = str(args.get("query") or "").strip()
        if not query:
            raise SkillError(
                "memory.recall: 'query' is required."
            )
        limit_raw = args.get("limit")
        try:
            limit = int(limit_raw) if limit_raw is not None else 5
        except (TypeError, ValueError):
            limit = 5
        limit = max(1, min(50, limit))
        tags_raw = str(args.get("tags") or "").strip()
        tag_filter = tuple(
            t.strip() for t in tags_raw.split(",") if t.strip()
        ) if tags_raw else ()

        mem = _resolve_memory(ctx)
        business_id = getattr(ctx.business, "id", None)
        founder_id = getattr(ctx.founder, "id", None)
        if business_id is None or founder_id is None:
            raise SkillError(
                "memory.recall: missing business / founder context."
            )

        # PR-INT-2: namespace enforcement.
        # Caller's own unit namespace is the default; foreign requires
        # active CrossNamespaceRecallGrant.
        from uuid import UUID as _U
        from korpha.business_units.model import BusinessUnit
        from korpha.memory.grants import check_recall_authorized

        own_ns = None
        if ctx.business_unit_id is not None:
            own_unit = ctx.session.get(BusinessUnit, ctx.business_unit_id)
            if own_unit is not None:
                own_ns = own_unit.memory_namespace_id

        # PR-INT-9: accept either a raw namespace UUID OR a unit
        # name/UUID and resolve to its memory_namespace_id.
        ns_arg = args.get("namespace_id") or args.get("business_unit_id")
        if ns_arg:
            try:
                requested_ns = _U(str(ns_arg))
            except (TypeError, ValueError):
                requested_ns = None
            if requested_ns is None:
                # Try resolving as unit name → unit → namespace
                from korpha.business_units.context import resolve_unit_id
                try:
                    target_unit_id = resolve_unit_id(
                        ctx.session, business_id, ns_arg,
                    )
                except ValueError as exc:
                    raise SkillError(
                        f"memory.recall: {exc}"
                    ) from exc
                target_unit = ctx.session.get(BusinessUnit, target_unit_id)
                requested_ns = target_unit.memory_namespace_id
            else:
                # Could be a namespace_id (matches a unit's
                # memory_namespace_id) OR a unit_id by accident — try
                # both. Look up by namespace_id first.
                from sqlmodel import select as _select
                hit = ctx.session.exec(
                    _select(BusinessUnit).where(
                        BusinessUnit.memory_namespace_id == requested_ns
                    )
                ).first()
                if hit is None:
                    # Maybe they passed a unit_id directly
                    maybe_unit = ctx.session.get(BusinessUnit, requested_ns)
                    if maybe_unit is not None:
                        requested_ns = maybe_unit.memory_namespace_id
            if own_ns is None or requested_ns != own_ns:
                # Foreign namespace — require grant
                if own_ns is None:
                    raise SkillError(
                        f"memory.recall: caller has no unit context; "
                        f"cannot grant foreign namespace access"
                    )
                if not check_recall_authorized(
                    ctx.session,
                    from_namespace_id=own_ns,
                    to_namespace_id=requested_ns,
                ):
                    raise SkillError(
                        f"memory.recall: cross-namespace access "
                        f"{own_ns} → {requested_ns} not authorized; "
                        f"propose CooperationProposal with "
                        f"cross_namespace_recall=True"
                    )
            target_ns = requested_ns
        else:
            target_ns = own_ns  # may be None for pre-PR9 callers

        try:
            entries = await mem.search(MemoryQuery(
                business_id=business_id,
                founder_id=founder_id,
                text=query,
                limit=limit,
                tags=tag_filter,
            ))
        except Exception as exc:  # noqa: BLE001
            raise SkillError(f"memory.recall: {exc}") from exc

        # PR-INT-2: filter results to the target namespace. Provider
        # may return entries from other namespaces (LIKE search); we
        # enforce the partition at the skill layer.
        if target_ns is not None:
            entries = [
                e for e in entries
                if getattr(e, "namespace_id", None) is None
                or _U(str(e.namespace_id)) == target_ns
            ]

        return SkillResult(
            skill_name=self.spec.name,
            summary=(
                f"Recalled {len(entries)} memory(ies) matching {query!r}."
                if entries else
                f"No memories found matching {query!r}."
            ),
            payload={
                "query": query,
                "provider": mem.name,
                "results": [
                    {
                        "id": e.id,
                        "text": e.text,
                        "tags": list(e.tags),
                        "score": e.score,
                        "created_at": (
                            e.created_at.isoformat() if e.created_at
                            else None
                        ),
                    }
                    for e in entries
                ],
            },
            cost_usd=0.0,
        )


register(MemoryRememberSkill())
register(MemoryRecallSkill())


__all__ = ["MemoryRecallSkill", "MemoryRememberSkill"]
