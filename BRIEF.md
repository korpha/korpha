# Korpha — Product Brief

*One-page product brief. Source of truth for product decisions. Revise as the product sharpens; do not let it drift from reality.*

---

## Who it's for

**Mike, 38.** Married, has a job he tolerates, dreams of quitting. Tried Amazon FBA, dropshipping, affiliate marketing, info products, course launches, a Substack, a YouTube channel — nothing stuck or nothing went big enough. Has skills (whatever they are), 5–10 hours a week, and the lingering belief that *this time, with AI, it's different*. Wants a side hustle that becomes a full-time income, with AI doing 95%+ of the work. He wants to feel in control while doing the bare minimum.

**Secondary avatar:** Someone already running something (consulting, a small product, a local service) who needs eyeballs on it. Korpha as **marketing agency replacement** — the thing solopreneurs can't normally afford ($3k–$15k/month for a real agency). They get a marketing cofounder for the price of API tokens.

---

## The cofounder hypothesis

A "cofounder" beats an "AI assistant" because **the cofounder finds the way** — it doesn't ask Mike what to do, it shows him what it would do and asks him to approve. Mike stays in the captain's chair through *approval*, not *direction*. When Mike is about to do something dumb, the cofounder pushes back with a better path — never a dead-end "no." Mike gets the feeling: *"I'm running this, but I'm not doing the work."* That's the entire trick. Most AI products make Mike feel like the bottleneck. Korpha makes him feel like the boss.

---

## What it does

**Start (the wedge — gets Mike in the door):**
- Pick a niche that fits his skills, time, and the market
- Validate it with real signal (search demand, competition, willingness-to-pay tests)
- Ship a landing page + offer
- Get the first 10 customers

**Run (the retention — keeps Mike paying):**
- Daily customer support and inbox triage
- Weekly content + ads, by channel
- Monthly P&L review with a strategy proposal
- Delegates engineering work to Claude Code / Codex / OpenCode
- Hires more specialized sub-agents as the business grows

Mike comes for *Start*. He stays for *Run*.

---

## The 5-minute demo

1. **0:00** — `korpha.com`. One field: *"What do you actually want?"* Mike types: *"$5k/month side hustle in 6 months, eventually quit my dev job. I know Python and B2B SaaS."*
2. **0:30** — Cofounder thinks visibly. Live updates: *"Scanning 47 micro-niches… filtering by demand × your skills × time available… checking 12 competitors…"* (real work, not theater).
3. **2:00** — Output: *"I think we should build [specific tool] for [specific role]. Here's why: [3 bullets]. Here's the 90-day plan. Here's what I'd do this week. Approve to start."* One button: **Approve**.
4. **2:30** — Mike clicks. Cofounder drafts landing page copy, hands it to Codex CLI to build, deploys to a subdomain.
5. **4:30** — Mike sees: live landing page URL, 10 LinkedIn prospects already drafted (awaiting approval), Stripe link armed, calendar slot for "kickoff with cofounder tomorrow."
6. **5:00** — *"Approve outreach? (10 messages, your voice, soft pitch.)"* — Mike clicks. Done.

The "oh shit" moment is **minute 4:30**: Mike has a deployed business in less time than it takes to make coffee.

---

## Model strategy — two-tier inference

| Tier | Model | When |
|---|---|---|
| **Flash** | DeepSeek V4 Flash | Routine ops: file edits, lookups, formatting, simple tool calls |
| **Pro** | DeepSeek V4 Pro | Harder work: planning, content drafts, decisions. **All C-suite agents (CEO, CMO, CTO, COO) always use Pro.** |
| **Consultant** | Claude / GPT-5 / Gemini Pro | Agent stuck: loop detected, low confidence, repeated errors, or explicit `consult()` call. Fully optional — disable for free / local-only operation. |

Cuts inference cost ~90% vs. all-Claude with comparable outcomes. The consultant is **a tool the agent calls** — not a step in the pipeline. Heuristics for "when to consult" are real product IP.

---

## Provider support

Inherits Hermes's provider matrix: Nous Portal, OpenRouter (200+ models), NVIDIA NIM, Xiaomi MiMo, z.ai/GLM, Kimi/Moonshot, MiniMax, Hugging Face, OpenAI, custom endpoints. **Adding a new provider is a plugin, not a fork.** Plus first-class DeepSeek and the "consultant slot" abstraction.

---

## What it's NOT

- Not a chatbot. (It already proposed a goal; it doesn't ask "what's your goal?")
- Not an agent IDE or framework. (Engineers are not the buyer.)
- Not for technical operators with 20 agents. (Paperclip's user, not ours.)
- Not no-code. (No drag-and-drop. No flows to design. The cofounder designs them.)
- Not a course or community. (The product is software, full stop.)
- Not a generic AI assistant. (One job: make Mike's business work.)

---

## Naming

**Korpha.** Trademark filed (training class). Software class filing in progress. No rename.
