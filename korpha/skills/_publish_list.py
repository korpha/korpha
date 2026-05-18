"""Curated list of first-party skills to publish to the AIgenteur hub.

Why a hand-curated list rather than "publish all bundled skills"?

Most of the 80+ bundled skills are internal plumbing — `hr.fire_worker`,
`cooperation.escalate`, `cron.create_watchdog`, etc. They only make
sense inside an AIgenteur install, not as standalone capabilities
someone might want to discover via the hub. The hub catalog is for
things a reader might browse to and think *"I'd reinstall this in
my own setup"* — flagship + reusable, with clear value.

Publish bar:
  - Standalone capability (not internal plumbing)
  - Either does real external work (Stripe / HeyGen / image gen) or
    encodes a battle-tested LLM prompt worth borrowing
  - Stable interface — no churn expected

Pushed via ``tools/seed_firstparty_hub.py`` with the deploy secret.
"""
from __future__ import annotations

from typing import Any

UPSTREAM_REPO = "AIgenteur/aigenteur_agent"


def _entry(
    name: str,
    display_name: str,
    description: str,
    *,
    long_description: str,
    tags: list[str],
    upstream_path: str,
    cofounder_protocol: bool = False,
) -> dict[str, Any]:
    return {
        "name": name,
        "display_name": display_name,
        "description": description,
        "long_description": long_description,
        "license": "MIT",
        "tags": tags,
        "cofounder_protocol": cofounder_protocol,
        "upstream_repo": UPSTREAM_REPO,
        "upstream_path": upstream_path,
    }


