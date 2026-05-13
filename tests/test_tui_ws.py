"""Tests for the TUI WebSocket transport (JSON-RPC dispatcher).

Two layers:
  1. Pure registry mechanics (register / dispatch / errors).
  2. Method handler invariants — exercised through a fake
     MethodContext so we don't need to spin up an actual WS or DB.

For end-to-end coverage including the WS framing, see
test_tui_ws_integration.py — that one boots a TestClient and
opens a real websocket. Kept separate because it's slower.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest

from korpha.api.tui_ws import (
    MethodContext,
    MethodRegistry,
    RpcError,
    RpcErrorCode,
    method,
    registry,
)


# ---- registry mechanics ----


def test_register_decorator_adds_method() -> None:
    reg = MethodRegistry()

    @reg.register("foo.bar")
    async def _h(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    assert "foo.bar" in reg.methods
    assert reg.get("foo.bar") is _h
    assert reg.get("missing") is None


def test_register_collision_raises() -> None:
    reg = MethodRegistry()

    @reg.register("clash")
    async def _a(ctx: MethodContext, params: dict[str, Any]) -> Any:
        return None

    with pytest.raises(ValueError, match="already registered"):
        @reg.register("clash")
        async def _b(ctx: MethodContext, params: dict[str, Any]) -> Any:
            return None


def test_names_returns_sorted_list() -> None:
    reg = MethodRegistry()

    @reg.register("zeta")
    async def _z(ctx: MethodContext, params: dict[str, Any]) -> Any:
        return None

    @reg.register("alpha")
    async def _a(ctx: MethodContext, params: dict[str, Any]) -> Any:
        return None

    assert reg.names() == ["alpha", "zeta"]


# ---- production registry has expected methods ----


def test_production_registry_has_core_methods() -> None:
    """If anyone removes one of these names, every TUI client
    instantly breaks. Test exists to make that obvious in PR
    review."""
    expected = {
        "me", "methods.list", "skills.list", "agents.list",
        "approvals.list", "approval.respond",
        "session.list", "session.history", "session.new", "session.resume",
        "prompt.submit", "prompt.interrupt",
    }
    assert expected.issubset(set(registry.names())), (
        f"missing methods: {expected - set(registry.names())}"
    )


# ---- RpcError shape ----


def test_rpc_error_carries_code_and_message() -> None:
    err = RpcError(
        RpcErrorCode.INVALID_PARAMS,
        "bad shape",
        data={"hint": "expected list"},
    )
    assert err.code == -32602
    assert err.message == "bad shape"
    assert err.data == {"hint": "expected list"}


# ---- MethodContext + handlers via fake ctx ----


def _make_ctx(
    *,
    session: Any | None = None,
    founder: Any | None = None,
    business: Any | None = None,
    request_id: Any = 1,
) -> tuple[MethodContext, list[tuple[str, dict[str, Any]]], asyncio.Event]:
    """Build a MethodContext with stubs. Returns the ctx + the
    captured events list + the cancel event so tests can drive
    interrupts."""
    captured_events: list[tuple[str, dict[str, Any]]] = []

    async def emit_event(name: str, params: dict[str, Any]) -> None:
        captured_events.append((name, dict(params)))

    cancel = asyncio.Event()
    ctx = MethodContext(
        session=session or MagicMock(),
        founder=founder or MagicMock(),
        business=business or MagicMock(),
        emit_event=emit_event,
        request_id=request_id,
        cancel_event=cancel,
    )
    return ctx, captured_events, cancel


@pytest.mark.asyncio
async def test_me_method_returns_identity() -> None:
    """The TUI calls `me` on connect to render its status bar.
    Returns founder + business without hitting the DB (everything
    comes from ctx)."""
    from uuid import uuid4

    handler = registry.get("me")
    assert handler is not None

    founder = MagicMock(spec_set=["id", "email", "display_name"])
    founder.id = uuid4()
    founder.email = "x@y.com"
    founder.display_name = "Mike"
    business = MagicMock(spec_set=["id", "name", "description"])
    business.id = uuid4()
    business.name = "WidgetCo"
    business.description = "test"
    ctx, events, _cancel = _make_ctx(founder=founder, business=business)
    result = await handler(ctx, {})
    assert result["founder"]["email"] == "x@y.com"
    assert result["business"]["name"] == "WidgetCo"


@pytest.mark.asyncio
async def test_methods_list_returns_all_registered_names() -> None:
    handler = registry.get("methods.list")
    assert handler is not None
    ctx, _events, _cancel = _make_ctx()
    result = await handler(ctx, {})
    assert "prompt.submit" in result
    assert "approval.respond" in result


@pytest.mark.asyncio
async def test_approval_respond_validates_decision() -> None:
    handler = registry.get("approval.respond")
    assert handler is not None
    ctx, _events, _cancel = _make_ctx()

    with pytest.raises(RpcError, match="approval_id"):
        await handler(ctx, {})

    with pytest.raises(RpcError, match="approve.*reject"):
        await handler(
            ctx,
            {"approval_id": "00000000-0000-0000-0000-000000000000",
             "decision": "shrug"},
        )


@pytest.mark.asyncio
async def test_approval_respond_validates_uuid() -> None:
    handler = registry.get("approval.respond")
    assert handler is not None
    ctx, _events, _cancel = _make_ctx()
    with pytest.raises(RpcError, match="bad approval_id"):
        await handler(ctx, {
            "approval_id": "not-a-uuid",
            "decision": "approve",
        })


@pytest.mark.asyncio
async def test_session_resume_validates_thread_id() -> None:
    handler = registry.get("session.resume")
    assert handler is not None
    ctx, _events, _cancel = _make_ctx()
    with pytest.raises(RpcError, match="thread_id"):
        await handler(ctx, {})
    with pytest.raises(RpcError, match="bad thread_id"):
        await handler(ctx, {"thread_id": "not-a-uuid"})


@pytest.mark.asyncio
async def test_prompt_submit_requires_message() -> None:
    handler = registry.get("prompt.submit")
    assert handler is not None
    ctx, _events, _cancel = _make_ctx()
    with pytest.raises(RpcError, match="message"):
        await handler(ctx, {"message": "  "})  # whitespace-only

    with pytest.raises(RpcError, match="message"):
        await handler(ctx, {})


# ---- Cancel registry interaction ----


@pytest.mark.asyncio
async def test_prompt_interrupt_with_no_inflight() -> None:
    handler = registry.get("prompt.interrupt")
    assert handler is not None
    ctx, _events, _cancel = _make_ctx(request_id=99)
    # Request_id 99 isn't tracked → cancelled list is empty
    result = await handler(ctx, {"request_id": 99})
    assert result["cancelled"] == []


@pytest.mark.asyncio
async def test_prompt_interrupt_sets_cancel_event() -> None:
    """Simulate a prompt.submit registering its cancel event,
    then call prompt.interrupt and verify the event fires."""
    from korpha.api.tui_ws import _PROMPT_CANCEL_REGISTRY

    handler = registry.get("prompt.interrupt")
    assert handler is not None

    target_event = asyncio.Event()
    _PROMPT_CANCEL_REGISTRY[55] = target_event

    try:
        ctx, _events, _cancel = _make_ctx(request_id=999)
        result = await handler(ctx, {"request_id": 55})
        assert result["cancelled"] == [55]
        assert target_event.is_set()
    finally:
        _PROMPT_CANCEL_REGISTRY.pop(55, None)


# ---- Sub-agent interrupts (TUI J) ----


@pytest.mark.asyncio
async def test_subagent_list_filters_to_current_business() -> None:
    """The TUI shows running directors per founder. If founder A
    has CTO running and founder B has CMO running, founder A's
    /subagents must NOT show CMO. Multi-tenant isolation is the
    whole point — tested explicitly because cross-business leakage
    would be a serious bug."""
    from uuid import uuid4

    from korpha.cofounder import workforce as wf

    handler = registry.get("subagent.list")
    assert handler is not None

    bid_a = str(uuid4())
    bid_b = str(uuid4())
    fake_task = MagicMock()
    fake_task.done.return_value = False
    wf._SUBAGENT_TASKS[(bid_a, "cto")] = fake_task
    wf._SUBAGENT_TASKS[(bid_b, "cmo")] = fake_task

    try:
        business = MagicMock()
        business.id = bid_a
        ctx, _events, _cancel = _make_ctx(business=business)
        result = await handler(ctx, {})
        roles = sorted(r["role_type"] for r in result)
        assert roles == ["cto"]
        assert all(r["business_id"] == bid_a for r in result)
    finally:
        wf._SUBAGENT_TASKS.pop((bid_a, "cto"), None)
        wf._SUBAGENT_TASKS.pop((bid_b, "cmo"), None)


@pytest.mark.asyncio
async def test_subagent_interrupt_requires_role_type() -> None:
    handler = registry.get("subagent.interrupt")
    assert handler is not None
    ctx, _events, _cancel = _make_ctx()
    with pytest.raises(RpcError, match="role_type"):
        await handler(ctx, {})
    with pytest.raises(RpcError, match="role_type"):
        await handler(ctx, {"role_type": "   "})


@pytest.mark.asyncio
async def test_subagent_interrupt_returns_false_when_nothing_to_cancel() -> None:
    """Founder hits /kill cto when no CTO is running — should NOT
    raise; returns cancelled=False so the TUI can show
    'no CTO running' instead of a scary error."""
    from uuid import uuid4
    handler = registry.get("subagent.interrupt")
    assert handler is not None
    business = MagicMock()
    business.id = uuid4()
    ctx, _events, _cancel = _make_ctx(business=business)
    result = await handler(ctx, {"role_type": "cto"})
    assert result == {"cancelled": False, "role_type": "cto"}


@pytest.mark.asyncio
async def test_subagent_interrupt_cancels_running_task_and_emits_event() -> None:
    """Interrupt must (a) actually cancel the asyncio.Task so the
    director's await unblocks, (b) emit subagent.cancelled so the
    TUI can update its running-agents panel without polling."""
    from uuid import uuid4

    from korpha.cofounder import workforce as wf

    handler = registry.get("subagent.interrupt")
    assert handler is not None

    business = MagicMock()
    business.id = uuid4()

    async def _slow() -> None:
        await asyncio.sleep(60)

    real_task = asyncio.create_task(_slow())
    key = (str(business.id), "cto")
    wf._SUBAGENT_TASKS[key] = real_task

    try:
        ctx, events, _cancel = _make_ctx(business=business)
        result = await handler(ctx, {"role_type": "CTO"})  # case-insensitive
        assert result["cancelled"] is True
        assert result["role_type"] == "cto"
        # Drain the cancellation
        with pytest.raises(asyncio.CancelledError):
            await real_task
        # Event surface
        assert any(name == "subagent.cancelled" for name, _ in events)
    finally:
        wf._SUBAGENT_TASKS.pop(key, None)


# ---- session.branch / session.undo (TUI J) ----


@pytest.mark.asyncio
async def test_session_branch_validates_message_id() -> None:
    handler = registry.get("session.branch")
    assert handler is not None
    ctx, _events, _cancel = _make_ctx()
    with pytest.raises(RpcError, match="message_id"):
        await handler(ctx, {})
    with pytest.raises(RpcError, match="bad message_id"):
        await handler(ctx, {"message_id": "not-a-uuid"})


@pytest.mark.asyncio
async def test_session_undo_validates_steps() -> None:
    handler = registry.get("session.undo")
    assert handler is not None
    ctx, _events, _cancel = _make_ctx()
    with pytest.raises(RpcError, match="steps"):
        await handler(ctx, {"steps": 0})


@pytest.mark.asyncio
async def test_session_undo_refuses_during_streaming() -> None:
    """If a prompt.submit is mid-flight, /undo would race the
    writer. Server refuses; the TUI also disables the slash, but
    we want the backstop in case someone hits the RPC directly."""
    from korpha.api.tui_ws import _PROMPT_CANCEL_REGISTRY

    handler = registry.get("session.undo")
    assert handler is not None
    _PROMPT_CANCEL_REGISTRY[7777] = asyncio.Event()
    try:
        ctx, _events, _cancel = _make_ctx()
        with pytest.raises(RpcError, match="streaming"):
            await handler(ctx, {})
    finally:
        _PROMPT_CANCEL_REGISTRY.pop(7777, None)


# ---- memory.* RPC ----


@pytest.mark.asyncio
async def test_memory_remember_validates_text() -> None:
    handler = registry.get("memory.remember")
    assert handler is not None
    ctx, _events, _cancel = _make_ctx()
    with pytest.raises(RpcError, match="text"):
        await handler(ctx, {"text": "  "})
    with pytest.raises(RpcError, match="text"):
        await handler(ctx, {})


@pytest.mark.asyncio
async def test_memory_remember_persists_via_active_provider(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Smoke: remember → search round-trip via the active provider
    (we install a tiny in-memory provider for the test)."""
    from uuid import uuid4

    from korpha.memory import (
        LongTermMemory, MemoryEntry, memory_registry,
    )

    captured: list[dict[str, Any]] = []

    class _Mem(LongTermMemory):
        name = "test-mem"

        async def add(
            self, *, business_id, founder_id, text,
            tags=(), metadata=None,
        ):
            captured.append({"text": text, "tags": list(tags)})
            return MemoryEntry(
                id="m1", text=text,
                business_id=business_id, founder_id=founder_id,
                tags=tuple(tags),
            )

        async def search(self, query):
            return []

        async def forget(self, **kw):
            return False

        async def close(self):
            return None

    memory_registry.set_active(_Mem(), plugin_name="test")
    try:
        handler = registry.get("memory.remember")
        assert handler is not None
        founder = MagicMock(spec_set=["id"])
        founder.id = uuid4()
        business = MagicMock(spec_set=["id"])
        business.id = uuid4()
        ctx, _events, _cancel = _make_ctx(
            founder=founder, business=business,
        )
        result = await handler(ctx, {
            "text": "Mike likes freelance designers",
            "tags": "niche,target",
        })
        assert result["memory_id"] == "m1"
        assert result["provider"] == "test-mem"
        assert captured == [
            {
                "text": "Mike likes freelance designers",
                "tags": ["niche", "target"],
            },
        ]
    finally:
        memory_registry.reset_to_noop()


