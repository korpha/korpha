# Which open-weights models work with AIgenteur — and which to avoid

We tested every major open-weights model on AIgenteur's actual role
prompts (`CEO`, `CMO`, `COO`, `CTO`, `COPYWRITER`, `DESIGNER`,
`SUPPORT`) so you don't have to figure out which one to pick by
trial-and-error. **Recommendations + warnings are below the
methodology.** Skip down to "Picks per use case" if you just want
the bottom line.

> ### 🟢 Use these — they work cleanly inside AIgenteur
> - **Local, ~3 GB VRAM** (laptop iGPU / Steam Deck): **Gemma-4-E2B-it**
>   — 96.2%, 8 min eval. The standout small-model pick.
> - **Local, ~8 GB VRAM** (mid-range dGPU): **Microsoft Phi-4** —
>   93.8%, 4 min eval. Fastest in this tier by a wide margin.
> - **Local, ~11 GB VRAM** (3060 12GB / 4070): **Ministral-3-14B-Reasoning**
>   — 93.8%, 6 min eval. The reasoning lift actually pays off at
>   this size.
> - **Local, ~22 GB VRAM** (3090 / 4090): **Gemma-4-31B** — 92.5%,
>   25 min eval. Long context (262k via TurboQuant turbo3).
> - **Cloud Pro tier**: **DeepSeek V4 Pro** — 96.2%. Frontier
>   open-weights, OpenCode Go subscription.
> - **Cloud Workhorse tier**: **DeepSeek V4 Flash** — 96.2%, 3×
>   faster. Pair with the Pro brain for split-tier routing.
>
> ### 🔴 Avoid these — they break AIgenteur's response format
> - **Qwen3.5-2B** (61.3%) — reasoning trace eats the visible-token
>   budget, many tasks return *empty content*. Worse than the
>   850 MiB Liquid model at 3.5× the VRAM. Skip.
> - **LiquidAI LFM2.5-350M** (76.2%) — only acceptable as a
>   curiosity for tiny-footprint demos; produces ugly cofounder
>   responses. Don't ship to founders.
> - **Microsoft Phi-4-reasoning-plus** (78.8%) — the reasoning
>   variant *loses* 5pp vs non-reasoning Phi-4 (93.8%) at 2× VRAM.
>   Use Phi-4 instead.
>
> **Why we publish this:** AIgenteur is only as good as the model
> you plug into it. Picking a 61% adherence model and watching the
> dashboard render 4000-word "tweets" with reasoning trace
> bleeding into the visible output is a guaranteed bad first
> experience — for you and for the cofounder layer above the model.
> We test broadly so you can pick confidently; we flag the bad
> picks so nobody has that experience by accident.

---

## Methodology + caveats

Scores below come from `korpha eval --tier pro --runs 3 --max-tokens 64000`
after `korpha config`.

**How scoring works:** deterministic substring / regex / word-count
assertions over the model's text output — no LLM-as-judge. Each
assertion passes or fails. Multiple runs flatten reasoning-model
nondeterminism. Provider is just the HTTP transport — provider
choice doesn't affect the score, only latency + cost.

