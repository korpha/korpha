# BusinessUnit Implementation — Engineering Doc

**Status:** Implemented — see PR1–PR13 + PR-INT-1 through PR-INT-30.
**Companion:** [`docs/ORG_MODEL.md`](../ORG_MODEL.md) (conceptual).
**Audience:** Engineers implementing the recursive org model + shared
resources + per-unit credentials.

This document is the implementation counterpart to `ORG_MODEL.md`. It
covers data model diffs, migration steps, new skills + plugin
contracts, resolution algorithms, and the test plan.

If you haven't read the conceptual doc, start there — this assumes you
understand *why* we need a recursive BusinessUnit tree and why
audiences are first-class.

---

## Scope of v1

What lands in the first ship:

- `BusinessUnit` model (recursive)
- `Product` model (leaf)
- `NicheProfile` (embedded JSON on `BusinessUnit`)
- `ExternalServiceAccount` model (replaces / supersedes
  `ProviderAccount` for LLM use)
- `SharedResource` model (Pattern-1 infra; AI mesh)
- `CooperationProposal` model (Pattern-2 cross-line agreements)
- Foreign-key additions on `KanbanCard`, `Goal`, `Approval`,
  `Activity`, `AgentRole`, `CostLog`
- Hierarchical credential resolver
- New HR skills (`hr.start_business_line`, `hr.spawn_type_manager`, …)
- New compatibility-check skill (`niche.score_fit`)
- Migration script (existing businesses get a default unit)
- Tests (model, resolver, migration, cross-line cooperation)

Out of scope for v1 (in `ORG_MODEL.md` deferred list):
internal chargeback automation, cross-line synergy ML, multi-business
roll-up, i18n playbook patching.

## Data Model

### `BusinessUnit` (new)

```python
class BusinessUnitKind(StrEnum):
    DEFAULT = "default"        # backfilled from pre-migration state
    LINE = "line"              # POD / KDP / Info / SaaS / Affiliate
    TYPE = "type"              # Romance / T-shirts / Course
    SERIES = "series"          # Highland Rogue saga
    NICHE = "niche"            # Cat lovers
    AUDIENCE = "audience"      # AI marketers list segment
    PRODUCT_VP = "product_vp"  # one app under SaaS
    CUSTOM = "custom"          # community-defined via Line Pack


class BusinessUnit(SQLModel, table=True):
    __tablename__ = "business_unit"

    id: UUID = primary_key_field()
    business_id: UUID = Field(foreign_key="business.id", index=True)
    parent_id: UUID | None = Field(
        foreign_key="business_unit.id", index=True, default=None,
    )

    kind: BusinessUnitKind = Field(default=BusinessUnitKind.LINE, index=True)
    name: str
    slug: str = Field(index=True)          # url-safe, unique within parent

    owner_agent_role_id: UUID | None = Field(
        foreign_key="agent_role.id", default=None,
    )

    # Playbook = skill bundle from skill hub. May be None at creation
    # if the founder is setting up a custom line.
    playbook_skill_pack: str | None = None  # e.g. "kdp-romance@1.2.0"

    # NicheProfile — embedded JSON for now; promote to its own
    # table if we ever need to query across profiles.
    niche_profile: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSON),
    )

    # Memory namespace — auto-generated immutable UUID. All memory rows,
    # vector embedding shards, transcripts, and prompt caches for this
    # unit's agents are partitioned by this namespace_id. Hard isolation
    # enforced at the skill API layer (see Memory Namespacing section).
    memory_namespace_id: UUID = Field(
        default_factory=uuid4, index=True, unique=True,
    )

    status: str = Field(default="active")  # active | paused | archived
    paused_at: datetime | None = None
    paused_reason: str | None = None

    created_at: datetime = timestamp_field()
    updated_at: datetime = timestamp_field()
```

**Constraints**:

- `(business_id, parent_id, slug)` is unique — siblings can't share
  slugs.
- `parent_id` can be NULL only when `kind = DEFAULT` (root unit) — every
  other unit has a parent.
- Soft delete via `status="archived"` — never hard-delete because
  artifacts reference units.
- `memory_namespace_id` is **immutable** after creation — moving memory
  across units (e.g. when re-organizing) requires explicit copy, not
  silent rebind.

### `DeploymentMode` enum (new)

```python
class DeploymentMode(StrEnum):
    LOCAL = "local"      # single founder, OAuth CLIs available, current target
    SAAS = "saas"        # multi-tenant hosted, API-keys-only, future
```

Detected at startup from `KORPHA_DEPLOYMENT_MODE` env var (default
`local`). Single value per instance — never mixed in one process. The
resolver and shared-resource enumeration both consult this value
before yielding eligible accounts/resources.

**Runtime change is not supported.** If the operator wants to flip
modes (e.g. local → SaaS migration), the process must be restarted.
At startup, the value is cached in a module-level constant; the
resolver and plugin loader reference the constant by name, so a
mid-run change would only affect new lookups while in-flight calls
keep the old value — guaranteed inconsistent state. Better to refuse:
`korpha doctor` warns if `KORPHA_DEPLOYMENT_MODE` differs from
the cached value at startup, suggesting a restart.

### Founder OAuth override escape hatch

The default routing (Pro → OAuth CLI, Workhorse → API) is correct for
99% of calls but occasionally the founder needs to override:

- Testing whether a specific call works against the API account
- Forcing a Workhorse call through Claude Code to compare quality
- Working around a temporarily exhausted OAuth subscription cap with
  a one-off API key call (without flipping the global routing)

Two override mechanisms:

1. **Per-call skill argument**: every LLM-using skill accepts
   `force_credentials_source: "oauth" | "api" | None`. None = default
   routing. Setting it bypasses the resolver's tier preference for
   that single call.
2. **CLI flag**: `korpha run --use-oauth <command>` or
   `--use-api <command>` flips the default for the duration of the
   invocation.

All overrides are logged to `Activity` with `override_reason` so the
founder can review unexpected routing in monthly review.

### `Product` (new)

Products are leaves under BusinessUnits.

```python
class ProductKind(StrEnum):
    BOOK = "book"               # KDP
    DESIGN = "design"           # POD
    COURSE = "course"           # Info
    EBOOK = "ebook"             # Info
    NEWSLETTER = "newsletter"   # Info
    SAAS_APP = "saas_app"       # SaaS
    CAMPAIGN = "campaign"       # Affiliate — time-bound
    CUSTOM = "custom"


class Product(SQLModel, table=True):
    __tablename__ = "business_product"

    id: UUID = primary_key_field()
    business_unit_id: UUID = Field(
        foreign_key="business_unit.id", index=True,
    )
    business_id: UUID = Field(foreign_key="business.id", index=True)
    kind: ProductKind = Field(default=ProductKind.CUSTOM, index=True)

    name: str
    slug: str = Field(index=True)

    # Time-bound products (kind=CAMPAIGN) populate these.
    starts_at: datetime | None = None
    ends_at: datetime | None = None

    # Free-form attributes. Keep schemaless until we have 3+ consumers
    # wanting structure. Examples by ProductKind:
    #
    #   BOOK (KDP):
    #     {"asin": "B0CXXXXX", "isbn_13": "9781234567890",
    #      "kdp_select": True, "kindle_unlimited_pages": 312,
    #      "categories": ["Highland Romance", "Scottish Historical"],
    #      "keywords_7slots": ["highland romance", ...]}
    #
    #   DESIGN (POD):
    #     {"design_sku": "CL-CAT-001", "platforms": ["printful", "etsy", "merch"],
    #      "file_paths": {"png_4500": "...", "svg": "..."},
    #      "size_options": ["S","M","L","XL","2XL"]}
    #
    #   CAMPAIGN (Affiliate):
    #     {"vendor": "marketro", "platform": "jvzoo", "product_ids": {"funnel": "438771", "bundle": "438791"},
    #      "commission_pct": 50, "leaderboard_url": "...",
    #      "bonus_stack_id": "..."}
    #
    #   SAAS_APP:
    #     {"deploy_url": "https://korpha.app", "repo": "github.com/...",
    #      "stripe_product_id": "prod_...", "mrr_current_usd": 14200}
    attributes: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON),
    )

    status: str = Field(default="active")  # active | paused | archived
    created_at: datetime = timestamp_field()
    updated_at: datetime = timestamp_field()
```