@pytest.mark.asyncio
async def test_memory_recall_validates_query() -> None:
    handler = registry.get("memory.recall")
    assert handler is not None
    ctx, _events, _cancel = _make_ctx()
    with pytest.raises(RpcError, match="query"):
        await handler(ctx, {})


@pytest.mark.asyncio
async def test_memory_recall_clamps_limit() -> None:
    """A hostile/fat-finger limit=99999 must not pull thousands of
    rows. Backend caps at 50."""
    from uuid import uuid4

    from korpha.memory import (
        LongTermMemory, MemoryQuery, memory_registry,
    )

    captured: list[MemoryQuery] = []

    class _Mem(LongTermMemory):
        name = "spy"

        async def add(self, **kw):
            from korpha.memory import MemoryEntry
            return MemoryEntry(
                id="x", text=kw["text"],
                business_id=kw["business_id"],
                founder_id=kw["founder_id"],
            )

        async def search(self, query):
            captured.append(query)
            return []

        async def forget(self, **kw):
            return False

        async def close(self):
            return None

    memory_registry.set_active(_Mem(), plugin_name="x")
    try:
        handler = registry.get("memory.recall")
        assert handler is not None
        founder = MagicMock(spec_set=["id"])
        founder.id = uuid4()
        business = MagicMock(spec_set=["id"])
        business.id = uuid4()
        ctx, _events, _cancel = _make_ctx(
            founder=founder, business=business,
        )
        await handler(ctx, {"query": "anything", "limit": 99999})
        assert captured[0].limit == 50
    finally:
        memory_registry.reset_to_noop()