> ## What this eval is and isn't — read before quoting the numbers
>
> **What it measures:** *instruction-following compliance with Korpha's
> role-prompt scaffolding.* Does the model write under the word cap,
> lead with the recommendation, hit required substrings, avoid
> forbidden phrases (`ETA`, marketing fluff, dead-end `no`), and
> match the format the dashboard expects. It's an **adherence test**
> with a fairly low ceiling — any modern instruction-tuned model can
> read "write under 80 words, end with a number" and execute it. This
> is why a Q4-quantized 31B model running on a single consumer GPU
> scores the same as a frontier ~1T-parameter cloud model. Both can
> follow simple format rules; that's the whole bar this eval sets.
>
> **What it does NOT measure:**
>
> - **Deep reasoning on novel problems.** Fixture prompts are
>   single-turn, scoped, and have clear right-answer *shapes* the
>   model only has to dress correctly. No multi-step decomposition,
>   no genuine planning, no hard math/logic chains. The big-vs-small
>   model gap shows up there — not here.
> - **Multi-step agentic execution.** Whether the model correctly
>   picks the right skill from 60+ available, handles partial
>   failures, recovers from a botched tool call, threads context
>   across 20 turns — none of that is in the fixture set.
> - **Factual accuracy.** Many assertions are format-based; the
>   model can hit them with confidently-worded nonsense.
> - **Tool-use quality.** No skill invocation, no JSON-schema
>   argument generation, no retry-on-error.
> - **Memory + context handling.** Each call is independent.
> - **Real founder outcomes.** Does the cofounder actually help you
>   ship a business? Not measured. That's what the live-API e2e
>   probe is for, and it's why we dogfood with Marketro.
>
> **What it IS useful for:** picking a model that will *work* inside
> Korpha's prompt scaffolding out of the box. A model that bombs
> this eval will produce ugly cofounder responses (over-long, missing
> the format the dashboard expects) even if it's brilliant on hard
> benchmarks. A model that passes will give you clean, well-shaped
> responses — but on a 50-step business plan, a complex code-edit
> task, or a thorny multi-day decision tree, the bigger model
> typically still wins on substance.
>
> **TL;DR — small local model tying big cloud model here ≠ they're
> equivalent cofounders.** It means both can follow Korpha's prompt
> rules. For raw capability, see each model's MMLU / GSM8K / HumanEval
> / SWE-Bench / MATH scores on its HuggingFace card; those measure
> what most people mean when they ask "is this model good."

---

## Full results — all models tested

Every model run through the same 7-role / 80-assertion fixture set.
Status column at a glance: 🟢 = ship-ready, 🟡 = works but rough,
🔴 = will produce broken output, skip.

### Cloud models

| Status | Model | Provider | Pass | Total | Overall | Wall time |
| :---: | ----- | -------- | ---- | ----- | ------- | --------- |
| 🟢 | DeepSeek V4 Pro | OpenCode Go (DeepSeek AI) | 77 | 80 | **96.2%** | ~75 min |
| 🟢 | DeepSeek V4 Flash (workhorse) | OpenCode Go (DeepSeek AI) | 77 | 80 | **96.2%** | ~25 min |
| 🟢 | nvidia/nemotron-3-super-120b-a12b | OpenRouter | 77 | 80 | **96.2%** | ~30 min |
| 🟢 | arcee-ai/trinity-large-thinking | OpenRouter | 77 | 80 | **96.2%** | ~27 min |
| 🟢 | openai/gpt-oss-120b | OpenRouter | 76 | 80 | **95.0%** | ~28 min |
| 🟢 | poolside/laguna-m.1 | OpenRouter | 76 | 80 | **95.0%** | ~26 min |
| 🟢 | nvidia/nemotron-3-nano-30b-a3b | OpenRouter | 75 | 80 | **93.8%** | ~25 min |
| 🟢 | poolside/laguna-xs.2 | OpenRouter | 75 | 80 | **93.8%** | ~22 min |
| 🟢 | Kimi K2.6 | OpenCode Go (Moonshot AI) | 74 | 80 | **92.5%** | 42 min |
| 🟢 | nvidia/nemotron-3-nano-omni-30b-a3b-reasoning | OpenRouter | 74 | 80 | **92.5%** | ~25 min |
| 🟢 | baidu/cobuddy | OpenRouter | 74 | 80 | **92.5%** | ~24 min |
| 🟢 | GLM 5.1 | OpenCode Go (Zhipu AI) | 73 | 80 | **91.2%** | 18 min |
| — | nvidia/llama-nemotron-embed-vl-1b-v2 | OpenRouter | _N/A_ | _N/A_ | _embedding-only_ | _N/A_ |

### Local models (RTX 3090, single card)

