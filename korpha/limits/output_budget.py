"""Per-result spillover + per-turn budget enforcement.

Inspired by Hermes' ``tools/tool_result_storage.py``, simplified for
Korpha's in-process execution model. Hermes writes to a sandbox
temp dir via ``env.execute()`` (because tools may run inside Docker /
SSH / Modal). Korpha runs everything in the FastAPI process so we
write to a local path directly.
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)


# Markers — the synthesizer / model can recognize already-persisted
# blocks and skip them when the budget check runs.
PERSISTED_OUTPUT_TAG = "<persisted-output>"
PERSISTED_OUTPUT_CLOSING_TAG = "</persisted-output>"

_PERSISTED_RE = re.compile(re.escape(PERSISTED_OUTPUT_TAG))


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if not raw:
        return default
    try:
        val = int(raw)
    except ValueError:
        logger.warning("ignoring non-int %s=%r, using default %d", key, raw, default)
        return default
    if val <= 0:
        logger.warning("ignoring non-positive %s=%d, using default %d", key, val, default)
        return default
    return val


# Resolved at import time — small surface, predictable. Tests that
# need different limits override via the function-arg path instead of
# mutating these.
PERSIST_THRESHOLD_CHARS: int = _env_int(
    "KORPHA_PERSIST_THRESHOLD_CHARS", 16_000,
)
PREVIEW_CHARS: int = _env_int("KORPHA_PREVIEW_CHARS", 4_000)
TURN_BUDGET_CHARS: int = _env_int(
    "KORPHA_TURN_BUDGET_CHARS", 200_000,
)


def _default_storage_dir() -> Path:
    """Where spilled tool results live. Honors KORPHA_DATA_DIR for
    test isolation; falls back to ``~/.korpha``. Created lazily
    by ``persist_if_oversized``."""
    base = os.environ.get("KORPHA_DATA_DIR")
    if base:
        return Path(base) / "tool_results"
    return Path.home() / ".korpha" / "tool_results"


def is_persisted(content: str) -> bool:
    """True iff ``content`` already carries a persisted-output block.
    Used by the budget enforcer to skip already-spilled results."""
    return PERSISTED_OUTPUT_TAG in content


def _generate_preview(content: str, max_chars: int) -> tuple[str, bool]:
    """Trim to ``max_chars`` at the last newline within budget. Returns
    ``(preview, has_more)``. If we can find a newline in the second
    half of the budget, cut there for cleaner output; otherwise hard
    cut so we never blow the cap."""
    if len(content) <= max_chars:
        return content, False
    truncated = content[:max_chars]
    last_nl = truncated.rfind("\n")
    if last_nl > max_chars // 2:
        truncated = truncated[: last_nl + 1]
    return truncated, True


def _format_size(num_chars: int) -> str:
    if num_chars >= 1_048_576:
        return f"{num_chars / 1_048_576:.1f} MB"
    if num_chars >= 1024:
        return f"{num_chars / 1024:.1f} KB"
    return f"{num_chars} chars"


def _wrap_persisted(
    *, preview: str, has_more: bool, original_size: int, file_path: Path,
) -> str:
    head = (
        f"{PERSISTED_OUTPUT_TAG}\n"
        f"Tool result was too large "
        f"({original_size:,} chars / {_format_size(original_size)}).\n"
        f"Full payload saved to: {file_path}\n"
        f"Read the file with read_file(offset, limit) if you need "
        f"more than the preview.\n\n"
        f"Preview (first {len(preview):,} chars):\n"
    )
    tail = "\n..." if has_more else ""
    return f"{head}{preview}{tail}\n{PERSISTED_OUTPUT_CLOSING_TAG}"


def persist_if_oversized(
    content: str,
    *,
    ref_id: str,
    threshold: int | None = None,
    preview_chars: int | None = None,
    storage_dir: Path | None = None,
) -> str:
    """If ``content`` exceeds the threshold, spill it to disk and
    return a ``<persisted-output>`` block with preview + path.
    Otherwise return ``content`` unchanged.

    ``ref_id`` is used as the filename. Caller picks something stable
    so the same logical result spills to the same place — pass the
    skill name + a unique id, e.g. ``"skill-research.scrape-uuid"``.
    Sanitized for filesystem safety.
    """
    threshold = threshold if threshold is not None else PERSIST_THRESHOLD_CHARS
    preview_chars = preview_chars if preview_chars is not None else PREVIEW_CHARS

    if len(content) <= threshold:
        return content
    if is_persisted(content):
        # Already spilled (e.g. nested call); leave alone.
        return content

    base_dir = storage_dir if storage_dir is not None else _default_storage_dir()
    safe_id = _sanitize_ref_id(ref_id)
    target = base_dir / f"{safe_id}.txt"
    try:
        base_dir.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        # Disk full / permissions / read-only fs: fall back to inline
        # truncation rather than letting the LLM call fail. Better to
        # lose the tail of one tool result than to crash the turn.
        logger.warning(
            "persist_if_oversized: write failed for %s (%s); inline truncating",
            target, exc,
        )
        preview, has_more = _generate_preview(content, preview_chars)
        suffix = "\n..." if has_more else ""
        return (
            f"{preview}{suffix}\n\n"
            f"[Truncated: full result was {len(content):,} chars; disk write failed.]"
        )

    preview, has_more = _generate_preview(content, preview_chars)
    return _wrap_persisted(
        preview=preview,
        has_more=has_more,
        original_size=len(content),
        file_path=target,
    )


_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_ref_id(ref_id: str) -> str:
    """Filesystem-safe filename. Strips path separators, control chars,
    anything weird; collapses runs of unsafe characters into a single
    dash. Caps at 100 chars so we never produce a too-long filename."""
    safe = _SAFE_RE.sub("-", ref_id).strip("-") or "tool-result"
    return safe[:100]


def enforce_turn_budget(
    items: list[str],
    *,
    ref_id_prefix: str,
    budget: int | None = None,
    threshold: int | None = None,
    preview_chars: int | None = None,
    storage_dir: Path | None = None,
) -> list[str]:
    """Aggregate-budget enforcement across a turn's tool results.

    Returns a new list where the LARGEST non-already-persisted items
    have been spilled until the aggregate is under budget. Already-
    persisted items are left alone (their preview is already cheap).

    Used by the workforce dispatch summarizer when many directors
    return medium-sized AttemptResults that individually pass the
    threshold but collectively blow the context window.
    """
    budget = budget if budget is not None else TURN_BUDGET_CHARS
    out = list(items)
    sizes = [len(s) for s in out]
    total = sum(sizes)
    if total <= budget:
        return out

    # Sort indices by size descending; persist largest non-persisted
    # first. Tied sizes keep input order (sort is stable).
    order = sorted(
        range(len(out)),
        key=lambda i: sizes[i],
        reverse=True,
    )
    for idx in order:
        if total <= budget:
            break
        if is_persisted(out[idx]):
            continue
        replacement = persist_if_oversized(
            out[idx],
            ref_id=f"{ref_id_prefix}-{idx}",
            # Forcing threshold=0 here: even small items get spilled
            # if we're over budget. They wouldn't normally pass the
            # per-result gate, but the aggregate trumps the per-item
            # call.
            threshold=0,
            preview_chars=preview_chars,
            storage_dir=storage_dir,
        )
        if replacement != out[idx]:
            total = total - sizes[idx] + len(replacement)
            out[idx] = replacement
    return out


def serialize_for_prompt(payload: object) -> str:
    """Pretty-print a skill payload for embedding in an LLM prompt.

    Centralized so callers don't all duplicate ``json.dumps(...,
    indent=2)`` (and so we have one place to change formatting).
    """
    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, indent=2, default=str)
    except TypeError:
        # Non-serializable object — fall back to repr so we never
        # crash the prompt construction.
        return repr(payload)
