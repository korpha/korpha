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
    SESSION_FINALIZE = "session_finalize"
    """Fires after SESSION_END, once all in-flight work has flushed.
    Distinct from SESSION_END because END fires immediately on close
    intent while FINALIZE waits for the trailing work (last LLM call,
    DB writes, blob uploads). Use this when you need 'the session is
    truly done' semantics for archival / summary generation."""

    SESSION_RESET = "session_reset"
    """Fires when a session is reset to its initial state (e.g. the
    founder clicks 'New chat' or runs /reset). Distinct from
    SESSION_END because the underlying thread persists — listeners
    that cleared context should re-prime."""

    TRANSFORM_LLM_OUTPUT = "transform_llm_output"
    """Fires after the agent's LLM response, before persist/send.
    Plugins return the (possibly-mutated) text. Use cases: redact
    secrets, enforce tone-of-voice, compress oversize output. Each
    listener receives the output of the previous one — they
    compose like middleware."""

    TRANSFORM_TERMINAL_OUTPUT = "transform_terminal_output"
    """Fires before output is rendered to a terminal/TUI surface.
    Plugins return the (possibly-mutated) text. Use cases:
    ANSI-color injection, hyperlink terminalize, markdown
    pre-render. Composes like TRANSFORM_LLM_OUTPUT."""

    PRE_LLM_CALL = "pre_llm_call"
    """Fires immediately before an LLM request goes out, with the
    full request payload (messages, tier, model, max_tokens).
    Plugins observe — they can log / count / sample but cannot
    mutate the request. For mutation use PRE_GATEWAY_DISPATCH (on
    the user-text path) or TRANSFORM_LLM_OUTPUT (on the response
    path)."""

    POST_LLM_CALL = "post_llm_call"
    """Fires after an LLM response lands (success or error),
    BEFORE TRANSFORM_LLM_OUTPUT. Carries cost + token counts +
    duration so observability plugins can ship metrics to
    Prometheus / Langfuse / etc."""

    SUBAGENT_STOP = "subagent_stop"
    """Fires when a subagent (Director / Worker / VP attempt)
    finishes — success, failure, blocked, all paths. Lets a
    coordinator plugin (e.g. progress dashboard) pulse on each
    sub-task completion without instrumenting every code path."""

    PRE_APPROVAL_REQUEST = "pre_approval_request"
    """Fires BEFORE a dangerous operation prompts the founder for
    approval. Observer-only — plugins can log/notify (e.g. ping
    Telegram so the founder sees a pending approval even if not
    in the dashboard) but cannot veto. Use PRE_SKILL_CALL to
    block before approval is reached."""

    POST_APPROVAL_RESPONSE = "post_approval_response"
    """Fires AFTER the founder responds to an approval prompt
    (approve / reject / timeout / always / once). Lets plugins
    track approval-fatigue, sample audit trail, escalate
    rejects."""

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


@dataclass(frozen=True)
class PreLlmCallEvent:
    """Payload for ``pre_llm_call``. Read-only observation event
    fired before an inference call. Includes the full message list
    so token-counting / safety plugins can inspect inputs."""

    model: str
    tier: str
    messages: list[dict[str, Any]]
    max_tokens: int | None
    business_id: UUID | None = None
    founder_id: UUID | None = None
    invoking_agent_role_id: UUID | None = None


@dataclass(frozen=True)
class PostLlmCallEvent:
    """Payload for ``post_llm_call``. Fires after inference returns
    (or errors). Carries cost + token + duration so observability
    plugins ship clean metrics without re-deriving from Cost rows."""

    model: str
    tier: str
    duration_seconds: float
    input_tokens: int
    output_tokens: int
    cost_usd: float
    business_id: UUID | None = None
    founder_id: UUID | None = None
    invoking_agent_role_id: UUID | None = None
    error: BaseException | None = None
    """``None`` on success."""


@dataclass(frozen=True)
class TransformTerminalOutputEvent:
    """Payload for ``transform_terminal_output``. Composes the same
    way as TRANSFORM_LLM_OUTPUT. Listeners may return mutated text
    or None for no-op."""

    text: str
    surface: str = "terminal"
    """Where the output is being rendered: ``terminal`` / ``tui`` /
    ``cli`` / ``stdout``. Lets ANSI / color plugins decide."""


@dataclass(frozen=True)
class SubagentStopEvent:
    """Payload for ``subagent_stop``. Fired when a Director / Worker
    / VP attempt finishes — success, blocked, or error. Lets
    coordinator plugins (progress dashboard, channel notifications)
    react to per-attempt completion without instrumenting every
    role."""

    business_id: UUID
    agent_role_id: UUID
    role_type: str
    """'director' / 'worker' / 'vp' / 'ceo' etc."""

    status: str
    """'shipped' / 'blocked' / 'failed' / 'cancelled'."""

    task_summary: str
    """One-line description of what the subagent was working on."""

    duration_seconds: float
    kanban_card_id: UUID | None = None
    error: BaseException | None = None


@dataclass(frozen=True)
class PreApprovalRequestEvent:
    """Payload for ``pre_approval_request``. Observer-only. Fired
    when a dangerous op needs founder approval — useful for
    pinging the founder out-of-band (e.g. Telegram) so they don't
    miss a pending approval."""

    command: str
    """Human-readable description of what's being requested."""

    description: str
    pattern_key: str
    """Approval-policy key (e.g. 'shell_exec', 'spend_above_50')
    so policy plugins can correlate with their rule book."""

    surface: str
    """'cli' / 'gateway' / 'tui' / 'dashboard'."""

    business_id: UUID | None = None
    founder_id: UUID | None = None
    session_key: str | None = None


@dataclass(frozen=True)
class PostApprovalResponseEvent:
    """Payload for ``post_approval_response``. Observer-only.
    Mirrors PreApprovalRequestEvent plus the founder's choice."""

    command: str
    description: str
    pattern_key: str
    surface: str
    choice: str
    """'once' / 'session' / 'always' / 'deny' / 'timeout'."""

    business_id: UUID | None = None
    founder_id: UUID | None = None
    session_key: str | None = None


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
        # TRANSFORM_TERMINAL_OUTPUT shares the same composition
        # semantics as TRANSFORM_LLM_OUTPUT — None = no-op (keep
        # current). Never propagates None to the caller.
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
    "HookFn",
    "HookKind",
    "HookRegistry",
    "PostApprovalResponseEvent",
    "PostLlmCallEvent",
    "PostSkillCallEvent",
    "PreApprovalRequestEvent",
    "PreGatewayDispatchEvent",
    "PreLlmCallEvent",
    "PreSkillCallEvent",
    "SessionEvent",
    "SubagentStopEvent",
    "TransformFn",
    "TransformLlmOutputEvent",
    "TransformTerminalOutputEvent",
    "WorkerHiredEvent",
    "hook_registry",
]