| Status | Model | VRAM | Pass | Total | Overall | Wall time |
| :---: | ----- | ---- | ---- | ----- | ------- | --------- |
| 🟢 | **Gemma-4-E2B-it (Q4_K_M)** | **~3 GB** | **77** | **80** | **96.2%** | **8 min** |
| 🟢 | Ministral-3-14B-Reasoning (Q4_K_M) | 11 GB | 75 | 80 | **93.8%** | 6 min |
| 🟢 | Microsoft Phi-4 (Q4_K_M, q8_0 kv) | ~8 GB | 75 | 80 | **93.8%** | ~4 min |
| 🟢 | Qwen3.5-9B (Q4_K_M, q8_0 kv, reasoning) | ~9 GB | 75 | 80 | **93.8%** | ~40 min |
| 🟢 | IBM Granite-4.1-8B (Q4_K_M, q8_0 kv) | ~8 GB | 74 | 80 | **92.5%** | ~100 min |
| 🟢 | Gemma-4-E4B-it (BF16) | ~9 GB | 74 | 80 | **92.5%** | 16 min |
| 🟢 | Gemma-4-31B (Q4_K_M, TurboQuant) | 23 GB | 74 | 80 | **92.5%** | 25 min |
| 🟢 | Qwen3.6-27B (Q4_K_M, TurboQuant turbo3) | ~22 GB | 74 | 80 | **92.5%** | ~68 min |
| 🟢 | IBM Granite-4.1-30B (Q4_K_M, TurboQuant turbo4) | ~16 GB | 73 | 80 | **91.2%** | ~125 min |
| 🟡 | Ministral-3-14B-Instruct (Q4_K_M) | 11 GB | 71 | 80 | **88.8%** | 6 min |
| 🟡 | IBM Granite-4.1-3B (Q4_K_M, q8_0 kv) | ~4 GB | 69 | 80 | **86.2%** | ~90 min |
| 🟡 | Qwen3.5-4B (Q4_K_M, q8_0 kv, reasoning) | ~5 GB | 68 | 80 | **85.0%** | ~45 min |
| 🔴 | Microsoft Phi-4-reasoning-plus (Q4_K_M, q8_0 kv) | ~16 GB | 63 | 80 | **78.8%** | ~38 min |
| 🔴 | LiquidAI LFM2.5-350M (Q4_K_M) | ~850 MiB | 61 | 80 | **76.2%** | ~20 s |
| 🔴 | Qwen3.5-2B (Q4_K_M, q8_0 kv, reasoning) | ~3 GB | 49 | 80 | **61.3%** | ~54 min |

**Status legend:**
- 🟢 **Ship-ready** (≥90%) — clean output inside AIgenteur's prompt scaffolding.
- 🟡 **Works but rough** (85–89%) — usable, but expect occasional
  format slips (long-form bloat, missing the right section label).
  Fine for hobby use; pick a 🟢 entry for founder-facing demos.
- 🔴 **Avoid** (<85%) — produces visibly broken cofounder responses
  (empty replies, 4000-word "tweets", reasoning trace bleeding into
  user-visible output). Not what you want a founder's first
  impression of AIgenteur to look like.

**Four local options across the quality spectrum:**

- **Ministral-3-14B-Reasoning** — 10.8 GB VRAM, 32k context, 6 min
  eval, **93.8%**. Tops cloud Kimi + cloud Gemma-31B on prompt
  adherence. Runs on a 12 GB card (3060 12GB / 4070 / 7700 XT).
  Reasoning layer adds 5pp over the Instruct variant on the same
  base — the model uses CoT to self-check brevity caps before
  responding. **Best local Pro-tier pick.**
- **Gemma-4-31B** — 22.9 GB VRAM, 262k context (via TurboQuant
  turbo3), 25 min eval, **92.5%**. Bigger model = much longer
  context for multi-turn cofounder conversation. Needs a 24 GB
  card (3090 / 4090 / 7900 XTX).
- **Qwen3.6-27B** — ~22 GB VRAM, 262k context (TurboQuant turbo3),
  ~68 min eval, **92.5%**. Bigger param count than Ministral 14B
  but loses to it on adherence (verbose copywriter, header-before-
  punchline CTO) — same failure shape as cloud Kimi K2.6. Pick this
  if you want the long-context window + are okay trading some
  format discipline for it; pick Gemma-4-31B at the same VRAM tier
  for faster runs.
- **Ministral-3-14B-Instruct** — 10.8 GB VRAM, 32k context, 6 min
  eval, **88.8%**. Same base as the Reasoning variant but no
  thinking layer. Faster, more predictable, slightly lower
  adherence. Useful for Workhorse-tier dispatch / format / draft
  work where Korpha's prompt does the structural heavy lifting.

