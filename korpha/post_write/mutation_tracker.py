"""Track file mutations across an agent turn + render a summary footer.

Usage::

    with FileMutationTracker() as tracker:
        # ... agent runs, calls Write/Edit tools ...
        for path in files_written:
            tracker.observe_write(path, before_text, after_text)
    footer = render_mutation_footer(tracker.mutations)
    # → appended to the turn's output

The cheap value: when the model claims "I added function foo" but
the diff didn't apply, the footer shows ``- 0 files modified`` and
the model self-corrects on the next turn.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self


@dataclass(frozen=True)
class FileMutation:
    """One observed file change."""

    path: str
    """Absolute or repo-relative path."""

    kind: str
    """One of 'created', 'modified', 'deleted'."""

    lines_before: int
    lines_after: int
    bytes_before: int
    bytes_after: int

    sha_before: str | None = None
    """Hex sha256 of pre-edit content. None when created."""

    sha_after: str | None = None
    """Hex sha256 of post-edit content. None when deleted."""

    @property
    def lines_delta(self) -> int:
        return self.lines_after - self.lines_before

    @property
    def changed(self) -> bool:
        """True if the file actually changed. False when a Write/Edit
        rewrote the file with identical content (which happens more
        often than you'd think — model "fixes" something that was
        already correct)."""
        return self.sha_before != self.sha_after


@dataclass
class FileMutationTracker:
    """Context manager + observation log for one agent turn."""

    mutations: list[FileMutation] = field(default_factory=list)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def observe_write(
        self,
        path: str | Path,
        before_text: str | None,
        after_text: str | None,
    ) -> FileMutation:
        """Record a single file write. ``before_text=None`` means the
        file didn't exist before (new file); ``after_text=None`` means
        it was deleted."""
        path_str = str(path)
        if before_text is None and after_text is None:
            kind = "deleted"  # unusual — both None means stub call
        elif before_text is None:
            kind = "created"
        elif after_text is None:
            kind = "deleted"
        else:
            kind = "modified"

        mutation = FileMutation(
            path=path_str,
            kind=kind,
            lines_before=(
                before_text.count("\n") + 1 if before_text else 0
            ),
            lines_after=(
                after_text.count("\n") + 1 if after_text else 0
            ),
            bytes_before=len(before_text or ""),
            bytes_after=len(after_text or ""),
            sha_before=_sha256(before_text) if before_text else None,
            sha_after=_sha256(after_text) if after_text else None,
        )
        self.mutations.append(mutation)
        return mutation


def render_mutation_footer(
    mutations: list[FileMutation],
    *,
    include_unchanged: bool = False,
) -> str:
    """Format a compact summary the agent sees as a system message.

    Empty when no mutations occurred (footer hidden — no noise on
    research-only turns). ``include_unchanged=True`` shows files
    that were rewritten with identical content (useful for catching
    "I 'fixed' it" hallucinations)."""
    if not mutations:
        return ""

    visible = [
        m for m in mutations
        if include_unchanged or m.changed or m.kind in ("created", "deleted")
    ]
    if not visible and not include_unchanged:
        # All mutations were no-op rewrites — show a brief warning.
        return (
            f"⚠ file-mutation: {len(mutations)} write(s) called but "
            f"none changed file content (model may be claiming changes "
            f"that didn't land)"
        )

    lines = [f"file-mutation: {len(visible)} file(s)"]
    for m in visible:
        if m.kind == "created":
            badge = "+"
            change = f"+{m.lines_after} lines"
        elif m.kind == "deleted":
            badge = "-"
            change = f"deleted ({m.lines_before} lines)"
        else:
            badge = "~" if m.changed else "="
            delta = m.lines_delta
            if delta > 0:
                change = f"+{delta} lines"
            elif delta < 0:
                change = f"{delta} lines"
            else:
                # Same line count but content changed (rewrite).
                change = (
                    "rewrite" if m.changed else "no-op (content identical)"
                )
        lines.append(f"  {badge} {m.path} ({change})")
    return "\n".join(lines)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = [
    "FileMutation",
    "FileMutationTracker",
    "render_mutation_footer",
]