@pytest.mark.asyncio
async def test_memory_forget_validates_id() -> None:
    handler = registry.get("memory.forget")
    assert handler is not None
    ctx, _events, _cancel = _make_ctx()
    with pytest.raises(RpcError, match="memory_id"):
        await handler(ctx, {})


# ---- methods exposed to the registry ----


def test_new_methods_in_production_registry() -> None:
    """Backstop test: nothing should remove these names without
    bumping the TUI client too."""
    expected = {
        "subagent.list", "subagent.interrupt",
        "session.branch", "session.undo",
        "memory.remember", "memory.recall", "memory.forget",
        "cron.list", "cron.run", "cron.toggle", "cron.delete",
        "kanban.list", "kanban.add", "kanban.move",
        "kanban.specify", "kanban.archive",
        "team.list", "team.hire", "team.fire",
        "note.list", "note.add", "note.remove",
    }
    assert expected.issubset(set(registry.names())), (
        f"missing: {expected - set(registry.names())}"
    )


# ---- cron.* RPC ----


@pytest.mark.asyncio
async def test_cron_list_filters_by_business(tmp_path) -> None:
    """A founder of business A only sees their own crons."""
    from uuid import uuid4

    from sqlmodel import Session as _S, SQLModel, create_engine
    from korpha.business.model import Business
    from korpha.identity.model import Founder
    from korpha.scriptcron.model import ScriptCron

    engine = create_engine(f"sqlite:///{tmp_path}/cron-rpc.db")
    SQLModel.metadata.create_all(engine)
    with _S(engine) as sess:
        f = Founder(email="x@y.com", display_name="Mike")
        sess.add(f); sess.commit(); sess.refresh(f)
        biz_a = Business(
            founder_id=f.id, name="A", description="",
        )
        biz_b = Business(
            founder_id=f.id, name="B", description="",
        )
        sess.add_all([biz_a, biz_b]); sess.commit()
        sess.refresh(biz_a); sess.refresh(biz_b)
        sess.add(ScriptCron(
            business_id=biz_a.id, name="ours",
            script_path="/bin/true", cadence="every 5m",
        ))
        sess.add(ScriptCron(
            business_id=biz_b.id, name="theirs",
            script_path="/bin/true", cadence="every 5m",
        ))
        sess.commit()

        handler = registry.get("cron.list")
        assert handler is not None
        ctx, _events, _cancel = _make_ctx(
            session=sess, business=biz_a, founder=f,
        )
        rows = await handler(ctx, {})
        names = {r["name"] for r in rows}
        assert names == {"ours"}


