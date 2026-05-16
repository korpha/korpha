"""Bundled observability plugin — in-process Prometheus metrics."""
from __future__ import annotations

import logging
from collections import Counter
from threading import Lock

logger = logging.getLogger(__name__)


# In-process counters. Reset on process restart — for durable storage
# we already have the Cost + Activity tables. This is for live
# scraping by Prometheus / Grafana / Loki.
_lock = Lock()
_counters: Counter[str] = Counter()
_histograms: dict[str, list[float]] = {}


def _bump(label: str, value: float = 1.0) -> None:
    with _lock:
        _counters[label] += value


def _observe(label: str, value: float) -> None:
    with _lock:
        _histograms.setdefault(label, []).append(value)


def _render_prometheus() -> str:
    """Render current counters + histograms in Prometheus text format
    (compatible with prometheus_client.exposition.generate_latest)."""
    lines: list[str] = []
    with _lock:
        for name, value in sorted(_counters.items()):
            lines.append(f"# TYPE korpha_{name} counter")
            lines.append(f"korpha_{name} {value}")
        for name, values in sorted(_histograms.items()):
            if not values:
                continue
            lines.append(f"# TYPE korpha_{name} summary")
            lines.append(f"korpha_{name}_count {len(values)}")
            lines.append(f"korpha_{name}_sum {sum(values)}")
            # Min / max / avg as gauges so Grafana can plot without
            # configuring a histogram backend.
            lines.append(f"korpha_{name}_min {min(values)}")
            lines.append(f"korpha_{name}_max {max(values)}")
            lines.append(f"korpha_{name}_avg {sum(values) / len(values)}")
    return "\n".join(lines) + "\n"


def render_prometheus() -> str:
    """Public entry point for /metrics. The dashboard route imports
    this directly so the bundled plugin can be queried even when no
    plugin host is around (tests, one-shot CLI)."""
    return _render_prometheus()


async def _on_post_skill_call(event: object) -> None:
    """Tick counters for every skill invocation."""
    succeeded = getattr(event, "succeeded", True)
    skill_name = getattr(event, "skill_name", "unknown")
    safe_name = skill_name.replace(".", "_").replace("-", "_")
    _bump(f"skill_calls_total{{name=\"{skill_name}\"}}")
    if not succeeded:
        _bump(f"skill_errors_total{{name=\"{skill_name}\"}}")
    duration = getattr(event, "duration_seconds", None)
    if isinstance(duration, (int, float)) and duration >= 0:
        _observe(f"skill_duration_seconds{{name=\"{skill_name}\"}}", float(duration))
    _ = safe_name  # reserved for label-prefixed counter naming if needed


async def _on_post_llm_call(event: object) -> None:
    """Tick counters for every inference call."""
    tier = getattr(event, "tier", "unknown")
    model = getattr(event, "model", "unknown")
    duration = getattr(event, "duration_seconds", None)
    cost = getattr(event, "cost_usd", None)
    in_tok = getattr(event, "input_tokens", 0)
    out_tok = getattr(event, "output_tokens", 0)
    error = getattr(event, "error", None)

    _bump(f"llm_calls_total{{tier=\"{tier}\",model=\"{model}\"}}")
    if error is not None:
        _bump(f"llm_errors_total{{tier=\"{tier}\",model=\"{model}\"}}")
    if isinstance(duration, (int, float)):
        _observe(
            f"llm_duration_seconds{{tier=\"{tier}\"}}", float(duration),
        )
    if isinstance(cost, (int, float)):
        _bump(
            f"llm_cost_usd_total{{tier=\"{tier}\"}}", float(cost),
        )
    if isinstance(in_tok, int):
        _bump(
            f"llm_input_tokens_total{{tier=\"{tier}\"}}", float(in_tok),
        )
    if isinstance(out_tok, int):
        _bump(
            f"llm_output_tokens_total{{tier=\"{tier}\"}}", float(out_tok),
        )


async def _on_session_event(event: object) -> None:
    channel = getattr(event, "channel", "unknown")
    _bump(f"sessions_started_total{{channel=\"{channel}\"}}")


def register(host: object) -> None:
    """Plugin entry point — wires the hook listeners into the global
    registry. The host arg is the :class:`PluginHost` from the loader
    (we don't need any of its capability-gated methods because we only
    subscribe to events; the registry is process-wide)."""
    from korpha.plugins.hooks import HookKind, hook_registry

    hook_registry.register(
        HookKind.POST_SKILL_CALL, _on_post_skill_call,
        plugin_name="observability",
    )
    hook_registry.register(
        HookKind.POST_LLM_CALL, _on_post_llm_call,
        plugin_name="observability",
    )
    hook_registry.register(
        HookKind.SESSION_START, _on_session_event,
        plugin_name="observability",
    )
    logger.info("observability plugin: registered 3 hook listeners")


__all__ = ["register", "render_prometheus"]