**Reasoning ≠ free win — counterexample (Phi-4-reasoning-plus):**
The Plus variant gets 78.8% — **5pp lower than Microsoft's non-
reasoning Phi-4 (93.8%) on the same eval, same hardware**. Failure
pattern is striking: it passes the structural assertions but blows
every brevity cap. e.g. `cmo.specific_headline` (cap: 200 words)
returns 4,150 words; `copywriter.tweet_announcement` (cap: 60)
returns 4,729. The reasoning trace bleeds into the visible response.
For cofounder workloads where formatting + word caps are the
primary failure mode, the unconditional reasoning layer is a net
negative. Use it for pure problem-solving tasks (math, code) where
the verbosity is the answer, not where the answer is "3 bullets".

**Same-base A/B (Reasoning vs Instruct, both Ministral-3-14B):**
The reasoning variant gains +5pp overall (88.8% → 93.8%) at
essentially the same wall time. The lift comes almost entirely
from word-cap and format compliance — Reasoning hits 100% on
CMO + COPYWRITER + CTO + DESIGNER, where Instruct misses
brevity caps in 2 of 3 runs. Reasoning costs you slightly higher
per-token latency but for cofounder workloads the trade is worth
it.

Read this as: **Korpha runs fully offline on consumer hardware.**
A 12 GB card and the Reasoning variant gets you 93.8% — beating
two of the four cloud options. Pair either local model with a
cloud Pro model via split-tier routing for a frontier brain doing
planning + local doing bulk drip work.

DeepSeek Pro and Flash tied at 96.2% — they trade individual
assertions but hit the same overall count, validating split-tier
routing (Pro for plan/score/decide, Workhorse for dispatch/format/
draft).

---

## Kimi K2.6 (3-run averaged, 7 roles)

Reasoning model from Moonshot AI. 256k context window. On OpenCode Go
as `kimi-k2.6` (resolves to `moonshotai/kimi-k2.6-20260420`). Also
available on Ollama Cloud as `kimi-k2.6:cloud`.

| Role        | Pass | Total | %          |
| ----------- | ---- | ----- | ---------- |
| CEO         | 16   | 16    | **100.0%** |
| CMO         | 10   | 10    | **100.0%** |
| COO         | 13   | 13    | **100.0%** |
| DESIGNER    | 10   | 10    | **100.0%** |
| COPYWRITER  |  9   | 11    | 81.8%      |
| CTO         |  9   | 11    | 81.8%      |
| SUPPORT     |  7   |  9    | 77.8%      |
| **Overall** | **74** | **80** | **92.5%** |

Cost: $0.0000 (subscription, not metered).
Raw: [`kimi-k2.6.txt`](kimi-k2.6.txt)

**Where Kimi loses points**: all 6 failures are **brevity-cap** or
**lead-with-the-recommendation** formatting. Kimi gets the right
answer; it just writes 130–250 words when the prompt caps at 80–200,
and starts with section headers (`Day 1**`, `Options:`) before the
punchline. Content is correct, presentation is wordy — consistent
with its reasoning-model nature.

---

## GLM 5.1 (3-run averaged, 7 roles)

Reasoning model from Zhipu AI. 200k context window. On OpenCode Go
as `glm-5.1` (resolves to `frank/GLM-5.1`). Also available on Ollama
Cloud as `glm-5.1:cloud`.

| Role        | Pass | Total | %          |
| ----------- | ---- | ----- | ---------- |
| CMO         | 10   | 10    | **100.0%** |
| COO         | 13   | 13    | **100.0%** |
| DESIGNER    | 10   | 10    | **100.0%** |
| CTO         | 10   | 11    | 90.9%      |
| CEO         | 14   | 16    | 87.5%      |
| COPYWRITER  |  9   | 11    | 81.8%      |
| SUPPORT     |  7   |  9    | 77.8%      |
| **Overall** | **73** | **80** | **91.2%** |

Cost: $0.0000 (subscription, not metered).
Raw: [`glm-5.1.txt`](glm-5.1.txt)

**Where GLM loses points**: brevity-cap on copywriter
(headline+subhead 222 words vs 80 cap; tweet 90 words vs 60 cap),
plus 2 CEO assertions where the model leads with `No. Not yet.` and
then explains — failing the "don't dead-end with no" rule. Same
verbose-reasoner pattern as Kimi.

