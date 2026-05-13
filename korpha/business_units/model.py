"""BusinessUnit + Product + NicheProfile + DeploymentMode.

This is the schema-only PR. Two new SQLModel tables (``business_unit``
and ``business_product``) plus three Pydantic / enum types. No FK
additions to existing models — those land in PR3.

Why the recursive ``BusinessUnit`` instead of separate ``Line`` /
``Type`` / ``Series`` tables: real solopreneur portfolios don't nest
to a fixed depth. KDP Romance needs Series (because trilogies are
real); KDP Coloring doesn't (each book stands alone). POD T-Shirts
sometimes needs a Niche layer for big shops, sometimes stays flat for
small ones. A single self-referential table with ``kind`` indicating
the *level* lets the agents shape the tree to match the work, instead
of forcing operators into a one-size-fits-all hierarchy.

Hard memory isolation per unit comes from ``memory_namespace_id`` — an
auto-generated immutable UUID that becomes the partition key for
``agent_memory`` / ``vector_memory_shard`` / ``agent_transcript`` rows
(those FKs are wired in PR9). Even if a future agent tries to read
across units, the recall skill API refuses without an active
``CrossNamespaceRecallGrant`` (PR8). The barrier sits at the skill
layer, not just the query layer — prompt injection cannot bypass it.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field as PydanticField
from sqlalchemy import JSON, Column
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel, UniqueConstraint

from korpha.db._base import (
    json_column, primary_key_field, timestamp_field, utcnow,
)


def _nullable_json_column() -> Column:
    """JSON column that allows NULL — for optional JSON-typed fields
    like ``BusinessUnit.niche_profile``. The shared ``json_column()``
    forces nullable=False, which works for required JSON bags but not
    for fields that legitimately start as None and get populated later."""
    return Column(
        JSON().with_variant(JSONB(), "postgresql"),
        nullable=True,
        default=None,
    )


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class BusinessUnitKind(StrEnum):
    """What *level* of the recursive tree this node sits at.

    Vocabulary is open (CUSTOM exists) but the canonical values give
    the dashboard + line packs a stable taxonomy to render against.
    """

    DEFAULT = "default"
    """Backfilled root for single-business installs — the unit that
    receives all existing kanban / goals / approvals when an existing
    install migrates. Most users will split this into LINE units
    after migration; the DEFAULT remains as a parent until empty."""

    LINE = "line"
    """One of the 6 canonical business lines: pod / kdp / info / saas
    / affiliate / agency. A Line VP owns it."""

    TYPE = "type"
    """Sub-category within a Line. Examples: Romance / Coloring /
    Cookbook under KDP. T-Shirts / Mugs / Stickers under POD.
    Courses / Ebooks / Newsletter under Info."""

    SERIES = "series"
    """Multi-product series sharing a bible. Only relevant when the
    Type warrants it (KDP Romance trilogies, multi-volume cookbooks)."""

    NICHE = "niche"
    """Audience or design niche within a Type. Examples: Cat-lovers
    under POD T-Shirts. Software-engineers under POD Mugs."""

    AUDIENCE = "audience"
    """Email list segment under Affiliate Line. Carries its own
    niche profile + list metrics + JV campaign calendar."""

    PRODUCT_VP = "product_vp"
    """Single product under SaaS Line (or any Line that has a single
    Product per unit). The Product VP role owns the product's roadmap
    + GTM + support — same scope as a Line VP but for one product."""

    CUSTOM = "custom"
    """Community-defined unit kinds shipped by Line Packs. The packs
    set their own semantics; Korpha core treats CUSTOM as opaque."""


class DeploymentMode(StrEnum):
    """Where the Korpha instance is running.

    Selected at startup via ``KORPHA_DEPLOYMENT_MODE`` env var.
    Single value per process; runtime change is not supported (see
    ``BUSINESS_UNITS.md`` Deployment Modes section).
    """

    LOCAL = "local"
    """Single founder on their own machine. OAuth-authorized CLIs
    (Claude Code, Codex CLI, OpenCode, …) are available as
    company-wide shared resources because they're physically bound
    to one machine."""

    SAAS = "saas"
    """Multi-tenant hosted. OAuth CLIs cannot be shared across
    tenants (Anthropic/OpenAI ToS forbid; technical model
    impossible). All routing goes through per-unit API keys."""


class ProductKind(StrEnum):
    """What kind of leaf product. Drives the dashboard renderer +
    Line Pack skills that target this product type."""

    BOOK = "book"
    """KDP — paperback, Kindle, or audiobook."""

    DESIGN = "design"
    """POD — t-shirt, mug, sticker, poster art file."""

    COURSE = "course"
    """Info — multi-module video course."""

    EBOOK = "ebook"
    """Info — standalone PDF / Kindle / Apple Books."""

    NEWSLETTER = "newsletter"
    """Info — recurring publication (Substack, Beehiiv, etc.)."""

    MEMBERSHIP = "membership"
    """Info — recurring access community / Skool / Mighty Networks."""

    SAAS_APP = "saas_app"
    """SaaS — one app."""

    CAMPAIGN = "campaign"
    """Affiliate — time-bound promo window for one vendor's launch.
    Uses ``starts_at`` / ``ends_at`` columns."""

    SERVICE = "service"
    """Agency — one service offering (Starter / Pro / Enterprise tier)."""

    CUSTOM = "custom"
    """Community-defined product types via Line Packs."""


# ---------------------------------------------------------------------------
# NicheProfile — embedded JSON on BusinessUnit
# ---------------------------------------------------------------------------


class NicheProfile(BaseModel):
    """Audience profile + compatibility metadata. Stored as JSON on
    ``BusinessUnit.niche_profile``; validated at boundary via this
    Pydantic shape so we never read a malformed profile.

    The compatibility scorer (``niche.score_fit``, PR7) consumes:

      * ``core_topics``, ``adjacent_topics``, ``off_limits_topics``
        for the base relevance score
      * ``last_promoted_at`` + ``promos_in_last_30_days`` for the
        promo-fatigue decay penalty (avoid burning compatible-but-tired
        lists with too-frequent promos)
      * ``last_burned_at`` for hard-decline of recently-burned lists

    Pre-1.0: extend by adding fields; existing rows back-compat default
    to safe values via Pydantic's optional defaults. After 1.0,
    field removals require migration.
    """

    core_topics: list[str] = PydanticField(default_factory=list)
    """High-relevance topics — base score weight 1.0 per match."""

    adjacent_topics: list[str] = PydanticField(default_factory=list)
    """Tangentially relevant — base score weight 0.5 per match."""

    off_limits_topics: list[str] = PydanticField(default_factory=list)
    """Topics this audience actively rejects. Hard penalty (-2.0) or
    outright DECLINE from the scorer when any match. Examples for an
    AI-marketers list: ['homesteading', 'personal_finance', 'dating']."""

    persona: str = ""
    """Short prose description of the audience for the LLM to
    contextualize. Example: 'marketing managers at 5-50 person SaaS
    companies, $100-300k income, frustrated by Hubspot complexity.'"""

    list_size: int = 0
    """Current subscriber count. Updated by the audience manager."""

    avg_open_rate: float = 0.0
    """Last 30-day average open rate (0.0-1.0)."""

    avg_click_rate: float = 0.0
    """Last 30-day average click-through rate (0.0-1.0)."""

    avg_epc: float = 0.0
    """Historical earnings per click on past affiliate promos. Used
    for ranking new campaign opportunities."""

    last_burned_at: datetime | None = None
    """Most recent time an off-niche promo damaged engagement (open
    rate dip, unsubscribe spike). Causes hard-decline from the
    scorer for N days after. Null if never burned."""

    last_burn_unsubscribes: int = 0
    """How many unsubscribes the last burn caused. Helps the scorer
    size the penalty proportional to damage."""

    last_promoted_at: datetime | None = None
    """Last time ANY promo (compatible or not) was sent to this list.
    Drives the promo-fatigue decay penalty:
    - <14 days: -0.30 to score
    - 14-28 days: -0.15 to score
    - >28 days: no penalty"""

    promos_in_last_30_days: int = 0
    """Density guard. Each promo above 2 in last 30 days adds
    -0.05 penalty (capped at -0.20). Even ideal-fit content can't
    save a list that's getting hit every 3 days."""

    notes: str = ""
    """Free-form operator notes. Not consumed by the scorer; shown
    in the dashboard alongside the profile."""


