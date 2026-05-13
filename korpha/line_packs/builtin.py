"""6 reference Line Packs — POD, KDP, Info, SaaS, Affiliate, Agency.

Each pack ships niche-profile defaults + KPI definitions + worker
specialty suggestions + required service list + customer support
autonomy default. Community can author additional packs (KDP Romance
Type Pack, POD Cat Lovers Niche Pack, etc.) using the same contract.

Defaults are conservative — the founder can override on first hire.
See ``docs/PRODUCT_LIFECYCLE.md`` for the per-Line rationale.
"""
from __future__ import annotations

from korpha.business_units.model import NicheProfile
from korpha.credentials.model import ExternalServiceKind
from korpha.line_packs.contract import (
    KpiDefinition, LinePack, default_registry,
)


# ---------------------------------------------------------------------------
# POD
# ---------------------------------------------------------------------------


class PodLinePack(LinePack):
    """Print on Demand — t-shirts / mugs / stickers / posters via
    Printful / Printify / Merch by Amazon / Etsy."""

    @property
    def pack_id(self) -> str: return "pod-line-pack@1.0.0"
    @property
    def line_kind(self) -> str: return "pod"
    @property
    def description(self) -> str:
        return (
            "POD line: design-led, niche-narrow, platform-multiplexed. "
            "Publishes many designs across t-shirts/mugs/stickers on "
            "Printful + Merch by Amazon + Redbubble + Etsy."
        )

    def default_niche_profile(self) -> NicheProfile:
        return NicheProfile(
            core_topics=["pod", "print_on_demand", "etsy", "merch_by_amazon"],
            adjacent_topics=["design", "niche_marketing", "pinterest"],
            off_limits_topics=[],
            persona=(
                "POD shop operators publishing many designs across "
                "platforms; care about royalties + listing optimization."
            ),
        )

    def kpi_definitions(self) -> list[KpiDefinition]:
        return [
            KpiDefinition("designs_published_per_week", "Designs/week", "count"),
            KpiDefinition("royalty_usd_per_month", "Royalties USD/mo", "usd_per_month"),
            KpiDefinition("winner_promotion_rate", "Winner rate", "pct"),
            KpiDefinition("avg_listing_age_to_first_sale_days", "Days-to-first-sale", "count"),
        ]

    def suggested_worker_specialties(self) -> list[str]:
        return ["designer", "niche_researcher", "listing_optimizer"]

    def required_services(self) -> list[ExternalServiceKind]:
        return [
            ExternalServiceKind.PRINTFUL,
            ExternalServiceKind.PRINTIFY,
            ExternalServiceKind.ETSY,
        ]

    def default_support_autonomy_level(self) -> int:
        return 3  # FAQ + order lookup; marketplaces handle most


# ---------------------------------------------------------------------------
# KDP
# ---------------------------------------------------------------------------


class KdpLinePack(LinePack):
    """Amazon KDP — books across genres (Romance / Coloring / Cookbook /
    Children's / Business Non-fiction)."""

    @property
    def pack_id(self) -> str: return "kdp-line-pack@1.0.0"
    @property
    def line_kind(self) -> str: return "kdp"
    @property
    def description(self) -> str:
        return (
            "KDP line: genre-fragmented, Amazon-dominated, audience-by-pen-name. "
            "Each genre has its own playbook; Type Packs (KDP Romance Type, "
            "KDP Coloring Type) ship as separate community packs."
        )

    def default_niche_profile(self) -> NicheProfile:
        return NicheProfile(
            core_topics=["kdp", "kindle_unlimited", "amazon_books", "self_publishing"],
            adjacent_topics=["author_marketing", "bookbub", "booktok"],
            off_limits_topics=[],
            persona=(
                "Self-publishing authors using Amazon KDP — managing "
                "pen names, BSR optimization, KU page-read economics."
            ),
        )

    def kpi_definitions(self) -> list[KpiDefinition]:
        return [
            KpiDefinition("books_shipped_per_quarter", "Books/quarter", "count"),
            KpiDefinition("bsr_avg_top_5", "BSR top-5 avg", "rank"),
            KpiDefinition("royalty_usd_per_month", "KDP royalties USD/mo", "usd_per_month"),
            KpiDefinition("review_avg_stars", "Avg star rating", "pct"),
            KpiDefinition("ku_pages_read_per_month", "KU pages/mo", "count"),
        ]

    def suggested_worker_specialties(self) -> list[str]:
        return [
            "author", "editor", "cover_designer",
            "launch_coordinator", "arc_team_lead",
        ]

    def required_services(self) -> list[ExternalServiceKind]:
        return [ExternalServiceKind.KDP_API]

    def default_support_autonomy_level(self) -> int:
        return 1  # Reputation-critical reviews; draft for approval


