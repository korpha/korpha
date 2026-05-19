# Next Steps

The single-user install is feature-rich and Mike can use it end-to-end
today. Remaining items are mostly external integrations that need
API tokens or accounts.

## ✅ Done in Day 1 + Day 2

**Foundations:** brief, architecture, Hermes vendoring, dev tooling, data
model, Inference Pool with cache affinity, ApprovalGate + trust envelope,
HiringService, CostTracker, Activity log, end-to-end integration test.

**Cofounder loop:** Chief of Staff with rule-based AND LLM-driven triage,
Director execution layer (CTO/CMO/COO with tuned personalities), Workforce
parallel dispatch with `[ROLE]`-tag routing, multi-task plans, persistent
memory across sessions, Approve/Deny/Discuss buttons, 503/529 fallback chain.

**Skills (10+ built-in covering BRIEF Start + Run):**
- niche.find_micro_niches  •  validate.score_idea
- product.first_feature  •  pricing.recommend_tiers  •  landing.draft_copy
- outreach.draft_cold_emails  •  growth.draft_content_plan
- support.triage_inbox  •  finance.weekly_review  •  analytics.weekly_review
- research.scrape_url  •  outreach.send_cold_email  •  commerce.create_payment_link

**Workers (3 default specialty sub-agents):** copywriter, designer, support
specialist — Directors spawn on demand.

**Skill-aware CEO** — `CEO.handle()` auto-routes Founder messages to the
right skill via a router + synth pattern, with empty-content fallback.

**Surfaces:** CLI (init/status/ask/chat/propose/pending/approve/reject/
execute/blockers/skill list/skill run/server/migrate/demo + browser-test/
browser-do + email-test/email-digest), HTTP API at `/healthz` /me /ask
/ask/stream /propose /approvals/* /skills /blockers, Swagger at /docs,
Linear-style dashboard at `/app/*`, legacy chat at `/chat`.

**Streaming SSE** — live chunk-by-chunk responses with blinking cursor.

**Provider stack** — 17 OpenAI-compatible presets (OpenCode Go, Ollama
Cloud, DeepSeek, Together, Groq, Cerebras, Anthropic, OpenAI, Nous Portal,
NVIDIA NIM, Z.AI, Moonshot, MiniMax, HuggingFace, Local Ollama, OpenCode
Zen, OpenRouter). Multi-key + multi-provider parallelism with session
affinity for prompt-cache hits. YAML config at `~/.korpha/providers.yaml`.

**Browser stack** — Playwright fetch (cheap, for scrape) + Playwright
action loop (LLM-driven multi-step navigate/click/type/scroll, headless
or headed). Same `BrowserProvider` ABC for future backends.

**Notifications** — Resend email backend, daily digest heartbeat handler,
Notifier ABC for future surfaces (SMS, push, Discord webhooks).

**Real side effects** — outreach.send_cold_email (Resend) +
commerce.create_payment_link (Stripe). Both compose → propose → approve →
execute, both audit-logged, both trust-envelope-aware.

**Migrations** — Alembic baselined; `korpha db-migrate` CLI.

## Phase 2 — channels (needs your tokens / accounts)

| # | Task | Needs |
|---|---|---|
| 1 | Telegram channel — single-bot CEO, per-platform autonomy | ✅ shipped (bot token from @BotFather) |
| 2 | Discord channel — one bot, per-C-suite channels | Discord app + bot token |
| 3 | Email outbound (digest, blocker alerts) | ✅ shipped (Resend API key + verified domain) |
| 4 | Email inbound (reply parsing) | IMAP creds or inbound webhook |

## Phase 3 — real-world side effects (needs your accounts)

| # | Task | Needs |
|---|---|---|
| 5 | Cold-email sender (warmup-aware) | ✅ shipped (Resend) |
| 6 | Payment link generator | ✅ shipped (Stripe API key) |
| 7 | Twitter/X poster | API token or browser-automation creds |
| 8 | LinkedIn poster | Browser automation (their API is hostile to indies) |
| 9 | Code-deploy: CTO calls Codex CLI to ship code | Codex CLI auth |
| 10 | agent-browser CLI as second browser backend | ✅ shipped (auto-discovers via PATH / npx) |

## Phase 4 — moat layers (deferred)

| | |
|---|---|
| Cofounder Protocol | Stripe / Vercel / ConvertKit / Beehiiv etc. integrate as "Korpha-native" — third-party services register as cofounder tools. |
| Postgres backend | When the SQLite single-user install outgrows itself; same SQLModel schema. |

## Things to push back on

1. **Provider rollout order** is flexible — adding any new OpenAI-compat
   service is a one-line preset. DeepSeek direct, OpenRouter, Together,
   Groq, vLLM are all 1-line additions.
2. **LLM-driven CoS** is OFF by default. Flip `use_llm_triage=True` on
   ChiefOfStaff once you have real blocker volume to justify the
   per-digest token cost.
3. **Workers are invisible** to the Founder by default. Dashboard
   doesn't show them yet — that's a "show workers" toggle (~30 LOC)
   when you want it.
4. **Alembic baseline** captures the initial schema. Future schema
   changes use `alembic revision --autogenerate -m "..."` to produce
   migration scripts.