# ---------------------------------------------------------------------------
# BusinessUnit — the recursive org node
# ---------------------------------------------------------------------------


class BusinessUnit(SQLModel, table=True):
    """A node in the recursive org tree under a Business.

    Identity = ``id``. Parent linkage = ``parent_id`` (self-ref).
    Slug uniqueness scoped to ``(business_id, parent_id, slug)`` so
    siblings can't collide; cross-branch slugs may repeat (POD has a
    'cat-lovers' niche; KDP also has 'cat-lovers' coloring books —
    that's fine because they live under different parents).

    Memory isolation: every unit has its own ``memory_namespace_id``
    generated at insertion. Future memory rows partition by this UUID;
    cross-unit recall requires explicit CooperationProposal grant.
    """

    __tablename__ = "business_unit"
    __table_args__ = (
        # Siblings can't share slugs. Cross-parent collision is fine.
        UniqueConstraint(
            "business_id", "parent_id", "slug",
            name="business_unit_sibling_slug_unique",
        ),
    )

    id: UUID = primary_key_field()
    business_id: UUID = Field(foreign_key="business.id", index=True)
    parent_id: UUID | None = Field(
        default=None, foreign_key="business_unit.id", index=True,
    )

    kind: BusinessUnitKind = Field(
        default=BusinessUnitKind.LINE, index=True,
    )

    name: str = Field(
        description=(
            "Human-readable display name. Example: 'KDP — Romance', "
            "'POD — Cat Lovers'. Shown in the dashboard org tree."
        ),
    )
    slug: str = Field(
        index=True,
        description=(
            "URL-safe identifier. Lowercase, alphanumeric + hyphens. "
            "Must be unique among siblings (same parent). Example: "
            "'kdp-romance', 'pod-cat-lovers', 'aff-ai-marketers'."
        ),
    )

    # Owner agent role — Line VP / Type Mgr / Series Lead / Audience
    # Mgr / Product VP. Null only for transitional units (e.g. a
    # newly-created Line awaiting its first VP hire).
    owner_agent_role_id: UUID | None = Field(
        default=None, foreign_key="agent_role.id", index=True,
    )

    # Playbook — skill pack ID from the skill hub providing this
    # unit's domain expertise. Example: 'kdp-romance@1.2.0'. Null
    # for custom / agent-authored unit configurations.
    playbook_skill_pack: str | None = Field(default=None)

    # Niche profile — embedded JSON. Validated against ``NicheProfile``
    # Pydantic model at read time. Null until the audience manager
    # populates it.
    niche_profile: dict[str, Any] | None = Field(
        default=None, sa_column=_nullable_json_column(),
    )

    # Memory namespace — immutable UUID partition key. Generated at
    # insertion; never updated. All future memory rows + vector index
    # partial-indexes scope on this. See PR9 for enforcement.
    memory_namespace_id: UUID = Field(
        default_factory=uuid4,
        index=True,
        unique=True,
        description=(
            "Immutable partition key for hard memory isolation. "
            "Cross-unit recall requires CooperationProposal grant."
        ),
    )

    # Lifecycle — active / paused / archived. Paused units block new
    # card claims; archived units are hidden from default views but
    # never hard-deleted.
    status: str = Field(default="active", index=True)
    paused_at: datetime | None = Field(default=None)
    paused_reason: str | None = Field(default=None)

    # Free-form unit-specific config. Example: support autonomy
    # level (per Customer Support Autonomy Ladder), KPI overrides,
    # operator notes. Schemaless — plugins read what they need.
    config: dict[str, Any] = Field(
        default_factory=dict, sa_column=json_column(),
    )

    created_at: datetime = timestamp_field()
    updated_at: datetime = timestamp_field()


