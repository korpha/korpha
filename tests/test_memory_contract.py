"""Tests for the long-term memory ABC + plugin contract."""
from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from korpha.memory import (
    LongTermMemory,
    MemoryEntry,
    MemoryQuery,
    MemoryRegistry,
    NoopLongTermMemory,
    active_long_term_memory,
    memory_registry,
    set_active_provider,
)


@pytest.fixture(autouse=True)
def _reset_registry():
    memory_registry.reset_to_noop()
    yield
    memory_registry.reset_to_noop()


# ---- NoopLongTermMemory ----


@pytest.mark.asyncio
async def test_noop_add_returns_synthetic_entry() -> None:
    noop = NoopLongTermMemory()
    biz, founder = uuid4(), uuid4()
    entry = await noop.add(
        business_id=biz, founder_id=founder,
        text="remember this", tags=["niche"],
    )
    assert entry.id.startswith("noop-")
    assert entry.text == "remember this"
    assert entry.business_id == biz
    assert entry.founder_id == founder
    assert entry.tags == ("niche",)


@pytest.mark.asyncio
async def test_noop_search_returns_empty() -> None:
    noop = NoopLongTermMemory()
    out = await noop.search(MemoryQuery(
        business_id=uuid4(), founder_id=uuid4(), text="anything",
    ))
    assert out == []


@pytest.mark.asyncio
async def test_noop_forget_returns_false() -> None:
    noop = NoopLongTermMemory()
    ok = await noop.forget(
        business_id=uuid4(), founder_id=uuid4(), memory_id="x",
    )
    assert ok is False


@pytest.mark.asyncio
async def test_noop_close_is_idempotent() -> None:
    noop = NoopLongTermMemory()
    await noop.close()
    await noop.close()


# ---- MemoryRegistry ----


def test_default_active_is_noop() -> None:
    reg = MemoryRegistry()
    assert isinstance(reg.active(), NoopLongTermMemory)


def test_set_active_replaces() -> None:
    reg = MemoryRegistry()

    class _StubMem(NoopLongTermMemory):
        name = "stub"

    stub = _StubMem()
    reg.set_active(stub)
    assert reg.active() is stub


def test_reset_to_noop() -> None:
    reg = MemoryRegistry()

    class _S(NoopLongTermMemory):
        name = "s"

    reg.set_active(_S())
    assert reg.active().name == "s"
    reg.reset_to_noop()
    assert reg.active().name == "noop"


def test_replacing_active_provider_takes_effect() -> None:
    """Two plugins both register a memory provider → the second one
    wins (last-registration semantics). The operator-visible
    warning is a separate concern; here we just verify the
    replacement actually happens."""
    reg = MemoryRegistry()

    class _A(NoopLongTermMemory):
        name = "a"

    class _B(NoopLongTermMemory):
        name = "b"

    reg.set_active(_A(), plugin_name="plugin-a")
    assert reg.active().name == "a"
    reg.set_active(_B(), plugin_name="plugin-b")
    assert reg.active().name == "b"


def test_module_level_helpers_use_singleton() -> None:
    class _S(NoopLongTermMemory):
        name = "module-singleton"

    set_active_provider(_S(), plugin_name="x")
    assert active_long_term_memory().name == "module-singleton"


# ---- ABC enforcement ----


def test_long_term_memory_is_abstract() -> None:
    """Can't instantiate the ABC directly — plugins must subclass."""
    with pytest.raises(TypeError):
        LongTermMemory()  # type: ignore[abstract]


# ---- PluginHost integration ----


def test_plugin_host_requires_long_term_memory_capability() -> None:
    from korpha.heartbeats import HandlerRegistry
    from korpha.plugins.host import PluginHost, PluginPermissionError
    from korpha.skills.registry import SkillRegistry

    host = PluginHost(
        plugin_name="bad",
        permissions=frozenset({"skills"}),
        skill_registry=SkillRegistry(),
        handler_registry=HandlerRegistry(),
    )

    class _Mem(NoopLongTermMemory):
        name = "x"

    with pytest.raises(PluginPermissionError, match="long_term_memory"):
        host.add_long_term_memory(_Mem())


