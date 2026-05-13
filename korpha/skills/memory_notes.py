"""``memory.note`` — agent-callable bounded note manager.

Wraps :class:`FounderNoteService` so the CEO can curate its own
``MEMORY`` and ``USER PROFILE`` blocks mid-conversation. The
blocks are auto-injected into every system prompt; this skill is
how the agent gets stuff *into* them.

Action shape mirrors Hermes:

  memory.note(action="add",     store="memory", content="...")
  memory.note(action="replace", store="user",   old_text="...", content="...")
  memory.note(action="remove",  store="memory", old_text="...")
  memory.note(action="list",    store="user")
"""
from __future__ import annotations

from typing import Any

from korpha.audit.model import InferenceTier
from korpha.memory.notes import (
    FounderNoteService,
    NoteCapacityError,
    NoteNotFound,
    STORES,
)
from korpha.skills.registry import register
from korpha.skills.types import (
    Skill, SkillContext, SkillError, SkillProvenance, SkillResult, SkillSpec,
)


_VALID_ACTIONS = ("add", "replace", "remove", "list")


class MemoryNoteSkill(Skill):
    """Curate the bounded MEMORY / USER blocks injected into every
    CEO system prompt. The agent's primary self-improvement lever."""

    spec = SkillSpec(
        name="memory.note",
        description=(
            "Manage your bounded MEMORY (your project notes) and "
            "USER (founder profile) blocks. These are auto-injected "
            "into every system prompt at session start, so anything "
            "you save here is what you'll know in future "
            "conversations. Save concise, dense entries. Replace or "
            "consolidate when a store fills up rather than letting "
            "old entries rot. Save: founder preferences, lessons "
            "learned, project facts, things that worked/didn't. "
            "Don't save: raw data, session-specific debugging state, "
            "stuff easily re-discovered."
        ),
        parameters={
            "action": (
                "'add' / 'replace' / 'remove' / 'list'."
            ),
            "store": (
                "'memory' (your notes about the project) or 'user' "
                "(the founder's profile + preferences)."
            ),
            "content": (
                "Required for add + replace. The note body. Use "
                "ONE concise sentence per entry — multi-fact "
                "entries beat sprawling diaries."
            ),
            "old_text": (
                "Required for replace + remove. A short unique "
                "substring that identifies exactly one existing "
                "entry. The full entry text is NOT required."
            ),
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        action = str(args.get("action") or "").strip().lower()
        if action not in _VALID_ACTIONS:
            raise SkillError(
                f"memory.note: action must be one of "
                f"{_VALID_ACTIONS}, got {action!r}"
            )

        store = str(args.get("store") or "memory").strip().lower()
        if store not in STORES:
            raise SkillError(
                f"memory.note: store must be 'memory' or 'user', "
                f"got {store!r}"
            )

        service = FounderNoteService(ctx.session)
        biz_id = ctx.business.id
        founder_id = ctx.founder.id

        if action == "list":
            rows = service.list(
                business_id=biz_id, founder_id=founder_id, store=store,
            )
            spec = STORES[store]
            used = sum(len(r.content) for r in rows)
            entries = [
                {"id": str(r.id), "content": r.content}
                for r in rows
            ]
            summary = (
                f"{store}: {len(rows)} entries, "
                f"{used}/{spec.char_limit} chars"
            )
            return SkillResult(
                skill_name=self.spec.name,
                summary=summary,
                payload={
                    "store": store,
                    "entries": entries,
                    "used": used,
                    "limit": spec.char_limit,
                },
                cost_usd=0.0,
            )

        if action == "add":
            content = str(args.get("content") or "").strip()
            if not content:
                raise SkillError("memory.note add: content required")
            try:
                note = service.add(
                    business_id=biz_id, founder_id=founder_id,
                    store=store, content=content,
                )
            except NoteCapacityError as exc:
                raise SkillError(
                    f"memory.note add: {exc}. Use action=list to "
                    "see existing entries; replace or remove one "
                    "before adding."
                ) from exc
            return SkillResult(
                skill_name=self.spec.name,
                summary=f"added to {store}: {note.content[:80]}",
                payload={
                    "id": str(note.id),
                    "store": store,
                    "content": note.content,
                },
                cost_usd=0.0,
            )

        if action == "replace":
            old_text = str(args.get("old_text") or "").strip()
            content = str(args.get("content") or "").strip()
            if not old_text:
                raise SkillError(
                    "memory.note replace: old_text required"
                )
            if not content:
                raise SkillError(
                    "memory.note replace: content required"
                )
            try:
                note = service.replace(
                    business_id=biz_id, founder_id=founder_id,
                    store=store, old_text=old_text, content=content,
                )
            except NoteNotFound as exc:
                raise SkillError(str(exc)) from exc
            except NoteCapacityError as exc:
                raise SkillError(str(exc)) from exc
            return SkillResult(
                skill_name=self.spec.name,
                summary=f"replaced in {store}: {note.content[:80]}",
                payload={
                    "id": str(note.id),
                    "store": store,
                    "content": note.content,
                },
                cost_usd=0.0,
            )

        # action == "remove"
        old_text = str(args.get("old_text") or "").strip()
        if not old_text:
            raise SkillError("memory.note remove: old_text required")
        try:
            note = service.remove(
                business_id=biz_id, founder_id=founder_id,
                store=store, old_text=old_text,
            )
        except NoteNotFound as exc:
            raise SkillError(str(exc)) from exc
        return SkillResult(
            skill_name=self.spec.name,
            summary=f"removed from {store}: {note.content[:80]}",
            payload={"removed_id": str(note.id), "store": store},
            cost_usd=0.0,
        )


register(MemoryNoteSkill())


__all__ = ["MemoryNoteSkill"]
