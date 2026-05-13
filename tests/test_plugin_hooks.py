"""Tests for plugin lifecycle hooks.

Covers:
  - HookRegistry register/dispatch/clear
  - Hook errors logged + swallowed (don't wedge the agent)
  - SkillRegistry.run fires pre + post for both success + error paths
  - PluginHost.add_lifecycle_hook capability gating
  - Hooks see the right event payload (skill_name, args, etc.)
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from korpha.plugins.hooks import (
    HookKind,
    HookRegistry,
    PostSkillCallEvent,
    PreSkillCallEvent,
    SessionEvent,
    hook_registry,
)
from korpha.skills.registry import SkillRegistry
from korpha.skills.types import (
    Skill, SkillContext, SkillError, SkillResult, SkillSpec,
)


@pytest.fixture(autouse=True)
def _reset_hooks():
    """Clear the process-wide registry between tests so leftovers
    from one test don't pollute another."""
    hook_registry.clear()
    yield
    hook_registry.clear()


# ---- HookRegistry mechanics ----


def test_register_and_listeners() -> None:
    reg = HookRegistry()

    async def _h1(_e): pass
    async def _h2(_e): pass

    reg.register(HookKind.PRE_SKILL_CALL, _h1, plugin_name="a")
    reg.register(HookKind.PRE_SKILL_CALL, _h2, plugin_name="b")
    listeners = reg.listeners(HookKind.PRE_SKILL_CALL)
    assert [n for n, _ in listeners] == ["a", "b"]
    assert reg.has(HookKind.PRE_SKILL_CALL) is True
    assert reg.has(HookKind.SESSION_END) is False


def test_clear_drops_all() -> None:
    reg = HookRegistry()

    async def _h(_e): pass
    reg.register(HookKind.PRE_SKILL_CALL, _h)
    assert reg.has(HookKind.PRE_SKILL_CALL) is True
    reg.clear()
    assert reg.has(HookKind.PRE_SKILL_CALL) is False


@pytest.mark.asyncio
async def test_dispatch_runs_callbacks_in_registration_order() -> None:
    reg = HookRegistry()
    seen: list[str] = []

    async def _a(_e): seen.append("a")
    async def _b(_e): seen.append("b")
    async def _c(_e): seen.append("c")

    reg.register(HookKind.PRE_SKILL_CALL, _a)
    reg.register(HookKind.PRE_SKILL_CALL, _b)
    reg.register(HookKind.PRE_SKILL_CALL, _c)
    await reg.dispatch(HookKind.PRE_SKILL_CALL, "evt")
    assert seen == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_dispatch_swallows_callback_exceptions() -> None:
    """A flaky hook shouldn't wedge later hooks or the agent."""
    reg = HookRegistry()
    seen: list[str] = []

    async def _bad(_e):
        raise RuntimeError("kapow")

    async def _ok(_e):
        seen.append("ok")

    reg.register(HookKind.PRE_SKILL_CALL, _bad, plugin_name="bad")
    reg.register(HookKind.PRE_SKILL_CALL, _ok)
    # Should not raise
    await reg.dispatch(HookKind.PRE_SKILL_CALL, "evt")
    assert seen == ["ok"]


@pytest.mark.asyncio
async def test_dispatch_propagates_cancelled_error() -> None:
    """Cancellation must propagate — caller is shutting down and a
    swallowing logger call would deadlock the cleanup."""
    import asyncio
    reg = HookRegistry()

    async def _cancel(_e):
        raise asyncio.CancelledError

    reg.register(HookKind.PRE_SKILL_CALL, _cancel)
    with pytest.raises(asyncio.CancelledError):
        await reg.dispatch(HookKind.PRE_SKILL_CALL, "evt")


# ---- SkillRegistry integration ----


class _StubSkill(Skill):
    spec = SkillSpec(name="stub.test", description="t")

    def __init__(self, *, raises: bool = False) -> None:
        self._raises = raises

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        if self._raises:
            raise SkillError("intentional")
        return SkillResult(
            skill_name=self.spec.name, summary="ok", payload={},
        )


@pytest.mark.asyncio
async def test_skill_run_fires_pre_then_post_on_success() -> None:
    reg = SkillRegistry()
    reg.add(_StubSkill())

    seen: list[tuple[str, Any]] = []

    async def _pre(evt: PreSkillCallEvent) -> None:
        seen.append(("pre", evt))

    async def _post(evt: PostSkillCallEvent) -> None:
        seen.append(("post", evt))

    hook_registry.register(HookKind.PRE_SKILL_CALL, _pre)
    hook_registry.register(HookKind.POST_SKILL_CALL, _post)

    ctx = SkillContext(
        business=type("B", (), {"id": uuid4()})(),
        founder=type("F", (), {"id": uuid4()})(),
        session=None, cost_tracker=None,
    )
    await reg.run("stub.test", ctx=ctx, args={"k": "v"})

    assert [name for name, _ in seen] == ["pre", "post"]
    pre_evt = seen[0][1]
    post_evt = seen[1][1]
    assert isinstance(pre_evt, PreSkillCallEvent)
    assert pre_evt.skill_name == "stub.test"
    assert pre_evt.args == {"k": "v"}
    assert isinstance(post_evt, PostSkillCallEvent)
    assert post_evt.succeeded is True
    assert post_evt.error is None
    assert post_evt.duration_seconds >= 0