@pytest.mark.asyncio
async def test_cron_run_validates_name() -> None:
    handler = registry.get("cron.run")
    assert handler is not None
    ctx, _events, _cancel = _make_ctx()
    with pytest.raises(RpcError, match="name"):
        await handler(ctx, {})


@pytest.mark.asyncio
async def test_cron_toggle_flips_enabled(tmp_path) -> None:
    from sqlmodel import Session as _S, SQLModel, create_engine
    from korpha.business.model import Business
    from korpha.identity.model import Founder
    from korpha.scriptcron.model import ScriptCron

    engine = create_engine(f"sqlite:///{tmp_path}/cron-toggle.db")
    SQLModel.metadata.create_all(engine)
    with _S(engine) as sess:
        f = Founder(email="x@y.com", display_name="M")
        sess.add(f); sess.commit(); sess.refresh(f)
        biz = Business(
            founder_id=f.id, name="B", description="",
        )
        sess.add(biz); sess.commit(); sess.refresh(biz)
        job = ScriptCron(
            business_id=biz.id, name="toggle-me",
            script_path="/bin/true", cadence="every 5m",
        )
        sess.add(job); sess.commit(); sess.refresh(job)

        handler = registry.get("cron.toggle")
        ctx, _events, _cancel = _make_ctx(
            session=sess, business=biz, founder=f,
        )
        # Initially enabled
        assert job.enabled is True
        result = await handler(ctx, {"name": "toggle-me"})
        assert result == {"name": "toggle-me", "enabled": False}


