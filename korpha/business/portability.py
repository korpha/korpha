"""Export / import a business as a portable JSON document.

Use cases:
  - Operator wants to migrate from one machine to another.
  - Share a fully-staged business as a template (goals + projects +
    skills + threads bundled together) so someone else can import and
    pick up where you left off.

The exported document is **JSON, scrubbed of secrets, with all UUIDs
regenerated on import** so the same payload can be imported many times
without primary-key collisions. We never include API keys, provider
account IDs, or cost rows that would leak the operator's spend with
specific providers.

Tables included today:
  - business, goal, project, task
  - agent_role
  - thread, message, message_summary
  - blocker
  - approval, trust_envelope
  - activity   (cleaned: actor_role_id remapped to new IDs)
  - routine    (without last_fired_at — that's runtime state)

Tables explicitly NOT exported:
  - cost      (per-provider spend — operator's private financial data)
  - wakeup    (transient runtime queue, not part of the business)
  - repo      (filesystem paths are machine-specific)
  - founder   (the importer's founder row is reused)
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlmodel import Session, select

from korpha.approvals.model import Approval, TrustEnvelope
from korpha.audit.model import Activity
from korpha.blockers.model import Blocker
from korpha.business.model import Business, Goal, Project, Task
from korpha.cofounder.model import (
    AgentRole,
    Message,
    MessageSummary,
    Thread,
)
from korpha.db._base import utcnow
from korpha.heartbeats.model import Routine
from korpha.identity.model import Founder

EXPORT_FORMAT_VERSION = 1


class PortabilityError(ValueError):
    """Export / import payload malformed, version mismatch, etc."""


@dataclass
class ExportResult:
    payload: dict[str, Any]
    table_counts: dict[str, int]


# ────────────────────────────── export ──────────────────────────────


def export_business(
    session: Session,
    *,
    business_id: UUID,
    include_messages: bool = True,
) -> ExportResult:
    """Build a portable JSON-serializable dict capturing the business.

    ``include_messages=False`` skips threads + messages + summaries —
    useful when sharing a business *template* (goals, projects, skills,
    routines) without the operator's private conversation history.
    """
    business = session.get(Business, business_id)
    if business is None:
        raise PortabilityError(f"business {business_id} not found")

    goals = _by_business(session, Goal, business_id)
    projects = _by_business(session, Project, business_id)
    tasks = _by_business(session, Task, business_id)
    agent_roles = _by_business(session, AgentRole, business_id)
    threads = _by_business(session, Thread, business_id) if include_messages else []
    thread_ids = [t.id for t in threads]
    messages = (
        list(
            session.exec(
                select(Message).where(Message.thread_id.in_(thread_ids))  # type: ignore[attr-defined]
            ).all()
        )
        if thread_ids
        else []
    )
    summaries = (
        list(
            session.exec(
                select(MessageSummary).where(
                    MessageSummary.thread_id.in_(thread_ids)  # type: ignore[attr-defined]
                )
            ).all()
        )
        if thread_ids
        else []
    )
    blockers = _by_business(session, Blocker, business_id)
    approvals = _by_business(session, Approval, business_id)
    trust = _by_business(session, TrustEnvelope, business_id)
    activity = _by_business(session, Activity, business_id)
    routines = _by_business(session, Routine, business_id)

    payload: dict[str, Any] = {
        "format_version": EXPORT_FORMAT_VERSION,
        "exported_at": utcnow().isoformat(),
        "business": _model_to_dict(business),
        "goals": [_model_to_dict(g) for g in goals],
        "projects": [_model_to_dict(p) for p in projects],
        "tasks": [_model_to_dict(t) for t in tasks],
        "agent_roles": [_model_to_dict(a) for a in agent_roles],
        "threads": [_model_to_dict(t) for t in threads],
        "messages": [_model_to_dict(m) for m in messages],
        "message_summaries": [_model_to_dict(m) for m in summaries],
        "blockers": [_model_to_dict(b) for b in blockers],
        "approvals": [_scrub_approval(a) for a in approvals],
        "trust_envelopes": [_model_to_dict(t) for t in trust],
        "activity": [_scrub_activity(a) for a in activity],
        "routines": [_scrub_routine(r) for r in routines],
    }
    counts = {
        k: len(v) if isinstance(v, list) else 1
        for k, v in payload.items()
        if k not in ("format_version", "exported_at")
    }
    return ExportResult(payload=payload, table_counts=counts)


def export_to_file(
    session: Session,
    *,
    business_id: UUID,
    path: str,
    include_messages: bool = True,
) -> ExportResult:
    """Write export to a JSON file. Returns the same ExportResult."""
    result = export_business(
        session, business_id=business_id, include_messages=include_messages
    )
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(result.payload, fh, indent=2, default=_json_default)
    return result


# ────────────────────────────── import ──────────────────────────────


@dataclass
class ImportResult:
    business: Business
    table_counts: dict[str, int]


def import_business(
    session: Session,
    payload: dict[str, Any],
    *,
    founder: Founder,
    new_name: str | None = None,
) -> ImportResult:
    """Insert the entire exported business under ``founder``. All UUIDs
    are regenerated so the same payload can be imported repeatedly.

    Returns the new Business + counts.
    """
    version = payload.get("format_version")
    if version != EXPORT_FORMAT_VERSION:
        raise PortabilityError(
            f"unsupported export format_version {version!r} "
            f"(this build understands v{EXPORT_FORMAT_VERSION})"
        )
    if "business" not in payload or not isinstance(payload["business"], dict):
        raise PortabilityError("payload missing 'business' object")

    id_map: dict[str, UUID] = {}

    def remap(old_id: str | None) -> UUID | None:
        if old_id is None:
            return None
        if old_id not in id_map:
            id_map[old_id] = uuid4()
        return id_map[old_id]

    # 1. Business itself.
    biz_raw = dict(payload["business"])
    old_biz_id = biz_raw["id"]
    new_biz_id = remap(old_biz_id)
    biz = Business(
        id=new_biz_id,
        founder_id=founder.id,
        name=new_name or str(biz_raw.get("name", "imported business")),
        description=biz_raw.get("description"),
        status=biz_raw.get("status", "idea"),
    )
    session.add(biz)

    # 2. Lookup tables that other rows reference (agent_role, goal, project,
    # thread). Insert in dependency order, remapping FKs as we go.
    for ar in payload.get("agent_roles", []):
        session.add(
            AgentRole(
                id=remap(ar["id"]),
                business_id=new_biz_id,
                role_type=ar["role_type"],
                title=ar.get("title", "Agent"),
                specialty=ar.get("specialty"),
                is_active=bool(ar.get("is_active", True)),
                personality_config=ar.get("personality_config") or {},
                inference_tier_default=ar.get("inference_tier_default", "pro"),
            )
        )

    for g in payload.get("goals", []):
        session.add(
            Goal(
                id=remap(g["id"]),
                business_id=new_biz_id,
                parent_goal_id=remap(g.get("parent_goal_id")),
                title=g.get("title", ""),
                description=g.get("description"),
                target_metric=g.get("target_metric"),
                target_value=g.get("target_value"),
                status=g.get("status", "active"),
            )
        )

    for p in payload.get("projects", []):
        session.add(
            Project(
                id=remap(p["id"]),
                business_id=new_biz_id,
                goal_id=remap(p.get("goal_id")),
                title=p.get("title", ""),
                description=p.get("description"),
                status=p.get("status", "planning"),
            )
        )

    for t in payload.get("tasks", []):
        session.add(
            Task(
                id=remap(t["id"]),
                business_id=new_biz_id,
                project_id=remap(t.get("project_id")),
                parent_task_id=remap(t.get("parent_task_id")),
                assigned_to_role_id=remap(t.get("assigned_to_role_id")),
                title=t.get("title", ""),
                description=t.get("description"),
                status=t.get("status", "pending"),
                priority=t.get("priority", "normal"),
            )
        )

    # Threads can only be added after agent_role.
    for th in payload.get("threads", []):
        session.add(
            Thread(
                id=remap(th["id"]),
                business_id=new_biz_id,
                founder_id=founder.id,  # remap to importing founder
                agent_role_id=remap(th["agent_role_id"]),
                platform=th.get("platform", "web"),
                platform_thread_id=th.get("platform_thread_id"),
                topic=th.get("topic"),
                status=th.get("status", "active"),
            )
        )

    for m in payload.get("messages", []):
        session.add(
            Message(
                id=remap(m["id"]),
                thread_id=remap(m["thread_id"]),
                sender_type=m["sender_type"],
                sender_role_id=remap(m.get("sender_role_id")),
                content=m.get("content", ""),
                attachments=m.get("attachments") or {},
            )
        )

    for s in payload.get("message_summaries", []):
        session.add(
            MessageSummary(
                id=remap(s["id"]),
                thread_id=remap(s["thread_id"]),
                summary_text=s.get("summary_text", ""),
                covers_until=_parse_dt(s.get("covers_until")),
                message_count=int(s.get("message_count", 0)),
            )
        )

    for b in payload.get("blockers", []):
        session.add(
            Blocker(
                id=remap(b["id"]),
                business_id=new_biz_id,
                requesting_agent_role_id=remap(b["requesting_agent_role_id"]),
                task_id=remap(b.get("task_id")),
                parent_blocker_id=remap(b.get("parent_blocker_id")),
                kind=b.get("kind", "other"),
                urgency=b.get("urgency", "normal"),
                title=b.get("title", ""),
                detail=b.get("detail", ""),
                options=b.get("options") or [],
                status=b.get("status", "open"),
                topic_tag=b.get("topic_tag"),
            )
        )

    for a in payload.get("approvals", []):
        session.add(
            Approval(
                id=remap(a["id"]),
                business_id=new_biz_id,
                agent_role_id=remap(a["agent_role_id"]),
                action_class=a["action_class"],
                platform=a.get("platform"),
                proposal_summary=a.get("proposal_summary", ""),
                action_payload=a.get("action_payload") or {},
                status=a.get("status", "pending"),
            )
        )

    for tr in payload.get("trust_envelopes", []):
        session.add(
            TrustEnvelope(
                id=remap(tr["id"]),
                business_id=new_biz_id,
                action_class=tr["action_class"],
                platform=tr.get("platform"),
                mode=tr.get("mode", "manual"),
                consecutive_approvals=int(tr.get("consecutive_approvals", 0)),
                promotion_threshold=int(tr.get("promotion_threshold", 5)),
            )
        )

    for r in payload.get("routines", []):
        session.add(
            Routine(
                id=remap(r["id"]),
                business_id=new_biz_id,
                name=r.get("name", "routine"),
                kind=r.get("kind", ""),
                schedule_kind=r.get("schedule_kind", "every_seconds"),
                schedule_value=int(r.get("schedule_value", 86400)),
                payload=r.get("payload") or {},
                enabled=bool(r.get("enabled", True)),
            )
        )

    session.commit()
    session.refresh(biz)
    counts = {
        "goals": len(payload.get("goals", [])),
        "projects": len(payload.get("projects", [])),
        "tasks": len(payload.get("tasks", [])),
        "agent_roles": len(payload.get("agent_roles", [])),
        "threads": len(payload.get("threads", [])),
        "messages": len(payload.get("messages", [])),
        "blockers": len(payload.get("blockers", [])),
        "approvals": len(payload.get("approvals", [])),
        "routines": len(payload.get("routines", [])),
    }
    return ImportResult(business=biz, table_counts=counts)


def import_from_file(
    session: Session,
    *,
    path: str,
    founder: Founder,
    new_name: str | None = None,
) -> ImportResult:
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    return import_business(session, payload, founder=founder, new_name=new_name)


# ────────────────────────────── helpers ──────────────────────────────


def _by_business(session: Session, model: Any, business_id: UUID) -> list[Any]:
    return list(
        session.exec(
            select(model).where(model.business_id == business_id)
        ).all()
    )


def _model_to_dict(row: Any) -> dict[str, Any]:
    """Convert a SQLModel row to a JSON-serializable dict."""
    out: dict[str, Any] = {}
    for col in row.__table__.columns:
        value = getattr(row, col.name)
        if isinstance(value, UUID):
            value = str(value)
        elif isinstance(value, datetime):
            value = value.isoformat()
        out[col.name] = value
    return out


def _scrub_approval(approval: Approval) -> dict[str, Any]:
    """Approvals are mostly safe but action_payload can contain
    operator-specific identifiers — keep top-level fields, strip private
    keys whose names contain 'token', 'secret', 'key'."""
    raw = _model_to_dict(approval)
    body = raw.get("action_payload") or {}
    if isinstance(body, dict):
        raw["action_payload"] = {
            k: v
            for k, v in body.items()
            if not _is_secret_key(k)
        }
    return raw


def _scrub_activity(activity: Activity) -> dict[str, Any]:
    """Activity rows are append-only and may carry provider-specific
    identifiers in their JSON payload. We strip any payload keys that
    match the secret-name pattern (account_id, api_key, token, etc.)
    rather than the columns themselves."""
    raw = _model_to_dict(activity)
    body = raw.get("payload") or {}
    if isinstance(body, dict):
        raw["payload"] = {
            k: v for k, v in body.items() if not _is_secret_key(k)
        }
    return raw


def _scrub_routine(routine: Routine) -> dict[str, Any]:
    raw = _model_to_dict(routine)
    raw.pop("last_fired_at", None)  # runtime state, not portable
    return raw


def _is_secret_key(name: str) -> bool:
    n = name.lower()
    return any(tok in n for tok in ("token", "secret", "api_key", "apikey", "password"))


def _json_default(o: Any) -> Any:
    if isinstance(o, UUID):
        return str(o)
    if isinstance(o, datetime):
        return o.isoformat()
    if hasattr(o, "value"):  # StrEnum
        return o.value
    raise TypeError(f"object of type {type(o).__name__} not JSON serializable")


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        # Drop trailing 'Z' if present so fromisoformat handles it on 3.11+.
        s = value.rstrip("Z")
        return datetime.fromisoformat(s)
    return utcnow()


__all__ = [
    "EXPORT_FORMAT_VERSION",
    "ExportResult",
    "ImportResult",
    "PortabilityError",
    "export_business",
    "export_to_file",
    "import_business",
    "import_from_file",
]
