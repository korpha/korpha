"""WebSocket transport for the TUI — JSON-RPC 2.0 over a WS frame.

Hermes uses the same shape (``tui_gateway/ws.py``). Our v0 TUI talks
directly to the in-process agent runtime; this module is what makes
``korpha tui`` connect to a *running server* instead, so the TUI
and web dashboard can share one process on a VPS.

Wire format
-----------

Every message is a single-line UTF-8 JSON object. Three shapes:

* **Request** (client → server, expects a response):
  ``{"jsonrpc":"2.0","id":42,"method":"prompt.submit","params":{...}}``
* **Response** (server → client, paired with the request id):
  ``{"jsonrpc":"2.0","id":42,"result":{...}}``  or  ``{"jsonrpc":"2.0","id":42,"error":{"code":-32603,"message":"..."}}``
* **Event** (server → client, no id, fire-and-forget notifications):
  ``{"jsonrpc":"2.0","method":"assistant.delta","params":{"text":"hi"}}``

The server never sends a response without a matching ``id`` from a
request. The client never sends events to the server.

Method registry
---------------

Methods register via the ``@method("name")`` decorator. Each method
gets a ``MethodContext`` with the SQLModel session, the founder +
business resolved from the connection, and an ``emit_event``
callback so methods can stream progress back to the same client.

Errors follow JSON-RPC 2.0 codes:

  -32700  Parse error          (invalid JSON)
  -32600  Invalid Request      (missing/bad fields)
  -32601  Method not found
  -32602  Invalid params       (wrong type / missing required)
  -32603  Internal error       (any uncaught exception)

Connection lifecycle
--------------------

On connect we resolve the active founder + business once and stash
them on the connection — cheaper than re-querying per request, and
matches the assumption Korpha runs single-tenant per process.
The ``gateway.ready`` event fires immediately after that resolution
with founder/business identity so the TUI can render its status bar
without a separate ``me`` round-trip.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from korpha.business.model import Business
from korpha.identity.model import Founder

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JSON-RPC error codes (subset — we only emit the ones we use)
# ---------------------------------------------------------------------------


class RpcErrorCode:
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603


class RpcError(Exception):
    """Raised inside method handlers to send a structured error
    response. Anything else (regular exceptions) gets wrapped as
    INTERNAL_ERROR with str(exc) as the message — handlers should
    raise RpcError when they want a specific code."""

    def __init__(
        self, code: int, message: str, data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


# ---------------------------------------------------------------------------
# Method registry — handler decorator + dispatch table
# ---------------------------------------------------------------------------


@dataclass
class MethodContext:
    """Per-call context. Methods get this + their params dict.

    ``emit_event`` is the streaming hook — handlers that take time
    (LLM calls, skill runs) push intermediate events through it
    while still computing their final return value. The TUI sees
    the events arrive on the same socket interleaved with other
    responses.
    """

    session: Session
    founder: Founder
    business: Business
    emit_event: Callable[[str, dict[str, Any]], Awaitable[None]]
    request_id: Any
    """JSON-RPC id of the in-flight request, when known. Methods can
    embed it in their events so multiple concurrent requests don't
    confuse the client."""

    cancel_event: asyncio.Event
    """Set by ``prompt.interrupt`` (or any method that wants to
    cancel an in-flight call). Long-running handlers check it
    periodically to abort cleanly."""


MethodHandler = Callable[[MethodContext, dict[str, Any]], Awaitable[Any]]


@dataclass
class MethodRegistry:
    """Mutable so plugins can call ``register()`` later. We don't
    ship a plugin contract for RPC methods yet — that's v2 — but
    the shape is ready."""

    methods: dict[str, MethodHandler] = field(default_factory=dict)

    def register(self, name: str) -> Callable[[MethodHandler], MethodHandler]:
        def deco(fn: MethodHandler) -> MethodHandler:
            if name in self.methods:
                raise ValueError(f"method {name!r} is already registered")
            self.methods[name] = fn
            return fn
        return deco

    def get(self, name: str) -> MethodHandler | None:
        return self.methods.get(name)

    def names(self) -> list[str]:
        return sorted(self.methods.keys())


registry = MethodRegistry()
method = registry.register
"""Decorator alias — ``@method("foo")`` is the canonical way to add
an RPC method."""


# ---------------------------------------------------------------------------
# Connection — owns the websocket, write lock, in-flight cancel registry
# ---------------------------------------------------------------------------


@dataclass
class Connection:
    """One open WebSocket connection. Tracks per-connection state
    that survives across requests on the same socket."""

    ws: WebSocket
    session_factory: Callable[[], Session]
    founder: Founder
    business: Business
    engine: Engine
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    """Serializes outbound writes. Multiple concurrent handlers can
    call ``send_event`` / ``send_response`` without interleaving on
    the wire — the lock guarantees one full JSON line per send."""

    cancel_events: dict[Any, asyncio.Event] = field(default_factory=dict)
    """``request_id → asyncio.Event``. Set by ``prompt.interrupt``
    to cancel a long-running handler. Cleaned up after each request."""

    async def send_response(
        self, request_id: Any, result: Any | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        if error is not None:
            payload = {"jsonrpc": "2.0", "id": request_id, "error": error}
        else:
            payload = {"jsonrpc": "2.0", "id": request_id, "result": result}
        async with self.write_lock:
            await self.ws.send_text(json.dumps(payload))

    async def send_event(
        self, name: str, params: dict[str, Any] | None = None,
    ) -> None:
        payload = {"jsonrpc": "2.0", "method": name, "params": params or {}}
        async with self.write_lock:
            try:
                await self.ws.send_text(json.dumps(payload))
            except RuntimeError:
                # Socket closed mid-stream. Caller's handler should
                # check self.cancel_events to back out cleanly.
                pass


# ---------------------------------------------------------------------------
# Session resolution — pick the active founder + business once on connect
# ---------------------------------------------------------------------------


def _resolve_active_identity(
    session: Session,
) -> tuple[Founder, Business]:
    from korpha.business.multi import (
        BusinessResolutionError,
        active_business,
    )

    founder = session.exec(select(Founder)).first()
    if founder is None:
        raise RpcError(
            RpcErrorCode.INVALID_REQUEST,
            "No founder configured. Run `korpha init` first.",
        )
    try:
        business = active_business(session, founder)
    except BusinessResolutionError as exc:
        raise RpcError(
            RpcErrorCode.INVALID_REQUEST,
            f"No active business: {exc}",
        ) from exc
    return founder, business


# ---------------------------------------------------------------------------
# WebSocket route — the public entry point
# ---------------------------------------------------------------------------


async def tui_websocket_handler(
    ws: WebSocket,
    *,
    session_factory: Callable[[], Session],
    engine: Engine,
) -> None:
    """Mount this on FastAPI:

        @app.websocket("/api/tui/ws")
        async def tui_ws(ws: WebSocket) -> None:
            return await tui_websocket_handler(
                ws,
                session_factory=app.state.session_factory,
                engine=app.state.engine,
            )

    Single connection per call; FastAPI spawns one task per
    accept. Inside the handler we accept, resolve identity, emit
    ``gateway.ready``, then run a read loop dispatching JSON-RPC
    until the client disconnects.
    """
    await ws.accept()

    # Resolve identity in a short-lived session — the per-call
    # session is built on demand inside dispatch().
    try:
        with session_factory() as boot_session:
            founder, business = _resolve_active_identity(boot_session)
    except RpcError as exc:
        await ws.send_text(json.dumps({
            "jsonrpc": "2.0",
            "method": "gateway.error",
            "params": {
                "code": exc.code, "message": exc.message,
            },
        }))
        await ws.close()
        return
    except Exception as exc:
        logger.exception("TUI WS identity resolution failed")
        await ws.send_text(json.dumps({
            "jsonrpc": "2.0",
            "method": "gateway.error",
            "params": {
                "code": RpcErrorCode.INTERNAL_ERROR, "message": str(exc),
            },
        }))
        await ws.close()
        return

    conn = Connection(
        ws=ws,
        session_factory=session_factory,
        founder=founder,
        business=business,
        engine=engine,
    )

    await conn.send_event("gateway.ready", {
        "founder": {
            "id": str(founder.id),
            "email": founder.email,
            "display_name": founder.display_name,
        },
        "business": {
            "id": str(business.id),
            "name": business.name,
            "description": business.description,
        },
        "methods": registry.names(),
    })

    in_flight: dict[Any, asyncio.Task[None]] = {}

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError as exc:
                await conn.send_response(None, error={
                    "code": RpcErrorCode.PARSE_ERROR,
                    "message": f"invalid JSON: {exc}",
                })
                continue
            if not isinstance(msg, dict):
                await conn.send_response(None, error={
                    "code": RpcErrorCode.INVALID_REQUEST,
                    "message": "top-level must be an object",
                })
                continue

            method_name = msg.get("method")
            request_id = msg.get("id")
            params = msg.get("params") or {}
            if not isinstance(method_name, str):
                await conn.send_response(request_id, error={
                    "code": RpcErrorCode.INVALID_REQUEST,
                    "message": "missing or non-string `method`",
                })
                continue
            if not isinstance(params, dict):
                await conn.send_response(request_id, error={
                    "code": RpcErrorCode.INVALID_PARAMS,
                    "message": "`params` must be an object",
                })
                continue

            handler = registry.get(method_name)
            if handler is None:
                await conn.send_response(request_id, error={
                    "code": RpcErrorCode.METHOD_NOT_FOUND,
                    "message": f"unknown method {method_name!r}",
                })
                continue

            # Spawn each request as its own task so concurrent
            # handlers don't block the read loop. The cancel_event
            # makes prompt.interrupt able to abort an in-flight
            # ``prompt.submit`` mid-stream.
            cancel_event = asyncio.Event()
            if request_id is not None:
                conn.cancel_events[request_id] = cancel_event
            task = asyncio.create_task(
                _run_handler(
                    conn=conn,
                    handler=handler,
                    method_name=method_name,
                    request_id=request_id,
                    params=params,
                    cancel_event=cancel_event,
                )
            )
            if request_id is not None:
                in_flight[request_id] = task
                task.add_done_callback(
                    lambda _t, _id=request_id: in_flight.pop(_id, None)
                )
                task.add_done_callback(
                    lambda _t, _id=request_id: conn.cancel_events.pop(_id, None)
                )
    except WebSocketDisconnect:
        pass
    finally:
        for t in in_flight.values():
            if not t.done():
                t.cancel()


async def _run_handler(
    *,
    conn: Connection,
    handler: MethodHandler,
    method_name: str,
    request_id: Any,
    params: dict[str, Any],
    cancel_event: asyncio.Event,
) -> None:
    """Run one method handler in its own task. Wraps result + errors
    so the dispatch loop stays clean."""
    # Build the per-call context with a fresh DB session — handlers
    # close it on their own via ``with`` blocks if they need to;
    # otherwise we close here.
    session = conn.session_factory()
    try:
        ctx = MethodContext(
            session=session,
            founder=conn.founder,
            business=conn.business,
            emit_event=conn.send_event,
            request_id=request_id,
            cancel_event=cancel_event,
        )
        try:
            result = await handler(ctx, params)
        except RpcError as exc:
            if request_id is not None:
                err: dict[str, Any] = {
                    "code": exc.code, "message": exc.message,
                }
                if exc.data is not None:
                    err["data"] = exc.data
                await conn.send_response(request_id, error=err)
            return
        except asyncio.CancelledError:
            if request_id is not None:
                await conn.send_response(request_id, error={
                    "code": RpcErrorCode.INTERNAL_ERROR,
                    "message": "cancelled",
                })
            raise
        except Exception as exc:
            logger.exception(
                "RPC handler %s raised", method_name,
            )
            if request_id is not None:
                await conn.send_response(request_id, error={
                    "code": RpcErrorCode.INTERNAL_ERROR,
                    "message": f"{type(exc).__name__}: {exc}",
                })
            return
        if request_id is not None:
            await conn.send_response(request_id, result=result)
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Methods — the actual capability surface the TUI calls into.
#
# Keep these small + delegated to existing services. Each handler
# is a thin RPC adapter; the real work stays in cofounder.ceo /
# skills.registry / approvals.gate.
# ---------------------------------------------------------------------------


@method("me")
async def _me(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Identity probe — TUI calls this on connect to render its
    status bar without re-deriving from gateway.ready.
    Idempotent + cheap."""
    return {
        "founder": {
            "id": str(ctx.founder.id),
            "email": ctx.founder.email,
            "display_name": ctx.founder.display_name,
        },
        "business": {
            "id": str(ctx.business.id),
            "name": ctx.business.name,
            "description": ctx.business.description,
        },
    }


