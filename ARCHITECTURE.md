# Korpha — Architecture

*Translates BRIEF.md into a concrete system. ~2 pages. Revise as the design sharpens; do not let it drift from the brief.*

---

## Org structure

```
       ┌───────────────┐
       │  Founder      │  ← Mike. Single human.
       └───────┬───────┘
               │  approvals, strategic input
       ┌───────▼───────┐
       │   CEO         │  ← AI Cofounder. Single point of contact by default.
       │ (Cofounder)   │
       └───────┬───────┘
               │  delegates
   ┌───────────┼───────────┬───────────┐
   ▼           ▼           ▼           ▼
 ┌────┐     ┌────┐      ┌────┐      ┌────┐
 │CTO │     │CMO │      │COO │      │... │  ← C-suite. Hired on demand.
 └─┬──┘     └─┬──┘      └─┬──┘      └────┘
   │          │           │
   ▼          ▼           ▼
 workers   workers     workers       ← never talk to Founder. Report to C-suite.
   │
   ▼
 Tools: Claude Code, Codex, OpenCode, MCP servers, HTTP, scripts.
```

**Rules:**
- Founder talks to CEO by default. May initiate with any C-suite directly (sticky thread).
- C-suite never asks Founder anything directly — escalates *via CEO* (single-voice rule).
- Workers never see Founder. They report up to their C-suite.
- Coding CLIs (Claude Code / Codex / OpenCode) are *tools* used by CTO and workers — not peers.

## Hiring rules (need-based, never revenue-based)

| Trigger | Hire |
|---|---|
| Sign-up | CEO only |
| First build/deploy/code task | + CTO |
| First launch plan, ad campaign, or content schedule | + CMO |
| Same manual ops task repeated 3+ times in 7 days (or other ops-repetition signal) | + COO |
| Specialist need (designer, copywriter, support) | Worker, hired by relevant C-suite |

CEO can hire any time; Founder can override (hire/fire) explicitly. The team grows visibly in the UI as the business grows — UX moment.

## Communication topology

**Default surface:**
- Web: chat with CEO. Other C-suite visible in a sidebar; clicking opens a sticky thread with that agent.
- Telegram (default): one bot, CEO only. Single voice.
- Discord (recommended for multi-channel): one bot, one channel per active C-suite agent. Founder can mute/unmute.
- Telegram multi-channel: optional, requires Founder to set up one bot per agent. Suggested, not forced.

**Sticky thread rules:**
- Founder initiates a thread with C-suite agent (e.g., CMO) → that thread sticks to CMO.
- Sticky window ends when: Founder explicitly closes ("done" / close button) **or** 24h elapse (configurable) **or** Founder starts a new topic in CEO chat.
- During sticky: CEO chat remains available for unrelated topics. Sticky only routes *that thread's* messages.
- Cross-C-suite work mid-thread: the active agent (CMO) handles other agents async; Founder only ever sees CMO in that thread.
- Per-platform per-thread: a Telegram CMO sticky doesn't affect the web app.

**Single-voice rule (the cofounder discipline):**
Any C-suite agent that needs Founder input *outside* a sticky thread must route the request through CEO. CEO consolidates and asks Founder. Founder is never pinged by 4 different agents in parallel.

## Autonomy and approval gates

**Default: maximally autonomous internally, draft-for-approval externally.**

| Action class | Default | Configurable |
|---|---|---|
| Internal work (research, planning, drafts) | Auto | — |
| Hiring C-suite or workers | CEO autonomous; Founder can veto retroactively | Lock to manual approval |
| Code changes / deploys | Auto within designated repos/branches | Per-repo policy |
| Public post (Twitter, LinkedIn, etc.) | Draft → approve | **Per-platform** auto/draft/off |
| Email send (cold outreach) | Draft → approve | Per-list auto/draft/off |
| Email send (reply to existing thread) | Auto within tone/scope | Lock to manual |
| Spend money | Auto under $X (Founder-set) | Hard stop above |
| Customer support reply | Draft → approve at first | Auto after N approved-as-is |

**Trust envelope** (real differentiator): when Founder approves **N=5** consecutive drafts of a class without edits (settable), Korpha offers to flip that class to auto. Trust grows; approval load shrinks. Reversible any time.

**Iteration cap**: each agent run has a hard stop at **60 iterations** (matches Hermes default, settable). Prevents runaway loops.