def test_plugin_host_registers_memory_with_capability() -> None:
    from korpha.heartbeats import HandlerRegistry
    from korpha.plugins.host import PluginHost
    from korpha.skills.registry import SkillRegistry

    host = PluginHost(
        plugin_name="mem-plugin",
        permissions=frozenset({"long_term_memory"}),
        skill_registry=SkillRegistry(),
        handler_registry=HandlerRegistry(),
    )

    class _Mem(NoopLongTermMemory):
        name = "registered"

    host.add_long_term_memory(_Mem())
    assert active_long_term_memory().name == "registered"
    assert "registered" in host.contributed_memory_providers


def test_plugin_host_rejects_non_memory_providers() -> None:
    """Defense: a plugin manifest claims long_term_memory but
    registers a non-LongTermMemory object → TypeError."""
    from korpha.heartbeats import HandlerRegistry
    from korpha.plugins.host import PluginHost
    from korpha.skills.registry import SkillRegistry

    host = PluginHost(
        plugin_name="confused",
        permissions=frozenset({"long_term_memory"}),
        skill_registry=SkillRegistry(),
        handler_registry=HandlerRegistry(),
    )
    with pytest.raises(TypeError, match="LongTermMemory"):
        host.add_long_term_memory("not a memory provider")


# ---- MemoryEntry / MemoryQuery shape ----


def test_memory_entry_defaults() -> None:
    e = MemoryEntry(
        id="x", text="t",
        business_id=uuid4(), founder_id=uuid4(),
    )
    assert e.tags == ()
    assert e.score is None
    assert e.metadata == {}


def test_memory_query_defaults() -> None:
    q = MemoryQuery(
        business_id=uuid4(), founder_id=uuid4(), text="hi",
    )
    assert q.limit == 10
    assert q.tags == ()


# ---- end-to-end with a fake provider ----


@pytest.mark.asyncio
async def test_fake_provider_round_trip() -> None:
    """Build a tiny in-memory provider that actually stores +
    retrieves, prove the contract is exercisable."""

    class _DictMem(LongTermMemory):
        name = "dict"

        def __init__(self) -> None:
            self._store: dict[str, MemoryEntry] = {}

        async def add(
            self, *, business_id, founder_id, text,
            tags=(), metadata=None,
        ) -> MemoryEntry:
            mid = f"dict-{len(self._store) + 1}"
            entry = MemoryEntry(
                id=mid, text=text,
                business_id=business_id, founder_id=founder_id,
                tags=tuple(tags), metadata=dict(metadata or {}),
            )
            self._store[mid] = entry
            return entry

        async def search(self, query: MemoryQuery) -> list[MemoryEntry]:
            return [
                e for e in self._store.values()
                if e.business_id == query.business_id
                and e.founder_id == query.founder_id
                and query.text.lower() in e.text.lower()
            ][:query.limit]

        async def forget(
            self, *, business_id, founder_id, memory_id,
        ) -> bool:
            existing = self._store.get(memory_id)
            if existing is None:
                return False
            if (
                existing.business_id != business_id
                or existing.founder_id != founder_id
            ):
                return False
            del self._store[memory_id]
            return True

        async def close(self) -> None:
            self._store.clear()

    biz, founder = uuid4(), uuid4()
    other_biz = uuid4()
    mem = _DictMem()
    a = await mem.add(
        business_id=biz, founder_id=founder,
        text="targeting freelance designers",
    )
    b = await mem.add(
        business_id=biz, founder_id=founder,
        text="Stripe key set up",
    )
    # Different business — must not leak
    await mem.add(
        business_id=other_biz, founder_id=founder,
        text="targeting freelance designers (other biz)",
    )

    hits = await mem.search(MemoryQuery(
        business_id=biz, founder_id=founder, text="freelance",
    ))
    assert len(hits) == 1  # the other_biz one is excluded
    assert hits[0].id == a.id

    # Forget removes
    assert await mem.forget(
        business_id=biz, founder_id=founder, memory_id=a.id,
    ) is True
    after = await mem.search(MemoryQuery(
        business_id=biz, founder_id=founder, text="freelance",
    ))
    assert after == []

    # Multi-tenant safety on forget
    assert await mem.forget(
        business_id=other_biz, founder_id=founder, memory_id=b.id,
    ) is False

    await mem.close()