@method("methods.list")
async def _methods_list(
    ctx: MethodContext, params: dict[str, Any],
) -> list[str]:
    """Discoverable RPC catalog — useful for the TUI's slash
    autocompletion + for diagnostics in CI."""
    return registry.names()


@method("skills.list")
async def _skills_list(
    ctx: MethodContext, params: dict[str, Any],
) -> list[dict[str, Any]]:
    from korpha.skills import default_registry as skills_registry
    return [
        {
            "name": s.name,
            "description": s.description,
            "parameters": list(s.parameters.keys()),
        }
        for s in sorted(
            skills_registry.list_specs(), key=lambda x: x.name,
        )
    ]


@method("agents.list")
async def _agents_list(
    ctx: MethodContext, params: dict[str, Any],
) -> list[dict[str, Any]]:
    """Hired agents for this business. The TUI's agent picker
    uses this to let the founder talk to a Director directly
    (CTO / CMO / COO) instead of routing through the CEO."""
    from korpha.cofounder.model import AgentRole

    rows = ctx.session.exec(
        select(AgentRole)
        .where(AgentRole.business_id == ctx.business.id)
        .where(AgentRole.is_active == True)  # noqa: E712
        .order_by(AgentRole.role_type)  # type: ignore[arg-type]
    ).all()
    return [
        {
            "id": str(r.id),
            "role_type": r.role_type.value,
            "title": r.title,
            "specialty": r.specialty,
        }
        for r in rows
    ]