**GLM is the fastest of the three** — 18 min total wall time vs
Kimi's 42 min vs DeepSeek's ~30 min. Useful when iterating on
prompts.

---

## DeepSeek V4 Pro (3-run averaged, 7 roles)

Open-weights frontier reasoning model from DeepSeek AI. On OpenCode
Go as `deepseek-v4-pro`. Tested with `--max-tokens 64000` to give
reasoning headroom; identical methodology to Kimi + GLM.

| Role        | Pass | Total | %          |
| ----------- | ---- | ----- | ---------- |
| CEO         | 16   | 16    | **100.0%** |
| CMO         | 10   | 10    | **100.0%** |
| COO         | 13   | 13    | **100.0%** |
| DESIGNER    | 10   | 10    | **100.0%** |
| SUPPORT     |  9   |  9    | **100.0%** |
| CTO         | 10   | 11    | 90.9%      |
| COPYWRITER  |  9   | 11    | 81.8%      |
| **Overall** | **77** | **80** | **96.2%** |

Cost: $0.0000 (subscription, not metered).
Raw: [`deepseek-v4-pro.txt`](deepseek-v4-pro.txt)

**Where DeepSeek loses points**: same uniform pattern as Kimi/GLM —
brevity caps (copywriter 95 words for headline+subhead vs 80 cap;
tweet 81 words vs 60 cap) and lead-with-recommendation formatting
(CTO writes `Plan: 2-day ship**` before the punchline in 2 of 3
runs). The historical 4-role baseline that scored 100% didn't
include the 3 Worker roles where verbose reasoning models naturally
overshoot caps.

---

## Gemma-4-31B local (3-run averaged, 7 roles)

Open-weights model from Google DeepMind, served entirely locally on
a single RTX 3090. Q4_K_M quantization (22.9 GB / 24.6 GB VRAM),
TurboQuant turbo3 KV-cache fork of llama.cpp, **full native 262k
context window**. ~36 tok/s decode, ~150 tok/s prompt processing.
No cloud calls, no API key, no subscription.

| Role        | Pass | Total | %          |
| ----------- | ---- | ----- | ---------- |
| CEO         | 16   | 16    | **100.0%** |
| COO         | 13   | 13    | **100.0%** |
| DESIGNER    | 10   | 10    | **100.0%** |
| CTO         | 10   | 11    | 90.9%      |
| CMO         |  9   | 10    | 90.0%      |
| SUPPORT     |  8   |  9    | 88.9%      |
| COPYWRITER  |  8   | 11    | 72.7%      |
| **Overall** | **74** | **80** | **92.5%** |

Cost: $0.0000 (local compute, your electricity bill).
Raw: [`gemma-4-31b-local.txt`](gemma-4-31b-local.txt)

**Where Gemma loses points** (same uniform pattern as the cloud
models, slightly amplified):
- COPYWRITER: writes 191 words for headline+subhead (vs 80 cap),
  66 words for tweet (vs 60 cap), occasionally drops "cutting-edge"
  as marketing fluff
- CMO: omits literal `Subject:` labels in 3 cold-email variants (uses
  bold markdown headers without the word). Same flake as DeepSeek
  Flash on this exact assertion.
- SUPPORT: writes "ETA" in a bug-report response (1 of 3 runs)
- CTO: leads with `Options for MVP hosting:` before the punchline

Same brevity + format pattern as the cloud models. Content is
correct; presentation overshoots in the same predictable ways.

**Why this matters**: Korpha's cofounder loop runs on a $700-used
GPU at 92.5% — the same score as Moonshot AI's frontier Kimi K2.6
served from the cloud. Pair this with the Stripe + (your CRM) +
(your email tool) integrations and you have a fully local AI
cofounder. No vendor lock-in, no data leaves your machine.

---

## Ministral-3-14B-Reasoning local (3-run averaged, 7 roles)

Mistral's 14B reasoning variant, served locally. Same base as the
Instruct version below + a thinking layer. Q4_K_M, 10.8 GB / 24.6 GB
VRAM, 32k context, upstream llama.cpp. Recipe: max_tokens 8000,
reasoning_budget_tokens 2000.