@pytest.mark.asyncio
async def test_skill_run_fires_post_on_error_path() -> None:
    """Skill raises → post still fires with error= populated. The
    pre fires too, the SkillError still propagates to the caller."""
    reg = SkillRegistry()
    reg.add(_StubSkill(raises=True))

    seen: list[tuple[str, Any]] = []

    async def _pre(evt): seen.append(("pre", evt))
    async def _post(evt): seen.append(("post", evt))

    hook_registry.register(HookKind.PRE_SKILL_CALL, _pre)
    hook_registry.register(HookKind.POST_SKILL_CALL, _post)

    ctx = SkillContext(
        business=type("B", (), {"id": uuid4()})(),
        founder=type("F", (), {"id": uuid4()})(),
        session=None, cost_tracker=None,
    )
    with pytest.raises(SkillError, match="intentional"):
        await reg.run("stub.test", ctx=ctx, args={})

    assert [name for name, _ in seen] == ["pre", "post"]
    post_evt = seen[1][1]
    assert post_evt.succeeded is False
    assert isinstance(post_evt.error, SkillError)
    assert post_evt.result is None


@pytest.mark.asyncio
async def test_skill_run_no_hooks_zero_cost() -> None:
    """When no hooks are registered, the skill runs without going
    through the dispatch path. The behavior-level guarantee is just
    that result still comes through."""
    reg = SkillRegistry()
    reg.add(_StubSkill())

    ctx = SkillContext(
        business=type("B", (), {"id": uuid4()})(),
        founder=type("F", (), {"id": uuid4()})(),
        session=None, cost_tracker=None,
    )
    result = await reg.run("stub.test", ctx=ctx, args={})
    assert result.skill_name == "stub.test"


@pytest.mark.asyncio
async def test_skill_run_flaky_hook_does_not_wedge_skill() -> None:
    """A pre-hook that raises must not block the skill from running."""
    reg = SkillRegistry()
    reg.add(_StubSkill())

    async def _flaky(_e):
        raise RuntimeError("plugin bug")

    hook_registry.register(HookKind.PRE_SKILL_CALL, _flaky)

    ctx = SkillContext(
        business=type("B", (), {"id": uuid4()})(),
        founder=type("F", (), {"id": uuid4()})(),
        session=None, cost_tracker=None,
    )
    result = await reg.run("stub.test", ctx=ctx, args={})
    assert result.skill_name == "stub.test"


# ---- PluginHost wiring ----


def test_plugin_host_requires_lifecycle_hooks_capability() -> None:
    """add_lifecycle_hook must refuse plugins without the
    ``lifecycle_hooks`` capability."""
    from korpha.plugins.host import PluginHost, PluginPermissionError
    from korpha.skills.registry import SkillRegistry
    from korpha.heartbeats import HandlerRegistry

    host = PluginHost(
        plugin_name="bad-plugin",
        permissions=frozenset({"skills"}),
        skill_registry=SkillRegistry(),
        handler_registry=HandlerRegistry(),
    )

    async def _h(_e): pass
    with pytest.raises(PluginPermissionError, match="lifecycle_hooks"):
        host.add_lifecycle_hook("pre_skill_call", _h)


def test_plugin_host_registers_hook_with_capability() -> None:
    from korpha.plugins.host import PluginHost
    from korpha.skills.registry import SkillRegistry
    from korpha.heartbeats import HandlerRegistry

    host = PluginHost(
        plugin_name="obs-plugin",
        permissions=frozenset({"lifecycle_hooks"}),
        skill_registry=SkillRegistry(),
        handler_registry=HandlerRegistry(),
    )

    async def _h(_e): pass
    host.add_lifecycle_hook("post_skill_call", _h)
    assert "post_skill_call:obs-plugin" in host.contributed_hooks
    assert hook_registry.has(HookKind.POST_SKILL_CALL)


