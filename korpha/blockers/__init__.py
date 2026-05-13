"""Blocker queue: agents submit, Chief of Staff triages.

When any agent (worker, C-suite) cannot proceed, it submits a Blocker rather
than pinging the Founder directly. The Chief of Staff (internal-only role)
deduplicates, groups, attempts cheap resolutions, prioritizes, and produces
a consolidated digest that the CEO uses when speaking to the Founder.

Founder never sees the raw blocker stream by default — only the CEO's tight
summary. Power users can inspect via `korpha blockers`.
"""
from __future__ import annotations

from korpha.blockers.model import (
    Blocker,
    BlockerKind,
    BlockerStatus,
    BlockerUrgency,
)
from korpha.blockers.queue import BlockerQueue, BlockerSubmission

__all__ = [
    "Blocker",
    "BlockerKind",
    "BlockerQueue",
    "BlockerStatus",
    "BlockerSubmission",
    "BlockerUrgency",
]
