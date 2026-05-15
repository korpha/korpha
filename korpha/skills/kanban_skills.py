"""Kanban skills the agent uses from chat.

The CEO uses these to put work on the board, and C-suite agents use
them to claim + work + submit evidence. Each is a thin wrapper over
the KanbanBoard service so the validation + audit log stay
centralized.

The board complements the existing Approval gate — Approvals are
transactional ("yes/no, do this one thing"), the board is durable
("here's the queue of work compounding over weeks"). They coexist:
a card can stage an Approval before its IN_PROGRESS work runs.
"""
from __future__ import annotations

from typing import Any
from uuid import UUID

from korpha.audit.model import InferenceTier
from korpha.kanban import (
    CreateCardInput,
    KanbanBoard,
    KanbanError,
)
from korpha.kanban.model import CardPriority, KanbanColumn
from korpha.skills.registry import register
from korpha.skills.types import (
    Skill, SkillContext, SkillError, SkillProvenance, SkillResult, SkillSpec,
)


def _coerce_uuid(value: Any, *, field: str) -> UUID:
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError) as exc:
        raise SkillError(f"kanban: {field} must be a UUID, got {value!r}") from exc


def _coerce_priority(value: Any) -> CardPriority:
    if isinstance(value, CardPriority):
        return value
    val = str(value or "normal").strip().lower()
    try:
        return CardPriority(val)
    except ValueError as exc:
        raise SkillError(
            f"kanban: priority must be high/normal/low, got {value!r}"
        ) from exc


def _coerce_column(value: Any) -> KanbanColumn:
    if isinstance(value, KanbanColumn):
        return value
    val = str(value or "").strip().lower()
    try:
        return KanbanColumn(val)
    except ValueError as exc:
        raise SkillError(
            f"kanban: unknown column {value!r}. Expected one of: "
            + ", ".join(c.value for c in KanbanColumn)
        ) from exc


# ----------------------------------------------------------------------
# Create
# ----------------------------------------------------------------------


