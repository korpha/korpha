"""SharedResource — company-wide infrastructure any unit can use.

Two categories:

  * **Tech infrastructure** (always available in both deployment modes):
    AI models on the GPU mesh (z-image-turbo, Whisper, Kokoro,
    OmniVoice, bg-removal), shared Cloudflare account, domain pool,
    VPS pool, plugin state.

  * **OAuth-authorized CLIs** (local install only):
    Claude Code, Codex CLI, OpenCode, Cursor, Gemini CLI, ACPX, PI.
    Physically one OAuth per machine — they're singletons by hard
    constraint. Excluded from SaaS-mode enumeration; in local mode the
    resolver consults them BEFORE per-unit API keys for Pro tier work.

The shared-resource skills (``image.generate``, ``audio.synthesize``,
etc.) come via plugin registration in PR5 — this module ships the
data model + usage tracking + helpers for plugin authors.
"""
from korpha.shared_resources.model import (
    SharedResource,
    SharedResourceKind,
    SharedResourceUsage,
)
from korpha.shared_resources.oauth_cli import (
    OAuthCliQuotaExhausted,
    detect_installed_oauth_clis,
    find_oauth_cli_for_service,
    record_oauth_call,
)

__all__ = [
    "OAuthCliQuotaExhausted",
    "SharedResource",
    "SharedResourceKind",
    "SharedResourceUsage",
    "detect_installed_oauth_clis",
    "find_oauth_cli_for_service",
    "record_oauth_call",
]