| Role        | Pass | Total | %          |
| ----------- | ---- | ----- | ---------- |
| CMO         | 10   | 10    | **100.0%** |
| COPYWRITER  | 11   | 11    | **100.0%** |
| CTO         | 11   | 11    | **100.0%** |
| DESIGNER    | 10   | 10    | **100.0%** |
| COO         | 12   | 13    | 92.3%      |
| CEO         | 14   | 16    | 87.5%      |
| SUPPORT     |  7   |  9    | 77.8%      |
| **Overall** | **75** | **80** | **93.8%** |

Cost: $0.0000 (local compute).
Raw: [`ministral-3-14b-reasoning-local.txt`](ministral-3-14b-reasoning-local.txt)

**Why this matters**: a 14B model in 10.8 GB VRAM, running on a
$300-used 3060 12GB-class card, **outperforms** cloud Kimi K2.6
and cloud Gemma-4-31B and ties a Pro-tier subscription cost-wise
at $0. The reasoning layer pulls it 5pp above the Instruct variant
of the same base (88.8% → 93.8%) — visible specifically on
brevity and format checks (COPYWRITER 100% vs Instruct's 81.8%,
CMO 100% vs 90%, CEO 87.5% vs 81.2%). The model uses CoT to
self-check word counts before responding.

**Where it still loses points**: CEO writes 26-bullet plans (vs
12-bullet cap), uses 'no.' on pushback (forbidden dead-end),
SUPPORT writes 373-word legal-thread reply (vs 200-word cap) and
mentions ETA in bug reports. Same uniform pattern as all the other
models.

---

## Ministral-3-14B-Instruct local (3-run averaged, 7 roles)

Same base as the Reasoning variant above, but **without** the
thinking layer. Non-reasoning instruct model. Q4_K_M, 10.8 GB
VRAM, 32k context, upstream llama.cpp.

| Role        | Pass | Total | %          |
| ----------- | ---- | ----- | ---------- |
| CTO         | 11   | 11    | **100.0%** |
| DESIGNER    | 10   | 10    | **100.0%** |
| COO         | 12   | 13    | 92.3%      |
| CMO         |  9   | 10    | 90.0%      |
| COPYWRITER  |  9   | 11    | 81.8%      |
| CEO         | 13   | 16    | 81.2%      |
| SUPPORT     |  7   |  9    | 77.8%      |
| **Overall** | **71** | **80** | **88.8%** |

Cost: $0.0000 (local compute).
Raw: [`ministral-3-14b-local.txt`](ministral-3-14b-local.txt)

**Where Ministral loses points**: CEO has the deepest miss — 29
bullet lines for `ceo.first_plan` (vs 12-bullet cap), and it leads
with `No.** Here's why:` on pushback (forbidden dead-end `no.`).
Worker roles miss on the same brevity caps as everyone else.

**Why it still matters**: 6-minute eval wall time, runs on a 12 GB
card, and **scores 100% on CTO + DESIGNER**. Even with the overall
88.8%, this is the right model for:

- **Workhorse-tier dispatch** when paired with a cloud Pro model
- **Fully offline founders on a budget GPU** (3060 12 GB / 4070)
- **Drip work where Korpha's prompt does the heavy lifting** —
  format, draft, simple-yes-no, single-skill dispatch

Pair Ministral as Workhorse + DeepSeek V4 Pro as Pro via the
split-tier provider chain and you get cloud-quality planning with
local-quality bulk execution at near-zero marginal cost per call.

---

## DeepSeek V4 Flash — workhorse tier (3-run averaged, 7 roles)

Cheaper sibling for bulk drip work (dispatch / format / draft) when
running with split-tier providers. Coverage extends to the 3 Worker
roles (designer / copywriter / support) — sub-agents Directors spawn
for specialty work.

| Role        | Pass | Total | %          |
| ----------- | ---- | ----- | ---------- |
| CEO         | 16   | 16    | **100.0%** |
| CMO         |  9   | 10    | 90.0%      |
| COO         | 13   | 13    | **100.0%** |
| CTO         | 11   | 11    | **100.0%** |
| COPYWRITER  | 10   | 11    | 90.9%      |
| DESIGNER    | 10   | 10    | **100.0%** |
| SUPPORT     |  8   |  9    | 88.9%      |
| **Overall** | **77** | **80** | **96.2%** |