def test_plugin_host_rejects_unknown_hook_kind() -> None:
    from korpha.plugins.host import PluginHost
    from korpha.skills.registry import SkillRegistry
    from korpha.heartbeats import HandlerRegistry

    host = PluginHost(
        plugin_name="obs",
        permissions=frozenset({"lifecycle_hooks"}),
        skill_registry=SkillRegistry(),
        handler_registry=HandlerRegistry(),
    )

    async def _h(_e): pass
    with pytest.raises(ValueError, match="unknown hook kind"):
        host.add_lifecycle_hook("not_a_real_kind", _h)


# ---- Event dataclass invariants ----


# ---- transform_llm_output ----


@pytest.mark.asyncio
async def test_transform_llm_output_chains_listeners() -> None:
    """Two listeners — first uppercases, second appends a suffix.
    Final output reflects both transforms."""
    from korpha.plugins.hooks import (
        TransformLlmOutputEvent, hook_registry,
    )

    hook_registry.clear()

    async def _upper(evt):
        return evt.text.upper()

    async def _suffix(evt):
        return evt.text + " [scanned]"

    hook_registry.register(HookKind.TRANSFORM_LLM_OUTPUT, _upper, plugin_name="a")
    hook_registry.register(HookKind.TRANSFORM_LLM_OUTPUT, _suffix, plugin_name="b")

    out = await hook_registry.dispatch_transform(
        HookKind.TRANSFORM_LLM_OUTPUT,
        text="hello world",
        event_factory=lambda current: TransformLlmOutputEvent(text=current),
    )
    assert out == "HELLO WORLD [scanned]"
    hook_registry.clear()


@pytest.mark.asyncio
async def test_transform_llm_output_none_means_passthrough() -> None:
    """A listener returning None should NOT drop the text — that's
    the pre_gateway_dispatch semantic, not transform_llm_output."""
    from korpha.plugins.hooks import (
        TransformLlmOutputEvent, hook_registry,
    )

    hook_registry.clear()

    async def _passthru(evt):
        return None  # explicit "no change"

    async def _suffix(evt):
        return evt.text + "!"

    hook_registry.register(
        HookKind.TRANSFORM_LLM_OUTPUT, _passthru, plugin_name="a",
    )
    hook_registry.register(
        HookKind.TRANSFORM_LLM_OUTPUT, _suffix, plugin_name="b",
    )

    out = await hook_registry.dispatch_transform(
        HookKind.TRANSFORM_LLM_OUTPUT,
        text="hi",
        event_factory=lambda current: TransformLlmOutputEvent(text=current),
    )
    assert out == "hi!"
    hook_registry.clear()


@pytest.mark.asyncio
async def test_transform_llm_output_no_listeners_returns_input() -> None:
    """No listeners registered → input passes through unchanged."""
    from korpha.plugins.hooks import (
        TransformLlmOutputEvent, hook_registry,
    )

    hook_registry.clear()
    out = await hook_registry.dispatch_transform(
        HookKind.TRANSFORM_LLM_OUTPUT,
        text="unchanged",
        event_factory=lambda current: TransformLlmOutputEvent(text=current),
    )
    assert out == "unchanged"


@pytest.mark.asyncio
async def test_transform_llm_output_listener_exception_passes_through() -> None:
    """Flaky transform raises — input passes unchanged for that
    step, downstream listeners still run."""
    from korpha.plugins.hooks import (
        TransformLlmOutputEvent, hook_registry,
    )

    hook_registry.clear()

    async def _boom(evt):
        raise RuntimeError("plugin bug")

    async def _suffix(evt):
        return evt.text + "!"

    hook_registry.register(
        HookKind.TRANSFORM_LLM_OUTPUT, _boom, plugin_name="bad",
    )
    hook_registry.register(
        HookKind.TRANSFORM_LLM_OUTPUT, _suffix, plugin_name="ok",
    )

    out = await hook_registry.dispatch_transform(
        HookKind.TRANSFORM_LLM_OUTPUT,
        text="hi",
        event_factory=lambda current: TransformLlmOutputEvent(text=current),
    )
    assert out == "hi!"
    hook_registry.clear()


@pytest.mark.asyncio
async def test_transform_llm_output_non_string_return_logged() -> None:
    """A buggy plugin returning a non-string — log + ignore that
    step."""
    from korpha.plugins.hooks import (
        TransformLlmOutputEvent, hook_registry,
    )

    hook_registry.clear()

    async def _bad_return(evt):
        return 42  # not a string

    async def _suffix(evt):
        return evt.text + "!"

    hook_registry.register(
        HookKind.TRANSFORM_LLM_OUTPUT, _bad_return, plugin_name="bad",
    )
    hook_registry.register(
        HookKind.TRANSFORM_LLM_OUTPUT, _suffix, plugin_name="ok",
    )

    out = await hook_registry.dispatch_transform(
        HookKind.TRANSFORM_LLM_OUTPUT,
        text="hi",
        event_factory=lambda current: TransformLlmOutputEvent(text=current),
    )
    assert out == "hi!"
    hook_registry.clear()