**Why not collapse `Product` into `BusinessUnit`?**
Products are leaves — they never have children. They're operational
*work output*, not org structure. Separating them lets BusinessUnit
queries (kanban scoping, unit tree walks) skip leaves cheaply.

### `NicheProfile` (embedded JSON shape, not a table)

Embedded on `BusinessUnit.niche_profile`:

```python
class NicheProfile(BaseModel):
    """Validated shape; stored as JSON on BusinessUnit."""

    core_topics: list[str] = []
    adjacent_topics: list[str] = []
    off_limits_topics: list[str] = []
    persona: str = ""               # "marketing managers at 5-50 person SaaS"
    list_size: int = 0
    avg_open_rate: float = 0.0
    avg_click_rate: float = 0.0
    avg_epc: float = 0.0
    last_burned_at: datetime | None = None
    last_burn_unsubscribes: int = 0
    # Promo-fatigue tracking. Even compatible promos depress opens
    # when fired too frequently. niche.score_fit applies a decay
    # penalty when last_promoted_at is recent (< 14 days = -0.3 score,
    # 14-28 days = -0.15, >28 days = no penalty).
    last_promoted_at: datetime | None = None
    promos_in_last_30_days: int = 0
    notes: str = ""
```

Pydantic validates on load/save. Backward-compat: missing fields
default to safe values.

### `ExternalServiceAccount` (new — replaces `ProviderAccount` long-term)

```python
class ExternalServiceKind(StrEnum):
    # AI / LLM
    LLM_OPENAI_COMPAT = "llm_openai_compat"  # any OpenAI-compat (existing 17 presets)
    LLM_ANTHROPIC = "llm_anthropic"

    # Non-LLM AI
    IMAGE_GEN = "image_gen"
    TTS = "tts"
    STT = "stt"
    BG_REMOVAL = "bg_removal"

    # Commerce / ops
    STRIPE = "stripe"
    PAYPAL = "paypal"
    JVZOO = "jvzoo"

    # Email / messaging
    RESEND = "resend"
    SENDGRID = "sendgrid"
    MAILGUN = "mailgun"

    # Publishing platforms
    KDP_API = "kdp_api"
    PRINTFUL = "printful"
    PRINTIFY = "printify"
    ETSY = "etsy"
    GUMROAD = "gumroad"

    # Infra
    CLOUDFLARE = "cloudflare"
    VPS_HOST = "vps_host"
    DOMAIN_REGISTRAR = "domain_registrar"


class ExternalServiceAccount(SQLModel, table=True):
    __tablename__ = "external_service_account"

    id: UUID = primary_key_field()
    business_id: UUID = Field(foreign_key="business.id", index=True)
    business_unit_id: UUID | None = Field(
        foreign_key="business_unit.id", index=True, default=None,
    )  # NULL = company-wide default

    service: ExternalServiceKind = Field(index=True)
    label: str  # human-readable: "Korpha Stripe — main"
    provider_meta: dict[str, Any] = Field(  # provider-specific
        default_factory=dict, sa_column=Column(JSON),
    )

    # Encrypted credentials — uses existing secrets vault (#208).
    # Stores Fernet-encrypted JSON: {"api_key": "...", "secret": "..."}.
    credentials_encrypted: bytes

    # Enforcement
    spending_cap_usd_per_month: float | None = None
    spending_used_this_month_usd: float = 0.0
    spending_cap_resets_at: datetime | None = None
    rate_limit_meta: dict[str, Any] | None = Field(
        default=None, sa_column=Column(JSON),
    )

    is_active: bool = Field(default=True)
    last_used_at: datetime | None = None

    created_at: datetime = timestamp_field()
    updated_at: datetime = timestamp_field()
```

**Resolution algorithm**:

```python
def resolve_account(
    session: Session,
    business_unit_id: UUID,
    service: ExternalServiceKind,
) -> ExternalServiceAccount | None:
    """Walk up the BusinessUnit tree, returning the most specific
    active account for the requested service. Falls back to
    company-wide (business_unit_id IS NULL) before failing."""
    unit_id: UUID | None = business_unit_id
    while unit_id is not None:
        account = session.exec(
            select(ExternalServiceAccount)
            .where(
                ExternalServiceAccount.business_unit_id == unit_id,
                ExternalServiceAccount.service == service,
                ExternalServiceAccount.is_active == True,  # noqa: E712
            )
        ).first()
        if account and not _cap_exhausted(account):
            return account
        unit = session.get(BusinessUnit, unit_id)
        unit_id = unit.parent_id if unit else None

    # Fall back to company-wide
    return session.exec(
        select(ExternalServiceAccount)
        .where(
            ExternalServiceAccount.business_unit_id.is_(None),
            ExternalServiceAccount.service == service,
            ExternalServiceAccount.is_active == True,
        )
    ).first()


def _cap_exhausted(account: ExternalServiceAccount) -> bool:
    """Skip accounts whose monthly cap is hit (don't use them).
    Resolver moves to the next candidate up the tree."""
    if account.spending_cap_usd_per_month is None:
        return False
    return account.spending_used_this_month_usd >= account.spending_cap_usd_per_month
```

**Cap enforcement**: on every successful call, increment
`spending_used_this_month_usd` by the call cost (from existing
`CostTracker`). When cap is hit:

1. Mark the account as exhausted (cap reached).
2. Resolver moves to the next-up parent account.
3. Surface alert in the founder's monthly review.

Caps reset on `spending_cap_resets_at` (default first of month UTC).

### `SharedResource` (new)

Pattern-1 infra (AI model mesh, shared Cloudflare account, GPU compute).

```python
class SharedResourceKind(StrEnum):
    AI_MODEL = "ai_model"        # z-image-turbo, Whisper, Kokoro, …
    COMPUTE = "compute"          # GPU pool
    DOMAIN_POOL = "domain_pool"
    HOST_POOL = "host_pool"
    SHARED_ACCOUNT = "shared_account"  # the Cloudflare account itself
    OAUTH_CLI = "oauth_cli"      # claude-code / codex / opencode / cursor / gemini / acpx / pi
    #                            # — physically one-OAuth-per-machine; local-mode only


class SharedResource(SQLModel, table=True):
    __tablename__ = "shared_resource"

    id: UUID = primary_key_field()
    business_id: UUID = Field(foreign_key="business.id", index=True)

    kind: SharedResourceKind
    name: str                                  # "z-image-turbo", "Kokoro TTS", "claude-code"
    label: str

    # Who built / hosts this. Nullable — some resources are just
    # rented services without a "host" line.
    host_business_unit_id: UUID | None = Field(
        foreign_key="business_unit.id", default=None,
    )

    # Access endpoint (URL or skill name) + opaque config.
    endpoint: str | None = None
    config: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON),
    )

    # Cost model — how to attribute usage.
    cost_model: str = "tracked_not_charged"
    # one of: "tracked_not_charged" (default — v1), "per_call",
    # "metered", "fixed_monthly_with_credits", "subscription_quota"
    fixed_monthly_cost_usd: float | None = None

    # Deployment-mode gating. OAuth CLI resources set this to ["local"]
    # because SaaS deployments physically cannot share an OAuth token
    # across tenants (ToS + technical constraint). The resolver filters
    # resources by this list at enumeration time.
    available_in_modes: list[str] = Field(
        default_factory=lambda: ["local", "saas"],
        sa_column=Column(JSON),
    )

    # Subscription quota tracking — used for OAuth CLI resources where
    # the constraint is a rolling window (e.g. Claude.ai 5h window,
    # ChatGPT Plus message cap). None for unmetered/local resources.
    quota_window_seconds: int | None = None   # 18000 for Claude.ai 5h
    quota_limit_in_window: int | None = None  # e.g. 50 Claude messages
    quota_calls_in_window: int = 0
    quota_window_started_at: datetime | None = None

    is_active: bool = Field(default=True)
    created_at: datetime = timestamp_field()
    updated_at: datetime = timestamp_field()


class SharedResourceUsage(SQLModel, table=True):
    """Append-only usage log per (resource, consumer unit)."""
    __tablename__ = "shared_resource_usage"

    id: UUID = primary_key_field()
    resource_id: UUID = Field(foreign_key="shared_resource.id", index=True)
    consumer_unit_id: UUID = Field(foreign_key="business_unit.id", index=True)
    used_at: datetime = timestamp_field()
    units_consumed: float = 1.0      # calls, tokens, seconds, etc.
    cost_attributed_usd: float = 0.0 # 0 in v1 (tracked-not-charged)
    skill_name: str | None = None    # which skill triggered usage
    notes: str | None = None
```

