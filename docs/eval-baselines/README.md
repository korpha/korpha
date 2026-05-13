# Eval baselines — open-weights model comparison

Live LLM eval scores for the role prompts (`CEO`, `CMO`, `COO`, `CTO`,
`COPYWRITER`, `DESIGNER`, `SUPPORT`) shipped after the Round-2 prompt
audit + brevity-discipline lift.

**Methodology** — same as Paperclip's ClawEval style: deterministic
substring / regex / word-count assertions, no LLM-as-judge. Each
assertion either passes or fails based on the LLM's text output.
Multiple runs flatten reasoning-model nondeterminism. Provider is just
the HTTP transport — provider choice doesn't affect the score, only
latency and cost.

**Run with**: `korpha eval --tier pro --runs 3 --max-tokens 64000`
after `korpha config`.

---

## The headline

Three frontier open-weights models, all clearing 90% on the same
7-role / 80-assertion test set. Korpha works with any of them — pick
the one you prefer:

| Model | Provider tested | Pass | Total | Overall | Wall time |
| ----- | --------------- | ---- | ----- | ------- | --------- |
| DeepSeek V4 Pro | OpenCode Go (DeepSeek AI) | 77 | 80 | **96.2%** | ~75 min |
| DeepSeek V4 Flash (workhorse) | OpenCode Go (DeepSeek AI) | 77 | 80 | **96.2%** | ~25 min |
| Kimi K2.6 | OpenCode Go (Moonshot AI) | 74 | 80 | **92.5%** | 42 min |
| GLM 5.1   | OpenCode Go (Zhipu AI)    | 73 | 80 | **91.2%** | 18 min |

Pro and Flash tied at 96.2% on the harder test set — different
assertions miss but the same overall count. Validates that you can
run Korpha on either tier and get equivalent quality, just with
different latency profiles.

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
