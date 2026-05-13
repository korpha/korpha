"""External-business integrations the cofounder uses on the Founder's behalf.

Distinct from ``korpha/notifications/`` (channels Founder receives) and
``korpha/commerce/`` (money). These are services Korpha calls into
during normal cofounder work — distribution, market intel, etc.

Today: ``rank_my_answer`` (GEO + SEO audit + schema generation).
"""
from korpha.integrations.rank_my_answer import (
    RankMyAnswerClient,
    RankMyAnswerError,
)

__all__ = ["RankMyAnswerClient", "RankMyAnswerError"]