## The Cofounder loop (CEO's main loop)

1. **Find the way.** CEO maintains a *next-best-move queue* derived from goals + KPIs + recent state. It never asks "what now?" — always proposes.
2. **Propose.** Surfaces top move with rationale, alternatives considered, expected impact, cost, time.
3. **Approve.** Founder clicks Approve / Reject / Modify. Rejection includes one-line "why" → CEO updates strategy doc.
4. **Execute.** CEO delegates to the right C-suite (hires if needed). Workers do the work. Progress streams back.
5. **Push back.** When Founder requests something CEO judges suboptimal, CEO proposes a *better path* with reasoning — never dead-ends with "no." Founder can override; CEO logs the override and learns from outcome.
6. **Report.** End-of-day or end-of-task: what happened, what's next, what needs Founder attention.

The loop is continuous. Founder dropping in mid-loop sees current state, not "what's your goal?"

## Provider strategy — two pools

Korpha uses two distinct provider pools because they solve different problems with different auth realities.

### Pool 1: Inference Pool (agents' own LLM calls)

Used by CEO/C-suite/workers when reasoning, planning, drafting.

| Tier | Model (default) | Auth | When |
|---|---|---|---|
| Workhorse | DeepSeek V4 Flash | API key | Routine ops, lookups, simple tool calls. Worker default. |
| Pro | DeepSeek V4 Pro | API key | All planning/decision/content for C-suite. C-suite default — **always Pro**. |
| Consultant | Anthropic Claude API (default) | API key | Escalation only. ~1% of calls. UI shows cost note: *"Expected $5–20/mo typical use."* User can swap for GPT-5, Gemini Pro, or disable. |

**Multi-key + multi-provider parallelism** supported (rate-limit headroom across keys, fault tolerance across providers — e.g., DeepSeek + OpenRouter + Together as alternates for the same tier). Pay-per-use fallback chain configurable.

**Session affinity rule (cache-aware routing):** within a single agent/session, route to the *same* provider+key for as long as possible — long agent prompts get prompt-cache hits and 50–90% cost reduction on cached prefix. Only swap provider/key on rate-limit hit, quota exhaustion, or provider failure. *Cross-agent* parallelism still distributes load across providers/keys; *intra-agent* affinity preserves the cache.

This makes the multi-provider story a feature, not just a fallback — and it's a real differentiator vs. naive routing.

**When agents call Consultant:**
- Loop detector: same action repeated N times with no progress.
- Low-confidence threshold for a critical decision.
- Explicit `consult(question, context)` tool call.
- Founder-flagged "premium output" on user-facing content (off by default).

**When they don't:** routine work even if hard, pure execution. Pro is the workhorse for C-suite.

Fully disable-able — runs on Workhorse + Pro alone for free / local-only operation.

### Pool 2: Coding Delegation Pool (CLI tools for code work)

Used when an agent (typically CTO) hands a coding task to a CLI.

| Tool | Auth |
|---|---|
| Claude Code | Mike's Claude login on his machine, or API key |
| Codex CLI | Mike's ChatGPT subscription, or API key |
| OpenCode | Provider API key |

**Multi-account locally:** Mike connects 2× Claude Pro / Codex subscriptions, Korpha runs each in isolated Docker container (separate `HOME`/login). Real parallelism, real cost savings vs. a single Max plan.

**Routing modes per Founder:**
- **Parallel** — load balance work across all active accounts/keys.
- **Sequential** — drain one until quota exhausted, then next.
- **Dedicated** — CTO always uses Account A, designer always uses Account B.
- **Fallback chain** — preferred plans → API keys → fail with clear message.

## Code execution

Where code (deploys, scripts, agent-written tools) actually runs. Tiered.

| Tier | Backend | When |
|---|---|---|
| Local | Docker on Mike's laptop | Default. Free, zero setup. |
| BYO-VPS | SSH to Mike's Hostinger / DigitalOcean / Linode VPS | Mike pastes IP + creds; Korpha injects scoped SSH key (encrypted at rest, revocable). Power-user upgrade. |

Inherits Hermes' terminal backends — local, Docker, SSH. We expose the choice in the setup wizard with a sensible default and never make Mike think about it on day 1.

## Module layout (Python monorepo)