@pytest.mark.asyncio
async def test_cron_run_unknown_name_raises(tmp_path) -> None:
    """Multi-tenant: looking up a name in the wrong business raises
    INVALID_PARAMS, doesn't leak existence."""
    from sqlmodel import Session as _S, SQLModel, create_engine
    from korpha.business.model import Business
    from korpha.identity.model import Founder

    engine = create_engine(f"sqlite:///{tmp_path}/cron-unknown.db")
    SQLModel.metadata.create_all(engine)
    with _S(engine) as sess:
        f = Founder(email="x@y.com", display_name="M")
        sess.add(f); sess.commit(); sess.refresh(f)
        biz = Business(
            founder_id=f.id, name="B", description="",
        )
        sess.add(biz); sess.commit(); sess.refresh(biz)
        handler = registry.get("cron.run")
        ctx, _e, _c = _make_ctx(session=sess, business=biz, founder=f)
        with pytest.raises(RpcError, match="no cron"):
            await handler(ctx, {"name": "nonexistent"})


@pytest.mark.asyncio
async def test_cron_delete_removes_row(tmp_path) -> None:
    from sqlmodel import Session as _S, SQLModel, create_engine, select as _select
    from korpha.business.model import Business
    from korpha.identity.model import Founder
    from korpha.scriptcron.model import ScriptCron

    engine = create_engine(f"sqlite:///{tmp_path}/cron-del.db")
    SQLModel.metadata.create_all(engine)
    with _S(engine) as sess:
        f = Founder(email="x@y.com", display_name="M")
        sess.add(f); sess.commit(); sess.refresh(f)
        biz = Business(
            founder_id=f.id, name="B", description="",
        )
        sess.add(biz); sess.commit(); sess.refresh(biz)
        job = ScriptCron(
            business_id=biz.id, name="bye",
            script_path="/bin/true", cadence="every 5m",
        )
        sess.add(job); sess.commit()

        handler = registry.get("cron.delete")
        ctx, _e, _c = _make_ctx(session=sess, business=biz, founder=f)
        result = await handler(ctx, {"name": "bye"})
        assert result == {"deleted": True, "name": "bye"}
        # Row gone
        remaining = list(sess.exec(_select(ScriptCron)).all())
        assert remaining == []


# ---- kanban.* RPC ----


def _seed_kanban_db(tmp_path, *, db_name: str = "kanban.db"):
    from sqlmodel import Session as _S, SQLModel, create_engine
    from korpha.business.model import Business
    from korpha.identity.model import Founder

    engine = create_engine(f"sqlite:///{tmp_path}/{db_name}")
    SQLModel.metadata.create_all(engine)
    sess = _S(engine)
    f = Founder(email="x@y.com", display_name="Mike")
    sess.add(f); sess.commit(); sess.refresh(f)
    biz = Business(founder_id=f.id, name="B", description="")
    sess.add(biz); sess.commit(); sess.refresh(biz)
    return sess, f, biz