@method("approvals.list")
async def _approvals_list(
    ctx: MethodContext, params: dict[str, Any],
) -> list[dict[str, Any]]:
    from korpha.approvals.model import Approval, ApprovalStatus

    rows = ctx.session.exec(
        select(Approval)
        .where(Approval.business_id == ctx.business.id)
        .where(Approval.status == ApprovalStatus.PENDING)
        .order_by(Approval.created_at.desc())  # type: ignore[attr-defined]
    ).all()
    return [
        {
            "id": str(r.id),
            "summary": r.proposal_summary,
            "action_class": r.action_class.value,
            "platform": r.platform,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@method("approval.respond")
async def _approval_respond(
    ctx: MethodContext, params: dict[str, Any],
) -> dict[str, Any]:
    from uuid import UUID

    from korpha.approvals.gate import ApprovalGate, Decision

    approval_id_raw = params.get("approval_id")
    decision_raw = str(params.get("decision") or "").lower()
    if not approval_id_raw:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS, "approval_id is required",
        )
    if decision_raw not in ("approve", "reject"):
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS,
            "decision must be 'approve' or 'reject'",
        )
    try:
        approval_id = UUID(str(approval_id_raw))
    except ValueError as exc:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS, f"bad approval_id: {exc}",
        ) from exc

    gate = ApprovalGate(ctx.session)
    result = gate.decide(
        approval_id=approval_id,
        decision=(
            Decision.APPROVE if decision_raw == "approve"
            else Decision.REJECT
        ),
        decided_by_founder_id=ctx.founder.id,
    )
    # Apply post-approve side effects (skill authoring) the same
    # way the HTTP /approve route does.
    if decision_raw == "approve":
        payload = result.approval.action_payload or {}
        kind = payload.get("kind")
        if kind == "author_skill":
            from korpha.skills.meta import (
                apply_skill_proposal_from_approval,
            )
            apply_skill_proposal_from_approval(result.approval)
        elif kind == "author_python_skill":
            from korpha.skills.meta import (
                apply_python_skill_proposal_from_approval,
            )
            apply_python_skill_proposal_from_approval(result.approval)
        elif kind == "create_cron":
            from korpha.skills.cron_author import (
                apply_cron_proposal_from_approval,
            )
            apply_cron_proposal_from_approval(result.approval)

    await ctx.emit_event("approval.decided", {
        "approval_id": str(approval_id),
        "decision": decision_raw,
        "status": result.approval.status.value,
    })

    return {
        "status": result.approval.status.value,
        "consecutive_approvals": result.envelope.consecutive_approvals,
        "threshold": result.envelope.threshold,
        "promotion_offered": result.promotion_offered,
    }


@method("subagent.list")
async def _subagent_list(
    ctx: MethodContext, params: dict[str, Any],
) -> list[dict[str, Any]]:
    """Currently-running director attempts for this business.

    The TUI shows this as a "live agents" panel so the founder
    knows what's running before they hit interrupt. Filtered to
    just THIS founder's business so concurrent businesses can't
    see each other's runs.
    """
    from korpha.cofounder.workforce import list_running_subagents

    rows = list_running_subagents()
    business_id = str(ctx.business.id)
    return [r for r in rows if r.get("business_id") == business_id]


@method("subagent.interrupt")
async def _subagent_interrupt(
    ctx: MethodContext, params: dict[str, Any],
) -> dict[str, Any]:
    """Cancel just one director's running attempt without killing
    the parent prompt.submit. Returns whether anything was actually
    cancelled (False = nothing matched the role / no attempt was
    in flight)."""
    from korpha.cofounder.workforce import cancel_subagent

    role_raw = str(params.get("role_type") or "").strip().lower()
    if not role_raw:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS,
            "role_type is required (e.g. 'cto', 'cmo', 'coo')",
        )
    cancelled = cancel_subagent(str(ctx.business.id), role_raw)
    if cancelled:
        await ctx.emit_event("subagent.cancelled", {
            "role_type": role_raw,
            "business_id": str(ctx.business.id),
        })
    return {"cancelled": cancelled, "role_type": role_raw}


@method("session.branch")
async def _session_branch(
    ctx: MethodContext, params: dict[str, Any],
) -> dict[str, Any]:
    """Fork the current thread at a specific message — copy every
    message up to + including ``message_id`` into a new ACTIVE
    thread. The original thread becomes inactive (closed) so we
    don't end up with two parallel timelines for the same founder.

    Why this exists: the LLM sometimes goes off the rails and the
    cleanest recovery is "rewind to the message before it derailed,
    take a different path." Branch lets the founder keep both
    timelines (the original is closed but accessible via
    /sessions) without the new path inheriting the bad turn.
    """
    from uuid import UUID, uuid4

    from korpha.cofounder.model import (
        Message, Thread, ThreadStatus,
    )

    message_id_raw = params.get("message_id")
    if not message_id_raw:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS, "message_id is required",
        )
    try:
        message_id = UUID(str(message_id_raw))
    except ValueError as exc:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS,
            f"bad message_id: {exc}",
        ) from exc

    # Resolve the source message + its thread; verify ownership.
    source_msg = ctx.session.get(Message, message_id)
    if source_msg is None:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS,
            f"message {message_id} not found",
        )
    source_thread = ctx.session.get(Thread, source_msg.thread_id)
    if (
        source_thread is None
        or source_thread.business_id != ctx.business.id
        or source_thread.founder_id != ctx.founder.id
    ):
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS,
            f"thread {source_msg.thread_id} not owned by this founder",
        )

    # Take every message in the source thread up to + including
    # the cut point. Copy them into the new thread with fresh ids.
    rows = list(ctx.session.exec(
        select(Message)
        .where(Message.thread_id == source_thread.id)
        .where(Message.created_at <= source_msg.created_at)
        .order_by(Message.created_at)
    ).all())

    # Source's new state
    if source_thread.status == ThreadStatus.ACTIVE:
        source_thread.status = ThreadStatus.CLOSED
        ctx.session.add(source_thread)

    # New active thread; topic captures the branch lineage so the
    # founder can find it later without scrolling.
    new_thread = Thread(
        id=uuid4(),
        business_id=source_thread.business_id,
        founder_id=source_thread.founder_id,
        agent_role_id=source_thread.agent_role_id,
        platform=source_thread.platform,
        topic=(
            (source_thread.topic or "branch")[:24] + " (branch)"
        ),
        status=ThreadStatus.ACTIVE,
    )
    ctx.session.add(new_thread)
    ctx.session.flush()  # need new_thread.id before copying messages

    copied = 0
    for msg in rows:
        clone = Message(
            id=uuid4(),
            thread_id=new_thread.id,
            sender_type=msg.sender_type,
            sender_role_id=msg.sender_role_id,
            content=msg.content,
            attachments=msg.attachments or {},
            created_at=msg.created_at,
        )
        ctx.session.add(clone)
        copied += 1
    ctx.session.commit()
    return {
        "new_thread_id": str(new_thread.id),
        "source_thread_id": str(source_thread.id),
        "messages_copied": copied,
    }


