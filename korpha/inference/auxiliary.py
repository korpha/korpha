"""Per-task model overrides for background / auxiliary LLM tasks.

The CEO chat needs Pro-tier reasoning. Title generation, thread
summarization, COS triage, curator-style decisions — these don't.
Pinning each background task to a cheaper Workhorse model cuts
cost without affecting the founder-facing experience.

Config: a single YAML file at ``~/.korpha/auxiliary.yaml``:

    tasks:
      summarize:    workhorse
      cos-triage:   workhorse
      director-cto: pro
      ceo-:         pro       # everything starting with 'ceo-'

Match is **longest prefix wins** against the request's
``session_key`` — so ``ceo-handle-<id>`` matches the ``ceo-`` rule
unless a more specific prefix is configured.

Empty config / missing file = passthrough (use whatever tier the
caller already picked). Lets us ship the contract today and let
the founder dial it in when their bill warrants the optimization.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from korpha.audit.model import InferenceTier

logger = logging.getLogger(__name__)


@dataclass
class AuxiliaryConfig:
    """Loaded per-task tier overrides. Use
    :func:`load_auxiliary_config` rather than constructing directly.

    Pattern matching is "longest-prefix wins" so a config like::

        tasks:
          ceo-:         pro
          ceo-handle-:  consultant

    routes ``ceo-handle-<id>`` to ``consultant`` even though
    ``ceo-`` would also match — it's the longer prefix.
    """

    tier_overrides: dict[str, InferenceTier] = field(default_factory=dict)
    """Mapping of session_key prefix → tier."""

    def resolve_tier(
        self,
        session_key: str | None,
        default: InferenceTier,
    ) -> InferenceTier:
        """Return the override for ``session_key`` if any, else
        ``default`` unchanged."""
        if not session_key or not self.tier_overrides:
            return default
        # Longest prefix wins — sort overrides by length desc.
        best: tuple[str, InferenceTier] | None = None
        for prefix, tier in self.tier_overrides.items():
            if not session_key.startswith(prefix):
                continue
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, tier)
        return best[1] if best is not None else default


def _config_path() -> Path:
    base = os.environ.get("KORPHA_DATA_DIR")
    return (
        (Path(base) / "auxiliary.yaml") if base
        else (Path.home() / ".korpha" / "auxiliary.yaml")
    )


_cached: AuxiliaryConfig | None = None


def load_auxiliary_config(
    *, force_refresh: bool = False, path: Path | None = None,
) -> AuxiliaryConfig:
    """Load + cache the config. Set ``force_refresh=True`` to drop
    the cache (tests / after editing the file).

    Returns an empty config (passthrough behavior) when:
      - The file doesn't exist
      - YAML parses but has no ``tasks`` mapping
      - YAML is malformed (logs a warning, doesn't raise)
    """
    global _cached
    if _cached is not None and not force_refresh:
        return _cached

    target = path if path is not None else _config_path()
    if not target.exists():
        _cached = AuxiliaryConfig()
        return _cached
    try:
        import yaml as _yaml
        body = _yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("auxiliary: parse %s failed: %s", target, exc)
        _cached = AuxiliaryConfig()
        return _cached
    tasks = body.get("tasks") if isinstance(body, dict) else None
    overrides: dict[str, InferenceTier] = {}
    if isinstance(tasks, dict):
        for raw_prefix, raw_tier in tasks.items():
            try:
                tier = InferenceTier(str(raw_tier).strip().lower())
            except ValueError:
                logger.warning(
                    "auxiliary: unknown tier %r for prefix %r; skipping",
                    raw_tier, raw_prefix,
                )
                continue
            overrides[str(raw_prefix)] = tier
    _cached = AuxiliaryConfig(tier_overrides=overrides)
    return _cached


def invalidate_cache() -> None:
    """Drop the in-process cache. Tests call this between cases."""
    global _cached
    _cached = None


__all__ = [
    "AuxiliaryConfig",
    "invalidate_cache",
    "load_auxiliary_config",
]