@pytest.mark.asyncio
async def test_kanban_add_creates_card_in_backlog(tmp_path) -> None:
    sess, f, biz = _seed_kanban_db(tmp_path)
    handler = registry.get("kanban.add")
    assert handler is not None
    ctx, _e, _c = _make_ctx(session=sess, business=biz, founder=f)
    result = await handler(ctx, {"title": "ship the demo"})
    assert result["title"] == "ship the demo"
    assert result["column"] == "backlog"


@pytest.mark.asyncio
async def test_kanban_add_rejects_blank_title(tmp_path) -> None:
    sess, f, biz = _seed_kanban_db(tmp_path, db_name="k1.db")
    handler = registry.get("kanban.add")
    ctx, _e, _c = _make_ctx(session=sess, business=biz, founder=f)
    with pytest.raises(RpcError, match="title"):
        await handler(ctx, {"title": "   "})


@pytest.mark.asyncio
async def test_kanban_add_rejects_long_title(tmp_path) -> None:
    sess, f, biz = _seed_kanban_db(tmp_path, db_name="k2.db")
    handler = registry.get("kanban.add")
    ctx, _e, _c = _make_ctx(session=sess, business=biz, founder=f)
    with pytest.raises(RpcError, match="too long"):
        await handler(ctx, {"title": "x" * 250})


@pytest.mark.asyncio
async def test_kanban_add_rejects_bad_priority(tmp_path) -> None:
    sess, f, biz = _seed_kanban_db(tmp_path, db_name="k3.db")
    handler = registry.get("kanban.add")
    ctx, _e, _c = _make_ctx(session=sess, business=biz, founder=f)
    with pytest.raises(RpcError, match="priority"):
        await handler(ctx, {"title": "x", "priority": "burning"})


@pytest.mark.asyncio
async def test_kanban_list_returns_snapshot(tmp_path) -> None:
    sess, f, biz = _seed_kanban_db(tmp_path, db_name="k4.db")
    add_handler = registry.get("kanban.add")
    list_handler = registry.get("kanban.list")
    ctx, _e, _c = _make_ctx(session=sess, business=biz, founder=f)
    await add_handler(ctx, {"title": "A"})
    await add_handler(ctx, {"title": "B"})
    result = await list_handler(ctx, {})
    titles = [c["title"] for c in result["snapshot"]["backlog"]]
    assert "A" in titles
    assert "B" in titles


@pytest.mark.asyncio
async def test_kanban_list_filters_to_one_column(tmp_path) -> None:
    sess, f, biz = _seed_kanban_db(tmp_path, db_name="k5.db")
    handler = registry.get("kanban.list")
    ctx, _e, _c = _make_ctx(session=sess, business=biz, founder=f)
    result = await handler(ctx, {"column": "ready"})
    assert result["column"] == "ready"
    assert result["cards"] == []


@pytest.mark.asyncio
async def test_kanban_list_unknown_column_raises(tmp_path) -> None:
    sess, f, biz = _seed_kanban_db(tmp_path, db_name="k6.db")
    handler = registry.get("kanban.list")
    ctx, _e, _c = _make_ctx(session=sess, business=biz, founder=f)
    with pytest.raises(RpcError, match="unknown column"):
        await handler(ctx, {"column": "nonsense"})


@pytest.mark.asyncio
async def test_kanban_move_invalid_transition(tmp_path) -> None:
    sess, f, biz = _seed_kanban_db(tmp_path, db_name="k7.db")
    add = registry.get("kanban.add")
    move = registry.get("kanban.move")
    ctx, _e, _c = _make_ctx(session=sess, business=biz, founder=f)
    card = await add(ctx, {"title": "x"})
    with pytest.raises(RpcError, match="cannot move"):
        await move(ctx, {
            "card_id": card["id"], "to_column": "done",
        })


@pytest.mark.asyncio
async def test_kanban_move_other_business_card_rejected(tmp_path) -> None:
    """Cross-business move attempts surface 'card not found'."""
    from sqlmodel import Session as _S, SQLModel, create_engine
    from korpha.business.model import Business
    from korpha.identity.model import Founder
    from korpha.kanban.model import KanbanCard

    engine = create_engine(f"sqlite:///{tmp_path}/k8.db")
    SQLModel.metadata.create_all(engine)
    sess = _S(engine)
    f = Founder(email="x@y.com")
    sess.add(f); sess.commit(); sess.refresh(f)
    biz_a = Business(founder_id=f.id, name="A", description="")
    biz_b = Business(founder_id=f.id, name="B", description="")
    sess.add_all([biz_a, biz_b]); sess.commit()
    sess.refresh(biz_a); sess.refresh(biz_b)
    other = KanbanCard(business_id=biz_b.id, title="theirs")
    sess.add(other); sess.commit(); sess.refresh(other)

    handler = registry.get("kanban.move")
    ctx, _e, _c = _make_ctx(session=sess, business=biz_a, founder=f)
    with pytest.raises(RpcError, match="card not found"):
        await handler(ctx, {
            "card_id": str(other.id), "to_column": "specify",
        })