@method("session.undo")
async def _session_undo(
    ctx: MethodContext, params: dict[str, Any],
) -> dict[str, Any]:
    """Drop the last N messages from the active thread.

    Hard delete — the messages don't go to a soft-deleted state
    because the memory loader + summarizer would still pull them
    in otherwise. ``steps`` defaults to 1 (drop last reply).
    Returns the count actually dropped, which can be less than
    ``steps`` if the thread has fewer messages.

    Refuses to run mid-stream (when a prompt.submit is in flight)
    because deleting a message that's still being written would
    confuse the router. The TUI should disable the /undo slash
    while streaming; the server enforces it as a backstop.
    """
    from korpha.cofounder.model import (
        Message, Thread, ThreadPlatform, ThreadStatus,
    )

    steps_raw = params.get("steps")
    steps = int(steps_raw) if steps_raw is not None else 1
    if steps < 1:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS,
            f"steps must be ≥ 1, got {steps}",
        )

    # Decline if there's an in-flight prompt — message rows might
    # be half-written.
    if _PROMPT_CANCEL_REGISTRY:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS,
            "Cannot /undo while a prompt is streaming. Use "
            "prompt.interrupt first.",
        )

    active_threads = list(ctx.session.exec(
        select(Thread)
        .where(Thread.business_id == ctx.business.id)
        .where(Thread.founder_id == ctx.founder.id)
        .where(Thread.platform == ThreadPlatform.WEB)
        .where(Thread.status == ThreadStatus.ACTIVE)
    ).all())
    if not active_threads:
        return {"undone": 0, "thread_id": None}

    thread_ids = [t.id for t in active_threads]
    recent = list(ctx.session.exec(
        select(Message)
        .where(Message.thread_id.in_(thread_ids))  # type: ignore[attr-defined]
        .order_by(Message.created_at.desc())  # type: ignore[attr-defined]
        .limit(steps)
    ).all())
    if not recent:
        return {"undone": 0, "thread_id": str(thread_ids[0])}

    for m in recent:
        ctx.session.delete(m)

    # Refresh the active thread's last_message_at — pick the new
    # max created_at (or fall back to thread.created_at if empty).
    primary_thread = active_threads[0]
    remaining_max_q = ctx.session.exec(
        select(Message.created_at)
        .where(Message.thread_id == primary_thread.id)
        .order_by(Message.created_at.desc())  # type: ignore[attr-defined]
        .limit(1)
    ).first()
    if remaining_max_q is not None:
        primary_thread.last_message_at = remaining_max_q
    else:
        primary_thread.last_message_at = primary_thread.created_at
    ctx.session.add(primary_thread)
    ctx.session.commit()
    return {
        "undone": len(recent),
        "thread_id": str(primary_thread.id),
    }


@method("session.list")
async def _session_list(
    ctx: MethodContext, params: dict[str, Any],
) -> list[dict[str, Any]]:
    from korpha.cofounder.model import (
        Thread, ThreadPlatform, ThreadStatus,
    )

    stmt = (
        select(Thread)
        .where(Thread.business_id == ctx.business.id)
        .where(Thread.founder_id == ctx.founder.id)
        .where(Thread.platform == ThreadPlatform.WEB)
        .order_by(Thread.last_message_at.desc())  # type: ignore[attr-defined]
    )
    rows = ctx.session.exec(stmt).all()
    return [
        {
            "id": str(t.id),
            "topic": t.topic or "Untitled",
            "status": t.status.value,
            "is_active": t.status == ThreadStatus.ACTIVE,
            "last_message_at": (
                t.last_message_at.isoformat() if t.last_message_at else None
            ),
        }
        for t in rows
    ]


@method("session.history")
async def _session_history(
    ctx: MethodContext, params: dict[str, Any],
) -> list[dict[str, Any]]:
    """Replay the active web thread for the TUI on connect. Caller
    can pass ``thread_id`` to load a specific past session."""
    from uuid import UUID

    from korpha.cofounder.model import (
        AgentRole, Message, Thread, ThreadPlatform, ThreadStatus,
    )

    thread_id_raw = params.get("thread_id")
    limit = int(params.get("limit") or 50)
    if thread_id_raw:
        try:
            target_thread_id = UUID(str(thread_id_raw))
        except ValueError as exc:
            raise RpcError(
                RpcErrorCode.INVALID_PARAMS,
                f"bad thread_id: {exc}",
            ) from exc
        thread_ids = [target_thread_id]
    else:
        # Default — recent active web thread(s) for this founder
        threads = ctx.session.exec(
            select(Thread)
            .where(Thread.business_id == ctx.business.id)
            .where(Thread.founder_id == ctx.founder.id)
            .where(Thread.platform == ThreadPlatform.WEB)
            .where(Thread.status == ThreadStatus.ACTIVE)
        ).all()
        thread_ids = [t.id for t in threads]
        if not thread_ids:
            return []

    rows = ctx.session.exec(
        select(Message)
        .where(Message.thread_id.in_(thread_ids))  # type: ignore[attr-defined]
        .order_by(Message.created_at)
    ).all()
    role_titles: dict[Any, str] = {}
    out: list[dict[str, Any]] = []
    for m in rows[-limit:]:
        if m.sender_role_id and m.sender_role_id not in role_titles:
            role = ctx.session.get(AgentRole, m.sender_role_id)
            role_titles[m.sender_role_id] = (
                role.title if role else "Cofounder"
            )
        title = (
            role_titles.get(m.sender_role_id) if m.sender_role_id
            else "Cofounder"
        )
        out.append({
            "id": str(m.id),
            "thread_id": str(m.thread_id),
            "sender_type": m.sender_type.value if m.sender_type else "system",
            "sender_role_title": title or "Cofounder",
            "content": m.content,
            "created_at": (
                m.created_at.isoformat() if m.created_at else None
            ),
        })
    return out


