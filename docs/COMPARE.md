# Korpha vs Hermes vs OpenClaw vs Paperclip

People landing here often already know one of the other three. This page is
honest about how they differ so you can pick the one that fits your stack
and how you want to work with AI.

Short version:

| | What it is | Who it's for |
| --- | --- | --- |
| **[Hermes](https://github.com/NousResearch/hermes-agent)** | Agent framework — multi-provider inference, eval harness, prompt cache | AI/ML engineers building their own agents |
| **OpenClaw** | Coding CLI à la Claude Code — terminal-first, ships code | Developers who want a self-hosted coding agent |
| **[Paperclip](https://github.com/paperclipai/paperclip)** | Open-source AI workforce (TypeScript) | Founders running a business via "AI employees" |
| **Korpha** | Open-source AI cofounder (Python) | Solopreneurs / SMB owners who want a peer, not a workforce to manage |

All four are MIT. All four are self-hostable. The differences below are
about ecosystem, framing, and what's in the box.

---

## The framing difference (the one that actually matters)

Hermes, OpenClaw, and Paperclip all use some flavor of **"employee" /
"worker"** framing in their materials. You tell them what to do, they do
it. You manage them.

Korpha is built on a **cofounder** frame. The CEO and C-suite hold your
business context, take initiative, propose plans, and ask for approval on
the calls that need a human signoff. You stay captain via approval, not
direction.

Why this is more than wordplay: managing AI employees adds cognitive load.
You assign tasks, review output, decide what's next. A real cofounder
*reduces* cognitive load — they know what to do without being told, and
the only thing you have to do is approve or redirect. That shows up
concretely in Korpha as:

- A trust envelope that widens with track record — interventions get rarer
  as the cofounder proves itself
- Approval queues with inline ✓ / ✗ / 💬 buttons — captain mode, not
  managerial mode
- Persistent memory that carries forward goals, constraints, and prior
  decisions — you don't repeat yourself
- A Day-0 brief intake that locks down hours/week, savings, skills,
  constraints — every downstream skill pulls from it

If you'd rather direct workers, Paperclip or a Hermes-built workforce will
serve you better.

---

## Side-by-side

| | Hermes | OpenClaw | Paperclip | **Korpha** |
| --- | --- | --- | --- | --- |
| **Project type** | Framework | Specialized tool (coding) | Product | Product |
| **Language stack** | Python | TypeScript / Node | TypeScript / Node (pnpm monorepo) | Python (single package) |
| **License** | MIT | MIT | MIT | MIT |
| **Self-hostable** | Yes | Yes | Yes | Yes |
| **Out-of-the-box agents** | None — you wire them | One (coding) | Multiple (employees) | CEO + CMO + COO + CTO + per-Line VPs |
| **Multi-line business model** | N/A | N/A | Partial (multi-company isolation) | **POD / KDP / Info / SaaS / Affiliate / Agency Line Packs ship by default** |
| **Approval-based UX** | N/A | N/A | Manage workforce | **Trust envelopes + inline approve/deny/discuss** |
| **Built-in skills** | N/A | Coding | Multiple agent skills | 17 founder-shaped skills (niche, validate, landing, outreach, P&L, etc.) |
| **Inference backbone** | First-party | First-party | First-party | **Hermes-agent (vendored)** |
| **Coding-CLI integration** | First-party | Is the coding CLI | First-party | **Uses OpenClaw / Codex CLI / Claude Code as plug-in tools** |
| **Eval harness** | First-party | N/A | First-party | First-party (uses same ClawEval fixtures) |
| **Provider matrix** | Largest (32+) | OpenAI-compat | Their own list | 21 native presets + custom OpenAI-compat |
| **Subscription auth (ChatGPT/Claude)** | Via CLI dispatch | Native | Their own | Via Codex CLI + Claude Code CLI |
| **Agent framing** | Worker | Coding employee | Employees | **Cofounder (peer, proposes, you approve)** |

---

## When you should pick which

### Pick **Hermes** if:
- You're a researcher or platform engineer building your own agent
- You want low-level control over the inference layer
- You don't need pre-built business agents — you'll write the workflow

### Pick **OpenClaw** if:
- You want a Claude-Code-style coding agent on your own infra
- The task is primarily *write code* — not run a business
- You're already terminal-native and don't need a web UI

### Pick **Paperclip** if:
- You live in the TypeScript / Node ecosystem
- The "AI workforce I manage" mental model fits how you think
- You prefer pnpm monorepo + Node tooling for your infra

### Pick **Korpha** if:
- You live in the Python / AI ecosystem (Hugging Face, pytorch, agent
  frameworks)
- You want a *cofounder*, not a workforce — agents that take initiative
  and ask for approval, not employees you direct
- You want pre-built business lines (POD, KDP, Info, SaaS, Affiliate,
  Agency) you can switch on, not a workforce you build from scratch
- You want to plug existing coding CLIs (OpenClaw, Codex, Claude Code) in
  as tools rather than rebuild the coding workflow

---

## How they fit together

None of these are zero-sum. The honest picture:

- **Korpha builds on Hermes.** The vendored `hermes/` directory provides
  the inference backbone — provider matrix, prompt cache, session
  affinity, retry logic. Korpha's contribution is what sits on top: the
  cofounder + C-suite + Line Pack layer.

- **Korpha calls OpenClaw as a tool.** When the CTO needs to ship code,
  it dispatches through OpenClaw / Codex CLI / Claude Code (whichever
  you have configured). Korpha doesn't compete with them — it consumes
  them.

- **Korpha is a Python-native alternative to Paperclip.** Same problem
  shape, different ecosystem, different relationship framing. Mike picks
  based on which language community he wants to live in and whether he
  prefers a cofounder or a workforce.

Pick the one that fits how you want to work. They all earn their license.