class KanbanCreateCardSkill(Skill):
    """Put a card on the BACKLOG."""

    spec = SkillSpec(
        name="kanban.create_card",
        description=(
            "Add a new card to the kanban board's BACKLOG column. "
            "Use when the founder mentions a new piece of work, OR "
            "when the CEO breaks a Plan into actionable tasks for "
            "the C-suite. The card lives until archived — Mike can "
            "watch it move through SPECIFY → READY → IN_PROGRESS → "
            "REVIEW → DONE. Cards are owned by a role (cto/cmo/coo) "
            "during specify; that role's agent will claim it later."
        ),
        parameters={
            "title": (
                "Short imperative — 'launch landing page', 'write 3 "
                "LinkedIn posts'. <= 200 chars."
            ),
            "body": (
                "Optional longer description: links, raw founder "
                "words, context the assignee will need. Empty is fine."
            ),
            "priority": (
                "Optional. 'high' / 'normal' / 'low'. Defaults to "
                "'normal'. Use 'high' sparingly — > 20% high cards "
                "loses meaning."
            ),
            "owner_role": (
                "Optional. 'cto' / 'cmo' / 'coo' if you already know "
                "who owns this. Otherwise leave empty and call "
                "kanban.specify_card later to set it."
            ),
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        title = str(args.get("title") or "").strip()
        if not title:
            raise SkillError("kanban.create_card: title required")
        if len(title) > 200:
            raise SkillError("kanban.create_card: title too long (>200 chars)")

        priority = _coerce_priority(args.get("priority", "normal"))
        owner_role: str | None = (
            (str(args.get("owner_role") or "")).strip().lower() or None
        )
        if owner_role and owner_role not in ("cto", "cmo", "coo"):
            raise SkillError(
                f"kanban.create_card: owner_role must be cto/cmo/coo, "
                f"got {owner_role!r}"
            )

        board = KanbanBoard(ctx.session)
        try:
            card = board.create(CreateCardInput(
                business_id=ctx.business.id,
                title=title,
                body=str(args.get("body") or ""),
                priority=priority,
                owner_role=owner_role,
                created_by_agent_role_id=ctx.invoking_agent_role_id,
            ))
        except KanbanError as exc:
            raise SkillError(str(exc)) from exc

        return SkillResult(
            skill_name=self.spec.name,
            summary=f"Card created in BACKLOG: {card.title}",
            payload={
                "card_id": str(card.id),
                "title": card.title,
                "column": card.column.value,
                "priority": card.priority.value,
            },
            cost_usd=0.0,
        )


# ----------------------------------------------------------------------
# Specify
# ----------------------------------------------------------------------


class KanbanSpecifyCardSkill(Skill):
    """Scope a BACKLOG/SPECIFY card with acceptance criteria + owner."""

    spec = SkillSpec(
        name="kanban.specify_card",
        description=(
            "Scope a card with acceptance criteria + owner_role so it "
            "can be claimed. The card moves into SPECIFY column "
            "(if it was in BACKLOG). Acceptance criteria are concrete "
            "checks: 'page deployed at /pricing', 'Stripe button "
            "fires checkout', 'analytics shows page-view event'. The "
            "agent who later claims this card uses these criteria as "
            "its definition of done."
        ),
        parameters={
            "card_id": "UUID of the card to specify.",
            "acceptance_criteria": (
                "List of strings — each one a concrete checkable "
                "condition. At least one required. Aim for 2-5 — "
                "more than that suggests the card should split."
            ),
            "owner_role": (
                "Optional. 'cto' / 'cmo' / 'coo'. Required before "
                "the card can leave SPECIFY for READY. If you don't "
                "set it here, you must call this skill again with it."
            ),
            "body": (
                "Optional. Replace the card body with refined "
                "description (e.g. after CEO has thought it through)."
            ),
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        cid = _coerce_uuid(args.get("card_id"), field="card_id")
        criteria_raw = args.get("acceptance_criteria")
        if not isinstance(criteria_raw, list):
            raise SkillError(
                "kanban.specify_card: acceptance_criteria must be a list"
            )
        criteria = [str(c).strip() for c in criteria_raw if str(c).strip()]
        if not criteria:
            raise SkillError(
                "kanban.specify_card: at least one criterion required"
            )

        owner_role: str | None = (
            (str(args.get("owner_role") or "")).strip().lower() or None
        )
        if owner_role and owner_role not in ("cto", "cmo", "coo"):
            raise SkillError(
                f"kanban.specify_card: owner_role must be cto/cmo/coo, "
                f"got {owner_role!r}"
            )

        body_raw = args.get("body")
        body: str | None = (
            str(body_raw) if body_raw not in (None, "") else None
        )

        board = KanbanBoard(ctx.session)
        try:
            card = board.specify(
                cid,
                acceptance_criteria=criteria,
                owner_role=owner_role,
                body=body,
                actor_agent_role_id=ctx.invoking_agent_role_id,
            )
        except KanbanError as exc:
            raise SkillError(str(exc)) from exc

        return SkillResult(
            skill_name=self.spec.name,
            summary=(
                f"Specified '{card.title}': {len(criteria)} criteria, "
                f"owner={card.owner_role or 'unset'}, "
                f"column={card.column.value}"
            ),
            payload={
                "card_id": str(card.id),
                "column": card.column.value,
                "owner_role": card.owner_role,
                "criteria_count": len(card.acceptance_criteria),
            },
            cost_usd=0.0,
        )


# ----------------------------------------------------------------------
# Move (covers SPECIFY → READY, REVIEW → DONE, etc.)
# ----------------------------------------------------------------------


class KanbanMoveCardSkill(Skill):
    """Transition a card to a new column. Enforces the TRANSITIONS map."""

    spec = SkillSpec(
        name="kanban.move_card",
        description=(
            "Move a card to a new column. Useful when the CEO has "
            "scoped a card and it's ready (SPECIFY → READY) or when "
            "the founder accepts a reviewed card (REVIEW → DONE). "
            "Refuses invalid transitions (e.g. BACKLOG → DONE) and "
            "the SPECIFY/REVIEW gates: leaving SPECIFY needs criteria "
            "+ owner; leaving REVIEW for DONE needs review_evidence."
        ),
        parameters={
            "card_id": "UUID of the card to move.",
            "to_column": (
                "Target column: 'backlog' / 'specify' / 'ready' / "
                "'in_progress' / 'review' / 'done' / 'blocked' / "
                "'archived'."
            ),
            "note": (
                "Optional rationale recorded in the audit log. Useful "
                "for kickbacks ('REVIEW → IN_PROGRESS — URL 404s')."
            ),
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        cid = _coerce_uuid(args.get("card_id"), field="card_id")
        col = _coerce_column(args.get("to_column"))
        note: str | None = (str(args.get("note") or "")).strip() or None

        board = KanbanBoard(ctx.session)
        try:
            card = board.move(
                cid, col,
                actor_agent_role_id=ctx.invoking_agent_role_id,
                note=note,
            )
        except KanbanError as exc:
            raise SkillError(str(exc)) from exc

        return SkillResult(
            skill_name=self.spec.name,
            summary=f"Moved '{card.title}' to {card.column.value}",
            payload={
                "card_id": str(card.id),
                "column": card.column.value,
                "review_verdict": card.review_verdict,
            },
            cost_usd=0.0,
        )


# ----------------------------------------------------------------------
# Claim
# ----------------------------------------------------------------------


class KanbanClaimCardSkill(Skill):
    """C-suite agent claims a READY card and starts work."""

    spec = SkillSpec(
        name="kanban.claim_card",
        description=(
            "Claim a READY card and move it to IN_PROGRESS. The "
            "claiming agent's role_id is recorded on the card so "
            "two agents don't double-dip. Refuses if the card is "
            "owned by a different role (e.g. CMO-owned cards refuse "
            "CTO claims). Use after kanban.list_board has shown you "
            "what's claimable."
        ),
        parameters={
            "card_id": "UUID of the READY card to claim.",
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        cid = _coerce_uuid(args.get("card_id"), field="card_id")
        if ctx.invoking_agent_role_id is None:
            raise SkillError(
                "kanban.claim_card: needs an invoking agent role; "
                "this skill must be called from a C-suite turn."
            )

        # Find the actor role-string for owner-mismatch check.
        from korpha.cofounder.model import AgentRole
        actor = ctx.session.get(AgentRole, ctx.invoking_agent_role_id)
        actor_role = actor.role_type.value if actor is not None else None

        board = KanbanBoard(ctx.session)
        try:
            card = board.claim(
                cid,
                agent_role_id=ctx.invoking_agent_role_id,
                actor_role=actor_role,
            )
        except KanbanError as exc:
            raise SkillError(str(exc)) from exc

        return SkillResult(
            skill_name=self.spec.name,
            summary=f"Claimed '{card.title}' (now IN_PROGRESS)",
            payload={
                "card_id": str(card.id),
                "column": card.column.value,
                "claimed_by_agent_role_id": str(
                    card.claimed_by_agent_role_id
                ),
            },
            cost_usd=0.0,
        )


# ----------------------------------------------------------------------
# Submit evidence (REVIEW gate)
# ----------------------------------------------------------------------


class KanbanSubmitEvidenceSkill(Skill):
    """Agent attaches evidence + moves card to REVIEW."""

    spec = SkillSpec(
        name="kanban.submit_evidence",
        description=(
            "After completing the work on a claimed card, attach "
            "evidence (URL of deployed page, message id of sent post, "
            "file path of written copy) and move the card to REVIEW. "
            "The reviewer (CEO or founder) verifies the evidence "
            "matches reality before accepting → DONE. This is the "
            "hallucination gate — never claim DONE without evidence."
        ),
        parameters={
            "card_id": (
                "UUID of the card you claimed. Must be in IN_PROGRESS."
            ),
            "evidence": (
                "What you did, in concrete terms: a URL, a message "
                "id, a file path, a screenshot path. The reviewer "
                "will follow this to verify. Empty / hand-wavy "
                "evidence will get the card kicked back."
            ),
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        cid = _coerce_uuid(args.get("card_id"), field="card_id")
        evidence = str(args.get("evidence") or "").strip()
        if not evidence:
            raise SkillError(
                "kanban.submit_evidence: evidence required (URL / "
                "message id / file path)"
            )

        board = KanbanBoard(ctx.session)
        try:
            card = board.submit_review_evidence(
                cid, evidence=evidence,
                actor_agent_role_id=ctx.invoking_agent_role_id,
            )
        except KanbanError as exc:
            raise SkillError(str(exc)) from exc

        return SkillResult(
            skill_name=self.spec.name,
            summary=f"Submitted evidence for '{card.title}' (now REVIEW)",
            payload={
                "card_id": str(card.id),
                "column": card.column.value,
                "evidence_chars": len(card.review_evidence or ""),
            },
            cost_usd=0.0,
        )


# ----------------------------------------------------------------------
# List board
# ----------------------------------------------------------------------


class KanbanListBoardSkill(Skill):
    """Read-only snapshot of the whole board for inspection."""

    spec = SkillSpec(
        name="kanban.list_board",
        description=(
            "Return a snapshot of every non-archived column with "
            "card titles + ids. Use to find what's claimable, what's "
            "in progress, what's awaiting review. Read-only — no "
            "side effects. Cheap to call repeatedly."
        ),
        parameters={
            "column": (
                "Optional. Filter to one column "
                "('backlog' / 'specify' / 'ready' / etc.). Empty "
                "returns all non-archived columns."
            ),
            "limit_per_column": (
                "Optional. Cap cards per column (default 50)."
            ),
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        limit_raw = args.get("limit_per_column")
        try:
            limit = int(limit_raw) if limit_raw not in (None, "") else 50
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(limit, 500))

        board = KanbanBoard(ctx.session)
        col_arg = args.get("column")
        if col_arg:
            col = _coerce_column(col_arg)
            cards = board.list_column(
                ctx.business.id, col, limit=limit,
            )
            payload_cards = [
                {
                    "id": str(c.id),
                    "title": c.title,
                    "owner_role": c.owner_role,
                    "priority": c.priority.value,
                } for c in cards
            ]
            return SkillResult(
                skill_name=self.spec.name,
                summary=f"{col.value}: {len(cards)} cards",
                payload={"column": col.value, "cards": payload_cards},
                cost_usd=0.0,
            )

        snapshot = board.board_snapshot(ctx.business.id)
        out: dict[str, list[dict[str, Any]]] = {}
        total = 0
        for col_key, cards in snapshot.items():
            limited = cards[:limit]
            out[col_key.value] = [
                {
                    "id": str(c.id),
                    "title": c.title,
                    "owner_role": c.owner_role,
                    "priority": c.priority.value,
                } for c in limited
            ]
            total += len(limited)
        summary_parts = [
            f"{col}: {len(out[col])}"
            for col in out if out[col]
        ]
        summary = (
            f"Board: {total} cards" + (
                f" — {', '.join(summary_parts)}" if summary_parts else ""
            )
        )
        return SkillResult(
            skill_name=self.spec.name,
            summary=summary,
            payload={"snapshot": out, "total": total},
            cost_usd=0.0,
        )


# Register all five.
_OWNER_KEYWORDS_CTO = (
    "design", "designs", "illustration", "illustrations",
    "generate", "interior", "build", "produce", "frame", "frames",
    "create video", "create videos", "graphic", "graphics",
    "pipeline", "draft animation",
)
_OWNER_KEYWORDS_COO = (
    "set up account", "connect", "fulfillment", "shipping",
    "order tracking", "sample", "samples", "set up printify",
    "set up etsy", "configure",
)
# everything else defaults to CMO (listings, marketing, social,
# storefront)


def _infer_owner_role(title: str, body: str) -> str:
    """Map a kanban card to one of cto/cmo/coo by title+body keywords."""
    blob = f"{title} {body}".lower()
    for kw in _OWNER_KEYWORDS_CTO:
        if kw in blob:
            return "cto"
    for kw in _OWNER_KEYWORDS_COO:
        if kw in blob:
            return "coo"
    return "cmo"


def _resolve_card(session: Any, ref: str, business_id: UUID) -> Any:
    """Resolve a card by full UUID or 8-char hex prefix.

    The CEO often cites cards by their 8-char prefix in chat
    (e.g. 'task 7d4b8115'). The fire-sprint skill accepts either."""
    from sqlmodel import select
    from korpha.kanban.model import KanbanCard
    ref = str(ref).strip()
    # Try full UUID first
    try:
        u = UUID(ref)
        card = session.get(KanbanCard, u)
        if card is not None and card.business_id == business_id:
            return card
    except (ValueError, TypeError):
        pass
    # Prefix lookup: hex-only prefix of length 4-32
    prefix = ref.lower()
    if not all(c in "0123456789abcdef-" for c in prefix):
        return None
    # SQLite stores UUIDs as 32-hex strings without dashes via SQLModel.
    # We try a LIKE match on the stringified id.
    rows = list(session.exec(
        select(KanbanCard)
        .where(KanbanCard.business_id == business_id)
    ))
    norm = prefix.replace("-", "")
    matches = [
        c for c in rows
        if str(c.id).replace("-", "").lower().startswith(norm)
    ]
    if len(matches) == 1:
        return matches[0]
    return None


class KanbanFireSprintSkill(Skill):
    """Bulk-move a list of backlog cards through SPECIFY → READY →
    IN_PROGRESS in one call, claiming each to the Line VP that owns
    its unit. Used when the founder says 'go' / 'fire it' / 'proceed'
    after the CEO has proposed a specific sprint of card IDs.

    Why bulk? The CEO router only picks one skill per turn. Without
    this, the founder would have to specify+ready+claim every card
    individually, which is the kind of grunt work that broke the
    'go' UX and led the CEO to hallucinate ('I assigned them').
    """

    spec = SkillSpec(
        name="kanban.fire_sprint",
        description=(
            "USE THIS when the founder says 'go' / 'fire it' / "
            "'proceed' / 'do it' / 'approve the sprint' after a "
            "recent CEO message that listed specific card IDs or "
            "task references. Bulk-promotes the cited cards from "
            "BACKLOG through SPECIFY → READY → IN_PROGRESS in one "
            "atomic call. Auto-claims each to the Line VP that "
            "owns its BusinessUnit. Args: "
            "card_ids=<list of card id prefixes or full UUIDs>."
        ),
        parameters={
            "card_ids": (
                "List of card IDs to fire. Each can be a full UUID "
                "or an 8-char hex prefix (as the CEO usually "
                "writes in chat)."
            ),
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        from sqlmodel import select
        from korpha.cofounder.model import AgentRole, RoleType
        from korpha.kanban.model import KanbanColumn

        raw_ids = args.get("card_ids") or []
        if isinstance(raw_ids, str):
            raw_ids = [
                t.strip() for t in raw_ids.replace(",", " ").split()
                if t.strip()
            ]
        if not isinstance(raw_ids, list) or not raw_ids:
            raise SkillError(
                "kanban.fire_sprint: card_ids=<list> required"
            )

        board = KanbanBoard(ctx.session)
        fired: list[dict[str, Any]] = []
        errors: list[str] = []

        # Cache Line VPs per unit (avoid N queries).
        line_vps_by_unit: dict[UUID, AgentRole] = {}

        def _line_vp_for_unit(unit_id: UUID | None) -> AgentRole | None:
            if unit_id is None:
                return None
            cached = line_vps_by_unit.get(unit_id)
            if cached is not None:
                return cached
            rows = list(ctx.session.exec(
                select(AgentRole)
                .where(AgentRole.business_unit_id == unit_id)
                .where(AgentRole.role_type == RoleType.WORKER)
                .where(AgentRole.is_active.is_(True))  # type: ignore[attr-defined]
            ))
            if rows:
                line_vps_by_unit[unit_id] = rows[0]
                return rows[0]
            return None

        for ref in raw_ids:
            card = _resolve_card(ctx.session, ref, ctx.business.id)
            if card is None:
                errors.append(f"{ref}: card not found")
                continue
            try:
                # If already in IN_PROGRESS, skip (idempotent).
                if card.column == KanbanColumn.IN_PROGRESS:
                    fired.append({
                        "card_id": str(card.id),
                        "title": card.title,
                        "skipped": "already_in_progress",
                    })
                    continue

                owner_role = card.owner_role or _infer_owner_role(
                    card.title, card.body or "",
                )

                # 1. SPECIFY — set acceptance + owner_role
                if card.column == KanbanColumn.BACKLOG:
                    board.specify(
                        card.id,
                        acceptance_criteria=[
                            "Deliverable matches the card title.",
                            "Output uploaded to the Korpha workspace "
                            "and linked in review evidence.",
                        ],
                        owner_role=owner_role,
                        actor_agent_role_id=ctx.invoking_agent_role_id,
                    )
                elif card.column == KanbanColumn.SPECIFY:
                    # ensure owner + criteria exist
                    if not card.owner_role:
                        card.owner_role = owner_role
                        ctx.session.add(card)
                        ctx.session.commit()
                    if not card.acceptance_criteria:
                        board.specify(
                            card.id,
                            acceptance_criteria=[
                                "Deliverable matches the card title.",
                            ],
                            owner_role=owner_role,
                            actor_agent_role_id=ctx.invoking_agent_role_id,
                        )

                # 2. SPECIFY → READY
                if card.column != KanbanColumn.READY:
                    board.move(
                        card.id, KanbanColumn.READY,
                        actor_agent_role_id=ctx.invoking_agent_role_id,
                    )

                # 3. CLAIM → IN_PROGRESS (auto-routed to Line VP)
                vp = _line_vp_for_unit(card.business_unit_id)
                if vp is None:
                    errors.append(
                        f"{str(card.id)[:8]}: no Line VP for unit"
                    )
                    continue
                board.claim(
                    card.id, agent_role_id=vp.id,
                    actor_role=owner_role,
                )
                fired.append({
                    "card_id": str(card.id),
                    "title": card.title,
                    "owner_role": owner_role,
                    "claimed_by_agent_role_id": str(vp.id),
                    "vp_title": vp.title,
                })
            except KanbanError as exc:
                errors.append(f"{str(card.id)[:8]}: {exc}")
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    f"{str(card.id)[:8]}: unexpected: "
                    f"{type(exc).__name__}: {exc}"
                )

        if not fired:
            raise SkillError(
                "kanban.fire_sprint: nothing fired. Errors: "
                f"{errors[:3]}"
            )

        summary = (
            f"Fired {len(fired)} card(s) into IN_PROGRESS, "
            f"claimed to their Line VPs"
        )
        if errors:
            summary += f". {len(errors)} failed."

        return SkillResult(
            skill_name=self.spec.name,
            summary=summary,
            payload={"fired": fired, "errors": errors},
            cost_usd=0.0,
        )


register(KanbanCreateCardSkill())
register(KanbanSpecifyCardSkill())
register(KanbanMoveCardSkill())
register(KanbanClaimCardSkill())
register(KanbanSubmitEvidenceSkill())
register(KanbanListBoardSkill())
register(KanbanFireSprintSkill())


__all__ = [
    "KanbanClaimCardSkill",
    "KanbanCreateCardSkill",
    "KanbanListBoardSkill",
    "KanbanMoveCardSkill",
    "KanbanSpecifyCardSkill",
    "KanbanSubmitEvidenceSkill",
]