# ---------------------------------------------------------------------------
# Product — the leaf under a BusinessUnit
# ---------------------------------------------------------------------------


class Product(SQLModel, table=True):
    """A leaf product (or campaign) under a BusinessUnit.

    Products never have children — they're the unit of work output,
    not org structure. A product belongs to exactly one BusinessUnit
    (its ``business_unit_id``) and rolls up to the Business via the
    unit. ``business_id`` is denormalized for fast P&L roll-up
    queries (avoiding a tree-walk per row).

    Time-bound products (kind=CAMPAIGN) populate ``starts_at`` and
    ``ends_at``. Evergreen products leave both null.

    The ``attributes`` JSON is intentionally schemaless. Examples
    by ProductKind:

      * BOOK: ``{"asin": "B0CXXXXX", "isbn_13": "...", "kdp_select": True,
        "kindle_unlimited_pages": 312, "categories": [...]}``
      * DESIGN: ``{"design_sku": "CL-CAT-001", "platforms": [...],
        "file_paths": {"png_4500": "...", "svg": "..."}}``
      * CAMPAIGN: ``{"vendor": "marketro", "platform": "jvzoo",
        "product_ids": {"funnel": "438771"}, "commission_pct": 50}``
      * SAAS_APP: ``{"deploy_url": "https://korpha.app",
        "stripe_product_id": "...", "mrr_current_usd": 14200}``

    Promote to structured columns only when 3+ consumers want the
    same field shape.
    """

    __tablename__ = "business_product"
    __table_args__ = (
        # Slug unique within the owning unit.
        UniqueConstraint(
            "business_unit_id", "slug",
            name="business_product_unit_slug_unique",
        ),
    )

    id: UUID = primary_key_field()
    business_unit_id: UUID = Field(
        foreign_key="business_unit.id", index=True,
    )
    business_id: UUID = Field(
        foreign_key="business.id", index=True,
        description=(
            "Denormalized for P&L roll-up queries. Must match the "
            "owning BusinessUnit's business_id; enforced at create."
        ),
    )

    kind: ProductKind = Field(
        default=ProductKind.CUSTOM, index=True,
    )

    name: str
    slug: str = Field(index=True)

    # Time-bound products use these. Affiliate campaigns are the
    # canonical case. Evergreen products (books, SaaS apps, designs)
    # leave both null.
    starts_at: datetime | None = Field(default=None)
    ends_at: datetime | None = Field(default=None)

    # See class docstring for kind-specific attribute conventions.
    attributes: dict[str, Any] = Field(
        default_factory=dict, sa_column=json_column(),
    )

    status: str = Field(default="active", index=True)
    """active | paused | archived. Same lifecycle as BusinessUnit."""

    created_at: datetime = timestamp_field()
    updated_at: datetime = timestamp_field()


__all__ = [
    "BusinessUnit",
    "BusinessUnitKind",
    "DeploymentMode",
    "NicheProfile",
    "Product",
    "ProductKind",
]