### `CooperationProposal` (new)

Pattern-2 cross-line proposals.

```python
class CooperationStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    ESCALATED_CEO = "escalated_ceo"
    ESCALATED_FOUNDER = "escalated_founder"
    EXPIRED = "expired"


class CooperationProposal(SQLModel, table=True):
    __tablename__ = "cooperation_proposal"

    id: UUID = primary_key_field()
    business_id: UUID = Field(foreign_key="business.id", index=True)

    from_unit_id: UUID = Field(foreign_key="business_unit.id", index=True)
    to_unit_id: UUID = Field(foreign_key="business_unit.id", index=True)

    summary: str
    details: str = ""           # markdown allowed
    proposed_terms: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON),
    )

    # Permissions the proposal requests / grants. Read by the
    # ``ask_about`` authorization check and by ``memory.recall``
    # when validating cross-namespace grants. Free-form keys so new
    # permission types can be added without schema changes.
    #
    # Known keys (v1):
    #   "cross_tree_query": bool          — allow ask_about across tree
    #   "cross_namespace_recall": bool    — allow memory.recall across ns
    #   "promo_slot_count": int           — affiliate slot allocation
    #   "royalty_share_pct": float        — POD/KDP cross-promo splits
    permissions: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON),
    )

    status: CooperationStatus = CooperationStatus.PROPOSED
    decision_note: str | None = None
    decided_by_agent_role_id: UUID | None = None

    created_at: datetime = timestamp_field()
    decided_at: datetime | None = None
    expires_at: datetime | None = None
```

### How a CooperationProposal grants memory access

When a proposal carrying `permissions["cross_namespace_recall"] = True`
transitions to `ACCEPTED`, the system **automatically** creates a
matching `CrossNamespaceRecallGrant` row (see Memory Namespacing):

```python
def on_cooperation_accepted(
    session: Session, proposal: CooperationProposal,
) -> None:
    """Hook fires when proposal.status transitions to ACCEPTED.
    Auto-creates derived grants based on permissions JSON."""
    if proposal.permissions.get("cross_namespace_recall"):
        from_unit = session.get(BusinessUnit, proposal.from_unit_id)
        to_unit = session.get(BusinessUnit, proposal.to_unit_id)
        session.add(CrossNamespaceRecallGrant(
            from_namespace_id=from_unit.memory_namespace_id,
            to_namespace_id=to_unit.memory_namespace_id,
            cooperation_proposal_id=proposal.id,
            granted_at=datetime.now(timezone.utc),
            expires_at=proposal.expires_at,
            granted_by_agent_role_id=proposal.decided_by_agent_role_id,
            is_active=True,
        ))
    # cross_tree_query permission is read directly off the proposal
    # at ask_about authorization time; no derived row needed.
```

When the proposal is later **revoked** (founder action, expiry, or
cooperation breakdown), the hook flips `is_active=False` on the
derived grant so subsequent `memory.recall` calls are immediately
blocked — no stale tokens leaking memory access after the
relationship ends.

### FK additions to existing models

| Existing model | New field | Why |
|---|---|---|
| `KanbanCard` | `business_unit_id: UUID | None` (nullable for backfill) | Scope kanban by unit |
| `Goal` | `business_unit_id: UUID | None` | Scope goals by unit |
| `Approval` | `business_unit_id: UUID | None` | Scope approvals + new `action_class=STRATEGIC` |
| `Activity` | `business_unit_id: UUID | None` | Per-unit activity feed |
| `AgentRole` | `business_unit_id: UUID | None` | Agents live inside a unit |
| `CostLog` | `business_unit_id: UUID | None` | Per-unit P&L truth |
| `ExternalServiceAccount` rebill | use `business_unit_id` for charge attribution |

