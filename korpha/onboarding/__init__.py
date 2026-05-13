"""Day-0 onboarding lifecycle.

Holds the post-pick-niche chain that produces the BRIEF.md "5:00 —
approve outreach" beat: after the Founder picks a niche, this module
runs validate / landing / outreach skills and persists each output as
a pending Approval so the dashboard fills with concrete artifacts the
Founder can act on.
"""
from korpha.onboarding.chain import run_post_pick_niche_chain

__all__ = ["run_post_pick_niche_chain"]
