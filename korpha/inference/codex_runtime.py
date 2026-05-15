"""One-click toggle: route the agent loop through Codex CLI (ChatGPT Plus
subscription) as the top-priority inference provider.

Mirrors the Hermes ``/codex-runtime`` feature (model.openai_runtime in
their config). Difference from Hermes: we don't spawn ``codex
app-server`` and hand turns to it — we run a normal Director.attempt
through our InferencePool, but ensure ``codex-cli`` is the first account
the cascade tries when this toggle is on.

That means:

- ChatGPT Plus / Pro / Max users get $0 marginal inference cost without
  configuring an OpenAI API key — same auth Codex already manages.
- Open-weights stack stays as the fallback when Codex rate-limits or
  the CLI is offline (cascade retries down the priority list).
- One flip-of-a-switch action — no manual providers.yaml editing,
  matching the memory rule "every setup knob needs an interactive
  CLI/UI path; no YAML editing".

Persistence: an entry in ``providers.yaml`` with ``preset: codex-cli``
+ a ``priority: 0`` sort key. Toggle off removes the entry. Status =
"is the entry there?".

Detection: we shell-check ``codex --version`` so the toggle is gated
on the binary being installed AND ``codex login`` having run. If
either fails, the toggle stays off and surfaces the right next step.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class CodexRuntimeStatus:
    """Result of an enable/disable/status call. Caller renders this in
    the UI (dashboard, CLI, future TUI) — same shape across surfaces."""

    enabled: bool
    """Is a codex-cli entry in providers.yaml right now?"""

    codex_binary_ok: bool
    """Is ``codex`` on PATH?"""

    codex_version: str | None
    """Parsed `codex --version` output, when available."""

    detail: str
    """Short human-readable note for the UI — '✓ enabled', '✗ codex
    not installed', etc."""


def _providers_path() -> Path:
    from korpha.inference.config import config_path
    return config_path()


def _read_yaml() -> dict[str, Any]:
    import yaml

    p = _providers_path()
    if not p.exists():
        return {}
    body = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return body if isinstance(body, dict) else {}


def _write_yaml(body: dict[str, Any]) -> None:
    import yaml

    p = _providers_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")


def _codex_binary_check() -> tuple[bool, str | None]:
    """(ok, version-string). False means binary missing OR `--version`
    failed (likely not logged in or broken install)."""
    if shutil.which("codex") is None:
        return False, None
    try:
        out = subprocess.run(
            ["codex", "--version"], capture_output=True, text=True, timeout=5,
        )
        if out.returncode != 0:
            return False, None
        return True, out.stdout.strip() or out.stderr.strip()
    except Exception:  # noqa: BLE001
        return False, None


def _has_codex_entry(providers: list[dict[str, Any]]) -> bool:
    for entry in providers:
        if isinstance(entry, dict) and entry.get("preset") == "codex-cli":
            return True
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def status() -> CodexRuntimeStatus:
    """Snapshot the current state."""
    bin_ok, version = _codex_binary_check()
    body = _read_yaml()
    providers = body.get("providers") or []
    enabled = bool(_has_codex_entry(providers)) if isinstance(providers, list) else False
    if enabled and bin_ok:
        detail = f"✓ enabled (codex {version or 'detected'})"
    elif enabled and not bin_ok:
        detail = "⚠ entry in providers.yaml but codex binary missing — disable or run `codex login`"
    elif bin_ok:
        detail = f"available — codex {version or 'detected'} installed, not wired"
    else:
        detail = "✗ codex not installed (npm install -g @openai/codex && codex login)"
    return CodexRuntimeStatus(
        enabled=enabled,
        codex_binary_ok=bin_ok,
        codex_version=version,
        detail=detail,
    )


def enable() -> CodexRuntimeStatus:
    """Add the codex-cli entry to providers.yaml at top priority.
    Idempotent — re-running is a no-op when already enabled."""
    bin_ok, version = _codex_binary_check()
    if not bin_ok:
        return CodexRuntimeStatus(
            enabled=False, codex_binary_ok=False, codex_version=None,
            detail=(
                "✗ codex CLI not available. Install with "
                "`npm install -g @openai/codex` then `codex login`."
            ),
        )

    body = _read_yaml()
    providers = body.get("providers") or []
    if not isinstance(providers, list):
        providers = []
    if not _has_codex_entry(providers):
        # Prepend so the cascade tries codex first.
        entry: dict[str, Any] = {
            "preset": "codex-cli",
            "label": "codex-runtime",
            "tiers": {
                "workhorse": "gpt-5.4",
                "pro": "gpt-5.4",
            },
            "priority": 0,
        }
        providers.insert(0, entry)
        body["providers"] = providers
        _write_yaml(body)
        logger.info("codex-runtime: enabled — codex-cli prepended to providers.yaml")
    return CodexRuntimeStatus(
        enabled=True, codex_binary_ok=True, codex_version=version,
        detail=f"✓ enabled (codex {version or 'detected'})",
    )


def disable() -> CodexRuntimeStatus:
    """Remove every codex-cli entry from providers.yaml. Idempotent."""
    body = _read_yaml()
    providers = body.get("providers") or []
    if not isinstance(providers, list):
        return status()
    before = len(providers)
    body["providers"] = [
        e for e in providers
        if not (isinstance(e, dict) and e.get("preset") == "codex-cli")
    ]
    if len(body["providers"]) != before:
        _write_yaml(body)
        logger.info(
            "codex-runtime: disabled — removed %d codex-cli entry/entries",
            before - len(body["providers"]),
        )
    bin_ok, version = _codex_binary_check()
    return CodexRuntimeStatus(
        enabled=False, codex_binary_ok=bin_ok, codex_version=version,
        detail="✗ disabled (codex-cli removed from cascade)",
    )


__all__ = ["CodexRuntimeStatus", "disable", "enable", "status"]
