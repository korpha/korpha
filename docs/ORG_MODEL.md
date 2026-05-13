# Korpha Org Model — The Fractal C-Suite

**Status:** Design — proposed, not yet implemented.
**Owner:** Architecture / product.
**Companion doc:** [`docs/dev/BUSINESS_UNITS.md`](./dev/BUSINESS_UNITS.md) — the engineering counterpart.

This document describes the conceptual org model Korpha is moving to.
It exists because the current single-CEO-per-business model breaks for the
target user: a solopreneur who runs multiple **business lines** (Print on
Demand, Amazon KDP, Info Products, SaaS apps, Affiliate Marketing) — each
with a fundamentally different operating model, audience, and playbook.

If you read only one section, read [The Big Insight](#the-big-insight).

---

## The Problem

Today, a `Business` in Korpha has one CEO agent that owns *everything*:
roadmap, marketing, pricing, support, hiring. That works when the business
is a single product. It breaks the moment a real solopreneur tries to use
the tool for what they actually do.

Concrete: Andrew Darius (Marketro LLC) runs:

- **Korpha** — SaaS, recurring revenue, churn-driven economics
- **RankMyAnswer** — SaaS, same shape as Korpha
- **Vidoyo** — SaaS, plus a GPU-mesh that hosts video models
- **Designo / Explaindio** — SaaS in adjacent niche
- **KDP books** — split by genre (Romance, Coloring, Cookbooks, Business
  Non-fiction, Children's…), each genre has its own pen name + audience
- **POD shops** — split by niche (Cat lovers, Software engineers, …),
  each niche has its own Etsy store + design system
- **Info products** — courses, ebooks, memberships
- **Affiliate marketing** — promoting *other* people's launches, where
  list segments per niche define which campaigns can be promoted

A single CEO agent cannot hold all of that in working memory and still
make good decisions. It also can't enforce the constraint that *the
audience built for KDP Romance must never be mailed an AI tool launch* —
that mistake burns lists, refund rates spike, the founder loses sleep.

## The Big Insight

> **The audience is the asset. Products are arrows you fire at audiences.**

A solopreneur with three well-defined audiences (12K AI marketers, 5K
solopreneurs, 2K KDP authors) is materially wealthier than one with 50K
"general business" subscribers, because the targeted lists actually
convert and the off-niche lists actively destroy value.

Every architectural decision in this doc serves that insight. The
recursive BusinessUnit, the per-unit playbooks, the niche-compatibility
gate, the per-unit credentials, the marketing concentration at the Line
VP layer — all of it exists so the AI can **protect the founder's
audience assets from the founder's own short-term temptations**.

## The Model

### Three layers, plus shared infrastructure

```
Founder
└─ Business (legal entity — Marketro LLC, tax ID, bank account)
   ├─ CEO ............... company-wide strategy, capital allocation, conflict arbitration
   ├─ Founder Brand ..... CEO-scoped only (Andrew Darius personal brand)
   │
   ├─ Company-wide Shared Resources (NOT a BusinessUnit; central pool)
   │   ├─ AI Model Mesh ........ z-image-turbo, Whisper, Kokoro, OmniVoice, bg removal
   │   ├─ LLM provider pool .... existing 17-preset matrix
   │   ├─ Shared accounts ...... Cloudflare, registrar, VPS pool (optional)
   │   └─ Per-unit credentials . OpenAI/Stripe/Resend/etc. scoped to a BusinessUnit
   │
   └─ BusinessUnit tree (recursive — Line → Type → Series → Audience → …)
       ├─ Line VP: KDP
       │   ├─ Type Mgr: Romance ........ uses pen-name, owns reader list
       │   │   ├─ Series Lead: "Highland Rogue saga"
       │   │   │   ├─ Product: book #1 (paperback + Kindle + audiobook)
       │   │   │   └─ Product: book #2
       │   │   └─ Series Lead: "Cozy small-town"
       │   ├─ Type Mgr: Coloring Books .. flat (no series layer), 90% sales via Amazon Ads
       │   ├─ Type Mgr: Cookbooks
       │   └─ Type Mgr: Business Non-fiction
       │
       ├─ Line VP: POD
       │   ├─ Type Mgr: T-Shirts
       │   │   ├─ Niche Mgr: Cat lovers ..... Etsy store + design pipeline
       │   │   └─ Niche Mgr: Software engineers
       │   ├─ Type Mgr: Mugs
       │   └─ Type Mgr: Stickers
       │
       ├─ Line VP: SaaS
       │   ├─ Product VP: Korpha
       │   └─ Product VP: Vidyo
       │
       ├─ Line VP: Info Products
       │
       └─ Line VP: Affiliate
           ├─ Audience Mgr: AI marketers ........ 12K subs, list segment, swipe templates, bonus stack
           │   ├─ Campaign: Promote Korpha (Jun 11, 2026)
           │   └─ Campaign: Promote RankMyAnswer (May 13, 2026)
           ├─ Audience Mgr: Solopreneur productivity
           └─ Audience Mgr: KDP author tools
```

### The recursive `BusinessUnit`

A `BusinessUnit` is a node in a self-referential tree. It can be a Line,
a Type, a Series, a Niche, an Audience, a Platform, a Season — the
vocabulary is open. Each node has:

- **An owning agent role** (Line VP, Type Mgr, Series Lead, Audience Mgr,
  Product VP). Optional — leaf units can have none.
- **A playbook** — a skill bundle from the skill hub that gives this
  node's owner agent domain expertise (KDP-Romance Type Manager's
  playbook knows tropes, KU economics, pen name management).
- **A niche profile** — JSON describing core/adjacent/off-limits topics,
  persona, list metrics. Drives compatibility routing.
- **Its own kanban view, KPIs, digest, weekly review** — all scoped to
  this unit and its descendants.
- **Optional credentials override** — per-unit API keys for external
  services. See [Per-unit credentials](#per-unit-credentials).

Children of a BusinessUnit are either more BusinessUnits (intermediate
nodes) or `Product` records (leaves — actual books, designs, courses,
SaaS apps, affiliate campaigns).

### Agents decide depth, not founders

Critical UX point: the founder never picks "I want a Type Manager
layer." The Line VP decides whether to create one based on its
playbook + observed load.

- KDP Line VP sees three romance books → proposes spawning a
  Romance Type Manager + series structure.
- KDP Line VP sees twelve coloring books → stays flat (coloring is
  batch-published, no series).
- POD Line VP sees one Etsy shop with seven cat-themed shirts → keeps
  it flat. Twenty designs across six niches → proposes a Type-by-Niche
  split.

This makes the AI an **org architect** in addition to an executor.

## The Five Canonical Lines

Each line ships as a **Line Pack** in the skill hub — a packaged
playbook the community can extend.

| Line | What's distinctive | Sub-structure |
|------|--------------------|---------------|
| **Print on Demand** | Design-led; platform-specific royalty math (Printful vs Merch by Amazon vs Redbubble vs Etsy); niche-narrow shops | Type (T-shirt / Mug / Sticker / Phone case) → Niche (Cat lovers / Software engineers / …) → Product (design) |
| **Amazon KDP** | Book launch mechanics, BSR rank, KU page reads, review strategy. Genre-specific everything. Pen names mandatory in fiction. | Type (Romance / Coloring / Cookbook / …) → Series (when applicable) → Product (book) |
| **Info Products** | Funnel-driven (lead magnet → tripwire → core → OTOs). JV-launch heavy. List segment per topic. | Type (Course / Ebook / Newsletter / Membership / DFY) → Product |
| **SaaS Apps** | Recurring revenue, churn, dev cycles, support tickets, feature velocity | Product VP per app — usually flat under Line |
| **Affiliate Marketing** | Time-bound campaigns, JV calendar, swipe writing, bonus stacks, reciprocation debt. **Audience-first** — each list segment runs its own JV calendar. | Audience Mgr (per list segment / niche) → Campaign (time-bound, has start/end + vendor) |

## Niche Compatibility — The Audience Protection Gate

Every BusinessUnit (at any depth) carries an optional `NicheProfile`:

```yaml
core_topics: [AI marketing, marketing automation]
adjacent_topics: [copywriting, analytics, lead generation]
off_limits_topics: [homesteading, personal finance, dating]
persona: "marketing managers at 5-50 person SaaS companies"
list_size: 12400
avg_open_rate: 0.31
avg_click_rate: 0.04
avg_epc: 1.85
last_burned_at: null         # set when an off-niche promo hurt opens
last_promoted_at: null       # last promo of any kind — drives promo-fatigue decay
promos_in_last_30_days: 0    # frequency guard; high values reduce score
```

When new work targets a unit (a new affiliate campaign proposal, a new
product idea, a JV invitation), the unit's owner agent runs a
**compatibility check** against its NicheProfile:

1. **Score the new work** against core/adjacent/off-limits topics.
2. **Refuse** if score is below threshold OR overlaps off-limits. The
   refusal goes back to CEO with a structured reason ("doesn't match
   audience, would risk a list burn, last burn was N days ago").
3. **Accept** silently if score is high.
4. **Escalate** to CEO if borderline — CEO weighs cross-line value.

This is the **"AI cofounder protects your list"** behavior. A
solopreneur cannot learn this fast enough; agents enforce it from day
one.

## Marketing Concentration

Marketing concentrates at the **Line VP** (or below), never at the CEO.
The reason: each line owns a distinct audience, and centralizing
marketing at CEO creates the burn-the-list disaster.

| Marketing concern | Owner | Why |
|---|---|---|
| Founder personal brand (Andrew Darius) | CEO | One human face for the company |
| Marketro LLC company news | CEO | Rare; tier of message that crosses lines |
| Korpha newsletter + social + blog | SaaS Line VP → Korpha Product VP | Audience is AI-curious solopreneurs |
| KDP author email list | KDP Line VP | Audience is KDP-curious authors |
| Romance pen-name newsletter | KDP Romance Type Mgr | Pseudonymous; readers must not know Andrew Darius |
| Affiliate-side promos for AI tools | Affiliate Line VP → AI Marketers Audience Mgr | List segment with niche profile |
| POD Etsy shop announcements | POD Line VP → per-shop Niche Mgr | Etsy shop-scoped subscribers |

The CEO sees aggregated marketing performance via the monthly review.
The CEO does **not** see or approve individual posts/emails. Trust
flows down the org tree; oversight rolls up.

## Shared Resources — Two Patterns

### Pattern 1: Tech infrastructure (always available, central pool)

The company-wide shared resource pool. Any agent in any line can use
these without permission. Examples:

- **AI Model Mesh** — z-image-turbo, Whisper, Kokoro TTS, OmniVoice
  TTS (with voice clone), background-removal model. Vidyo originally
  built the GPU mesh for video. KDP Romance uses z-image-turbo for
  cover concepts. Info Products uses OmniVoice for course narration.
  POD uses background removal for product mockups. Same infrastructure,
  many consumers.
- **LLM provider pool** — existing 17-preset env-fallback matrix
  (DeepSeek, OpenAI, Anthropic, …).
- **Shared accounts** — Cloudflare account, domain registrar account,
  possibly shared VPS pool when justified.
- **OAuth-authorized CLIs** — Claude Code, Codex CLI, OpenCode, Cursor,
  Gemini CLI, ACPX, PI. Physically constrained to one OAuth session
  per machine — you cannot run multiple installations with different
  OAuth identities — so they're company-wide singletons by hard
  constraint, not by choice. Any unit can use them via the shared
  resource skill API. Only available in **local install** deployment,
  not SaaS (see [Deployment Modes](#deployment-modes--local-vs-saas)).

Implementation: extends your existing inference-provider plugin
contract. Vidyo's GPU mesh becomes a plugin shipping skills like
`image.generate(model="z-image-turbo")`, `audio.synthesize(model="kokoro")`,
`image.remove_background()`. Any agent calls these the same way it
calls any other skill.

**Tracking, no chargeback (for now).** Usage logs by
`(business_unit_id, resource_id)` so monthly review surfaces who used
what. Internal chargebacks are deferred until the founder feels real
pain — manual quarterly allocation works fine at solopreneur scale.

### Deployment Modes — Local vs SaaS

Two deployment modes, with different shared-resource availability:

| Mode | OAuth CLIs available | Per-unit credentials | Default routing |
|---|---|---|---|
| **Local install** (single founder, current target) | ✓ shared across all lines | ✓ optional override | OAuth subscription for Pro tier · per-unit API for Workhorse · company default fallback |
| **SaaS install** (multi-tenant hosted, future) | ✗ impossible | ✓ required | All routing through per-unit API keys; no OAuth fallback exists |

SaaS deployment fundamentally cannot share OAuth tokens across tenants
— Anthropic/OpenAI ToS forbid it, and the technical model (one OAuth
session per machine) makes it impossible anyway. SaaS tenants must
configure API keys at the BusinessUnit level.

This reinforces the existing **tier separation**:

- **Pro tier work** (deep reasoning, careful decisions, code authoring):
  in local mode, prefers OAuth CLI quotas first, then falls back to
  per-unit Anthropic/OpenAI API keys. In SaaS mode, always API keys.
- **Workhorse tier work** (cold-email bulk drafting, image batch
  generation, simple summarization): NEVER uses OAuth CLI — always
  per-unit API providers (DeepSeek, Groq, etc.). Bulk work would burn
  OAuth subscription quota in minutes; the tier separation prevents
  that automatically.

### Pattern 2: Cross-line cooperation (opt-in, conditional)

Voluntary cross-line proposals. A Line VP can propose to a sibling Line
VP:

- KDP Romance publishes book #5 in a series →
  POD Line VP proposes "merch around the Highland Rogue series, 20% of
  POD royalty back to the series budget."
- Affiliate Line VP sees Korpha launch on the calendar →
  SaaS Line VP (Korpha owner) is the vendor; Affiliate Line VP
  decides whether to allocate audience promo slots.
- Info Products course launch coming up →
  Affiliate Line VP can promote to compatible audiences if niche-match
  scores well.

**Rules**:
1. Always voluntary. Either side can refuse.
2. Both sides must agree the deal benefits *their* unit.
3. The receiving unit's niche-compatibility gate still applies.
4. If they disagree, CEO arbitrates.
5. If CEO can't decide (strategic question), it escalates to the
   founder via the existing Approval flow with a new
   `action_class=STRATEGIC` value.

Tracked as `CooperationProposal` artifacts so there's a paper trail
and the monthly review can surface which cooperation flows actually
generated revenue.

### The "phone call" API — cooperation without memory access

Cooperation requests never grant direct memory access between units.
They go through `cooperation.ask_about` — a structured question dispatched
to another unit's owner agent:

> **Affiliate Line VP** wants to know: *"do we have a romance-author
> bonus we could stack for this affiliate promo?"*
>
> → `cooperation.ask_about(target_unit=KDP-Romance, question="bonus stackable for affiliate promo to AI marketers?")`
>
> The asking agent **never** reads KDP Romance's memory directly. The
> skill dispatches the question to the KDP Romance Type Manager. That
> agent processes the question **with its own scoped memory access**
> and returns a structured response: `{available: true, bonus_id: "...", terms: {...}}`.
>
> The asking agent receives only the response. KDP Romance's underlying
> memories, transcripts, customer data — never touched.

This is identical to how teams talk in a real company. Nobody walks
into another department and reads files; they ask a question, the
other team's expert answers.

Authorization defaults:
- **Sibling units** (same parent): can ask each other
- **Descendant → ancestor**: can ask up the tree
- **Ancestor → descendant**: can ask down the tree
- **Unrelated cross-tree**: requires explicit grant via `CooperationProposal`

Every cross-unit query is audit-logged. Monthly review shows the
founder *"Affiliate Line VP asked KDP Romance Type Mgr about bonus
stacks 4× this month"* — so synergy that's actually producing value
becomes visible.

## Memory + Data Isolation — Hybrid Model

A single founder running 5 business lines must not have those lines'
memories cross-pollute. KDP Romance's reader-survey notes cannot ever
surface in a POD design agent's context — different audiences, different
voices, different pen names (sometimes legally pseudonymous). At the
same time, the founder dashboard needs to roll up financials across all
lines in one query.

This produces a deliberate hybrid: **hard barrier where it matters
(memory + sensitive context), soft barrier where the founder needs
visibility (financial roll-up, monthly review).**

### Hard-isolated (separate namespace per BusinessUnit)
- Long-term memory (the recall danger zone)
- Vector embeddings index (partitioned by namespace_id, not just filtered)
- Filesystem state (agent caches, prompt caches, work artifacts)
- Conversation transcripts
- Custom-authored playbook content
- Audience profile + list metadata

### Soft-tagged with `business_unit_id` (aggregatable, joinable)
- Kanban cards (P&L view rolls up across lines)
- Goals + approvals
- Activity log (monthly review needs cross-unit summary)
- Cost log (per-unit P&L truth requires single-table aggregate)
- External-service accounts (resolver walks the tree)

### Company-wide instance-level
- Shared AI model mesh (Vidyo GPU)
- OAuth-authorized CLI sessions (Claude Code, Codex, etc.)
- Shared API accounts (Cloudflare, registrar)
- Plugin registry + skill hub catalog

The hard isolation enforces at the **skill API layer** — `memory.recall`
takes a namespace_id parameter that defaults to the caller's unit
namespace and refuses to accept a different namespace without an
active `CooperationProposal` grant. Even if an agent's instructions
tell it to ignore the rule, the skill refuses. Vector indexes are
partitioned by namespace_id (not just filtered), so cross-unit
similarity contamination is impossible.

### Why not full Paperclip-style tenant isolation?

Paperclip puts a hard tenant barrier on *everything* — including
operational tables — so cross-company queries don't even exist in their
API. We don't go that far because:

1. **Founder needs the roll-up.** Opening the monthly P&L wants
   `SELECT business_unit_id, SUM(...) FROM ... GROUP BY business_unit_id`
   — one query, cross-line. Forcing N tenant queries + app-level
   aggregation is fine in Paperclip's enterprise multi-customer
   context but is a real UX downgrade for a single founder running
   their portfolio.
2. **Cooperation happens often.** Paperclip companies are separate
   businesses with no relationship. Korpha lines are siblings under
   one founder who *expects* synergy. Building the friction of "each
   line is a separate tenant" makes cooperation heavier than it needs
   to be — while still failing to protect what actually matters
   (memory cross-pollution).

The hybrid keeps the barrier where it matters and removes friction
where it doesn't.

## Per-Unit Credentials

External-service API keys are scoped to BusinessUnits. Why this matters:

| Reason | Concrete example |
|---|---|
| **Rate limits** | KDP Romance burning Anthropic for ghostwriting shouldn't 429 the Korpha support agent on the same key |
| **Spending caps** | $300/mo cap on the Romance OpenAI key. When it trips, Romance pauses, other lines keep running |
| **P&L truth** | Per-key billing = ground truth attribution. Tax 1099s are already per-account anyway. You know which line earns vs loses on the *first* of the month, not by guess |
| **Deliverability isolation** | Resend domain for KDP Romance is separate from Korpha — if one gets a complaint, the other isn't affected |
| **Legal isolation** | Separate Stripe accounts per pen name (real KDP authors do this) keep tax reporting clean |

The data model unifies LLM provider accounts and non-LLM service
accounts (Stripe, Resend, Printful, KDP API, Etsy API, JVZoo) under one
schema, with hierarchical resolution. See
[`docs/dev/BUSINESS_UNITS.md`](./dev/BUSINESS_UNITS.md) for schema
details.

**Resolution order** when an agent needs to call an external service:

1. Look for an account scoped to the calling unit. Use it.
2. If none, walk up to the parent unit. Use that.
3. Repeat until reaching the Business root. Use the company-wide
   default.
4. If still none, hard fail with a setup-wizard prompt.

**Setup ergonomics for non-tech founders:** the Line VP itself runs the
credential-setup conversation when spawned. *"Do you want a separate
Stripe account for this line? If you're not sure, say no — we'll use
the company default and split later when revenue justifies it."* No
YAML editing, no env vars, no manual key management.

## Conflict Resolution

```
Line VP ↔ Line VP disagreement
   ↓ (cannot resolve)
CEO arbitrates (uses existing approval gate, sees both proposals)
   ↓ (cannot decide — strategic question)
Founder via Approval card (action_class=STRATEGIC)
```

CEO **arbitrates** — does not broker. Line VPs propose cooperation to
each other directly. CEO only steps in when they disagree. CEO sees
aggregated performance but does not see every cross-line message;
otherwise the CEO becomes the bottleneck the org structure is supposed
to remove.

## The Moat — Line Packs

Once the recursive BusinessUnit + per-unit playbooks + niche-compat
gate are in place, Korpha is the only AI cofounder tool that can
honestly claim:

> *"Korpha already knows how to run any combination of: a romance
> pen name, a coloring book operation, a SaaS app, a JV affiliate
> operation, an Etsy print shop — and can stand up the right org
> chart for each one in 60 seconds."*

Community contributors package their playbooks as Line Packs and Type
Packs in the existing skill hub. Someone running a successful romance
pen name packages their "Romance Type Pack" — pricing strategy, trope
research prompts, KU page-read maximization, cover style locks — and
sells it to other authors. The skill hub turns Korpha into a
**marketplace of business operating systems**, not just a marketplace
of generic skills.

This is the real defensibility story. Generic "AI agents for
solopreneurs" is a crowded category. "AI agents that already know how
to run your specific business model" is not.

## Real-World Walkthrough — Marketro LLC, May 2026

Andrew opens Korpha. The CEO greets him.

> Andrew: "I want to add a new Romance pen name and start the next series
> in Highland Rogue."

CEO routes to KDP Line VP. KDP Line VP sees:
- Existing Type Mgr: Romance → existing Series Lead: Highland Rogue
- Series Lead spawns 6 BACKLOG kanban cards (book draft → cover → ARC
  list refresh → launch sequence → KU promo → series-bundle update).
- z-image-turbo (from Vidyo's GPU mesh) is auto-routed for cover
  concepts.
- OmniVoice voice clone (also shared resource) is queued for audiobook
  narration draft.
- KDP Romance's pen-name Resend account is used for ARC outreach (not
  Andrew's main list).
- KDP Romance's OpenAI key (capped at $200/mo) handles all writing
  passes.

Meanwhile, POD Line VP sees the `unit.published` event on the new
series →

> POD Line VP: "Highland Rogue book #6 just kicked off. The 'Highland
> Rogue cosplay' tee design from last cycle had a 4% CTR on the romance
> audience. Want me to spin up an 8-design merch refresh tied to the
> book launch, royalty split 80/20 KDP/POD?"

Andrew approves via dashboard. POD Line VP spawns its own cards. Both
units now reference the same launch in their kanban.

That evening, Andrew sees a new affiliate JV invitation in his inbox
for a homesteading SaaS launch. Affiliate Line VP processes it →

> Affiliate Line VP: "Compatibility check: none of your 3 audiences
> match (AI marketers, solopreneur productivity, KDP authors). Closest
> is solopreneur productivity at 0.31 — below threshold. Recommend
> decline. Last off-niche promo (Dec 2025 dating-app launch) cost you
> 412 unsubscribes. Want me to refuse politely?"

Andrew: "Yes."

Affiliate Line VP sends the polite decline, logs the refusal, updates
the JV-calendar.

End of month. CEO produces the monthly review. Founder sees:

- KDP Romance: revenue $3,400, AI spend $187 (Romance OpenAI key,
  capped), net $3,213 ✓
- POD T-Shirts: revenue $1,100, AI spend $40 (z-image-turbo via Vidyo
  mesh, attributed to POD), net $1,060 ✓
- SaaS / Korpha: revenue $14,200 MRR, AI spend $890, net $13,310 ✓
- SaaS / Vidyo: GPU mesh cost amortized $2,300, internal usage
  credits $260 (from POD + KDP + Info), net cost $2,040 — flagged for
  founder review on whether to charge back at year-end.
- Affiliate / AI marketers: $4,800 commission earned across 2 launches,
  zero refunds, zero unsubscribes — list is healthier than last month.

Andrew goes to sleep at 10pm and his cofounders keep working.

## Deferred Decisions

To keep scope honest, these are explicitly *not* in v1:

1. **Internal chargeback automation.** Manual quarterly allocation by
   the founder is fine at solopreneur scale.
2. **Cross-line synergy ML.** Cooperation proposals come from Line VPs
   reading each other's `unit.published` events, not from a separate
   "synergy detector." Add when we see real demand.
3. **Multi-Business-per-Founder consolidation.** Each `Business` is
   still its own legal entity. A founder with 3 LLCs still sees 3
   businesses in the picker. We don't roll up across Businesses.
4. **Per-unit secondary-language playbooks.** A Spanish KDP Romance
   type would clone the playbook and patch language-specific bits, but
   we don't ship i18n machinery for playbooks in v1.

## Migration

Existing single-CEO businesses keep working without changes. The
default state is:

- `Business` exists (no migration needed).
- A default `BusinessUnit` is auto-created with the existing CEO as its
  owner agent. Kind = `"default"`.
- All existing `KanbanCard`, `Goal`, `Approval` rows get
  `business_unit_id = default_unit.id` via backfill.
- `korpha business split-into-lines` is an opt-in CLI command that
  walks the founder through carving the default unit into multiple
  Line VPs.

See [`docs/dev/BUSINESS_UNITS.md`](./dev/BUSINESS_UNITS.md) for the
exact migration script.

## See Also

- [`docs/dev/BUSINESS_UNITS.md`](./dev/BUSINESS_UNITS.md) — engineering
  doc: data model, skills, plugin contracts, migration, tests.
- [`BRIEF.md`](../BRIEF.md) — original product brief.
- Skill hub (#213) — where Line Packs ship.
- Inference provider plugin (#123) — the contract Pattern-1 shared
  resources extend.
- Liveness classifier (#202), Budgets (#203) — apply per-unit
  automatically once `business_unit_id` is plumbed.
