# Prompt Audit — Korpha vs Paperclip

Comparing Korpha's home-built role harnesses against
[paperclipai/paperclip](https://github.com/paperclipai/paperclip)'s CEO
onboarding assets (MIT licensed). **No code or text is copied** — only
patterns and structural decisions are absorbed.

Audit date: 2026-05-04. Paperclip head:
[`server/src/onboarding-assets/ceo/`](https://github.com/paperclipai/paperclip/tree/master/server/src/onboarding-assets/ceo)
(SOUL.md, AGENTS.md, HEARTBEAT.md, TOOLS.md).

## 1. Their structure

Paperclip decomposes a role into **four files** loaded together at agent
boot:

| File | Purpose | Length |
|---|---|---|
| `SOUL.md` | Persona — strategic posture + voice/tone | ~30 specific bullets |
| `AGENTS.md` | Runtime rules — what you do/don't, delegation, references | ~40 directives |
| `HEARTBEAT.md` | Cycle checklist run on every wake/tick | ~25 numbered steps |
| `TOOLS.md` | Tool inventory (often a stub the role fills in) | varies |

The CEO's AGENTS.md ends with a `## References` block pointing at the
other three so re-reading is forced.

## 2. What they got right (patterns to absorb)

### 2.1 Strategic posture is operational, not motivational

Paperclip's CEO SOUL.md has 13 specific posture lines covering economics,
P&L thinking, focus, hiring, customer closeness. Each is a *call* the
agent has to make in real situations. Examples (paraphrased): own the
P&L, optimize for learning speed and reversibility, hire slow / fire
fast, treat every dollar as a bet with a thesis, pull for bad news.

**Korpha today:** zero strategic posture. The CEO `cofounder_voice`
is one paragraph of behavior hints (`"Be direct, specific, and brief"`),
no economic / capital-allocation framing.

### 2.2 Voice and Tone is its own section, with concrete don'ts

Paperclip splits **persona** from **voice**. Their voice section has
absolute rules: "no exclamation points unless something is on fire",
"Lead with the point, then give context", "Default to async-friendly
writing", "praise rare and specific".

**Korpha today:** voice is mixed into the same paragraph as the
strategic stance. No section break, no concrete "never do X" lines.

### 2.3 Hard "MUST" rules in the runtime instructions

AGENTS.md uses capital-letter directives where the wrong default is
catastrophic:
- "You MUST delegate work rather than doing it yourself"
- "Do NOT write code, implement features, or fix bugs yourself"

These exist because LLMs default to *helpfulness* and will happily roll
up sleeves and write the code themselves, which collapses the role
hierarchy. Paperclip's CEO is told flatly to refuse.

**Korpha today:** Director prompts say things like *"You default to
ACTION"* — which is the opposite gravity. CTO/CMO/COO are told to do
the work. CEO doesn't have a "MUST delegate" rule at all.

### 2.4 Two explicit lists: "What you DO personally" / "What you DON'T"

Paperclip's CEO has a numbered list of the 6 things the CEO actually
does (set priorities, resolve conflicts, communicate with the board,
approve/reject proposals, hire, unblock reports) and the implicit
inverse — everything else gets delegated.

**Korpha today:** No explicit list. Domain keywords tell `Workforce`
which Director gets a tagged task, but the *CEO itself* doesn't know
the policy.

### 2.5 Routing rules embedded in the CEO's prompt, not just code

Paperclip's CEO AGENTS.md spells out: code/bugs/features → CTO,
marketing/content/social → CMO, UX/design → UXDesigner, cross-functional
→ break into subtasks. The CEO can therefore make routing calls
*without consulting the framework code*.

**Korpha today:** routing lives in
`korpha/cofounder/director.py:_DOMAINS` as keyword lists; the CEO
has no idea which Director owns what. When the CEO is composing a plan
with `[CTO]` / `[CMO]` / `[COO]` tags, it's *guessing*.

### 2.6 Status taxonomy with semantics

Paperclip's HEARTBEAT.md defines explicit states (todo / in_progress /
in_review / blocked / done / cancelled) with one-line semantics for
each, plus transition rules (e.g. "Never retry a 409 — that task
belongs to someone else"). The agent's prompt knows the lifecycle.

**Korpha today:** `TaskStatus` is a Python enum the framework uses;
no Director or CEO prompt mentions states. They produce free-text
updates.

### 2.7 Heartbeat checklist is numbered and concrete

Paperclip's CEO has a 7-step ordered checklist for every cycle:
identity check → local plan → approvals → assignments → checkout →
delegation → fact extraction. Each step has API calls / files / fields.

**Korpha today:** no per-tick checklist. The CEO responds
to whatever the Founder typed. There's no "every morning, here's what
you do" routine.

### 2.8 References block at the end of AGENTS.md

A pointer list to the other files (`./HEARTBEAT.md`, `./SOUL.md`,
`./TOOLS.md`) with one-line purpose each. Forces re-loading.

**Korpha today:** monolithic single-paragraph prompts. Nothing to
re-load.

## 3. What we got right that they didn't (worth keeping)

- **Brevity by default.** Paperclip's CEO harness loaded together is
  ~3,500 tokens. Ours is ~200. Short prompts are cheaper at every turn
  and force the model to reason from priors. Match Paperclip's specificity
  without bloating to 4 files for every Worker.
- **Domain keywords for cheap routing.** Code-level routing is faster
  and cheaper than asking the LLM to classify. Keep that as the primary
  path; reserve LLM routing for ambiguous cases.
- **Trust envelope** — a real auto-promotion mechanism Paperclip
  doesn't appear to have. Don't drop this when adopting their patterns.

## 4. Pattern-by-pattern lift, ordered by leverage

| # | Pattern | Where to apply | Effort |
|---|---|---|---|
| 1 | Hard "MUST delegate / MUST NOT do the work" rules | CEO `cofounder_voice` | trivial |
| 2 | Routing rules in the CEO prompt | CEO `cofounder_voice` (add CTO/CMO/COO/Workers domain map) | small |
| 3 | "What you DO / DON'T" explicit lists per Director | each `*_PERSONALITY.system_prompt` | small |
| 4 | Voice/Tone section with concrete don'ts | each role | small |
| 5 | Strategic posture (P&L, capital allocation, focus) | CEO only | small |
| 6 | Status taxonomy in the prompt | shared addendum to all roles | small |
| 7 | Heartbeat checklist as a separate constant | new `HEARTBEAT_PROMPT` (used by tick handler) | medium |
| 8 | Four-file harness on disk | NOT recommended for the OSS install — overkill for one-Founder use, but worth offering as a power-user option later |

## 5. Decisions

- **Apply 1–6 now.** They're the highest-leverage and don't add
  filesystem complexity.
- **Defer 7** to when we wire heartbeats per role (we have heartbeats
  for cron-style work but no per-role checklist today).
- **Skip 8.** Mike's install shouldn't have a `~/.korpha/agents/ceo/`
  directory of `.md` files. The four-file pattern serves Paperclip's
  multi-user, multi-agent companies — overengineering for our shape.

## 6. Outstanding gaps Paperclip can't fix

- **No evals.** We don't know if our prompts are good *or* if the
  Paperclip-inspired refinement is better. Wire ClawEval to score
  before/after — that's the only honest signal.
- **No worker-spawning isolation.** Workers run in their parent
  Director's session. OpenClaw's subagent pattern (separate session +
  cheaper model per subagent) is a better answer than anything in
  Paperclip. Worth a separate audit.

## 7. Attribution

Paperclip is MIT-licensed. We've borrowed *patterns* (decomposition,
hard-rule directives, routing-in-prompt, voice/tone separation), not
prose. Korpha's `BRIEF.md` and `README.md` already credit Paperclip
as architectural inspiration — no further attribution needed for
pattern-level borrowing under MIT.
