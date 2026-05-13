"""Plugin lifecycle hooks — observability + policy gates without
patching core.

Plugins register callbacks for moments in the agent loop:

  - ``pre_skill_call``  — every skill invocation, before run()
  - ``post_skill_call`` — every skill invocation, after run() (both
                          success + failure paths)
  - ``session_start``   — a new founder chat session opens
  - ``session_end``     — a session closes (clean exit or interrupt)

Use cases this enables (without touching core):
  - Telemetry: Langfuse / PostHog plugins capture every skill call
  - Policy: an enterprise plugin denies skills against
    a per-business allowlist
  - Audit enrichment: write extra rows for compliance reporting
  - Caching: short-circuit identical skill calls within a session

Adapted from Hermes' plugin lifecycle pattern in
``hermes_cli/plugins.py``. Kept small intentionally — the four
hook kinds cover 95% of what observability plugins need; more
granular hooks (pre_inference, post_inference, etc.) get added
when a real consumer wants them.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


class HookKind(StrEnum):
    """Lifecycle moments plugins can subscribe to."""

    PRE_SKILL_CALL = "pre_skill_call"
    POST_SKILL_CALL = "post_skill_call"
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    TRANSFORM_LLM_OUTPUT = "transform_llm_output"
    """Fires after the agent's LLM response, before persist/send.
    Plugins return the (possibly-mutated) text. Use cases: redact
    secrets, enforce tone-of-voice, compress oversize output. Each
    listener receives the output of the previous one — they
    compose like middleware. From Hermes v0.13 (audit recommended)."""

    PRE_GATEWAY_DISPATCH = "pre_gateway_dispatch"
    """Fires on every inbound founder message BEFORE the agent
    sees it. Plugins return the (possibly-mutated) message text or
    None to skip the message entirely. Use cases: translate to
    English before routing, canonicalize commands, enrich with
    context, drop spam. Each listener receives the output of the
    previous; returning None breaks the chain (subsequent
    listeners + the agent don't see it). From Hermes v0.13."""

    WORKER_HIRED = "worker_hired"
    """Fires after a worker is hired (via hr.hire_worker skill /
    CLI / dashboard form). Plugins can use it to push a "hire
    confirmed, starting work" notification to the founder's
    channel, ping a Slack room, write to a CRM, etc. Each
    listener gets a WorkerHiredEvent; return value is ignored."""


@dataclass(frozen=True)
class PreSkillCallEvent:
    """Payload for ``pre_skill_call`` hooks. Read-only — hooks can
    inspect but not mutate the skill or args (would create
    spooky-action-at-distance bugs)."""

    skill_name: str
    args: dict[str, Any]
    business_id: UUID | None = None
    founder_id: UUID | None = None
    invoking_agent_role_id: UUID | None = None


@dataclass(frozen=True)
class PostSkillCallEvent:
    """Payload for ``post_skill_call`` hooks. Includes both success
    + error paths — read ``error`` first to decide which fields are
    meaningful."""

    skill_name: str
    args: dict[str, Any]
    duration_seconds: float
    business_id: UUID | None = None
    founder_id: UUID | None = None
    invoking_agent_role_id: UUID | None = None
    result: Any = None
    """``SkillResult`` on success, ``None`` on error."""

    error: BaseException | None = None
    """``None`` on success."""

    @property
    def succeeded(self) -> bool:
        return self.error is None


@dataclass(frozen=True)
class SessionEvent:
    """Payload for ``session_start`` / ``session_end``. Channel
    identifies where the session lives (web / telegram / tui /
    cli)."""

    business_id: UUID
    founder_id: UUID
    channel: str
    thread_id: UUID | None = None


@dataclass(frozen=True)
class TransformLlmOutputEvent:
    """Payload for ``transform_llm_output``. Listeners get the
    most-recent LLM-produced text + context; they return either
    the (possibly-mutated) text or None to leave it unchanged.
    Listeners compose like middleware — each sees the output of
    the previous one."""

    text: str
    """Current text. The dispatcher updates this between listeners."""

    business_id: UUID | None = None
    founder_id: UUID | None = None
    thread_id: UUID | None = None
    role: str = "assistant"
    """Role this output came from — usually 'assistant', sometimes
    'tool' for skill-result text."""


@dataclass(frozen=True)
class WorkerHiredEvent:
    """Payload for ``worker_hired``. Plugins receive the hired
    role + the source string (CLI / skill / dashboard) so they
    can produce contextual notifications. Read-only — listeners
    don't mutate the role."""

    business_id: UUID
    founder_id: UUID | None
    agent_role_id: UUID
    title: str
    specialty: str | None
    role_type: str
    """Almost always 'worker', but typed loosely so non-WORKER
    role types (future direct-hires of CTO/CMO/etc.) get the
    same notification surface."""

    source: str
    """Free-form provenance label — 'cli:hire' / 'skill:hr.hire_worker' /
    'dashboard:hire' — so listeners can route different
    notifications to different channels per source."""

    reason: str | None = None


@dataclass(frozen=True)
class PreGatewayDispatchEvent:
    """Payload for ``pre_gateway_dispatch``. Listeners get the
    raw inbound founder message + identity. They return either
    the (possibly-mutated) text or None to drop the message
    entirely (subsequent listeners + the agent never see it)."""

    text: str
    business_id: UUID
    founder_id: UUID
    channel: str
    """Where the message came from: web / telegram / tui / cli."""

    thread_id: UUID | None = None


HookFn = Callable[[Any], Awaitable[None]]
"""Async observation callback. Receives the matching event
dataclass; returns None. Errors raised inside hooks are caught by
the dispatcher and logged — never wedge the agent loop on a
flaky plugin."""

TransformFn = Callable[[Any], Awaitable["str | None"]]
"""Async transform callback. Used by transform_llm_output and
pre_gateway_dispatch. Returns the new text, or None to keep the
input unchanged (transform_llm_output) / skip the message
entirely (pre_gateway_dispatch)."""


@dataclass
class HookRegistry:
    """Per-process hook callbacks, grouped by kind."""

    _hooks: dict[HookKind, list[tuple[str, HookFn]]] = field(
        default_factory=dict,
    )

    def register(
        self,
        kind: HookKind,
        fn: HookFn,
        *,
        plugin_name: str = "",
    ) -> None:
        """Add a hook callback for ``kind``. ``plugin_name`` is used
        in log messages so a misbehaving hook is identifiable."""
        self._hooks.setdefault(kind, []).append((plugin_name, fn))

    def listeners(self, kind: HookKind) -> list[tuple[str, HookFn]]:
        return list(self._hooks.get(kind, ()))

    def has(self, kind: HookKind) -> bool:
        return bool(self._hooks.get(kind))

    async def dispatch(self, kind: HookKind, event: Any) -> None:
        """Fire every callback for ``kind``. Each runs serially in
        registration order; an exception in one is logged + the rest
        continue. We don't parallelize because some plugins
        legitimately depend on side effects from earlier ones (e.g.
        a metrics plugin recording a span that a logging plugin
        annotates)."""
        for plugin_name, fn in self._hooks.get(kind, ()):
            try:
                await fn(event)
            except asyncio.CancelledError:
                # Don't swallow cancellation — caller is shutting down.
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "plugin hook %s.%s failed: %s",
                    plugin_name or "unknown", kind.value, exc,
                )

    async def dispatch_transform(
        self, kind: HookKind, text: str, event_factory,
    ) -> str | None:
        """Chain-dispatch transform hooks (transform_llm_output,
        pre_gateway_dispatch). Each listener gets an event built
        with the CURRENT text. Listeners return:
          - new text → becomes the input for the next listener
          - None → keep current text unchanged (transform_llm_output
            semantics) OR drop the message (pre_gateway_dispatch
            semantics — we propagate None upward)

        Returns the final text, or None if any listener returned
        None AND ``kind == PRE_GATEWAY_DISPATCH`` (skip message).
        For TRANSFORM_LLM_OUTPUT, None always means "no change" —
        we never propagate None to the caller.

        Listener exceptions are caught + logged like dispatch();
        the input passes through unchanged for that step.
        """
        is_skip_propagating = kind == HookKind.PRE_GATEWAY_DISPATCH
        current = text
        for plugin_name, fn in self._hooks.get(kind, ()):
            event = event_factory(current)
            try:
                result = await fn(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "plugin transform hook %s.%s failed: %s",
                    plugin_name or "unknown", kind.value, exc,
                )
                continue
            if result is None:
                if is_skip_propagating:
                    return None  # downstream + agent never see it
                continue  # transform_llm_output: no-op = keep current
            if not isinstance(result, str):
                logger.warning(
                    "plugin transform hook %s.%s returned non-str %r; "
                    "ignoring",
                    plugin_name or "unknown", kind.value, type(result),
                )
                continue
            current = result
        return current

    def clear(self) -> None:
        """Drop everything. Tests use this between runs."""
        self._hooks.clear()


# Process-wide registry. The PluginHost.add_lifecycle_hook helper
# registers into this so plugins don't have to import it directly.
hook_registry = HookRegistry()


__all__ = [
    "PreGatewayDispatchEvent",
    "TransformFn",
    "TransformLlmOutputEvent",
    "HookFn",
    "HookKind",
    "HookRegistry",
    "PostSkillCallEvent",
    "PreSkillCallEvent",
    "SessionEvent",
    "WorkerHiredEvent",
    "hook_registry",
]
