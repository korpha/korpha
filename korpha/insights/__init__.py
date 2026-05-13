"""Insights engine — aggregate ``Activity`` + ``Cost`` rows into a
founder-facing weekly report.

The retention hook is "your AI cofounder cost $14, ran 132 skills,
saved you ~6h." Turn the existing audit data into a single
screenshot-able number Mike can show at his Skool meetup. Reuses
data already captured by every skill / inference call, so this is
pure aggregation — no new instrumentation.
"""
from korpha.insights.engine import (
    InsightsReport,
    ProviderBreakdown,
    SkillUsage,
    compute_insights,
    estimate_hours_saved,
    render_insights_terminal,
)

__all__ = [
    "InsightsReport",
    "ProviderBreakdown",
    "SkillUsage",
    "compute_insights",
    "estimate_hours_saved",
    "render_insights_terminal",
]