All new fields are nullable initially. After backfill (every existing
row gets the business's default unit), make them non-null in a
follow-up migration.

### Index strategy

- `BusinessUnit.parent_id` indexed for tree walks.
- `BusinessUnit.business_id, slug` composite for fast resolution.
- `BusinessUnit.memory_namespace_id` unique (memory ABC lookup hot path).
- `ExternalServiceAccount.business_unit_id, service` composite for the
  resolver hot path.
- `Product.business_unit_id, status` composite for unit dashboards.
- Memory tables: `(namespace_id, ...)` composite indexes; vector
  indexes partitioned by namespace_id (see Memory Namespacing).

## Memory Namespacing — Hard Isolation Between Units

Memory is the highest-risk surface for cross-pollution. A POD designer
agent doing batch image generation must not see KDP Romance editorial
notes via a similarity-search hit on a shared embedding index. The
hybrid model (see `ORG_MODEL.md`) commits memory to *hard isolation*
while operational tables stay soft-tagged.

### Schema changes

`BusinessUnit.memory_namespace_id` is the partition key. Every memory
record carries `namespace_id`:

```python
class AgentMemory(SQLModel, table=True):
    # ... existing fields ...
    namespace_id: UUID = Field(index=True)
    # = owning unit's memory_namespace_id; non-null after backfill.

class VectorMemoryShard(SQLModel, table=True):
    # ... existing fields ...
    namespace_id: UUID = Field(index=True)
    # Vector index queries always include WHERE namespace_id = ?.

class AgentTranscript(SQLModel, table=True):
    # ... existing fields ...
    namespace_id: UUID = Field(index=True)
```

The `MEMORY.md` + `USER.md` parity files (from #192) get a per-unit
copy under each unit's filesystem subtree (see Filesystem Layout).

### Recall API change

`memory.recall(query, namespace_id=None, top_k=10)`:

- If `namespace_id` is omitted, the skill defaults to the calling
  agent's BusinessUnit's namespace. Most callers never pass it.
- If a different `namespace_id` is passed, the skill checks for an
  active `CooperationProposal` granting `cross_namespace_recall`
  permission. Without one, the skill raises
  `SkillError("cross-namespace memory access not authorized")`.
- The skill enforces at the **API layer**. Even if the agent's
  instructions tell it to ignore the rule, the skill refuses. No
  prompt injection path to memory access.

```python
class RecallSkill(Skill):
    async def run(self, *, ctx, args):
        requested_ns = args.get("namespace_id")
        own_ns = ctx.agent_role.business_unit.memory_namespace_id

        if requested_ns is None:
            namespace_id = own_ns
        elif UUID(requested_ns) == own_ns:
            namespace_id = own_ns
        else:
            target_ns = UUID(requested_ns)
            grant = _find_active_recall_grant(
                ctx.session, from_ns=own_ns, to_ns=target_ns,
            )
            if grant is None:
                raise SkillError(
                    f"cross-namespace memory access not authorized: "
                    f"{own_ns} → {target_ns}"
                )
            namespace_id = target_ns
            _log_cross_namespace_recall(ctx.session, grant.id)

        return _do_recall(
            ctx.session, namespace_id=namespace_id,
            query=args["query"], top_k=int(args.get("top_k", 10)),
        )
```

### Vector index partitioning

Vector indexes are **partitioned** by namespace_id, not just filtered.
Reasons:

1. **No similarity contamination.** Filtering after similarity scoring
   could still bias retrieval. Partitioning at index level guarantees
   the calling unit's index does not even contain other units' data.
2. **Index size stays small per unit.** Smaller indexes mean better
   cosine-similarity precision and faster recall latency.
3. **Adding/removing a unit doesn't rebalance the whole index.**

For Postgres + pgvector specifically: use partial indexes per
namespace (`CREATE INDEX ... USING ivfflat (embedding) WHERE
namespace_id = '...'`) or PostgreSQL table partitioning by
namespace_id. We default to partial indexes for v1 (simpler) and can
migrate to table partitioning if we hit scale (10K+ namespaces in a
single Postgres instance).

For SQLite mode (single-user local dev), partial indexes work the
same way.

**Index naming scheme.** Postgres caps identifier names at 63
characters. A naive `agent_mem_vec_<full_uuid>` produces 13 + 36 = 49
chars which fits, but a future double-prefix (e.g. environment-name
appended) would overflow. To stay safe + readable:

```
agent_mem_vec_<base32(uuid)[:13]>
vec_shard_<base32(uuid)[:13]>
transcript_<base32(uuid)[:13]>
```

13 chars of base32 = 65 bits of namespace, more than enough to avoid
collisions for the foreseeable future. Full UUID stays queryable via
the `namespace_id` column; the truncation is only for the index name.

```python
def _index_name(prefix: str, namespace_id: UUID) -> str:
    """Stable, collision-resistant short ID for index naming.
    Postgres caps at 63 chars; <prefix>_<13chars> stays well under."""
    short = base64.b32encode(namespace_id.bytes).decode("ascii").rstrip("=")[:13].lower()
    return f"{prefix}_{short}"
```

### Cross-namespace recall grants

`CrossNamespaceRecallGrant` (new):

```python
class CrossNamespaceRecallGrant(SQLModel, table=True):
    __tablename__ = "cross_namespace_recall_grant"

    id: UUID = primary_key_field()
    from_namespace_id: UUID = Field(index=True)  # who can read
    to_namespace_id: UUID = Field(index=True)    # what they can read
    cooperation_proposal_id: UUID = Field(
        foreign_key="cooperation_proposal.id", index=True,
    )
    granted_at: datetime = timestamp_field()
    expires_at: datetime | None = None
    granted_by_agent_role_id: UUID
    is_active: bool = Field(default=True)
    # Audit: each grant ties back to the CooperationProposal that
    # authorized it. Founder can revoke any grant by setting
    # is_active=False.
```

Grants are rare. The default cooperation path is `ask_about` (see
New Skills), which dispatches a structured question to the target
unit's owner agent and gets a structured response — no memory access
needed. Recall grants exist only for cases where the target unit's
agent agrees the asker should be able to search its archive directly
(e.g. a Series Lead granting its Type Manager full search access for
a quarterly review).

### Migration backfill

On migration, every existing memory row gets the `namespace_id` of
its owning agent's default BusinessUnit (the auto-created one from
the BusinessUnit migration step). Idempotent + reversible:

```python
async def backfill_memory_namespaces(session: Session) -> None:
    """Assign namespace_id to every existing memory row based on
    the agent role's default unit. Safe to re-run."""
    rows = session.exec(
        select(AgentMemory)
        .where(AgentMemory.namespace_id.is_(None))
    )
    for memory in rows:
        agent = session.get(AgentRole, memory.agent_role_id)
        if agent is None or agent.business_unit_id is None:
            continue  # orphaned; surfaced in monthly review
        unit = session.get(BusinessUnit, agent.business_unit_id)
        memory.namespace_id = unit.memory_namespace_id
        session.add(memory)
    session.commit()
```

Same pattern for `VectorMemoryShard` and `AgentTranscript`. After
backfill is verified clean across one release cycle, make
`namespace_id` non-null.

### Why this matters in practice

Pre-hybrid (today, all memories in one shared table): a POD agent
running batch image generation could surface KDP Romance's reader-
survey notes by accidental cosine-similarity match. The two domains
differ in audience and intent but share enough surface vocabulary
("audience reaction to last batch") that vector recall blends them.
Result: drift in the POD agent's behavior, slow degradation of POD
quality, and the founder cannot easily diagnose why.

After the hybrid, the POD agent's vector index physically does not
contain KDP Romance's data. The mistake is no longer possible to
make — not "shouldn't happen", *cannot happen at the index level*.

## New Skills

### `hr.start_business_line(kind: str, name: str, playbook: str | None)`

Spawns a Line VP and the Line BusinessUnit.

```python
class StartBusinessLineSkill(Skill):
    spec = SkillSpec(
        name="hr.start_business_line",
        description=(
            "Start a new business line under the founder's Business. "
            "Creates the Line BusinessUnit + Line VP agent role + "
            "loads the playbook skill pack if provided."
        ),
        parameters={
            "kind": "One of: pod | kdp | info | saas | affiliate | custom",
            "name": "Display name — e.g. 'Print on Demand'",
            "playbook": "Skill pack ID from skill hub (optional)",
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )
    # Returns: BusinessUnit.id + AgentRole.id of the new Line VP
```

Sister skills:

- `hr.spawn_type_manager(parent_unit_id, type_name, playbook)` — creates
  Type Mgr under a Line VP.
- `hr.spawn_audience_manager(parent_unit_id, niche_profile)` — creates
  Audience Mgr (for Affiliate Line).
- `hr.spawn_product_vp(parent_unit_id, product_name)` — creates Product
  VP under SaaS Line.
- `hr.pause_business_unit(unit_id, reason)` — soft-pause (no
  cards run, no work claimed).
- `hr.resume_business_unit(unit_id)` — undo pause.
- `hr.archive_business_unit(unit_id)` — soft-delete after wind-down.

### `niche.score_fit(unit_id, work_summary, work_topics)`

Compatibility check. Returns a score 0.0–1.0 + verdict.

```python
class ScoreNicheFitSkill(Skill):
    spec = SkillSpec(
        name="niche.score_fit",
        description=(
            "Score a new piece of work (new campaign, new product, "
            "incoming JV invitation) against a BusinessUnit's niche "
            "profile. Returns score + recommendation (accept/decline/"
            "escalate)."
        ),
        parameters={
            "unit_id": "UUID of the BusinessUnit being evaluated",
            "work_summary": "Short summary of the proposed work",
            "work_topics": "List of topic tags the work covers",
        },
        ...
    )
```

Scoring algorithm (deterministic — no LLM in v1):

```
# Step 1: topic-relevance base score
base = (
    sum(weight=1.0 for t in work_topics if t in core_topics)
  + sum(weight=0.5 for t in work_topics if t in adjacent_topics)
  - sum(weight=2.0 for t in work_topics if t in off_limits_topics)
) / max(len(work_topics), 1)
base = clamp(base, 0.0, 1.0)

# Step 2: promo-fatigue decay (avoid burning compatible-but-tired list)
days_since_promo = days_since(last_promoted_at) if last_promoted_at else infinity
fatigue_penalty = (
    0.30 if days_since_promo < 14
    else 0.15 if days_since_promo < 28
    else 0.00
)
# Plus density penalty if many promos in last 30 days
density_penalty = min(0.20, max(0, promos_in_last_30_days - 2) * 0.05)

score = clamp(base - fatigue_penalty - density_penalty, 0.0, 1.0)

verdict =
  ACCEPT  if score >= 0.7
  DECLINE if score <= 0.2 or any(t in off_limits_topics for t in work_topics)
  ESCALATE otherwise (CEO decides)
```

V2 can swap in an LLM-driven scorer once we have ground-truth training
data from founder accept/decline patterns.

### `cooperation.propose(from_unit, to_unit, summary, terms)`

Line VP proposes cross-line cooperation. Creates a
`CooperationProposal` and notifies the target unit.

### `cooperation.decide(proposal_id, decision, note)`

Target unit accepts/declines. If they can't decide and want CEO input,
they call `cooperation.escalate(proposal_id)`.

### `cooperation.ask_about(target_unit_id, question, context, response_schema)` — the phone-call API

Cooperation requests never grant direct memory access. The asking
agent dispatches a structured question to the target unit's owner
agent. That agent processes with **its own** scoped memory access and
returns a response. The asker receives only the response — the target
unit's memories, transcripts, customer data are never touched.

```python
class AskAboutSkill(Skill):
    spec = SkillSpec(
        name="cooperation.ask_about",
        description=(
            "Ask another BusinessUnit's owner agent a structured "
            "question. Never grants direct memory access — the "
            "target unit's agent processes with ITS OWN scoped "
            "memory and returns a response. Authorized by default "
            "for sibling units + ancestor/descendant axis; "
            "cross-tree queries require an active CooperationProposal."
        ),
        parameters={
            "target_unit_id": "UUID of the target BusinessUnit",
            "question": "Structured question; the target agent reads + responds",
            "context": "Optional explicit context the asker offers (not memory; explicit text)",
            "response_schema": "Optional JSON schema the asker wants for the response",
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(self, *, ctx, args):
        target_unit_id = UUID(args["target_unit_id"])
        target_unit = ctx.session.get(BusinessUnit, target_unit_id)
        if target_unit is None:
            raise SkillError(f"unit {target_unit_id} not found")

        # Authorization check — sibling / ancestor / descendant default,
        # cross-tree requires CooperationProposal grant.
        if not _ask_about_authorized(
            session=ctx.session,
            from_unit_id=ctx.agent_role.business_unit_id,
            to_unit_id=target_unit_id,
        ):
            raise SkillError(
                f"cross-tree query from {ctx.agent_role.business_unit_id} "
                f"to {target_unit_id} requires CooperationProposal"
            )

        # Audit-log the query (founder visibility in monthly review)
        log_cross_unit_query(
            session=ctx.session,
            from_unit_id=ctx.agent_role.business_unit_id,
            to_unit_id=target_unit_id,
            question_summary=args["question"][:200],
        )

        # Dispatch to target unit's owner agent. That agent runs with
        # its OWN namespace_id — no memory leak possible.
        target_agent = get_owner_agent(ctx.session, target_unit)
        response = await dispatch_question(
            target_agent=target_agent,
            question=args["question"],
            context=args.get("context"),
            response_schema=args.get("response_schema"),
        )
        return SkillResult(
            skill_name=self.spec.name,
            payload={"response": response},
        )
```

#### Authorization rules

| From → To | Authorized? |
|---|---|
| Sibling units (same parent) | ✓ default |
| Descendant → ancestor | ✓ default |
| Ancestor → descendant | ✓ default |
| Unrelated cross-tree | requires `CooperationProposal` granting `cross_tree_query` permission |

#### Audit log

Every cross-unit `ask_about` call inserts a `CrossUnitQueryLog` row:

```python
class CrossUnitQueryLog(SQLModel, table=True):
    __tablename__ = "cross_unit_query_log"

    id: UUID = primary_key_field()
    business_id: UUID = Field(foreign_key="business.id", index=True)
    from_unit_id: UUID = Field(foreign_key="business_unit.id", index=True)
    to_unit_id: UUID = Field(foreign_key="business_unit.id", index=True)
    asked_by_agent_role_id: UUID = Field(foreign_key="agent_role.id", index=True)
    question_summary: str       # first 200 chars
    response_summary: str | None = None  # first 200 chars; logged after dispatch
    asked_at: datetime = timestamp_field()
```

Surfaced in monthly review as a cooperation map: *"Affiliate Line VP
asked KDP Romance Type Mgr about bonus stacks 4× this month → 3
accepts, 1 decline"*. Lets the founder see whether cross-unit
discovery is producing value or wasting agent time.

### Shared-resource skills (Pattern 1)

These are exposed by the Vidyo GPU mesh plugin (or any other
mesh-style provider). Skills register through the existing plugin
contract; nothing about how an agent calls them changes.

- `image.generate(prompt, model="z-image-turbo", size, style)`
- `image.remove_background(image_url_or_bytes)`
- `audio.synthesize(text, voice="kokoro:joe" | "omnivoice:cloned-andrew")`
- `audio.transcribe(audio_url_or_bytes, model="whisper", language)`

Each call:

1. Logs to `SharedResourceUsage` with the calling unit ID.
2. Increments resource counters.
3. Returns the result (image URL, audio URL, transcript, etc.) to the
   caller.

Attribution comes from the SkillContext — the agent role's
`business_unit_id` is the consumer.

### `credentials.set(unit_id, service, label, credentials, cap)`

Founder-only skill (gated by approval) to add a per-unit API key.

```python
class SetCredentialsSkill(Skill):
    spec = SkillSpec(
        name="credentials.set",
        description=(
            "Add or update an external service API key scoped to a "
            "BusinessUnit (or company-wide if unit_id is null). "
            "Credentials are encrypted via the secrets vault before "
            "storage. Requires founder approval (CODE_CHANGE class)."
        ),
        parameters={
            "unit_id": "BusinessUnit ID, or null for company-wide default",
            "service": "ExternalServiceKind value (openai, stripe, etc.)",
            "label": "Human-readable name",
            "credentials": "Object with api_key + any service-specific fields",
            "spending_cap_usd_per_month": "Optional monthly cap",
        },
        ...
    )
```

Setup ergonomics: invoked automatically by `hr.start_business_line`
when the new Line VP asks the founder *"separate API key for this
line?"* — yes path triggers `credentials.set`; no path leaves the
account record absent and resolver falls back up the tree.

## Migration

### Step 1 — schema

Three Alembic migrations land together (single revision):

1. Create `business_unit`, `business_product`, `external_service_account`,
   `shared_resource`, `shared_resource_usage`, `cooperation_proposal`.
2. Add nullable `business_unit_id` columns on `kanban_card`,
   `agent_goal`, `approval`, `activity`, `agent_role`, `cost_log`.
3. Add new approval `action_class` enum value `STRATEGIC`.

### Step 2 — backfill

On first boot post-migration:

```python
async def backfill_default_units(session: Session) -> None:
    """For every existing Business with no BusinessUnit, create a
    default unit and reattach all existing rows to it."""
    businesses = session.exec(select(Business)).all()
    for biz in businesses:
        existing = session.exec(
            select(BusinessUnit)
            .where(BusinessUnit.business_id == biz.id)
        ).first()
        if existing:
            continue
        default_unit = BusinessUnit(
            business_id=biz.id,
            parent_id=None,
            kind=BusinessUnitKind.DEFAULT,
            name=biz.name,
            slug=slugify(biz.name),
            owner_agent_role_id=_find_ceo_role(session, biz.id),
        )
        session.add(default_unit)
        session.flush()  # need ID for backfill

        for model in (KanbanCard, Goal, Approval, Activity, AgentRole, CostLog):
            session.exec(
                update(model)
                .where(model.business_id == biz.id)
                .where(model.business_unit_id.is_(None))
                .values(business_unit_id=default_unit.id)
            )
    session.commit()
```

Idempotent — safe to re-run.

### Step 3 — make required (follow-up migration)

After backfill confirmed clean across all installs (1 release cycle
later), make `business_unit_id` non-null on the FK columns. This is a
separate migration to keep the first one reversible.

### Step 4 — opt-in CLI for splitting

```bash
korpha business split-into-lines
```

Interactive flow: walks founder through which lines they want, lets
them assign existing kanban cards to the new Line VPs, archives the
default unit if everything migrates out.

## Resolution: How an agent uses an external service

The resolver is deployment-mode-aware AND tier-aware. Local install with
OAuth CLIs available routes differently from SaaS or workhorse-tier work.

### Full resolution algorithm

```python
def resolve_credentials(
    session: Session,
    business_unit_id: UUID,
    service: ExternalServiceKind,
    tier: InferenceTier,
    deployment_mode: DeploymentMode,
) -> ResolvedCredentials:
    """Returns credentials for the calling agent's external service
    call. Walks the BusinessUnit tree, respecting deployment mode +
    tier separation. Raises SkillError if nothing resolves."""

    # 1. SaaS mode: OAuth CLIs are unavailable. Go straight to API key
    #    resolution.
    if deployment_mode == DeploymentMode.SAAS:
        account = _resolve_external_service_account(
            session, business_unit_id, service,
        )
        if account is None:
            raise SkillError(
                f"No {service} account available for unit "
                f"{business_unit_id}. SaaS mode requires per-unit or "
                f"company-default API keys."
            )
        return ResolvedCredentials.from_account(account)

    # 2. LOCAL mode, PRO tier: prefer OAuth CLI if available + quota OK.
    if tier == InferenceTier.PRO:
        oauth = _find_oauth_cli_for_service(session, service)
        if oauth is not None and not _oauth_quota_exhausted(oauth):
            return ResolvedCredentials.from_shared_resource(oauth)
        # Fall through to API key if OAuth quota is exhausted

    # 3. LOCAL mode, WORKHORSE tier OR (PRO with OAuth exhausted):
    #    walk the unit tree for an API key.
    account = _resolve_external_service_account(
        session, business_unit_id, service,
    )
    if account is None:
        raise SkillError(
            f"No {service} account available for unit "
            f"{business_unit_id}. Have your Line VP set one up "
            f"via `korpha credentials set`."
        )
    return ResolvedCredentials.from_account(account)


def _resolve_external_service_account(
    session: Session,
    business_unit_id: UUID,
    service: ExternalServiceKind,
) -> ExternalServiceAccount | None:
    """Walk up the BusinessUnit tree, returning the most specific
    active uncapped account for the requested service. Falls back to
    company-wide (business_unit_id IS NULL) before returning None."""
    unit_id: UUID | None = business_unit_id
    while unit_id is not None:
        account = session.exec(
            select(ExternalServiceAccount)
            .where(
                ExternalServiceAccount.business_unit_id == unit_id,
                ExternalServiceAccount.service == service,
                ExternalServiceAccount.is_active == True,  # noqa: E712
            )
        ).first()
        if account and not _cap_exhausted(account):
            return account
        unit = session.get(BusinessUnit, unit_id)
        unit_id = unit.parent_id if unit else None
    # Company-wide fallback
    return session.exec(
        select(ExternalServiceAccount)
        .where(
            ExternalServiceAccount.business_unit_id.is_(None),
            ExternalServiceAccount.service == service,
            ExternalServiceAccount.is_active == True,
        )
    ).first()


def _oauth_quota_exhausted(resource: SharedResource) -> bool:
    """5-hour Claude.ai / ChatGPT quota window tracking.
    Returns True if window is full and not yet rolled over."""
    if resource.quota_window_seconds is None or resource.quota_limit_in_window is None:
        return False
    now = datetime.now(timezone.utc)
    started = resource.quota_window_started_at
    if started is None:
        return False
    window_age = (now - started).total_seconds()
    if window_age > resource.quota_window_seconds:
        # Window expired — caller is responsible for rolling it over
        # on next use, so for now we treat as fresh.
        return False
    return resource.quota_calls_in_window >= resource.quota_limit_in_window


def _find_oauth_cli_for_service(
    session: Session, service: ExternalServiceKind,
) -> SharedResource | None:
    """Find an installed OAuth CLI that can serve this LLM service.
    Mapping: LLM_ANTHROPIC → claude-code; LLM_OPENAI_COMPAT → codex-cli
    (only the OpenAI subset). Other services have no OAuth CLI mapping."""
    cli_name = _OAUTH_CLI_FOR_SERVICE.get(service)
    if cli_name is None:
        return None
    return session.exec(
        select(SharedResource)
        .where(
            SharedResource.kind == SharedResourceKind.OAUTH_CLI,
            SharedResource.name == cli_name,
            SharedResource.is_active == True,
        )
    ).first()


_OAUTH_CLI_FOR_SERVICE = {
    ExternalServiceKind.LLM_ANTHROPIC: "claude-code",
    ExternalServiceKind.LLM_OPENAI_COMPAT: "codex-cli",
    # (extend as more CLIs are mapped: opencode, gemini-cli, etc.)
}
```

### Post-call accounting

```python
# For API-key path: increment per-unit usage + check cap
if isinstance(creds, ApiKeyCredentials):
    creds.account.spending_used_this_month_usd += call_cost_usd
    session.add(creds.account)
    if creds.account.spending_used_this_month_usd >= (
        creds.account.spending_cap_usd_per_month or float("inf")
    ):
        _trigger_cap_alert(session, creds.account)

# For OAuth CLI path: increment quota counter + log SharedResourceUsage
if isinstance(creds, SharedResourceCredentials):
    creds.resource.quota_calls_in_window += 1
    if creds.resource.quota_window_started_at is None:
        creds.resource.quota_window_started_at = datetime.now(timezone.utc)
    session.add(creds.resource)
    session.add(SharedResourceUsage(
        resource_id=creds.resource.id,
        consumer_unit_id=business_unit_id,
        skill_name=current_skill_name,
        units_consumed=1.0,
        cost_attributed_usd=0.0,  # v1: tracked-not-charged
    ))

session.commit()
```

## Filesystem Layout

Mirrors Paperclip's per-company directory pattern, adapted for the
recursive BusinessUnit tree. Each unit gets its own subtree under the
instance directory:

```
~/.korpha/instances/<instance-name>/
├── db/                              # single Postgres data dir (all units share)
├── shared/
│   ├── model-mesh-cache/            # shared-resource output cache
│   ├── plugin-state/                # plugin registry + per-plugin state
│   ├── oauth-cli/                   # claude-code / codex / etc. credentials
│   │   ├── claude-code-config.json
│   │   ├── codex-cli-config.json
│   │   └── ...
│   └── skill-hub-catalog/           # cached skill hub manifests
└── business-units/
    └── <business-unit-id>/
        ├── agents/                  # agent state per role
        │   └── <agent-role-id>/
        │       ├── instructions/    # role prompts + playbook references
        │       ├── transcripts/     # conversation logs (hard-isolated)
        │       └── life/            # agent lifecycle state
        ├── prompt-cache/            # cached system prompts (per unit)
        ├── work-artifacts/          # generated files per kanban card
        ├── memory-blobs/            # vector index shards (namespace-scoped)
        ├── MEMORY.md                # per-unit memory parity file (from #192)
        ├── USER.md                  # per-unit USER profile parity file
        └── backups/                 # local per-unit backups
```

### Per-unit backup/restore

`korpha unit backup <id>` tars the unit's subtree (`business-units/<id>/`)
plus exports its database rows scoped by `(business_unit_id, namespace_id)`
into a single archive. Restore swaps the subtree atomically + reinserts
the DB rows. This is one of the practical wins of the hybrid model:
losing a backup of KDP Romance does **not** require also restoring SaaS
Vidyo.

**What's NOT in the unit backup:**

- `shared/` (model mesh, OAuth CLI configs, plugin state) — these are
  *shared* infrastructure, not owned by any single unit. Operators
  back these up separately via `korpha instance backup-shared`.
- `SharedResource` rows themselves — same reason; they live at the
  Business level, not the unit level.
- `SharedResourceUsage` rows authored by *other* units consuming the
  unit's hosted resources — those are the consumers' attribution
  records, not the host's.

**What IS in the unit backup:**

- The unit's subtree (`business-units/<id>/`)
- `BusinessUnit` row + descendant `BusinessUnit` rows + `Product` rows
- `KanbanCard`, `Goal`, `Approval`, `Activity`, `CostLog` rows where
  `business_unit_id` matches the unit or any descendant
- `AgentRole` rows scoped to the unit
- `ExternalServiceAccount` rows scoped to the unit (with credentials
  re-encrypted on restore — encryption keys do NOT cross-machines)
- `AgentMemory`, `VectorMemoryShard`, `AgentTranscript` rows where
  `namespace_id` matches the unit's memory_namespace_id
- `CrossUnitQueryLog` entries where the unit is `from_unit_id` OR
  `to_unit_id` (both perspectives preserved)
- `CooperationProposal` rows involving the unit
- `SharedResourceUsage` rows where the unit was the **consumer**
  (not where it was the host — those belong to consumers)

### Cross-unit symlinks for shared resources

Shared resources live under `shared/` but the unit subtree may include
read-only symlinks (`business-units/<id>/shared/model-mesh-cache`) so
agents can locate cache hits without coordinating paths. Symlinks are
created at unit creation time.

### SaaS mode

For SaaS deployment, the filesystem layer is replaced by object storage
(S3 / R2) using the same `business-units/<id>/` prefix structure as S3
keys. The Postgres database can stay shared. The `shared/oauth-cli/`
directory is absent in SaaS mode — OAuth CLIs are not available.

## Plugin Contract Extensions

### `inference_provider` plugin (existing) — gains scope-awareness

Existing plugins for LLM providers (#123) work unchanged. New behavior:
the plugin's `register()` function returns provider records that the
resolver consults *after* per-unit `ExternalServiceAccount` checks fail.
This means plugin providers are the "default of last resort" for the
company.

### `shared_resource` plugin (new contract)

Plugins that ship Pattern-1 infrastructure (Vidyo GPU mesh, custom
TTS, etc.) implement:

```python
class SharedResourceProvider(Protocol):
    def register_resources(self) -> list[SharedResource]: ...
    def get_skill_handlers(self) -> dict[str, SkillHandler]: ...
```

On plugin install:

1. `register_resources` runs, creates `SharedResource` rows for each
   model/account/pool the plugin exposes.
2. `get_skill_handlers` returns skills like `image.generate` →
   handler. Skills register in the existing skill registry.

Vidyo example:

```python
class VidyoMeshPlugin:
    def register_resources(self) -> list[SharedResource]:
        return [
            SharedResource(
                kind="ai_model", name="z-image-turbo",
                label="z-image-turbo (Vidyo mesh)",
                endpoint="https://mesh.vidyo.internal/image",
                cost_model="tracked_not_charged",
            ),
            SharedResource(
                kind="ai_model", name="kokoro-tts",
                label="Kokoro TTS (Vidyo mesh)",
                endpoint="https://mesh.vidyo.internal/tts",
                cost_model="tracked_not_charged",
            ),
            # ... whisper, omnivoice, bg-removal ...
        ]

    def get_skill_handlers(self):
        return {
            "image.generate": handle_image_generate,
            "image.remove_background": handle_bg_removal,
            "audio.synthesize": handle_tts,
            "audio.transcribe": handle_whisper,
        }
```

### `line_pack` plugin (new contract) — community Line Packs

```python
class LinePack(Protocol):
    """Packaged playbook for a business line (or sub-type)."""

    pack_id: str        # "kdp-romance@1.2.0"
    line_kind: str      # "kdp" — which line this pack belongs to
    unit_kind: str      # "type" or "line" — what level it slots into

    def setup_unit(self, unit: BusinessUnit, session: Session) -> None:
        """Configure the unit with default niche_profile, kanban
        templates, KPI definitions, recommended worker types, etc."""

    def required_skills(self) -> list[str]:
        """Skill names this pack expects to be installed."""

    def required_services(self) -> list[ExternalServiceKind]:
        """External services this pack needs (KDP API, Resend, etc.).
        Founder is prompted to set credentials for any missing ones
        during pack install."""
```

Ship via skill hub publish/install (#213). Same `pack_skill()` +
`LocalSource.fetch()` machinery already shipped.

## Cross-line Cooperation Flow

```
Step 1: Line VP A publishes work
   → emits event "unit.published" with summary + topics

Step 2: Line VP B subscribes to sibling events on same Business
   → niche.score_fit(B.unit_id, work) → if score >= 0.6, consider cooperation
   → cooperation.propose(from=A, to=B, summary, terms)
   → CooperationProposal status=PROPOSED

Step 3: B's owner agent evaluates
   → if clearly aligned: cooperation.decide(decision=ACCEPTED)
   → if clearly off-niche: cooperation.decide(decision=DECLINED)
   → if borderline: cooperation.escalate() → status=ESCALATED_CEO

Step 4: CEO arbitration (if escalated)
   → CEO sees proposal in its approvals queue (existing flow)
   → CEO decides; if can't, escalate to founder:
   → Approval row with action_class=STRATEGIC enters founder queue

Step 5: Resolution
   → ACCEPTED: both units update their kanbans with the cooperation card
   → DECLINED: no further action; reason recorded
   → Tracked for monthly review attribution
```

### Conflict invariants

- A unit can never accept a cooperation that contradicts its
  `niche_profile.off_limits_topics`. The decision skill enforces this
  hard — even if the agent tries to override, the skill refuses.
- An accepted cooperation auto-creates kanban cards on both sides with
  a `cooperation_id` link.
- `last_burned_at` increments on the receiving unit if a cooperation
  was accepted AND retrospectively flagged as a list burn (e.g. spike
  in unsubscribes within 7 days).

## Workforce Sharing Across Units

Workers (hired via existing `hr.*` skills, #196 / #199) are **shared
across BusinessUnits** by default, not strictly line-scoped. A single
Designer agent can produce POD t-shirt designs for KDP Romance covers
in the same hire. The agent doesn't belong to a Line; the *assignment*
carries the BusinessUnit context.

### Mechanics

`AgentRole` gains optional `business_unit_id` (added in PR3) to track
the agent's *primary* unit — but each `KanbanCard` or `Goal` the agent
works on carries its own `business_unit_id`. When the agent dispatches:

```python
# WorkAssignment carries the context, not the agent itself
@dataclass
class WorkAssignment:
    agent_role_id: UUID
    card_id: UUID
    business_unit_id: UUID    # ← assignment-scoped
    namespace_id: UUID         # ← memory access for THIS assignment
```

The Workforce dispatcher (#199) reads `card.business_unit_id`, looks
up the unit's `memory_namespace_id`, and runs the agent with that
namespace as its memory scope. The agent reads/writes memory in that
namespace only — never crosses into another unit's namespace, even if
the agent is "shared" across units.

### Hiring once vs hiring per-unit

Two valid patterns:

1. **Shared worker** (default): Hire one Designer agent. The Designer
   handles POD t-shirts, KDP covers, Info-product slide visuals. Each
   work item runs in its assigning unit's namespace. The agent's
   *baseline* memory (style preferences, skill expertise) lives in the
   Designer's *own* AgentRole memory; per-unit context lives in the
   unit's namespace and is loaded per-assignment.

2. **Unit-scoped worker** (opt-in): Hire a Designer specifically for
   KDP Romance covers. Set `agent_role.business_unit_id = KDP_Romance_id`.
   The agent can be assigned cards only from its scoped unit (or
   descendants). Useful when the work requires deep domain
   immersion (a pen-name-specific Designer who only sees Highland
   Rogue series style).

Default to pattern 1. Pattern 2 is for cases where domain immersion
matters more than reuse leverage. The Line VP makes the call when
hiring.

### Cost attribution

`CostLog` carries `business_unit_id` (added in PR3) — set from the
assignment, not the agent. A shared Designer agent's costs are
attributed to whichever unit's card it was working on at the time.
P&L roll-up by unit therefore reflects actual workload distribution,
not who "owns" the worker.

## Marketing Concentration in Code

```python
class MarketingScope(StrEnum):
    FOUNDER_BRAND = "founder_brand"   # CEO-owned; rare
    COMPANY_WIDE = "company_wide"     # CEO-owned; rare
    LINE = "line"                     # Line VP owned
    TYPE = "type"                     # Type Mgr owned
    AUDIENCE = "audience"             # Audience Mgr owned
    PRODUCT = "product"               # Product VP owned

# On every marketing.* skill, scope is enforced:
class MarketingSkill(Skill):
    def run(self, *, ctx, args):
        scope = self.spec.parameters["scope"]
        if scope == MarketingScope.LINE:
            assert ctx.agent_role.kind in {AgentKind.LINE_VP, AgentKind.CEO}
        # ... etc
```

This isn't security — it's an architectural guardrail that surfaces
bugs early. CEO can override scope when needed (e.g. for cross-line
emergency announcements) by passing `--ceo-override`, which logs the
override + sends notification.

## Tests

### Unit tests

- `BusinessUnit.parent` walks → returns ancestors in order.
- `BusinessUnit.subtree()` → returns self + descendants.
- `BusinessUnit.memory_namespace_id` is unique and immutable across saves.
- `Product` cannot have children (constraint).
- `NicheProfile` pydantic validation rejects malformed JSON.
- `resolve_credentials` walks tree correctly (5 fixtures: leaf-has-key,
  parent-has-key, grandparent-has-key, company-default,
  nothing-anywhere).
- `_cap_exhausted` skips capped accounts and resolver promotes to
  next.
- `niche.score_fit` returns correct verdicts on 12+ topic-mix
  fixtures (core overlap, adjacent overlap, off-limits hit,
  no overlap).
- **Deployment mode** — SaaS mode never returns OAuth CLI resource.
- **Deployment mode** — local mode + Pro tier picks OAuth CLI when
  quota available; falls through to API key when exhausted.
- **Deployment mode** — local mode + Workhorse tier never picks OAuth CLI.
- `_oauth_quota_exhausted` returns False when window expired
  (roll-over case) and True when within active window at cap.
- **Memory namespacing** — `memory.recall` with no namespace_id
  defaults to caller's unit namespace.
- **Memory namespacing** — `memory.recall` with foreign namespace_id
  + no active grant raises `SkillError`.
- **Memory namespacing** — `memory.recall` with foreign namespace_id
  + active `CrossNamespaceRecallGrant` succeeds; revoking grant
  blocks subsequent calls.
- **Cooperation phone-call** — `cooperation.ask_about` from sibling
  unit succeeds without explicit grant.
- **Cooperation phone-call** — `cooperation.ask_about` cross-tree
  without grant raises authorization error.
- **Cooperation phone-call** — every `ask_about` call inserts one
  `CrossUnitQueryLog` row.

### Integration tests

- Spawn a Line VP via `hr.start_business_line` → BusinessUnit row
  exists, agent role exists, playbook (mock) installs.
- Submit a kanban card to a unit → card's `business_unit_id` set;
  `/app/kanban` filtered view returns it only when unit-filter matches.
- Per-unit OpenAI key with cap=$10 → 10 calls exhaust → resolver
  promotes to parent → parent calls succeed → both accounts'
  usage logs show correct attribution.
- Shared-resource skill (`image.generate`) called from KDP Romance
  unit → `SharedResourceUsage` row attributes consumption to that
  unit; Vidyo (host unit) gets credit in monthly review.
- Cross-line cooperation flow: A proposes, B accepts → both kanbans
  get cards; B declines → A gets refusal with reason; B escalates →
  CEO approval queue populated.
- **Phone-call cooperation** — Affiliate Line VP calls
  `cooperation.ask_about(KDP-Romance, "bonus stackable?")` → KDP
  Romance Type Mgr's owner agent receives the question, processes
  with its own memory namespace, returns structured response.
  Affiliate Line VP receives only the response payload; no row from
  KDP Romance's memory table is fetched by the asking agent.
- **Memory partition** — 2 units with overlapping topic vocabulary
  (e.g. KDP Romance and Info-Products course on writing) → vector
  recall in unit A returns only rows with `namespace_id = A.ns`;
  unit B's similar embedding never surfaces.
- **OAuth CLI shared resource** — Vidyo plugin registers
  `claude-code` as `SharedResource(kind=OAUTH_CLI,
  available_in_modes=["local"])`. In SaaS mode the resource is not
  enumerable; in local mode it is.
- **Filesystem layout** — creating a new BusinessUnit creates
  `~/.korpha/instances/<x>/business-units/<id>/{agents,prompt-cache,work-artifacts,memory-blobs,backups}/`.
- **Per-unit backup** — `korpha unit backup <id>` produces a tar
  that round-trips on restore + reinserts DB rows scoped to the unit.
  Restoring KDP Romance unit does not touch SaaS Vidyo unit's data.

### Migration tests

- Existing single-CEO business + 50 kanban cards + 20 approvals
  → after backfill: 1 default unit, all rows now reference it, no
  data loss.
- Idempotency: re-run backfill → no duplicate units, no row corruption.

### Property tests

- For any unit tree depth N ≤ 8, `resolve_account` returns the
  correct account (most-specific-active-uncapped).
- Niche scoring is deterministic (same inputs → same output).
- Cap-exhaustion never returns an exhausted account; never skips a
  non-exhausted one.

### Regression tests

- Existing 2058-test unit suite passes after migration.
- Existing single-CEO mode works without ever spawning a Line VP.

## Surface Bundle

What the new shape exposes to the founder:

| Surface | What changes |
|---|---|
| Dashboard `/app/kanban` | Unit filter ribbon (All / KDP / SaaS / …) at top |
| Dashboard `/app/monthly` | P&L grouped by unit, drill-down to sub-units |
| Dashboard `/app/units` (new) | Tree view of the org with status pills |
| Dashboard `/app/credentials` (new) | Per-unit credentials manager with cap visualization |
| TUI `/units` (new slash command) | List units, focus one |
| TUI `/coop` (new slash command) | Show pending cooperation proposals |
| CLI `korpha unit list/show/pause/resume/archive` | Unit ops |
| CLI `korpha business split-into-lines` | Migration helper |
| CLI `korpha credentials set/list/cap` | Credentials ops |
| CLI `korpha cooperation list/show/accept/decline` | Cooperation ops |

## Order of Implementation

Suggested PR sequence — each PR is mergeable independently and unit-tests
pass:

1. **PR1** — `BusinessUnit` + `Product` models (incl. `memory_namespace_id`),
   no FK additions yet. Just the new tables + their CRUD. Tests on tree walks.
2. **PR2** — Migration + backfill. Existing data migrates to default units;
   `memory_namespace_id` populated.
3. **PR3** — FK additions on `KanbanCard`, `Goal`, `Approval`,
   `Activity`, `AgentRole`, `CostLog`. Nullable. Backfill those FKs.
4. **PR4** — `DeploymentMode` enum + `ExternalServiceAccount` model +
   deployment-mode-aware + tier-aware hierarchical resolver +
   `credentials.set` skill. Migrate existing `ProviderAccount` rows to
   `ExternalServiceAccount` with `business_unit_id=NULL` (company
   default).
5. **PR5** — `SharedResource` + `SharedResourceUsage` +
   `available_in_modes` + OAuth-CLI quota tracking + plugin contract
   for `SharedResourceProvider`.
6. **PR6** — HR skills (`hr.start_business_line` etc.).
7. **PR7** — `NicheProfile` + `niche.score_fit` skill.
8. **PR8** — `CooperationProposal` + `permissions` field + cooperation
   skills (`propose` / `decide` / `escalate` / `ask_about`) +
   `CrossUnitQueryLog` + CEO arbitration + `STRATEGIC` approval class.
9. **PR9** — Memory namespacing: add `namespace_id` to AgentMemory /
   VectorMemoryShard / AgentTranscript + partitioned vector indexes +
   `CrossNamespaceRecallGrant` + `memory.recall` namespace enforcement.
   Backfill existing memories into default units' namespaces.
10. **PR10** — Filesystem layout: per-unit subtrees under
   `business-units/<id>/` + symlinks to `shared/` + per-unit backup CLI
   (`korpha unit backup <id>`).
11. **PR11** — `LinePack` plugin contract + 6 reference Line Packs
    (POD, KDP, Info, SaaS, Affiliate, Agency) shipped as builtins.
12. **PR12** — Dashboard updates + TUI commands + CLI commands.
13. **PR13** — End-to-end integration test (full scenario from
    ORG_MODEL §Walkthrough).

Each PR adds tests for its own slice. The full suite should grow from
~2058 to ~2500 tests by the end.

## Open Questions for Implementation Time

These are decisions punted until we're writing code, but flag them now:

1. **Event bus shape.** Currently `Activity` rows serve as a passive
   event log. For sibling units to *subscribe* to `unit.published`, we
   need either (a) polling on Activity rows, (b) an in-process pub/sub,
   or (c) an actual message bus. My instinct: start with (a) — every
   minute the system scans recent Activity rows and routes events to
   subscribers. Simple. Move to (b) if latency becomes a complaint.
2. **Niche-compat scorer LLM upgrade.** Deterministic v1 is good enough
   to ship. When we have 1000+ ground-truth accept/decline samples per
   founder, swap in a small fine-tuned classifier. Not yet.
3. **Per-unit playbook hot-reload.** When a community Line Pack
   updates, do existing units pick up the change automatically? My
   instinct: no — pin units to their installed pack version, surface a
   "new playbook version available" notification in the unit's monthly
   review, let the Line VP propose the upgrade as a kanban card the
   founder approves.
4. **Founder-facing pricing of Line Packs.** Free at launch (#213
   skill hub is free). When community contributors want to monetize
   their packs, integrate Stripe-via-skill-hub (orthogonal).

## See Also

- [`docs/ORG_MODEL.md`](../ORG_MODEL.md) — the conceptual / product
  side of this design.
- [`BRIEF.md`](../../BRIEF.md) — original product brief.
- [`docs/PROVIDERS.md`](../PROVIDERS.md) — current LLM provider
  architecture (extends to non-LLM).
- [`docs/SKILLS.md`](../SKILLS.md) — skill system + plugin contracts
  this design extends.
- Skill hub (#213) — packaging mechanism for Line Packs.
- Secrets vault (#208) — encrypts credentials in
  `ExternalServiceAccount.credentials_encrypted`.