PUBLISHABLE_SKILLS: list[dict[str, Any]] = [
    _entry(
        "niche.find_micro_niches",
        "Niche · find micro-niches",
        "Generate 5 micro-niches from a founder brief, scored for fit.",
        long_description=(
            "Takes a free-text brief (\"I want to sell to solo SaaS "
            "founders\") and produces 5 concrete micro-niches with TAM "
            "estimate, channel hypothesis, and a 1-10 fit score. The "
            "CEO uses this as the first step of every new business "
            "line. Honors `brief.product_kind` (course / SaaS / POD / "
            "etc.) so it doesn't drift from what the founder asked for."
        ),
        tags=["ideation", "ceo", "niche", "research"],
        upstream_path="korpha/skills/niche.py",
    ),
    _entry(
        "niche.score_fit",
        "Niche · score founder-fit",
        "Deterministic 0-10 fit score for a given niche + founder profile.",
        long_description=(
            "Pure scoring function — no LLM call, fast and "
            "reproducible. Combines TAM, founder edge, channel "
            "viability, capital intensity, and competitor density. "
            "Used by Line VPs to triage whether to invest more cycles "
            "in a niche before going to the founder for approval."
        ),
        tags=["niche", "scoring", "deterministic"],
        upstream_path="korpha/skills/niche.py",
    ),
    _entry(
        "creative.heygen_avatar",
        "Creative · HeyGen talking-head avatar",
        "Generate a talking-head video via HeyGen given script + voice.",
        long_description=(
            "Thin wrapper over the HeyGen API. Takes a script + voice "
            "selector + avatar id and returns a downloadable mp4. "
            "Handles polling for job completion, retries on transient "
            "5xx, and credit-cost reporting. Drop-in for cofounder "
            "video drafting workflows."
        ),
        tags=["video", "avatar", "heygen", "creative"],
        upstream_path="korpha/skills/creative.py",
    ),
    _entry(
        "creative.hyperframes",
        "Creative · HyperFrames local video composition",
        "Compose a multi-clip video locally — no API, no cost-per-run.",
        long_description=(
            "Local-only video composition: stitches clips, overlays "
            "text/captions, adds transitions and audio. Built on "
            "ffmpeg under the hood. Pairs with `creative.heygen_avatar` "
            "in `marketing.video_from_post` to assemble the final "
            "post-ready video without paying per-frame API costs."
        ),
        tags=["video", "ffmpeg", "creative", "local"],
        upstream_path="korpha/skills/creative.py",
    ),
    _entry(
        "marketing.video_from_post",
        "Marketing · post → video",
        "Turn a written post into a finished video (avatar + B-roll + captions).",
        long_description=(
            "End-to-end marketing chain. Takes a written post and "
            "produces a ready-to-publish video: HeyGen avatar reads "
            "the script, HyperFrames adds B-roll + captions, returns "
            "a downloadable mp4. The CMO uses this for the weekly "
            "video output on every business line."
        ),
        tags=["marketing", "video", "cmo", "automation"],
        upstream_path="korpha/skills/marketing.py",
    ),
    _entry(
        "imagery.generate_image",
        "Imagery · generate image",
        "Generate an image from a prompt via the configured image provider.",
        long_description=(
            "Provider-agnostic image generation. Routes to whatever "
            "image backend is configured (Replicate / OpenAI / "
            "Together / Fal / etc.) and returns the resulting URL + "
            "cost. Falls back across providers when one fails. "
            "Used by landing-page draft + social-post generators."
        ),
        tags=["image", "generation", "creative"],
        upstream_path="korpha/skills/imagery.py",
    ),
    _entry(
        "commerce.create_payment_link",
        "Commerce · create Stripe payment link",
        "Create a one-time Stripe payment link from a product + price.",
        long_description=(
            "Per-BusinessUnit Stripe integration. Resolves the right "
            "Stripe account from the unit's credentials, creates a "
            "Product + Price + PaymentLink in one call, and returns "
            "the shareable URL. Multi-tenant safe — never routes a "
            "POD shop's checkout through the KDP account."
        ),
        tags=["commerce", "stripe", "payments"],
        upstream_path="korpha/skills/commerce.py",
    ),
    _entry(
        "founder.intake_brief",
        "Founder · intake brief",
        "Structured first-conversation intake of a founder's brief.",
        long_description=(
            "Interactive intake skill that asks ~6 questions to "
            "extract: product_kind, target audience, ICP signals, "
            "monetization model, channels, ambition window. Output "
            "is a normalized JSON brief that downstream skills "
            "(niche generator, Line Pack selector, hiring) all read. "
            "Designed for non-technical founders — no jargon, no "
            "open-ended 'tell me everything'."
        ),
        tags=["onboarding", "founder", "intake"],
        upstream_path="korpha/skills/founder.py",
    ),
    _entry(
        "finance.monthly_review",
        "Finance · monthly P&L review",
        "Generate a monthly P&L digest with anomaly callouts.",
        long_description=(
            "End-of-month finance digest. Pulls revenue from Stripe "
            "events, costs from CostLog (LLM + tools), categorizes "
            "by business unit, and produces a markdown report with "
            "month-over-month deltas + anomaly callouts (\"LLM spend "
            "tripled — Reasoning model added mid-month\"). The CFO "
            "agent uses this for the monthly founder digest."
        ),
        tags=["finance", "p&l", "monthly"],
        upstream_path="korpha/skills/finance.py",
    ),
    _entry(
        "landing.draft_copy",
        "Landing · draft landing-page copy",
        "Draft full landing-page copy: hero, features, CTA, FAQ.",
        long_description=(
            "Encodes a battle-tested prompt for landing-page copy. "
            "Forces specificity (no \"revolutionary\"), 5 concrete "
            "features instead of 3 vague ones, and a CTA that ties "
            "to a measurable outcome. Output is structured JSON the "
            "deploy skill can render straight into HTML."
        ),
        tags=["copywriting", "landing", "marketing"],
        upstream_path="korpha/skills/landing.py",
    ),
    _entry(
        "growth.draft_content_plan",
        "Growth · draft 30-day content plan",
        "Plan a 30-day content calendar (post + video) from a niche brief.",
        long_description=(
            "Produces a 30-day content calendar with concrete post "
            "topics, hooks, and one video idea per week. Optimizes "
            "for the channels declared in the niche brief — won't "
            "propose TikTok content if the brief says \"LinkedIn + "
            "email\". Output plugs straight into kanban as cards."
        ),
        tags=["growth", "content", "marketing", "planning"],
        upstream_path="korpha/skills/growth.py",
    ),
    _entry(
        "kanban.create_card",
        "Kanban · create card",
        "Create a kanban card under the current business unit.",
        long_description=(
            "Core task-tracking skill. Creates a kanban card with "
            "title, description, owner, urgency, and blockers. "
            "Auto-stamps the business_unit_id from the calling "
            "agent's context so cards are scoped right by default. "
            "Used by every planning skill to break work into "
            "trackable units."
        ),
        tags=["kanban", "tasks", "core"],
        upstream_path="korpha/skills/kanban_skills.py",
    ),
]


__all__ = ["PUBLISHABLE_SKILLS", "UPSTREAM_REPO"]
