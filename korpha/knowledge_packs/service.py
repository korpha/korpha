"""Discover + load + select knowledge packs for the agent."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


# Where bundled packs live. Mike can override with an env var to layer
# in private packs (e.g. an agency's internal playbooks).
_BUNDLED_ROOT = Path(__file__).parent
USER_PACKS_DIR_ENV = "KORPHA_KNOWLEDGE_PACKS_DIR"

# Capability tag → category dirs that satisfy it. Used by the agent
# turn builder to pull only relevant packs into context.
_CAPABILITY_MAP: dict[str, tuple[str, ...]] = {
    "productivity": (
        "productivity",
        "research",  # research workflows are productivity-flavored
    ),
    "developer": (
        "github", "devops", "software-development",
        "data-science", "mlops",
        "mcp",           # Model Context Protocol — agent-tool standard
        "red-teaming",   # security adversarial testing
        "domain",        # passive domain reconnaissance (Python stdlib)
    ),
    "creative": ("creative", "diagramming", "media", "gifs"),
    "communication": ("email", "social-media", "note-taking"),
    # Agent-design meta packs apply to roles that PLAN (CEO, Line VPs).
    # The capabilities_for_role mapper in cofounder/knowledge_inject.py
    # tags CEO + VPs with this so they pick up best-practice playbooks
    # for delegation, prompt design, attempt structuring.
    "agent_design": ("autonomous-ai-agents",),
}


class KnowledgePackError(ValueError):
    """Raised when a pack directory exists but its SKILL.md is missing
    or malformed beyond what the loader can recover from."""


@dataclass(frozen=True)
class KnowledgePack:
    """One playbook the agent can reference."""

    name: str
    """Pack identifier, derived from the directory name."""

    category: str
    """Top-level category (productivity, github, devops, ...)."""

    path: Path
    """Directory holding SKILL.md (+ optional references / scripts)."""

    content: str
    """Full SKILL.md text."""

    @property
    def slug(self) -> str:
        """category/name form used in prompt headers + URLs."""
        return f"{self.category}/{self.name}"

    @property
    def char_length(self) -> int:
        return len(self.content)


@dataclass
class _PackCache:
    by_slug: dict[str, KnowledgePack] = field(default_factory=dict)
    loaded: bool = False


_cache = _PackCache()


def _user_packs_dir() -> Path | None:
    import os
    raw = os.getenv(USER_PACKS_DIR_ENV, "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if p.is_dir() else None


def _discover_one_root(root: Path) -> list[KnowledgePack]:
    """Walk ``<root>/<category>/<pack>/SKILL.md`` and load each."""
    if not root.is_dir():
        return []
    packs: list[KnowledgePack] = []
    for cat_dir in sorted(root.iterdir()):
        if not cat_dir.is_dir() or cat_dir.name.startswith((".", "_")):
            continue
        category = cat_dir.name
        for pack_dir in sorted(cat_dir.rglob("SKILL.md")):
            # rglob gives us the SKILL.md; the pack is its parent dir.
            pack_root = pack_dir.parent
            name = pack_root.name
            try:
                content = pack_dir.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning(
                    "knowledge_packs: failed to read %s: %s",
                    pack_dir, exc,
                )
                continue
            packs.append(KnowledgePack(
                name=name, category=category,
                path=pack_root, content=content,
            ))
    return packs


def reload_packs() -> int:
    """Re-scan bundled + user-override roots into the in-memory cache.
    Returns the number of packs loaded. Idempotent — safe to call
    anywhere; downstream code that has cached pack references should
    re-query :func:`available_packs` after."""
    bundled = _discover_one_root(_BUNDLED_ROOT)
    user_root = _user_packs_dir()
    user = _discover_one_root(user_root) if user_root else []
    # User packs override bundled by slug (lets agencies replace a
    # default playbook with their own without forking the repo).
    merged: dict[str, KnowledgePack] = {}
    for p in bundled:
        merged[p.slug] = p
    for p in user:
        merged[p.slug] = p
    _cache.by_slug = merged
    _cache.loaded = True
    logger.info(
        "knowledge_packs: loaded %d packs (%d bundled, %d user)",
        len(merged), len(bundled), len(user),
    )
    return len(merged)


def _ensure_loaded() -> None:
    if not _cache.loaded:
        reload_packs()


def available_packs() -> list[KnowledgePack]:
    """All packs the loader knows about. Sorted by slug for stable
    rendering in lists / dashboard."""
    _ensure_loaded()
    return sorted(_cache.by_slug.values(), key=lambda p: p.slug)


def available_categories() -> list[str]:
    """Distinct categories present in the loaded packs, sorted."""
    _ensure_loaded()
    return sorted({p.category for p in _cache.by_slug.values()})


def get_pack(slug: str) -> KnowledgePack | None:
    _ensure_loaded()
    return _cache.by_slug.get(slug)


def select_packs_for_capability(
    capabilities: Iterable[str],
    *,
    extra_slugs: Iterable[str] = (),
) -> list[KnowledgePack]:
    """Return packs matching any of the given capability tags, plus
    any explicit ``extra_slugs``.

    Used by the agent turn builder: an AgentRole with
    capability_tags=['productivity'] gets all 8 productivity packs;
    adding ``extra_slugs=['github/github-pr-workflow']`` layers in a
    specific dev pack the role needs even though it's not productivity-
    flavored."""
    _ensure_loaded()
    wanted_cats: set[str] = set()
    for cap in capabilities:
        for c in _CAPABILITY_MAP.get(cap.strip().lower(), ()):
            wanted_cats.add(c)
    out: dict[str, KnowledgePack] = {}
    for pack in _cache.by_slug.values():
        if pack.category in wanted_cats:
            out[pack.slug] = pack
    for slug in extra_slugs:
        pack = _cache.by_slug.get(slug)
        if pack is not None:
            out[pack.slug] = pack
    return sorted(out.values(), key=lambda p: p.slug)


@dataclass
class KnowledgePackService:
    """Stateful facade for use from skills / dashboard / CLI."""

    def list_all(self) -> list[KnowledgePack]:
        return available_packs()

    def list_categories(self) -> list[str]:
        return available_categories()

    def get(self, slug: str) -> KnowledgePack | None:
        return get_pack(slug)

    def select(
        self,
        capabilities: Iterable[str],
        *,
        extra_slugs: Iterable[str] = (),
    ) -> list[KnowledgePack]:
        return select_packs_for_capability(
            capabilities, extra_slugs=extra_slugs,
        )

    def reload(self) -> int:
        return reload_packs()


__all__ = [
    "KnowledgePack",
    "KnowledgePackError",
    "KnowledgePackService",
    "available_categories",
    "available_packs",
    "get_pack",
    "reload_packs",
    "select_packs_for_capability",
]
