"""Remote model recommendation catalog with disk cache.

The open-weights model landscape (DeepSeek, Kimi, Qwen, GLM, Llama)
ships new generations monthly. Hardcoded recommendations in
``providers/builtins.py`` mean every release pinning a "use Kimi-K4
for Pro tier instead of K2.6" requires an Korpha release. For
non-technical Mike, "best Pro model right now" should auto-update.

This module fetches a JSON manifest from a docs URL, caches it on
disk (24h TTL), parses + validates the schema, and exposes
``recommended_models(provider_name)`` so the setup wizard can offer
fresh suggestions. Falls back to the hardcoded TierCapability
defaults on any failure — network, parse error, schema mismatch.

Schema (version 1):

    {
      "version": 1,
      "updated_at": "2026-05-07T12:00:00Z",
      "providers": {
        "<profile-name>": {
          "models": {
            "<tier>": {
              "id": "<model-id>",
              "context_length": 128000,
              "note": "free-form display string"
            }
          }
        }
      }
    }

Unknown keys at any level are ignored — extra metadata can be
added without bumping ``version``. Bumping is reserved for
breaking changes (renaming ``providers`` / changing model shape).

Adapted from Hermes' ``hermes_cli/model_catalog.py``. Stripped to
the Korpha surfaces (one canonical schema, generic accessor;
no Hermes-specific OpenRouter / Nous accessors).
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


SUPPORTED_SCHEMA_VERSION = 1
DEFAULT_CATALOG_URL = (
    "https://korpha.dev/catalog/models.json"
)
"""Override per-deploy via ``KORPHA_MODEL_CATALOG_URL``. The default
points at the Korpha docs site; a 404 there is fine — the
hardcoded fallback in ``providers/builtins.py`` keeps everything
working."""

DEFAULT_TTL_SECONDS = 24 * 60 * 60
DEFAULT_FETCH_TIMEOUT = 8.0
_USER_AGENT = "korpha-catalog/1.0"


@dataclass(frozen=True)
class ModelHint:
    """A single recommended model for one (provider, tier) pair."""

    model_id: str
    context_length: int | None = None
    note: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "ModelHint | None":
        """Lenient parser. Returns None if the dict doesn't have at
        least a model id — prevents bad rows from polluting suggestions."""
        if not isinstance(data, dict):
            return None
        mid = str(data.get("id") or data.get("model_id") or "").strip()
        if not mid:
            return None
        ctx_raw = data.get("context_length")
        ctx = int(ctx_raw) if isinstance(ctx_raw, (int, str)) and str(ctx_raw).isdigit() else None
        return cls(
            model_id=mid,
            context_length=ctx,
            note=str(data.get("note") or "").strip(),
        )


# ----------------------- caches --------------------------------------


_in_process_cache: dict | None = None
_in_process_loaded_at: float = 0.0


def _cache_path() -> Path:
    base = os.environ.get("KORPHA_DATA_DIR")
    return (
        (Path(base) / "cache" / "models.json")
        if base
        else (Path.home() / ".korpha" / "cache" / "models.json")
    )


def _catalog_url() -> str:
    return os.environ.get("KORPHA_MODEL_CATALOG_URL") or DEFAULT_CATALOG_URL


def _ttl_seconds() -> int:
    raw = os.environ.get("KORPHA_MODEL_CATALOG_TTL_SECONDS")
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            pass
    return DEFAULT_TTL_SECONDS


# ----------------------- fetch + cache ------------------------------


def _read_disk_cache() -> dict | None:
    path = _cache_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("catalog: ignoring corrupt disk cache %s: %s", path, exc)
        return None


def _write_disk_cache(payload: dict) -> None:
    path = _cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, indent=2), encoding="utf-8",
        )
        tmp.replace(path)
    except OSError as exc:
        logger.debug("catalog: failed to write disk cache: %s", exc)


def _disk_cache_age_seconds() -> float | None:
    path = _cache_path()
    if not path.exists():
        return None
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return None


def _fetch_remote(url: str, *, timeout: float) -> dict | None:
    """Attempt the HTTP GET. Returns None on any failure (network,
    parse, non-200) — caller falls back to disk cache or hardcoded
    defaults."""
    req = urllib.request.Request(
        url, headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                logger.debug("catalog: %s returned %s", url, resp.status)
                return None
            data = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.debug("catalog: fetch %s failed: %s", url, exc)
        return None
    try:
        return json.loads(data)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.debug("catalog: parse %s failed: %s", url, exc)
        return None


def _is_supported(payload: dict) -> bool:
    if not isinstance(payload, dict):
        return False
    version = payload.get("version")
    if isinstance(version, int) and version <= SUPPORTED_SCHEMA_VERSION:
        return True
    # Lenient: missing version → treat as v1 (early manifests may omit).
    return version is None


def get_catalog(
    *,
    force_refresh: bool = False,
    url: str | None = None,
    timeout: float = DEFAULT_FETCH_TIMEOUT,
) -> dict:
    """Return the catalog dict (possibly empty on persistent failure).

    Cache resolution:
      1. In-process cache (cleared via ``invalidate_cache()``)
      2. Disk cache if fresher than TTL
      3. Remote fetch — refresh disk cache on success
      4. Stale disk cache (better than empty)
      5. Empty dict (callers fall back to hardcoded defaults)
    """
    global _in_process_cache, _in_process_loaded_at
    if _in_process_cache is not None and not force_refresh:
        return _in_process_cache

    # Step 2: fresh disk cache wins, no network call needed.
    age = _disk_cache_age_seconds()
    if not force_refresh and age is not None and age < _ttl_seconds():
        cached = _read_disk_cache()
        if cached is not None and _is_supported(cached):
            _in_process_cache = cached
            _in_process_loaded_at = time.time()
            return cached

    # Step 3: remote fetch
    fetched = _fetch_remote(
        url or _catalog_url(), timeout=timeout,
    )
    if fetched is not None and _is_supported(fetched):
        _write_disk_cache(fetched)
        _in_process_cache = fetched
        _in_process_loaded_at = time.time()
        return fetched

    # Step 4: stale disk cache as a backstop
    stale = _read_disk_cache()
    if stale is not None and _is_supported(stale):
        logger.info(
            "catalog: serving stale disk cache (fetch failed); "
            "next refresh will retry",
        )
        _in_process_cache = stale
        _in_process_loaded_at = time.time()
        return stale

    # Step 5: nothing available — caller falls back to hardcoded
    logger.info("catalog: no remote / cache available; using empty catalog")
    _in_process_cache = {}
    return _in_process_cache


def invalidate_cache() -> None:
    """Drop the in-process cache. Disk cache is left alone — next
    ``get_catalog()`` will re-read it. Useful after the user runs
    a manual refresh command."""
    global _in_process_cache, _in_process_loaded_at
    _in_process_cache = None
    _in_process_loaded_at = 0.0


# ----------------------- accessors ----------------------------------


def recommended_models(
    provider_name: str, *, force_refresh: bool = False,
) -> dict[str, ModelHint]:
    """Return the catalog's recommendations for ``provider_name``,
    keyed by tier name (``"pro"``, ``"workhorse"``, etc.).

    Empty dict means "no remote recommendation available" — caller
    should fall back to the profile's hardcoded TierCapability
    defaults (which is the steady-state path when the catalog is
    unreachable).
    """
    catalog = get_catalog(force_refresh=force_refresh)
    providers = catalog.get("providers")
    if not isinstance(providers, dict):
        return {}
    section = providers.get(provider_name)
    if not isinstance(section, dict):
        return {}
    models = section.get("models")
    if not isinstance(models, dict):
        return {}
    out: dict[str, ModelHint] = {}
    for tier_name, tier_data in models.items():
        hint = ModelHint.from_dict(tier_data)
        if hint is not None:
            out[str(tier_name).lower()] = hint
    return out


__all__ = [
    "DEFAULT_CATALOG_URL",
    "DEFAULT_TTL_SECONDS",
    "ModelHint",
    "get_catalog",
    "invalidate_cache",
    "recommended_models",
]