```
korpha_agent/
├── BRIEF.md
├── ARCHITECTURE.md
├── LICENSE                   # MIT (yours)
├── NOTICES                   # MIT texts for hermes-agent + paperclip-inspiration credit
├── README.md
├── pyproject.toml
├── korpha/                # ← YOUR code, all new
│   ├── cofounder/
│   │   ├── ceo.py            # CEO loop, single point of contact
│   │   ├── csuite/           # CTO, CMO, COO definitions and tools
│   │   ├── workers/          # designer, copywriter, support, etc.
│   │   ├── hiring.py         # hiring triggers and team evolution
│   │   ├── routing.py        # sticky threads, single-voice rule
│   │   └── consultant.py     # escalation heuristics
│   ├── business/
│   │   ├── model.py          # Business, Goal, Project, Task, Customer, Costs
│   │   ├── kpi.py            # tracked metrics, trend detection
│   │   └── state.py          # persistent business state across sessions
│   ├── approvals/
│   │   ├── gates.py          # per-class autonomy rules
│   │   ├── trust_envelope.py # auto-promote-to-auto after N approvals
│   │   └── platforms.py      # per-platform posting policy
│   ├── inference/            # Pool 1: agent LLM calls
│   │   ├── pool.py           # provider pool, multi-key, rate-limit handling
│   │   ├── routing.py        # workhorse/pro/consultant dispatch
│   │   └── consultant.py     # escalation heuristics
│   ├── delegation/           # Pool 2: coding CLI delegation
│   │   ├── claude_code.py    # Claude Code wrapper, multi-account isolation
│   │   ├── codex.py          # Codex CLI wrapper
│   │   ├── opencode.py       # OpenCode wrapper
│   │   ├── isolation.py      # Docker-per-account for local multi-login
│   │   └── base.py           # common interface
│   ├── execution/            # where code runs (Hermes backends + VPS-paste)
│   │   ├── local.py
│   │   ├── docker.py
│   │   ├── ssh_byo.py        # encrypted creds, scoped key injection
│   │   ├── daytona.py
│   │   └── modal.py
│   ├── channels/
│   │   ├── telegram.py       # default single-bot CEO
│   │   ├── discord.py        # multi-channel one-bot
│   │   └── policy.py         # per-platform routing
│   ├── api/                  # FastAPI server + Jinja2 dashboard
│   ├── cli.py                # `korpha` command
│   └── skills/               # Founder-facing playbooks (niche pick, validation, launch)
└── hermes/                   # ← VENDORED via git subtree
    └── ... (full hermes-agent tree, MIT headers preserved)
```

**Why this split:** `korpha/` is fresh code you own; `hermes/` is vendored upstream you can `git subtree pull` from periodically. Korpha calls into Hermes for agent runtime primitives (tool execution, session state, MCP, memory, provider matrix). Hermes never imports from Korpha.

## Data model (core entities)

```
Founder ──1:1── Account
   │
   └── Business* (one Founder can run many)
         ├── Goals (KPI-bound, hierarchical)
         ├── Strategy (current, versioned)
         ├── Org (CEO + active C-suite + workers)
         ├── Projects ──── Tasks ──── Subtasks
         ├── Threads (CEO + sticky C-suite threads, per-platform)
         ├── Approvals (queue + history, per class)
         ├── TrustEnvelope (per action class, per-platform)
         ├── Costs (token spend by agent/model/task)
         └── Customers / Revenue / Channels
```

All mutating actions emit an immutable `Activity` event (audit log). Foundation for the future protocol.

## Locked decisions

- **Backend**: Python everywhere.
- **Web UI**: Server-rendered Jinja2 + htmx + Alpine.js (no build step).
- **Workhorse / Pro**: DeepSeek V4 Flash / Pro (API key).
- **Consultant default**: Anthropic Claude API (with user-visible cost note).
- **Iteration cap**: 60 (Hermes default).
- **Trust envelope**: 5 consecutive approvals → offer auto.
- **Code execution default**: Docker on Mike's laptop.
- **Worker visibility**: opt-in setting, default off.

## Open architectural decisions

1. **Persistent store.** SQLite by default. Postgres possible via the same SQLModel schema for users who outgrow local.
2. **Worker concurrency model.** Lean toward `asyncio.TaskGroup` + database-backed queue (Paperclip heartbeat pattern) over Celery.
3. **Approval UI surface.** Both: inline approve/reject in thread plus a dedicated approvals page.
