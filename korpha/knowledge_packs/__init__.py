"""Knowledge packs — agent-readable playbooks for working with
third-party tools / domains.

Each pack is a ``SKILL.md`` (Hermes-style) sitting under
``korpha/knowledge_packs/<category>/<pack_name>/`` with optional
``references/`` and ``scripts/`` siblings. The pack tells the agent
what the tool is, what shape its API has, and the right way to
operate it — without us having to write Python adapters for every
SaaS in the world.

How packs reach the model:

  - On agent turn build-up, :func:`select_packs_for_capability` picks
    the packs matching the active capability tag (productivity,
    developer, creative, communication) plus any explicit
    ``capability_packs`` set on the agent role.
  - Selected packs are injected into the system prompt under a
    ``<knowledge_pack name="...">`` section. The agent treats them
    as authoritative reference for that tool.
  - When a Python skill exists for the same tool (e.g. ``notion.*``),
    the pack still loads — they're complementary: the Python skill
    does the call, the pack explains the call's semantics + edge
    cases.

Packs are read once at startup and cached in-memory; reload via
:func:`reload_packs` (CLI: ``aigenteur knowledge reload``).
"""
from korpha.knowledge_packs.service import (
    KnowledgePack,
    KnowledgePackError,
    KnowledgePackService,
    available_packs,
    available_categories,
    reload_packs,
    select_packs_for_capability,
)

__all__ = [
    "KnowledgePack",
    "KnowledgePackError",
    "KnowledgePackService",
    "available_categories",
    "available_packs",
    "reload_packs",
    "select_packs_for_capability",
]