@method("session.new")
async def _session_new(
    ctx: MethodContext, params: dict[str, Any],
) -> dict[str, Any]:
    """Archive the active web thread + report success. The next
    ``prompt.submit`` will land in a fresh thread automatically
    via the ConversationRouter."""
    from korpha.cofounder.model import (
        Thread, ThreadPlatform, ThreadStatus,
    )

    threads = ctx.session.exec(
        select(Thread)
        .where(Thread.business_id == ctx.business.id)
        .where(Thread.founder_id == ctx.founder.id)
        .where(Thread.platform == ThreadPlatform.WEB)
        .where(Thread.status == ThreadStatus.ACTIVE)
    ).all()
    for t in threads:
        t.status = ThreadStatus.CLOSED
        ctx.session.add(t)
    ctx.session.commit()
    return {"closed": len(threads)}


@method("session.resume")
async def _session_resume(
    ctx: MethodContext, params: dict[str, Any],
) -> dict[str, Any]:
    """Mark a previously-closed thread active. Closes any other
    active web thread first so we never have two actives at once."""
    from uuid import UUID

    from korpha.cofounder.model import (
        Thread, ThreadPlatform, ThreadStatus,
    )

    thread_id_raw = params.get("thread_id")
    if not thread_id_raw:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS, "thread_id is required",
        )
    try:
        target_id = UUID(str(thread_id_raw))
    except ValueError as exc:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS, f"bad thread_id: {exc}",
        ) from exc

    target = ctx.session.get(Thread, target_id)
    if (
        target is None
        or target.business_id != ctx.business.id
        or target.founder_id != ctx.founder.id
    ):
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS,
            f"thread {target_id} not found for this founder",
        )

    others = ctx.session.exec(
        select(Thread)
        .where(Thread.business_id == ctx.business.id)
        .where(Thread.founder_id == ctx.founder.id)
        .where(Thread.platform == ThreadPlatform.WEB)
        .where(Thread.status == ThreadStatus.ACTIVE)
    ).all()
    for o in others:
        if o.id != target.id:
            o.status = ThreadStatus.CLOSED
            ctx.session.add(o)
    target.status = ThreadStatus.ACTIVE
    ctx.session.add(target)
    ctx.session.commit()
    return {"resumed_thread_id": str(target.id)}


@method("memory.remember")
async def _memory_remember(
    ctx: MethodContext, params: dict[str, Any],
) -> dict[str, Any]:
    """Store a long-term memory for the founder.

    Mirrors the ``memory.remember`` skill — accepts the same
    ``text`` + ``tags`` shape so the TUI and the agent take the
    same path. Used by the TUI's ``/remember`` slash so the
    founder can stash things directly without going through the
    LLM (saves a turn).
    """
    text = str(params.get("text") or "").strip()
    if not text:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS, "text is required",
        )
    tags_raw = params.get("tags") or []
    if isinstance(tags_raw, str):
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    elif isinstance(tags_raw, list):
        tags = [str(t).strip() for t in tags_raw if str(t).strip()]
    else:
        tags = []

    from korpha.memory import (
        NoopLongTermMemory, active_long_term_memory,
    )
    from korpha.memory.db_backend import DbLongTermMemory

    active = active_long_term_memory()
    mem = (
        DbLongTermMemory(ctx.session)
        if isinstance(active, NoopLongTermMemory) else active
    )
    try:
        entry = await mem.add(
            business_id=ctx.business.id,
            founder_id=ctx.founder.id,
            text=text,
            tags=tags,
        )
    except ValueError as exc:
        raise RpcError(RpcErrorCode.INVALID_PARAMS, str(exc)) from exc
    return {
        "memory_id": entry.id,
        "text": entry.text,
        "tags": list(entry.tags),
        "provider": mem.name,
    }


@method("memory.recall")
async def _memory_recall(
    ctx: MethodContext, params: dict[str, Any],
) -> dict[str, Any]:
    """Search the founder's long-term memory. Returns the same
    shape as ``memory.recall`` skill payloads."""
    from korpha.memory import (
        MemoryQuery, NoopLongTermMemory, active_long_term_memory,
    )
    from korpha.memory.db_backend import DbLongTermMemory

    query_text = str(params.get("query") or "").strip()
    if not query_text:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS, "query is required",
        )
    try:
        limit = int(params.get("limit") or 5)
    except (TypeError, ValueError):
        limit = 5
    limit = max(1, min(50, limit))
    tags_raw = params.get("tags") or []
    if isinstance(tags_raw, str):
        tag_filter = tuple(
            t.strip() for t in tags_raw.split(",") if t.strip()
        )
    elif isinstance(tags_raw, list):
        tag_filter = tuple(
            str(t).strip() for t in tags_raw if str(t).strip()
        )
    else:
        tag_filter = ()

    active = active_long_term_memory()
    mem = (
        DbLongTermMemory(ctx.session)
        if isinstance(active, NoopLongTermMemory) else active
    )
    entries = await mem.search(MemoryQuery(
        business_id=ctx.business.id,
        founder_id=ctx.founder.id,
        text=query_text,
        limit=limit,
        tags=tag_filter,
    ))
    return {
        "query": query_text,
        "provider": mem.name,
        "results": [
            {
                "id": e.id,
                "text": e.text,
                "tags": list(e.tags),
                "score": e.score,
                "created_at": (
                    e.created_at.isoformat() if e.created_at else None
                ),
            }
            for e in entries
        ],
    }


@method("memory.forget")
async def _memory_forget(
    ctx: MethodContext, params: dict[str, Any],
) -> dict[str, Any]:
    """Drop a stored memory. Multi-tenant safe — the underlying
    backend rejects cross-tenant deletes."""
    from korpha.memory import (
        NoopLongTermMemory, active_long_term_memory,
    )
    from korpha.memory.db_backend import DbLongTermMemory

    memory_id = str(params.get("memory_id") or "").strip()
    if not memory_id:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS, "memory_id is required",
        )
    active = active_long_term_memory()
    mem = (
        DbLongTermMemory(ctx.session)
        if isinstance(active, NoopLongTermMemory) else active
    )
    ok = await mem.forget(
        business_id=ctx.business.id,
        founder_id=ctx.founder.id,
        memory_id=memory_id,
    )
    return {"forgot": ok, "memory_id": memory_id}


# ---- cron.* — agentless script cron ----


@method("cron.list")
async def _cron_list(
    ctx: MethodContext, params: dict[str, Any],
) -> list[dict[str, Any]]:
    """List ScriptCron jobs for the founder's business."""
    from korpha.scriptcron.model import ScriptCron

    rows = list(ctx.session.exec(
        select(ScriptCron)
        .where(ScriptCron.business_id == ctx.business.id)
        .order_by(ScriptCron.created_at.desc())  # type: ignore[attr-defined]
    ).all())
    return [
        {
            "id": str(r.id),
            "name": r.name,
            "cadence": r.cadence,
            "enabled": r.enabled,
            "last_status": r.last_status.value,
            "last_run_at": (
                r.last_run_at.isoformat() if r.last_run_at else None
            ),
            "deliver_platform": r.deliver_platform,
            "deliver_recipient": r.deliver_recipient,
            "last_output": r.last_output,
            "last_error": r.last_error,
        }
        for r in rows
    ]