@pytest.mark.asyncio
async def test_kanban_move_bad_uuid(tmp_path) -> None:
    sess, f, biz = _seed_kanban_db(tmp_path, db_name="k9.db")
    handler = registry.get("kanban.move")
    ctx, _e, _c = _make_ctx(session=sess, business=biz, founder=f)
    with pytest.raises(RpcError, match="UUID"):
        await handler(ctx, {
            "card_id": "not-a-uuid", "to_column": "specify",
        })


@pytest.mark.asyncio
async def test_kanban_specify_attaches_criteria(tmp_path) -> None:
    sess, f, biz = _seed_kanban_db(tmp_path, db_name="k10.db")
    add = registry.get("kanban.add")
    spec = registry.get("kanban.specify")
    ctx, _e, _c = _make_ctx(session=sess, business=biz, founder=f)
    card = await add(ctx, {"title": "x"})
    result = await spec(ctx, {
        "card_id": card["id"],
        "acceptance_criteria": ["page loads", "Stripe charges"],
        "owner_role": "cto",
    })
    assert result["criteria_count"] == 2
    assert result["owner_role"] == "cto"
    assert result["column"] == "specify"


@pytest.mark.asyncio
async def test_kanban_specify_requires_list(tmp_path) -> None:
    sess, f, biz = _seed_kanban_db(tmp_path, db_name="k11.db")
    add = registry.get("kanban.add")
    spec = registry.get("kanban.specify")
    ctx, _e, _c = _make_ctx(session=sess, business=biz, founder=f)
    card = await add(ctx, {"title": "x"})
    with pytest.raises(RpcError, match="must be a list"):
        await spec(ctx, {
            "card_id": card["id"],
            "acceptance_criteria": "nope",
        })


@pytest.mark.asyncio
async def test_kanban_archive_moves_to_archived(tmp_path) -> None:
    sess, f, biz = _seed_kanban_db(tmp_path, db_name="k12.db")
    add = registry.get("kanban.add")
    archive = registry.get("kanban.archive")
    list_ = registry.get("kanban.list")
    ctx, _e, _c = _make_ctx(session=sess, business=biz, founder=f)
    card = await add(ctx, {"title": "doomed"})
    result = await archive(ctx, {"card_id": card["id"]})
    assert result["archived"] is True
    # Snapshot omits archived
    snap = await list_(ctx, {})
    titles = [
        c["title"]
        for cards in snap["snapshot"].values()
        for c in cards
    ]
    assert "doomed" not in titles


# ---- team.* RPC ----


def _seed_team_db(tmp_path):
    from sqlmodel import Session as _S, SQLModel, create_engine
    from korpha.business.model import Business
    from korpha.cofounder.hiring import HiringService
    from korpha.identity.model import Founder

    engine = create_engine(f"sqlite:///{tmp_path}/team.db")
    SQLModel.metadata.create_all(engine)
    sess = _S(engine)
    f = Founder(email="x@y.com", display_name="Mike")
    sess.add(f); sess.commit(); sess.refresh(f)
    biz = Business(founder_id=f.id, name="B", description="")
    sess.add(biz); sess.commit(); sess.refresh(biz)
    HiringService(sess).ensure_ceo(biz.id)
    return sess, f, biz


@pytest.mark.asyncio
async def test_team_list_includes_ceo(tmp_path) -> None:
    sess, f, biz = _seed_team_db(tmp_path)
    handler = registry.get("team.list")
    ctx, _e, _c = _make_ctx(session=sess, business=biz, founder=f)
    result = await handler(ctx, {})
    assert any(
        c["role_type"] == "ceo" for c in result["c_suite"]
    )
    assert result["workers"] == []


@pytest.mark.asyncio
async def test_team_hire_creates_worker(tmp_path) -> None:
    sess, f, biz = _seed_team_db(tmp_path)
    hire = registry.get("team.hire")
    list_ = registry.get("team.list")
    ctx, _e, _c = _make_ctx(session=sess, business=biz, founder=f)
    result = await hire(ctx, {"specialty": "copywriter"})
    assert result["specialty"] == "copywriter"
    after = await list_(ctx, {})
    assert any(
        w["specialty"] == "copywriter" for w in after["workers"]
    )


@pytest.mark.asyncio
async def test_team_hire_rejects_blank(tmp_path) -> None:
    sess, f, biz = _seed_team_db(tmp_path)
    handler = registry.get("team.hire")
    ctx, _e, _c = _make_ctx(session=sess, business=biz, founder=f)
    with pytest.raises(RpcError, match="specialty"):
        await handler(ctx, {"specialty": "  "})