# ---- pre_gateway_dispatch ----


@pytest.mark.asyncio
async def test_pre_gateway_dispatch_chains_rewrites() -> None:
    from uuid import uuid4

    from korpha.plugins.hooks import (
        PreGatewayDispatchEvent, hook_registry,
    )

    hook_registry.clear()
    biz, founder = uuid4(), uuid4()

    async def _trim(evt):
        return evt.text.strip()

    async def _prefix(evt):
        return f"[user] {evt.text}"

    hook_registry.register(
        HookKind.PRE_GATEWAY_DISPATCH, _trim, plugin_name="t",
    )
    hook_registry.register(
        HookKind.PRE_GATEWAY_DISPATCH, _prefix, plugin_name="p",
    )

    out = await hook_registry.dispatch_transform(
        HookKind.PRE_GATEWAY_DISPATCH,
        text="   hello   ",
        event_factory=lambda current: PreGatewayDispatchEvent(
            text=current,
            business_id=biz, founder_id=founder, channel="web",
        ),
    )
    assert out == "[user] hello"
    hook_registry.clear()


@pytest.mark.asyncio
async def test_pre_gateway_dispatch_none_drops_message() -> None:
    """Spam filter returns None → downstream listeners + the agent
    never see this message. The dispatcher returns None upward
    so the caller knows to skip processing."""
    from uuid import uuid4

    from korpha.plugins.hooks import (
        PreGatewayDispatchEvent, hook_registry,
    )

    hook_registry.clear()
    biz, founder = uuid4(), uuid4()

    seen_after: list[str] = []

    async def _spam_filter(evt):
        if "buy now" in evt.text.lower():
            return None  # drop
        return evt.text

    async def _later(evt):
        seen_after.append(evt.text)
        return evt.text

    hook_registry.register(
        HookKind.PRE_GATEWAY_DISPATCH, _spam_filter, plugin_name="filter",
    )
    hook_registry.register(
        HookKind.PRE_GATEWAY_DISPATCH, _later, plugin_name="logger",
    )

    out = await hook_registry.dispatch_transform(
        HookKind.PRE_GATEWAY_DISPATCH,
        text="BUY NOW for cheap",
        event_factory=lambda current: PreGatewayDispatchEvent(
            text=current,
            business_id=biz, founder_id=founder, channel="telegram",
        ),
    )
    assert out is None  # dropped — caller skips
    assert seen_after == []  # downstream NOT called
    hook_registry.clear()


@pytest.mark.asyncio
async def test_pre_gateway_dispatch_normal_message_passes() -> None:
    """Non-spam text — listeners chain, final text returned."""
    from uuid import uuid4

    from korpha.plugins.hooks import (
        PreGatewayDispatchEvent, hook_registry,
    )

    hook_registry.clear()

    async def _passthru(evt):
        return evt.text

    hook_registry.register(
        HookKind.PRE_GATEWAY_DISPATCH, _passthru, plugin_name="a",
    )

    out = await hook_registry.dispatch_transform(
        HookKind.PRE_GATEWAY_DISPATCH,
        text="What should I work on?",
        event_factory=lambda current: PreGatewayDispatchEvent(
            text=current,
            business_id=uuid4(), founder_id=uuid4(), channel="web",
        ),
    )
    assert out == "What should I work on?"
    hook_registry.clear()


# ---- PluginHost wiring still works for new kinds ----


def test_plugin_host_registers_new_hook_kinds() -> None:
    from korpha.heartbeats import HandlerRegistry
    from korpha.plugins.host import PluginHost
    from korpha.plugins.hooks import hook_registry
    from korpha.skills.registry import SkillRegistry

    hook_registry.clear()
    host = PluginHost(
        plugin_name="redactor",
        permissions=frozenset({"lifecycle_hooks"}),
        skill_registry=SkillRegistry(),
        handler_registry=HandlerRegistry(),
    )

    async def _h(_e):
        return None

    host.add_lifecycle_hook("transform_llm_output", _h)
    host.add_lifecycle_hook("pre_gateway_dispatch", _h)
    assert hook_registry.has(HookKind.TRANSFORM_LLM_OUTPUT)
    assert hook_registry.has(HookKind.PRE_GATEWAY_DISPATCH)
    hook_registry.clear()


def test_post_event_succeeded_property() -> None:
    e_ok = PostSkillCallEvent(
        skill_name="x", args={}, duration_seconds=0.1, error=None,
    )
    assert e_ok.succeeded is True
    e_err = PostSkillCallEvent(
        skill_name="x", args={}, duration_seconds=0.1,
        error=RuntimeError("x"),
    )
    assert e_err.succeeded is False