def _resolve_cron_by_name(
    ctx: MethodContext, name: str,
) -> "object":
    """Look up a cron job by name, scoped to the business. Raises
    RpcError if not found — callers don't have to repeat the
    multi-tenant guard."""
    from korpha.scriptcron.model import ScriptCron

    job = ctx.session.exec(
        select(ScriptCron)
        .where(ScriptCron.business_id == ctx.business.id)
        .where(ScriptCron.name == name)
    ).first()
    if job is None:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS,
            f"no cron named {name!r} for this business",
        )
    return job


@method("cron.run")
async def _cron_run(
    ctx: MethodContext, params: dict[str, Any],
) -> dict[str, Any]:
    """Run a cron job immediately, ignoring its cadence. Useful for
    "did I write that script right?"."""
    from korpha.scriptcron import run_job

    name = str(params.get("name") or "").strip()
    if not name:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS, "name is required",
        )
    job = _resolve_cron_by_name(ctx, name)
    outcome = await run_job(ctx.session, job)
    return {
        "name": name,
        "status": outcome.status.value,
        "exit_code": outcome.exit_code,
        "delivered": outcome.delivered,
        "stdout": outcome.stdout[:2000],
        "stderr": outcome.stderr[:2000],
        "error": outcome.error,
    }


@method("cron.toggle")
async def _cron_toggle(
    ctx: MethodContext, params: dict[str, Any],
) -> dict[str, Any]:
    """Flip enabled. Mike pauses a noisy watchdog without
    deleting it."""
    name = str(params.get("name") or "").strip()
    if not name:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS, "name is required",
        )
    job = _resolve_cron_by_name(ctx, name)
    job.enabled = not job.enabled
    ctx.session.add(job)
    ctx.session.commit()
    return {"name": name, "enabled": job.enabled}


@method("cron.delete")
async def _cron_delete(
    ctx: MethodContext, params: dict[str, Any],
) -> dict[str, Any]:
    name = str(params.get("name") or "").strip()
    if not name:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS, "name is required",
        )
    job = _resolve_cron_by_name(ctx, name)
    ctx.session.delete(job)
    ctx.session.commit()
    return {"deleted": True, "name": name}


# ---- kanban.* — durable C-suite board ----


@method("kanban.list")
async def _kanban_list(
    ctx: MethodContext, params: dict[str, Any],
) -> dict[str, Any]:
    """Snapshot of every non-archived column. Optional ``column``
    param filters to one. Read-only."""
    from korpha.kanban import KanbanBoard
    from korpha.kanban.model import KanbanColumn

    board = KanbanBoard(ctx.session)
    col_arg = params.get("column")
    if col_arg:
        try:
            col = KanbanColumn(str(col_arg).strip().lower())
        except ValueError as exc:
            raise RpcError(
                RpcErrorCode.INVALID_PARAMS,
                f"unknown column {col_arg!r}",
            ) from exc
        cards = board.list_column(ctx.business.id, col)
        return {
            "column": col.value,
            "cards": [_kanban_card_dict(c) for c in cards],
        }
    snapshot = board.board_snapshot(ctx.business.id)
    return {
        "snapshot": {
            col.value: [_kanban_card_dict(c) for c in cards]
            for col, cards in snapshot.items()
        },
    }


def _kanban_card_dict(card) -> dict[str, Any]:
    return {
        "id": str(card.id),
        "title": card.title,
        "column": card.column.value,
        "priority": card.priority.value,
        "owner_role": card.owner_role,
        "criteria_count": len(card.acceptance_criteria),
        "has_evidence": bool(card.review_evidence),
        "claimed": card.claimed_by_agent_role_id is not None,
    }


@method("kanban.add")
async def _kanban_add(
    ctx: MethodContext, params: dict[str, Any],
) -> dict[str, Any]:
    """Quick-add a card to BACKLOG. Title is the only required field."""
    from korpha.kanban import (
        CreateCardInput, KanbanBoard, KanbanError,
    )
    from korpha.kanban.model import CardPriority

    title = str(params.get("title") or "").strip()
    if not title:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS, "title is required",
        )
    if len(title) > 200:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS, "title too long (>200 chars)",
        )

    priority_raw = (
        str(params.get("priority") or "normal").strip().lower()
    )
    try:
        priority = CardPriority(priority_raw)
    except ValueError as exc:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS,
            f"priority must be high/normal/low, got {priority_raw!r}",
        ) from exc

    owner_raw = str(params.get("owner_role") or "").strip().lower()
    owner = owner_raw or None
    if owner and owner not in ("cto", "cmo", "coo"):
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS,
            f"owner_role must be cto/cmo/coo, got {owner!r}",
        )

    board = KanbanBoard(ctx.session)
    try:
        card = board.create(CreateCardInput(
            business_id=ctx.business.id,
            title=title,
            body=str(params.get("body") or ""),
            priority=priority,
            owner_role=owner,
            created_by_founder_id=ctx.founder.id,
        ))
    except KanbanError as exc:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS, str(exc),
        ) from exc

    return _kanban_card_dict(card)


def _kanban_resolve_card(ctx: MethodContext, card_id_raw: str):
    """Resolve a UUID + multi-tenant scope guard. Raises RpcError
    on bad input or cross-business attempts."""
    from uuid import UUID as _UUID

    from korpha.kanban.model import KanbanCard

    try:
        cid = _UUID(str(card_id_raw))
    except (TypeError, ValueError) as exc:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS, "card_id must be a UUID",
        ) from exc
    card = ctx.session.get(KanbanCard, cid)
    if card is None or card.business_id != ctx.business.id:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS, "card not found",
        )
    return card


@method("kanban.move")
async def _kanban_move(
    ctx: MethodContext, params: dict[str, Any],
) -> dict[str, Any]:
    """Transition a card. Surfaces the precise KanbanError when
    the move violates TRANSITIONS or a gate."""
    from korpha.kanban import KanbanBoard, KanbanError
    from korpha.kanban.model import KanbanColumn

    card_id = params.get("card_id")
    if not card_id:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS, "card_id is required",
        )
    card = _kanban_resolve_card(ctx, card_id)

    to_raw = str(params.get("to_column") or "").strip().lower()
    try:
        to = KanbanColumn(to_raw)
    except ValueError as exc:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS,
            f"unknown column {to_raw!r}",
        ) from exc

    board = KanbanBoard(ctx.session)
    try:
        moved = board.move(
            card.id, to,
            actor_founder_id=ctx.founder.id,
            note=(str(params.get("note") or "")) or None,
        )
    except KanbanError as exc:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS, str(exc),
        ) from exc
    return _kanban_card_dict(moved)