@pytest.mark.asyncio
async def test_team_fire_drops_worker(tmp_path) -> None:
    sess, f, biz = _seed_team_db(tmp_path)
    hire = registry.get("team.hire")
    fire = registry.get("team.fire")
    ctx, _e, _c = _make_ctx(session=sess, business=biz, founder=f)
    hired = await hire(ctx, {"specialty": "copywriter"})
    result = await fire(ctx, {"agent_role_id": hired["id"]})
    assert result["id"] == hired["id"]


@pytest.mark.asyncio
async def test_team_fire_refuses_ceo(tmp_path) -> None:
    """CEO is not a worker → fire RPC refuses."""
    sess, f, biz = _seed_team_db(tmp_path)
    list_ = registry.get("team.list")
    fire = registry.get("team.fire")
    ctx, _e, _c = _make_ctx(session=sess, business=biz, founder=f)
    team = await list_(ctx, {})
    ceo = next(c for c in team["c_suite"] if c["role_type"] == "ceo")
    with pytest.raises(RpcError, match="refuses"):
        await fire(ctx, {"agent_role_id": ceo["id"]})


# ---- note.* RPC ----


@pytest.mark.asyncio
async def test_note_add_persists(tmp_path) -> None:
    sess, f, biz = _seed_team_db(tmp_path)
    add = registry.get("note.add")
    list_ = registry.get("note.list")
    ctx, _e, _c = _make_ctx(session=sess, business=biz, founder=f)
    await add(ctx, {"store": "user", "content": "Mike speaks German"})
    result = await list_(ctx, {"store": "user"})
    contents = [e["content"] for e in result["entries"]]
    assert any("German" in c for c in contents)
    assert result["limit"] > 0
    assert result["used"] > 0


@pytest.mark.asyncio
async def test_note_add_rejects_unknown_store(tmp_path) -> None:
    sess, f, biz = _seed_team_db(tmp_path)
    handler = registry.get("note.add")
    ctx, _e, _c = _make_ctx(session=sess, business=biz, founder=f)
    with pytest.raises(RpcError, match="store must"):
        await handler(ctx, {"store": "bogus", "content": "x"})


@pytest.mark.asyncio
async def test_note_add_rejects_blank_content(tmp_path) -> None:
    sess, f, biz = _seed_team_db(tmp_path)
    handler = registry.get("note.add")
    ctx, _e, _c = _make_ctx(session=sess, business=biz, founder=f)
    with pytest.raises(RpcError, match="content"):
        await handler(ctx, {"store": "memory", "content": "  "})


@pytest.mark.asyncio
async def test_note_remove_drops_entry(tmp_path) -> None:
    sess, f, biz = _seed_team_db(tmp_path)
    add = registry.get("note.add")
    remove = registry.get("note.remove")
    list_ = registry.get("note.list")
    ctx, _e, _c = _make_ctx(session=sess, business=biz, founder=f)
    await add(ctx, {
        "store": "memory", "content": "doomed entry to be removed",
    })
    await remove(ctx, {"store": "memory", "old_text": "doomed"})
    result = await list_(ctx, {"store": "memory"})
    assert result["entries"] == []


@pytest.mark.asyncio
async def test_note_remove_no_match_raises(tmp_path) -> None:
    sess, f, biz = _seed_team_db(tmp_path)
    handler = registry.get("note.remove")
    ctx, _e, _c = _make_ctx(session=sess, business=biz, founder=f)
    with pytest.raises(RpcError):
        await handler(ctx, {
            "store": "memory", "old_text": "ghost",
        })


@pytest.mark.asyncio
async def test_prompt_interrupt_no_id_cancels_all() -> None:
    """Calling prompt.interrupt without a request_id wakes
    every in-flight cancel event. Useful when the TUI isn't
    sure which prompt is hanging."""
    from korpha.api.tui_ws import _PROMPT_CANCEL_REGISTRY

    handler = registry.get("prompt.interrupt")
    assert handler is not None

    e1 = asyncio.Event()
    e2 = asyncio.Event()
    _PROMPT_CANCEL_REGISTRY[101] = e1
    _PROMPT_CANCEL_REGISTRY[102] = e2

    try:
        ctx, _events, _cancel = _make_ctx(request_id=999)
        result = await handler(ctx, {})
        assert set(result["cancelled"]) >= {101, 102}
        assert e1.is_set() and e2.is_set()
    finally:
        _PROMPT_CANCEL_REGISTRY.pop(101, None)
        _PROMPT_CANCEL_REGISTRY.pop(102, None)