# ---------------------------------------------------------------------------
# Info Products
# ---------------------------------------------------------------------------


class InfoLinePack(LinePack):
    """Info products — courses, ebooks, newsletters, memberships."""

    @property
    def pack_id(self) -> str: return "info-line-pack@1.0.0"
    @property
    def line_kind(self) -> str: return "info"
    @property
    def description(self) -> str:
        return (
            "Info products: funnel-driven, audience-segmented, JV-launch-amplified. "
            "FE + 4 OTOs ladder, recurring upsells, JV launches multiply EPC."
        )

    def default_niche_profile(self) -> NicheProfile:
        return NicheProfile(
            core_topics=["info_products", "courses", "online_education"],
            adjacent_topics=["copywriting", "funnels", "email_marketing"],
            off_limits_topics=[],
            persona=(
                "Info product creators selling courses/ebooks/memberships "
                "via funnels + JV launches."
            ),
        )

    def kpi_definitions(self) -> list[KpiDefinition]:
        return [
            KpiDefinition("course_launches_per_year", "Course launches/yr", "count"),
            KpiDefinition("avg_epc", "Average EPC", "usd_per_month"),
            KpiDefinition("refund_rate", "Refund rate", "pct"),
            KpiDefinition("backend_stick_rate", "Stick rate", "pct"),
            KpiDefinition("recurring_mrr_usd", "Recurring MRR USD", "usd_per_month"),
        ]

    def suggested_worker_specialties(self) -> list[str]:
        return [
            "funnel_architect", "webinar_producer",
            "email_sequence_writer", "community_manager",
        ]

    def required_services(self) -> list[ExternalServiceKind]:
        return [
            ExternalServiceKind.STRIPE,
            ExternalServiceKind.CONVERTKIT,
            ExternalServiceKind.TEACHABLE,
        ]

    def default_support_autonomy_level(self) -> int:
        return 2  # Mixed — pre-sale L2, FAQ L3, refunds L1


# ---------------------------------------------------------------------------
# SaaS
# ---------------------------------------------------------------------------


class SaasLinePack(LinePack):
    """SaaS apps — recurring revenue, churn, dev cycles."""

    @property
    def pack_id(self) -> str: return "saas-line-pack@1.0.0"
    @property
    def line_kind(self) -> str: return "saas"
    @property
    def description(self) -> str:
        return (
            "SaaS apps: recurring revenue, churn-driven economics, "
            "continuous dev. Each app gets a Product VP."
        )

    def default_niche_profile(self) -> NicheProfile:
        return NicheProfile(
            core_topics=["saas", "b2b_software", "product_led_growth"],
            adjacent_topics=["dev_tools", "automation", "api_first"],
            off_limits_topics=[],
            persona=(
                "Indie SaaS founders running production apps with paying "
                "customers, MRR growth focus, churn-sensitive."
            ),
        )

    def kpi_definitions(self) -> list[KpiDefinition]:
        return [
            KpiDefinition("mrr_usd", "MRR USD", "usd_per_month"),
            KpiDefinition("churn_rate_monthly", "Monthly churn", "pct"),
            KpiDefinition("ltv_usd", "LTV USD", "usd_per_month"),
            KpiDefinition("active_paid_users", "Active paid users", "count"),
            KpiDefinition("nps_score", "NPS", "rank"),
        ]

    def suggested_worker_specialties(self) -> list[str]:
        return [
            "dev", "support_engineer", "product_manager",
            "content_marketer",
        ]

    def required_services(self) -> list[ExternalServiceKind]:
        return [
            ExternalServiceKind.STRIPE,
            ExternalServiceKind.RESEND,
            ExternalServiceKind.CLOUDFLARE,
        ]

    def default_support_autonomy_level(self) -> int:
        return 3  # Most mature; refunds under $50 auto


# ---------------------------------------------------------------------------
# Affiliate
# ---------------------------------------------------------------------------


