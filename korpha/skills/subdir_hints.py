"""Discover ``AGENTS.md`` / ``CLAUDE.md`` / ``.cursorrules`` from
the user's repo as the agent navigates into it.

Why: Codex (and any future file-reading skill) operates in
``ctx.business.workspace_path``. Real user repos commonly carry
per-directory AGENTS.md files that codify their conventions
("imports go in this order", "use trio not asyncio", "tests live
in tests/integration/"). If the prompt we hand Codex doesn't
mention those conventions, the cofounder's commits are visibly
off-pattern — an instant trust-loss for the "actually ships code"
claim. Codex will eventually load AGENTS.md itself when it walks
into a subdir, but mentioning the convention in the *initial*
prompt sets framing earlier and surfaces conflicts up-front.

Adapted from Hermes' ``agent/subdirectory_hints.py``. Simplified:
no security scanner — the file content comes from the founder's
own repo, not an untrusted source. Capped at a max-chars-per-file
limit so a 50KB AGENTS.md doesn't blow the prompt budget.
"""
from __future__ import annotations

import logging
import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


# Filenames we look for. Match Hermes + Codex + Claude + Cursor
# conventions. First match in a directory wins to avoid duplicating
# the same content under two filenames.
_HINT_FILENAMES: tuple[str, ...] = (
    "AGENTS.md", "agents.md",
    "CLAUDE.md", "claude.md",
    ".cursorrules",
)

# Per-file cap. 8K chars ≈ 2K tokens — substantial guidance without
# blowing the prompt.
DEFAULT_MAX_HINT_CHARS = 8_000

# How far up to walk from a discovered file path. Reading
# ``project/src/lib/foo.py`` should still pick up ``project/AGENTS.md``
# even when intermediate dirs have nothing — but we cap the climb so
# we don't walk all the way to ``/`` for paths in deep trees.
DEFAULT_MAX_ANCESTOR_WALK = 5


@dataclass
class SubdirectoryHintTracker:
    """Tracks which directories the agent has visited; loads + returns
    hint-file content the first time each one shows up.

    Stateful so the same hint never gets re-injected into a follow-up
    skill call in the same session — the LLM has already seen it.

    Single-thread / single-loop assumption: if we ever fan this out
    to parallel workers, each gets its own tracker.
    """

    working_dir: Path
    """Anchor for relative-path resolution. Usually
    ``ctx.business.workspace_path`` or the cwd."""

    max_hint_chars: int = DEFAULT_MAX_HINT_CHARS
    max_ancestor_walk: int = DEFAULT_MAX_ANCESTOR_WALK

    assume_root_visited: bool = True
    """When True (the default), the working_dir is pre-marked as
    visited — typical for the long-running agent loop where startup
    context already loaded the root AGENTS.md. Set False for
    one-shot callers (e.g. ``code.ship_via_codex`` building a fresh
    Codex prompt) that need the root hints emitted too."""

    _loaded_dirs: set[Path] = field(default_factory=set, init=False)

    def __post_init__(self) -> None:
        self.working_dir = self.working_dir.expanduser().resolve()
        if self.assume_root_visited:
            self._loaded_dirs.add(self.working_dir)

    def hints_for_paths(self, paths: list[str | Path]) -> str | None:
        """Resolve each path, find directories not yet visited, return
        the concatenated hint content. Returns None when nothing new
        was discovered (so callers can short-circuit appending)."""
        candidates: set[Path] = set()
        for raw in paths:
            self._add_path_candidate(str(raw), candidates)
        if not candidates:
            return None
        sections: list[str] = []
        # Sort by depth (shallowest first) so root-level AGENTS.md
        # comes before per-package overrides. The LLM sees general
        # rules then specific overrides, which is the natural
        # reading order.
        for d in sorted(candidates, key=lambda p: len(p.parts)):
            section = self._load_hints_for_directory(d)
            if section:
                sections.append(section)
        if not sections:
            return None
        return "\n\n".join(sections)

    def hints_for_command(self, cmd: str) -> str | None:
        """Extract path-like tokens from a shell command and load
        hints for their containing dirs. Useful for ``terminal`` /
        ``codex`` invocations where the agent doesn't pass a path
        arg directly."""
        return self.hints_for_paths(_path_tokens(cmd))

    def reset(self) -> None:
        """Clear the visited-dir set. Useful for tests, or when the
        caller wants to force a re-emit (e.g. after the user edits
        an AGENTS.md mid-session)."""
        self._loaded_dirs = (
            {self.working_dir} if self.assume_root_visited else set()
        )

    # ---- internals ----

    def _add_path_candidate(
        self, raw_path: str, candidates: set[Path],
    ) -> None:
        try:
            p = Path(raw_path).expanduser()
            if not p.is_absolute():
                p = self.working_dir / p
            p = p.resolve()
            # If it's a file (or looks like one — has a suffix), use
            # the parent directory.
            if p.suffix or (p.exists() and p.is_file()):
                p = p.parent
            for _ in range(self.max_ancestor_walk):
                if p in self._loaded_dirs:
                    break
                if self._is_valid_subdir(p):
                    candidates.add(p)
                parent = p.parent
                if parent == p:
                    # Filesystem root — stop climbing
                    break
                p = parent
        except (OSError, ValueError) as exc:
            logger.debug("subdir_hints: bad path %r: %s", raw_path, exc)

    def _is_valid_subdir(self, path: Path) -> bool:
        try:
            if not path.is_dir():
                return False
        except OSError:
            return False
        if path in self._loaded_dirs:
            return False
        return True

    def _load_hints_for_directory(self, directory: Path) -> str | None:
        # Mark visited before reading so a transient read failure
        # doesn't cause us to re-attempt every poll.
        self._loaded_dirs.add(directory)
        for filename in _HINT_FILENAMES:
            hint_path = directory / filename
            try:
                if not hint_path.is_file():
                    continue
                content = hint_path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError) as exc:
                logger.debug(
                    "subdir_hints: cannot read %s: %s", hint_path, exc,
                )
                continue
            if not content:
                continue
            if len(content) > self.max_hint_chars:
                content = (
                    content[: self.max_hint_chars]
                    + f"\n\n[...truncated {filename}: "
                    f"{len(content):,} chars total]"
                )
            rel = self._friendly_relpath(hint_path)
            return f"[Project context: {rel}]\n{content}"
        return None

    def _friendly_relpath(self, path: Path) -> str:
        """Render the path relative to the working dir when possible,
        else relative to ``$HOME`` (with ``~`` prefix), else absolute.
        Just for prompt readability — the LLM doesn't act on it."""
        try:
            return str(path.relative_to(self.working_dir))
        except ValueError:
            pass
        try:
            return "~/" + str(path.relative_to(Path.home()))
        except ValueError:
            pass
        return str(path)


def _path_tokens(cmd: str) -> list[str]:
    """Pull path-looking tokens out of a shell command. Heuristic:
    contains ``/`` or ``.``, doesn't start with a flag dash, isn't a
    URL. False positives are fine — they just resolve to nonexistent
    dirs and get ignored."""
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        tokens = cmd.split()
    out: list[str] = []
    for tok in tokens:
        if not tok or tok.startswith("-"):
            continue
        if tok.startswith(("http://", "https://", "git@", "ssh://")):
            continue
        if "/" not in tok and "." not in tok:
            continue
        out.append(tok)
    return out


__all__ = ["SubdirectoryHintTracker"]