@method("kanban.specify")
async def _kanban_specify(
    ctx: MethodContext, params: dict[str, Any],
) -> dict[str, Any]:
    """Attach acceptance criteria + owner so a card can leave SPECIFY."""
    from korpha.kanban import KanbanBoard, KanbanError

    card_id = params.get("card_id")
    if not card_id:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS, "card_id is required",
        )
    card = _kanban_resolve_card(ctx, card_id)

    criteria_raw = params.get("acceptance_criteria")
    if not isinstance(criteria_raw, list):
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS,
            "acceptance_criteria must be a list of strings",
        )
    criteria = [str(c).strip() for c in criteria_raw if str(c).strip()]
    if not criteria:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS,
            "at least one acceptance criterion required",
        )

    owner = (str(params.get("owner_role") or "")).strip().lower() or None
    if owner and owner not in ("cto", "cmo", "coo"):
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS,
            f"owner_role must be cto/cmo/coo, got {owner!r}",
        )

    board = KanbanBoard(ctx.session)
    try:
        out = board.specify(
            card.id,
            acceptance_criteria=criteria,
            owner_role=owner,
            body=(str(params.get("body") or "")) or None,
            actor_founder_id=ctx.founder.id,
        )
    except KanbanError as exc:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS, str(exc),
        ) from exc
    return _kanban_card_dict(out)


@method("kanban.archive")
async def _kanban_archive(
    ctx: MethodContext, params: dict[str, Any],
) -> dict[str, Any]:
    """Soft-delete: card moves to ARCHIVED column. Reversible via
    kanban.move with to_column='backlog'."""
    from korpha.kanban import KanbanBoard, KanbanError
    from korpha.kanban.model import KanbanColumn

    card_id = params.get("card_id")
    if not card_id:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS, "card_id is required",
        )
    card = _kanban_resolve_card(ctx, card_id)
    board = KanbanBoard(ctx.session)
    try:
        out = board.move(
            card.id, KanbanColumn.ARCHIVED,
            actor_founder_id=ctx.founder.id,
        )
    except KanbanError as exc:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS, str(exc),
        ) from exc
    return {"id": str(out.id), "archived": True}


# ---- team.* — org chart from chat ----


@method("team.list")
async def _team_list(
    ctx: MethodContext, params: dict[str, Any],
) -> dict[str, Any]:
    """Return the active team — C-suite + workers."""
    from korpha.cofounder.model import AgentRole, RoleType

    rows = list(ctx.session.exec(
        select(AgentRole)
        .where(AgentRole.business_id == ctx.business.id)
        .where(AgentRole.is_active)
    ).all())
    return {
        "c_suite": [
            {
                "id": str(r.id),
                "role_type": r.role_type.value,
                "title": r.title,
            }
            for r in rows
            if r.role_type != RoleType.WORKER
        ],
        "workers": [
            {
                "id": str(r.id),
                "title": r.title,
                "specialty": r.specialty,
            }
            for r in rows if r.role_type == RoleType.WORKER
        ],
    }


@method("team.hire")
async def _team_hire(
    ctx: MethodContext, params: dict[str, Any],
) -> dict[str, Any]:
    """Hire a worker. Refuses any role_type other than worker."""
    from korpha.cofounder.hiring import HiringService
    from korpha.cofounder.model import RoleType

    specialty = str(params.get("specialty") or "").strip().lower()
    if not specialty or " " in specialty or len(specialty) > 60:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS,
            "specialty required (lowercase, hyphens, no spaces, "
            "<=60 chars)",
        )
    title = str(params.get("title") or "").strip() or (
        specialty.replace("-", " ").title()
    )
    role = HiringService(ctx.session).hire(
        ctx.business.id, RoleType.WORKER,
        title=title, specialty=specialty,
        source="tui:hire",
    )
    return {
        "id": str(role.id),
        "specialty": specialty,
        "title": role.title,
    }


@method("team.fire")
async def _team_fire(
    ctx: MethodContext, params: dict[str, Any],
) -> dict[str, Any]:
    """Fire a worker by UUID. Refuses C-suite."""
    from uuid import UUID as _UUID

    from korpha.cofounder.hiring import HiringService
    from korpha.cofounder.model import AgentRole, RoleType

    raw = str(params.get("agent_role_id") or "").strip()
    if not raw:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS, "agent_role_id required",
        )
    try:
        rid = _UUID(raw)
    except ValueError as exc:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS, f"bad UUID: {raw}",
        ) from exc
    role = ctx.session.get(AgentRole, rid)
    if role is None or role.business_id != ctx.business.id:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS, "role not found",
        )
    if role.role_type != RoleType.WORKER:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS,
            f"refuses to fire role_type={role.role_type.value}",
        )
    fired = HiringService(ctx.session).fire(
        rid, reason=str(params.get("reason") or "").strip() or None,
    )
    return {
        "id": str(fired.id),
        "specialty": fired.specialty,
        "title": fired.title,
    }


# ---- note.* — bounded MEMORY/USER blocks ----


@method("note.list")
async def _note_list(
    ctx: MethodContext, params: dict[str, Any],
) -> dict[str, Any]:
    """Return entries for one bounded note store."""
    from korpha.memory.notes import FounderNoteService, STORES

    store = str(params.get("store") or "memory").strip().lower()
    if store not in STORES:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS,
            f"store must be 'memory' or 'user', got {store!r}",
        )
    svc = FounderNoteService(ctx.session)
    rows = svc.list(
        business_id=ctx.business.id,
        founder_id=ctx.founder.id,
        store=store,  # type: ignore[arg-type]
    )
    used = sum(len(r.content) for r in rows)
    spec = STORES[store]  # type: ignore[index]
    return {
        "store": store,
        "entries": [
            {"id": str(r.id), "content": r.content}
            for r in rows
        ],
        "used": used,
        "limit": spec.char_limit,
    }


@method("note.add")
async def _note_add(
    ctx: MethodContext, params: dict[str, Any],
) -> dict[str, Any]:
    """Add an entry to a note store."""
    from korpha.memory.notes import (
        FounderNoteService, NoteCapacityError, STORES,
    )

    store = str(params.get("store") or "memory").strip().lower()
    if store not in STORES:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS,
            f"store must be 'memory' or 'user', got {store!r}",
        )
    content = str(params.get("content") or "").strip()
    if not content:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS, "content required",
        )
    svc = FounderNoteService(ctx.session)
    try:
        note = svc.add(
            business_id=ctx.business.id,
            founder_id=ctx.founder.id,
            store=store,  # type: ignore[arg-type]
            content=content,
        )
    except NoteCapacityError as exc:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS, str(exc),
        ) from exc
    return {
        "id": str(note.id),
        "store": store,
        "content": note.content,
    }