class AffiliateLinePack(LinePack):
    """Affiliate marketing — promote OTHER people's launches.
    Audience-first; campaigns are time-bound."""

    @property
    def pack_id(self) -> str: return "affiliate-line-pack@1.0.0"
    @property
    def line_kind(self) -> str: return "affiliate"
    @property
    def description(self) -> str:
        return (
            "Affiliate line: audience-segmented, campaign-time-bounded. "
            "Audience Managers (one per niche list segment) decide which "
            "JV invitations to accept via niche-compatibility scoring."
        )

    def default_niche_profile(self) -> NicheProfile:
        return NicheProfile(
            core_topics=["affiliate_marketing", "jv_launches"],
            adjacent_topics=["email_marketing", "list_building"],
            off_limits_topics=[],
            persona=(
                "Affiliates running niche email lists, promoting JV "
                "launches that match their audience's interests."
            ),
        )

    def kpi_definitions(self) -> list[KpiDefinition]:
        return [
            KpiDefinition("commission_usd_per_month", "Commissions USD/mo", "usd_per_month"),
            KpiDefinition("leaderboard_finishes", "Top-3 finishes/year", "count"),
            KpiDefinition("reciprocity_debt_owed", "Mailings owed", "count"),
            KpiDefinition("list_size_total", "Total list size", "count"),
            KpiDefinition("avg_open_rate", "Average open rate", "pct"),
        ]

    def suggested_worker_specialties(self) -> list[str]:
        return [
            "swipe_writer", "bonus_crafter", "webinar_host",
            "list_segmentation_specialist",
        ]

    def required_services(self) -> list[ExternalServiceKind]:
        return [
            ExternalServiceKind.JVZOO,
            ExternalServiceKind.WARRIOR_PLUS,
            ExternalServiceKind.CONVERTKIT,
        ]

    def default_support_autonomy_level(self) -> int:
        return 3  # Mostly "did I get my bonus?" — FAQ + lookup


# ---------------------------------------------------------------------------
# Agency
# ---------------------------------------------------------------------------


class AgencyLinePack(LinePack):
    """Agency services — service-billed, retainer-driven."""

    @property
    def pack_id(self) -> str: return "agency-line-pack@1.0.0"
    @property
    def line_kind(self) -> str: return "agency"
    @property
    def description(self) -> str:
        return (
            "Agency line: service-billed, retainer-driven, deliverable-tracked. "
            "Sells time + expertise, not goods."
        )

    def default_niche_profile(self) -> NicheProfile:
        return NicheProfile(
            core_topics=["agency", "client_services", "consulting"],
            adjacent_topics=["b2b_sales", "linkedin_marketing"],
            off_limits_topics=[],
            persona=(
                "Solo agencies / consultancies selling expertise to "
                "small-business clients on retainer or project basis."
            ),
        )

    def kpi_definitions(self) -> list[KpiDefinition]:
        return [
            KpiDefinition("active_retainer_mrr_usd", "Retainer MRR USD", "usd_per_month"),
            KpiDefinition("project_revenue_usd", "Project revenue USD", "usd_per_month"),
            KpiDefinition("client_count", "Active client count", "count"),
            KpiDefinition("utilization_rate", "Utilization", "pct"),
            KpiDefinition("avg_deal_size_usd", "Avg deal size", "usd_per_month"),
        ]

    def suggested_worker_specialties(self) -> list[str]:
        return [
            "service_designer", "deliverable_manager",
            "client_success", "proposal_writer",
        ]

    def required_services(self) -> list[ExternalServiceKind]:
        return [
            ExternalServiceKind.STRIPE,
            ExternalServiceKind.RESEND,
        ]

    def default_support_autonomy_level(self) -> int:
        return 2  # Client relationships are high-stakes; draft for approval


# ---------------------------------------------------------------------------
# Register all 6 in the default registry
# ---------------------------------------------------------------------------


def _register_builtins() -> None:
    default_registry.register(PodLinePack())
    default_registry.register(KdpLinePack())
    default_registry.register(InfoLinePack())
    default_registry.register(SaasLinePack())
    default_registry.register(AffiliateLinePack())
    default_registry.register(AgencyLinePack())


_register_builtins()


__all__ = [
    "AffiliateLinePack",
    "AgencyLinePack",
    "InfoLinePack",
    "KdpLinePack",
    "PodLinePack",
    "SaasLinePack",
]
