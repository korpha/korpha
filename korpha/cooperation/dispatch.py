"""Synchronous ask_about dispatcher.

PR-INT-6 — when an asking unit calls ``cooperation.ask_about``, this
module runs the question against the TARGET unit's owner agent in
the target's memory namespace. Returns a structured response.

The full agent-loop invocation (system prompt + tool use loop) needs
the LLM pool wired with credentials — which is per-deployment config.
For test environments + new installs without LLM keys, the dispatcher
takes a deterministic path: searches the target unit's memory
namespace for relevant entries and returns them as the answer. The
asking agent gets actionable structured data ('here are 3 stored
memories the target unit has about your question') without anyone
needing an LLM key.

When the inference pool is wired, callers can opt into a real
LLM-driven response by setting ``DISPATCH_LLM_RUNNER`` (test/runtime
injection hook). The default stays deterministic so tests don't
require LLM mocking.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Awaitable
from uuid import UUID

from sqlmodel import Session, select

from korpha.business_units.model import BusinessUnit
from korpha.cofounder.model import AgentRole
from korpha.memory.contract import MemoryQuery
from korpha.memory.db_backend import DbLongTermMemory

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DispatchResult:
    """What the dispatcher returns. Shape matches what ``ask_about``
    serializes back to the asker."""

    answer: str
    target_unit_id: UUID
    target_namespace_id: UUID
    target_agent_role_id: UUID | None
    relevant_memories: list[dict[str, Any]]


# Test/runtime injection — if set, dispatch routes through this
# instead of the deterministic memory-search path. Production wires
# this to the real agent invocation when LLM credentials are
# available.
DISPATCH_LLM_RUNNER: Callable[..., Awaitable[str]] | None = None


async def dispatch_ask_about(
    *,
    ctx,
    from_unit_id: UUID,
    to_unit_id: UUID,
    question: str,
    extra_context: str = "",
) -> dict[str, Any]:
    """Run the question against the target unit's owner + memory.

    Returns a dict (not a dataclass) so it serializes cleanly into
    the ask_about skill's payload.
    """
    session: Session = ctx.session
    target_unit = session.get(BusinessUnit, to_unit_id)
    if target_unit is None:
        return {
            "answer": f"target unit {to_unit_id} not found",
            "target_unit_id": str(to_unit_id),
            "relevant_memories": [],
        }

    owner: AgentRole | None = None
    if target_unit.owner_agent_role_id is not None:
        owner = session.get(AgentRole, target_unit.owner_agent_role_id)

    # Search target's memory namespace for relevant entries.
    mem = DbLongTermMemory(session)
    try:
        entries = await mem.search(MemoryQuery(
            business_id=ctx.business.id,
            founder_id=ctx.founder.id,
            text=question,
            limit=5,
        ))
        # Filter to target's namespace (defense-in-depth)
        target_ns = target_unit.memory_namespace_id
        entries = [
            e for e in entries
            if e.namespace_id is None or e.namespace_id == target_ns
        ]
    except Exception:  # noqa: BLE001
        entries = []

    # LLM runner path (when wired in production)
    if DISPATCH_LLM_RUNNER is not None:
        try:
            answer = await DISPATCH_LLM_RUNNER(
                target_unit=target_unit,
                target_agent=owner,
                question=question,
                extra_context=extra_context,
                memory_hits=entries,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "dispatch_ask_about: LLM runner failed: %s",
                exc, exc_info=True,
            )
            answer = _stub_answer(target_unit, owner, entries, question)
    else:
        answer = _stub_answer(target_unit, owner, entries, question)

    return {
        "answer": answer,
        "target_unit_id": str(to_unit_id),
        "target_namespace_id": str(target_unit.memory_namespace_id),
        "target_agent_role_id": (
            str(owner.id) if owner is not None else None
        ),
        "target_agent_title": owner.title if owner is not None else None,
        "relevant_memories": [
            {
                "id": e.id, "text": e.text[:200],
                "tags": list(e.tags), "score": e.score,
            }
            for e in entries
        ],
    }


def _stub_answer(
    target_unit: BusinessUnit,
    owner: AgentRole | None,
    entries: list,
    question: str,
) -> str:
    """Deterministic placeholder reply for the LLM-less path.

    Production-grade response is the LLM runner output; this is what
    the asker gets when the operator hasn't wired credentials. Still
    structured + informative (target unit name, owner agent title,
    matching memories) so the asking agent can make a decision."""
    owner_label = (
        f"{owner.title}" if owner is not None else "(unowned unit)"
    )
    if not entries:
        return (
            f"{owner_label} of {target_unit.name} ({target_unit.kind.value}) "
            f"has no specific memories matching {question!r}; "
            f"asker should make a generic ask or follow up."
        )
    summaries = [f"- {e.text[:120]}" for e in entries[:3]]
    return (
        f"{owner_label} of {target_unit.name} found "
        f"{len(entries)} relevant memories:\n"
        + "\n".join(summaries)
    )


__all__ = ["DISPATCH_LLM_RUNNER", "DispatchResult", "dispatch_ask_about"]