Cost: $0.0000 (subscription, not metered).
Raw: [`deepseek-v4-flash-workers.txt`](deepseek-v4-flash-workers.txt)

---

## What this tells us

**Korpha is not a single-model story.** Three open-weights frontier
reasoning models — DeepSeek V4 Pro, Kimi K2.6, GLM 5.1 — all clear
90% on the same fixture set. Run any of them and get a working
cofounder; the differences are stylistic (Kimi is verbose, GLM is
fast, DeepSeek is tight) not capability.

**The remaining ~8% miss is uniform across models**: brevity caps
(reasoning models naturally write longer than 80-word headlines) and
"lead with the recommendation" formatting (models like to label
sections before the punchline). These are prompt-tuning targets, not
model-capability gaps. The same prompt that gets a 100% from one
model gets a 92% from another by overshooting word counts on 2 of 80
assertions.

**Tier-aware deployment confirmed.** Workhorse (deepseek-v4-flash)
trails Pro by ~4 pts. Validates the split-tier routing recipe (Pro
for plan/score/decide, Workhorse for dispatch/format/draft).

---

## Lift trajectory — Round-2 prompt audit

Every step measured against the same fixture set, using DeepSeek V4
Pro as the historical reference:

| Iteration | Overall | CEO | CMO | COO | CTO |
| --- | --- | --- | --- | --- | --- |
| Initial (8k tokens cap) | 72.0% | 68.8% | 70.0% | 92.3% | 54.5% |
| 16k tokens floor | 86.0% | 81.2% | 90.0% | 84.6% | 90.9% |
| + CMO/COO brevity | 92.0%¹ | 93.8% | 100% | 100% | 72.7% |
| + CEO bracket-tags + bullet cap | 92.0%¹ | 93.8% | 100% | 100% | 72.7% |
| + CTO language patterns | 92.0%¹ | 93.8% | 80% | 100% | 90.9% |
| **+ CTO options-list + CEO this-week + CMO label fix** | **100%** | **100%** | **100%** | **100%** | **100%** |

¹ same 92% reached different ways; CTO ↔ CMO traded score on each
iteration as prompt-changes shifted model behavior.

**Three things that actually moved the needle**:
1. **Reasoning-headroom floor** — 16k max_tokens (now 64k for the
   new eval set) unlocked +14pp by stopping the model from
   truncating mid-CoT.
2. **Explicit language patterns** — bracket-tag delegation (CEO),
   timeline vocabulary (CTO), brevity caps (CMO/COO/CEO) — these
   constrain the model's natural drift.
3. **Multi-run averaging** — surfaces flakiness honestly. Without
   `--runs 3` we'd ship a "looks 100%" prompt that flips to 70%
   one run in three.

---

## Known noise

Reasoning models are non-deterministic without a fixed seed. Borderline
assertions can flip run-to-run. Three remediations that work:

1. Run the eval 3× and average — flattens the borderline flips.
2. Widen the substring alternations on assertions where synonyms
   are clearly equivalent.
3. Tighten the prompt where the model's natural output drifts from
   the desired pattern.

## Reproducing

```bash
# 1. configure provider (any open-weights frontier model)
korpha config

# 2. swap providers.yaml model name to test a different model
#    pro: kimi-k2.6        # Moonshot AI K2.6, 256k context
#    pro: glm-5.1          # Zhipu GLM 5.1, 200k context
#    pro: deepseek-v4-pro  # DeepSeek V4 Pro

# 3. run the 3-run averaged sweep with reasoning headroom
korpha eval --tier pro --runs 3 --max-tokens 64000

# 4. Workhorse sweep
korpha eval --tier workhorse --runs 3

# 5. JSON output for CI / regression checks
korpha eval --tier pro --json > baseline.json
```

The eval reads `korpha/inference/limits.py` for max_tokens (default
16,000). For reasoning models we override to 64,000 via
`--max-tokens` so the chain-of-thought has headroom. Override in
`providers.yaml` under `defaults:` if you want this persisted.
