"""Cross-line cooperation — the "phone call" API + STRATEGIC approval.

Cooperation proposals are voluntary cross-unit agreements (POD merch
around a KDP series, affiliate promo of a SaaS launch, etc.). The
asking unit creates a CooperationProposal; the target unit decides;
CEO arbitrates on disagreement; founder via Approval action_class=
STRATEGIC if CEO can't decide.

Plus ``cooperation.ask_about`` — the structured "phone call" API.
Lets a Line VP ask another Line VP a question via the target unit's
owner agent. The target agent processes with its OWN scoped memory
and returns a structured response. No memory access leaks.
"""
from korpha.cooperation.model import (
    CooperationProposal,
    CooperationStatus,
    CrossUnitQueryLog,
)

__all__ = [
    "CooperationProposal",
    "CooperationStatus",
    "CrossUnitQueryLog",
]
