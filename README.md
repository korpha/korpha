# Korpha

> Your AI cofounder for the online business you keep saying you'll start.

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Eval: 100%](https://img.shields.io/badge/eval-100%25-4f46e5)](docs/eval-baselines/README.md)


**Korpha** ( *KOR · fa*, like *Alpha* ) is an AI cofounder, not an AI assistant.
It doesn't ask you what to do — it shows you what *it* would do, and asks for
your approval. You stay in the captain's chair through approval, not direction.

It's built for the **wannabe solopreneur**: someone who's tried Amazon FBA,
dropshipping, affiliate marketing, info products, a Substack, a YouTube
channel — and is ready for AI to actually do the work this time.

It helps you **start** a business (pick a niche, validate it, ship a landing
page, get the first 10 customers) and helps you **run** it (daily support,
weekly content, monthly P&L reviews, delegated coding work).

> Already know Hermes / OpenClaw / Paperclip? See [**how Korpha
> compares**](docs/COMPARE.md) — short version: cofounder framing,
> Python-native, Line Packs out of the box.

---

## Status

🟢 **Alpha — feature-complete for single-user.** Brief and architecture
are locked. The core agent loop works end-to-end: install → onboard →
ask → see your cofounder ship a niche, landing copy, and outreach
drafts. **521 tests, mypy --strict clean, 100% on the eval harness**
(see below).

---

## How we know the agents actually work

Most "AI cofounder" projects ship a prompt and call it done. We built
an internal eval harness — exact substring / regex / word-count
assertions, no LLM-as-judge — and score every role prompt against
50 founder-asks. Same code, same fixtures, same scoring. Reproducible.

**Open-weights baselines (3-run averaged, same 80-assertion 7-role fixture set):**

| Model | Where it runs | CEO | CMO | COO | CTO | Workers† | Overall |
| --- | --- | --- | --- | --- | --- | --- | --- |
| DeepSeek V4 Pro | cloud | 100% | 100% | 100% | 91% | 94%‡ | **96.2%** |
| DeepSeek V4 Flash (workhorse) | cloud | 100% | 90% | 100% | 100% | 93%‡ | **96.2%** |
| Kimi K2.6 (Moonshot, 256k ctx) | cloud | 100% | 100% | 100% | 82% | 89%‡ | **92.5%** |
| **Gemma-4-31B (Q4_K_M, 262k ctx)** | **local RTX 3090** | **100%** | **90%** | **100%** | **91%** | **87%‡** | **92.5%** |
| GLM 5.1 (Zhipu, 200k ctx) | cloud | 88% | 100% | 100% | 91% | 86%‡ | **91.2%** |

† Designer / Copywriter / Support Workers each have a fixture set
covering hard rules from their prompt (no auto-promised refunds,
no marketing fluff, mobile-first specs, etc.).

‡ Worker scores averaged across Designer / Copywriter / Support roles.

**All three frontier open-weights models clear 90%.** Korpha isn't
tied to one. Pick the model you trust — Kimi if you want the longest
context, GLM if you want the fastest eval turnaround, DeepSeek if
you want the tightest brevity discipline. The remaining ~8% miss is
uniform across models: brevity caps and "lead with the recommendation"
formatting — prompt-tuning targets, not capability gaps.

Reproduce yourself: `korpha eval --tier pro --runs 3 --max-tokens 64000`
after configuring any provider. Full per-role breakdowns + raw output:
[docs/eval-baselines/](docs/eval-baselines/README.md).

**Picking a local model?** If you're running Korpha against a local
Ollama / LM Studio / vLLM and want to know whether a given open-weights
model will hit Korpha's quality bar, the [ClawEval harness](https://github.com/AIgenteur/ClawEval)
scores any open-weights model against the same cofounder prompts.
Useful before committing GPU time — score Qwen 3.5 or Gemma 4 (or
whichever open-weights model in that consumer-runnable class you're
eyeing) against the fixtures and see what passes.

---

## Repo layout

```
korpha/                # Korpha cofounder layer (this is the new code)
├── identity/             # Founder model
├── business/             # Business, Goal, Project, Task
├── cofounder/            # AgentRole, Thread, Message, HiringService
├── approvals/            # Approval, TrustEnvelope, ApprovalGate
├── audit/                # Activity, Cost
├── inference/            # Inference Pool: tiered routing + session affinity
│   ├── providers/        # Mock provider (real ones coming)
│   ├── pool.py
│   ├── router.py
│   ├── registry.py
│   └── cost_tracker.py
├── db/                   # SQLModel engine + session + registry
├── config.py             # Settings
└── cli.py                # `korpha` entry point
hermes/                   # vendored hermes-agent (MIT, Nous Research)
tests/                    # 527 tests, all green; mypy --strict clean
BRIEF.md                  # product source of truth
ARCHITECTURE.md           # system design
PROGRESS.md               # build log
NEXT_STEPS.md             # prioritized roadmap
LICENSE                   # MIT
NOTICES                   # third-party attributions
```

## Your first 5 minutes

This is what `korpha init && korpha server` gets you, with one
LLM key configured:

1. **0:00** — Visit `http://localhost:8765/app/dashboard`. Empty install
   redirects you to `/app/onboard` with one open question:
   *"What do you actually want?"*
2. **0:30** — You type one paragraph: goal, hours/week, savings, what
   you're good at. Submit.
3. **1:00** — `/app/onboard/done` shows your structured brief and your
   cofounder starts thinking visibly: *"Scanning micro-niches that match
   your skills, time, and savings…"*
4. **2:00** — 3-5 niche cards swap in, with one highlighted as
   recommended. Each card shows target avatar, value prop, price band,
   and a concrete validation experiment for this week.
5. **2:30** — You click "Go with this →" on the recommended one. Behind
   the scenes: a Goal is created (`Validate: <niche>`), the validation
   experiment becomes Task **AIG-1**, and a background chain kicks off
   running validate / landing / outreach skills.
6. **3:30** — You land on the dashboard. The Approvals tab now shows 3
   waiting items: a validation report (Verdict: GO, Score 7/10), landing
   copy (with a "Preview as page →" button that renders it), and 5
   cold-email opener variants ready to send.
7. **5:00** — You preview the landing copy, approve it, click
   "Approve" on one of the email variants, and your cofounder has
   produced a week's worth of work in five minutes.

The brief defaults stay attached to the business — every later skill
run pulls hours/week, savings, skills, and constraints from it
automatically. You only answer the Day-0 question once.

---

## What works today (pre-alpha)

- ✅ **Day-0 onboarding flow** — *"What do you actually want?"* → niche
  proposals → pick → auto-fan-out (validate + landing copy + cold-email
  drafts) → preview landing as a real page from the approval queue
- ✅ **Web UI** at `/` — chat, approvals sidebar, CoS digest panel, skill runner, **live streaming** (chunk-by-chunk during 30-90s thinking-model waits), blinking cursor, **inline ✓ Approve / ✗ Deny / 💬 Discuss buttons** when CEO ends with an approval-ask
- ✅ **Mobile-responsive** — single-column on phones, slide-over right panel via ☰, full-width composer, ≥44px tap targets, no iOS zoom-on-focus
- ✅ **HTTP API** with Swagger at `/docs` — `/ask`, `/ask/stream`, `/propose`, `/approvals/*`, `/execute`, `/blockers`, `/skills`, `/skills/{name}/run`
- ✅ **CLI** — `init` / `status` / `ask` / `chat` / `propose` / `pending` / `approve` / `reject` / `execute` / `blockers` / `skill list` / `skill run` / `server` / `migrate` / `demo`
- ✅ **Skill-aware CEO** — auto-routes Founder messages to one of **17 built-in skills** covering Day-0 intake (founder.intake_brief), Start (niche/validate/product/pricing/landing/outreach + browser research), real side-effects (cold-email send via Resend, Stripe payment-link creation, **Codex-CLI code dispatch** so the CTO can actually ship code via your ChatGPT subscription, **GEO + SEO audits via RankMyAnswer.com**), AND Run (growth/support/finance/analytics)
- ✅ **Multi-task parallel dispatch** — CEO produces tasks tagged `[CTO]` / `[CMO]` / `[COO]`; Workforce runs them in parallel
- ✅ **Worker agents** — Directors spawn copywriter / designer / support sub-agents on demand
- ✅ **Chief of Staff** — blockers dedupe + triage + surface as ONE consolidated digest. Optional **LLM-driven triage** for opinionated ranking once blocker volume justifies it.
- ✅ **Persistent memory across sessions** — CEO recalls constraints, decisions, prior conversations
- ✅ **Split-tier provider chain** — Pro tier (plan/score/decide) + Workhorse tier (dispatch/format/draft) routed through any combination of: OpenCode Go, Ollama Cloud, DeepSeek direct, OpenRouter, Together, Groq, Cerebras, local Ollama, vLLM, LM Studio, custom OpenAI-compat endpoints. Subscription auth supported via Codex CLI (ChatGPT) and Claude Code CLI. Auto-swap on 503/529 overload.
- ✅ **Reasoning extraction** — chain-of-thought from thinking models captured + hidden from end users by default
- ✅ Tiered inference (Workhorse / Pro / Consultant) with multi-key + multi-provider parallelism and **session affinity** for prompt-cache hits
- ✅ Need-based hiring of CEO + C-suite + Workers
- ✅ Approval gate with per-(action × platform) autonomy + trust envelope that auto-promotes after N consecutive approvals
- ✅ Conversation router with sticky threads (24h TTL) + single-voice rule
- ✅ Cost tracking integrated with the inference pool
- ✅ **Alembic migrations** — `korpha migrate` for safe schema upgrades
- ✅ Immutable activity log on every state transition

## Quickstart

**One-liner install** (macOS + Linux; for Windows use WSL):

```bash
curl -fsSL https://raw.githubusercontent.com/Korpha/korpha/main/install.sh | bash
```

That installs [uv](https://docs.astral.sh/uv/) (the Python toolchain)
if missing, then drops the `korpha` binary on your PATH. No virtualenv
juggling, no Python pre-install required.

Then walk through the interactive setup — you'll be asked your email,
business name, and which LLM provider to use (OpenAI, Anthropic,
DeepSeek, OpenRouter, your own endpoint, …):

```bash
korpha init
```

The `init` wizard prompts for an API key from one of the supported
providers and writes everything to `~/.korpha/`. **Mike-friendly**:
no manual `.env` editing, no YAML, no env-var setup.

<details>
<summary>Manual install (for contributors / developers)</summary>

```bash
git clone https://github.com/korpha/korpha.git
cd korpha
uv venv .venv --python 3.12
source .venv/bin/activate
uv pip install -e ".[dev]"
korpha init
```
</details>

### Optional add-ons (skip any of these)

After `korpha init` you have a working install. The following are
all optional — you only need them if/when a skill asks for them.

```bash
# Image generation (Replicate / fal.ai / local SD WebUI / Codex CLI)
korpha config-image-add

# GEO + SEO via RankMyAnswer.com — let your cofounder work on getting
# eyeballs to your product or service across LLM answers (ChatGPT,
# Perplexity, Claude, Gemini) and Google
korpha config-rankmyanswer-add

# Verify everything that's configured
korpha doctor

# See your providers
korpha providers
```

**Vision** is auto-detected. If your Pro model already supports vision
(Kimi K2.6, Qwen3-VL, Llama-3.2-Vision, GLM-4V, NVIDIA Nemotron 3 Nano
Omni, …), the wizard wires it for you. Otherwise it prints the
suggestion and moves on — you can skip it entirely until a skill
needs it.

**Coding delegation** (the CTO hands code work to a CLI agent) is
optional. If you have either of these installed + logged in, Korpha
uses them; if not, the CTO falls back to drafting code instructions
the Founder runs:

```bash
# Claude Code (Pro/Max subscription)
curl -fsSL https://claude.ai/install.sh | bash && claude

# Codex CLI (ChatGPT subscription)
npm install -g @openai/codex && codex login
```

`korpha doctor` reports whether each delegation CLI is installed +
authed. Both are optional — `korpha init` doesn't force them.

### Run the web UI

```bash
korpha server
# → starts FastAPI at http://localhost:8765
```

Open `http://localhost:8765`. You get a chat interface with a sidebar of
pending approvals (Approve / Reject / Execute) and a panel showing the
Chief of Staff blocker digest. CEO is skill-aware — ask *"help me pick a
niche"* and it will auto-invoke `niche.find_micro_niches`.

OpenAPI / Swagger docs at `http://localhost:8765/docs`.

### Cost story

Korpha uses **two tiers** and you pick a provider for each:

- **Pro** — brain work: plan, score, decide. Quality matters; volume
  small.
- **Workhorse** — bulk drip: dispatch, format, draft. Cheap matters;
  volume large.

Pick a provider for each tier. The wizard runs once per provider —
typically a strong one for Pro, a cheaper one for Workhorse. We support
17+ providers (OpenAI, Anthropic, DeepSeek, OpenRouter, Together, Groq,
Cerebras, local Ollama, custom OpenAI-compat endpoints, …) plus
optional subscription auth via Codex CLI / Claude Code.

If you already have a ChatGPT Plus / Pro or Claude Pro / Max
subscription, you can route the Pro tier through it via the
`codex-cli` or `claude-code-cli` presets. Subscription quotas
fill fast, so still pair with an API-key workhorse.

What model + provider you pick is your call — Korpha stays
neutral. OpenRouter is a solid default if you want one key that
fronts most models.

### Or use the CLI

```bash
# Auto-routing: niche-shaped question → CEO calls niche.find_micro_niches
korpha ask "I'm a solo Python dev with 5 hours per week. Help me pick a micro-niche."

# Structured plan (multi-task, dispatched to CTO/CMO/COO in parallel)
korpha propose "Plan a parallel push: ship a landing page, recruit interviewees, set up signup analytics."

korpha pending                # see pending approvals
korpha approve <id>           # ✓ envelope counter: 1/5
korpha execute <id>           # → workforce dispatches to C-suite
korpha blockers               # CoS digest + open blockers
korpha skill list             # browse available skills
korpha skill run niche.find_micro_niches \
  --arg "skills=Python, FastAPI, Docker" \
  --arg "time_budget_hours=5" \
  --arg "savings_usd=2000"
korpha status                 # business + org chart + activity + spend
```

Memory persists across `ask` invocations — tell CEO a constraint once and
the next ask remembers it.

## Backup + restore

Every Korpha install lives under `~/.korpha/` (or wherever `KORPHA_DATA_DIR` points). That directory holds the sqlite DB, agent-authored skills, cron scripts, plugin configs, audit archives, and checkpoint blobs — i.e. everything the agent has learned about your business.

```bash
# Snapshot everything to a tarball
korpha backup
# → ./korpha-backup-20260508-141022.tar.gz

# Or pick the destination
korpha backup --output /mnt/backups/aig-$(date +%F).tar.gz

# Restore (refuses to clobber an existing data dir without --force)
korpha restore ./korpha-backup-20260508-141022.tar.gz
korpha restore <path> --force   # overwrite existing data dir
```

For unattended off-machine backups, schedule the `korpha backup` command via cron and rotate the resulting tarballs to S3 / Backblaze / wherever. The tarballs are gzip-compressed and self-contained — restoring on a fresh machine reproduces the entire cofounder state.

## Health monitoring

The HTTP server exposes `/healthz` for uptime monitors:

```json
{
  "status": "ok",            // "degraded" if DB unreachable
  "has_provider": true,      // an LLM provider key is configured
  "skills_loaded": 42,
  "db_reachable": true,      // SELECT 1 round-trip succeeded
  "version": "0.1.0",
  "uptime_seconds": 1287.5   // since process start
}
```

Treat anything other than `status: ok` as a paging signal. The `/healthz` response is also the simplest external contract for verifying a deploy rolled out — bump the version string and watch for it to flip.

## What's coming

See [NEXT_STEPS.md](NEXT_STEPS.md). Highest-value next items: **Cofounder
Protocol** (third-party services like Stripe / Vercel / ConvertKit
register as Korpha-native cofounder tools — moat layer), more
channels (Email reply parsing), broader skill marketplace (community
contributors ship their proven playbooks as installable skills).

---

## Built on

- **[hermes-agent](https://github.com/NousResearch/hermes-agent)** (MIT, Nous Research) — vendored as the agent runtime foundation. We get a great agent loop, skills, memory, MCP, multi-platform messaging, and provider matrix for free.
- **[paperclip](https://github.com/paperclipai/paperclip)** (MIT, paperclipai) — architectural inspiration for multi-agent business orchestration. No code redistributed; the cofounder framing is our own.

See [NOTICES](NOTICES) for full attributions.

---

## Documentation

📚 **[Full docs index → `docs/README.md`](docs/README.md)** — start here.

User guides:

- [Provider setup](docs/PROVIDERS.md) — pick LLMs, multi-account chains, recommended setups by budget
- [Skills](docs/SKILLS.md) — what each of the 17 built-in skills does
- [Approvals + trust envelope](docs/APPROVALS.md) — Approve / Deny / Discuss + auto-promotion
- [Channels](docs/CHANNELS.md) — Telegram, Discord, email setup
- [Themes](docs/THEMES.md) — change how your dashboard looks
- [Costs + spend caps](docs/COSTS.md), [Routines + heartbeats](docs/ROUTINES.md),
  [MCP servers](docs/MCP.md), [Codex delegation](docs/CODEX_DELEGATION.md),
  [Memory](docs/MEMORY.md), [Troubleshooting](docs/TROUBLESHOOTING.md)

Reference:

- [CLI reference](docs/CLI_REFERENCE.md) — every command with examples
- [API reference](docs/API_REFERENCE.md) — HTTP endpoints with curl
- [Eval baselines](docs/eval-baselines/README.md) — canonical 100% / 96.2% scores

Project:

- [BRIEF.md](BRIEF.md) — what Korpha is and who it's for
- [ARCHITECTURE.md](ARCHITECTURE.md) — system design and locked decisions
- [Cofounder Protocol](docs/COFOUNDER_PROTOCOL.md) — third-party SaaS integration spec
- [Theme contest](docs/THEME_CONTEST.md) — quarterly community theme contest

---

## Community

- **GitHub Issues** — bugs and feature requests
- **GitHub Discussions** — questions, builds, playbooks
- **GitHub Discussions** — ideas and RFC

---

## License

MIT — see [LICENSE](LICENSE).