@method("note.remove")
async def _note_remove(
    ctx: MethodContext, params: dict[str, Any],
) -> dict[str, Any]:
    """Remove a note matching old_text (substring)."""
    from korpha.memory.notes import (
        FounderNoteService, NoteNotFound, STORES,
    )

    store = str(params.get("store") or "memory").strip().lower()
    if store not in STORES:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS,
            f"store must be 'memory' or 'user', got {store!r}",
        )
    old_text = str(params.get("old_text") or "").strip()
    if not old_text:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS, "old_text required",
        )
    svc = FounderNoteService(ctx.session)
    try:
        note = svc.remove(
            business_id=ctx.business.id,
            founder_id=ctx.founder.id,
            store=store,  # type: ignore[arg-type]
            old_text=old_text,
        )
    except NoteNotFound as exc:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS, str(exc),
        ) from exc
    return {
        "removed_id": str(note.id),
        "store": store,
    }


@method("prompt.interrupt")
async def _prompt_interrupt(
    ctx: MethodContext, params: dict[str, Any],
) -> dict[str, Any]:
    """Cancel an in-flight ``prompt.submit`` (or anything else
    keyed by request_id). The handler checks its cancel_event
    periodically while streaming and bails cleanly."""
    target_id = params.get("request_id")
    if target_id is None:
        # Cancel everything in-flight as a fallback.
        cancelled: list[Any] = []
        # We need access to the connection to enumerate; the
        # MethodContext carries a per-call cancel_event but not
        # the full registry. Resolution: the dispatcher exposes
        # the connection's cancel_events dict via a side channel.
        # See _PROMPT_CANCEL_REGISTRY below.
        for rid, ev in list(_PROMPT_CANCEL_REGISTRY.items()):
            ev.set()
            cancelled.append(rid)
        return {"cancelled": cancelled}
    ev = _PROMPT_CANCEL_REGISTRY.get(target_id)
    if ev is None:
        return {"cancelled": []}
    ev.set()
    return {"cancelled": [target_id]}


# Process-wide registry of prompt cancel events. Populated by
# ``prompt.submit`` so ``prompt.interrupt`` can find the right one
# even when called over a different request id. We key by request_id
# of the prompt.submit call, not by the connection.
_PROMPT_CANCEL_REGISTRY: dict[Any, asyncio.Event] = {}


@method("prompt.submit")
async def _prompt_submit(
    ctx: MethodContext, params: dict[str, Any],
) -> dict[str, Any]:
    """The headline method. Equivalent to the HTTP /ask/stream:
    routes the message through ConversationRouter, loads memory,
    runs CEO.handle_stream, persists outbound, emits events.

    Events the TUI sees while this runs:

      ``phase``             — phase: "router" | "skill" | "synth"
      ``content.delta``     — text: str (token-ish chunks)
      ``reasoning.delta``   — text: str (chain-of-thought from
                              reasoning models — TUI hides by default)
      ``tool.start``        — name, args (skill name + invocation args)
      ``tool.end``          — name, summary, cost_usd
      ``done``              — content (final), skills_used, cost_usd

    Honors ``ctx.cancel_event`` — set by ``prompt.interrupt`` to
    stop early. The handler emits ``done`` with an ``interrupted``
    flag in that case so the TUI can render a clear marker.
    """
    text = str(params.get("message") or "").strip()
    if not text:
        raise RpcError(
            RpcErrorCode.INVALID_PARAMS, "`message` is required",
        )

    # Register this prompt's cancel event so prompt.interrupt can
    # find it. We use the request_id as the key.
    if ctx.request_id is not None:
        _PROMPT_CANCEL_REGISTRY[ctx.request_id] = ctx.cancel_event

    try:
        from korpha.cofounder.hiring import HiringService
        from korpha.cofounder.memory import MemoryService
        from korpha.cofounder.model import ThreadPlatform
        from korpha.cofounder.router import ConversationRouter
        from korpha.api.server import _build_ceo  # type: ignore[attr-defined]

        ceo = _build_ceo(ctx.session)
        hiring = HiringService(ctx.session)
        router = ConversationRouter(session=ctx.session, hiring=hiring)
        decision = router.route_inbound(
            business_id=ctx.business.id,
            founder_id=ctx.founder.id,
            platform=ThreadPlatform.WEB,
            content=text,
        )
        memory = MemoryService(session=ctx.session)
        history = memory.load_recent(
            business_id=ctx.business.id,
            founder_id=ctx.founder.id,
            platform=ThreadPlatform.WEB,
            limit=20,
        )
        if (
            history
            and history[-1].role.value == "user"
            and history[-1].content == text
        ):
            history = history[:-1]

        full_content = ""
        full_reasoning = ""
        skills_used: list[str] = []
        cost_usd = 0.0
        interrupted = False

        stream = await ceo.handle_stream(
            business=ctx.business,
            founder=ctx.founder,
            founder_message=text,
            history=history,
            thread_id=decision.thread_id,
        )
        async for evt in stream:
            if ctx.cancel_event.is_set():
                interrupted = True
                break
            etype = evt.get("type")
            if etype == "phase":
                await ctx.emit_event("phase", {
                    "phase": evt.get("phase"),
                })
            elif etype == "content":
                chunk = evt.get("text", "")
                full_content += chunk
                await ctx.emit_event("content.delta", {
                    "text": chunk,
                })
            elif etype == "reasoning":
                chunk = evt.get("text", "")
                full_reasoning += chunk
                await ctx.emit_event("reasoning.delta", {
                    "text": chunk,
                })
            elif etype == "tool":
                # CEO emits tool.start/end-style payloads as a
                # single 'tool' event with metadata; pass through.
                await ctx.emit_event("tool.event", evt)
            elif etype == "done":
                full_content = evt.get("content") or full_content
                skills_used = list(evt.get("skills_used") or [])
                cost_usd = float(evt.get("cost_usd") or 0.0)

        if not interrupted and full_content:
            router.route_outbound(
                business_id=ctx.business.id,
                founder_id=ctx.founder.id,
                platform=ThreadPlatform.WEB,
                content=full_content,
                requesting_agent_role_id=decision.delivering_agent_role_id,
            )

        await ctx.emit_event("done", {
            "content": full_content,
            "reasoning": full_reasoning,
            "skills_used": skills_used,
            "cost_usd": cost_usd,
            "interrupted": interrupted,
            "thread_id": str(decision.thread_id),
        })

        return {
            "thread_id": str(decision.thread_id),
            "interrupted": interrupted,
            "skills_used": skills_used,
            "cost_usd": cost_usd,
            "content_chars": len(full_content),
        }
    finally:
        if ctx.request_id is not None:
            _PROMPT_CANCEL_REGISTRY.pop(ctx.request_id, None)


__all__ = [
    "Connection",
    "MethodContext",
    "MethodRegistry",
    "RpcError",
    "RpcErrorCode",
    "method",
    "registry",
    "tui_websocket_handler",
]
