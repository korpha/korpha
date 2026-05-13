# Provider setup — pick LLMs for your cofounder

**Audience**: anyone setting Korpha up for the first time, OR
adding a second / third provider for fallback or cost optimization.

**The 30-second version**: run `korpha config`, pick a provider
from the menu, paste your API key, done. The wizard handles the rest.

---

## Two tiers, one provider per tier

Korpha splits LLM calls into two tiers:

- **Pro** — brain work: plan, score, decide. Quality matters; volume small.
- **Workhorse** — bulk drip: dispatch, format, draft. Cheap matters; volume large.

You pick a provider for each tier. They can be the same provider
(both tiers point at OpenRouter, say) or different (Pro on
DeepSeek direct, Workhorse on Groq).

The wizard runs **once per provider**. Most users do two passes:
strong-and-careful for Pro, fast-and-cheap for Workhorse.

---

## Supported providers

Run `korpha config` and pick from this list:

### OpenAI-compatible (API key)

| Preset | Endpoint | Why pick it |
| --- | --- | --- |
| `openai` | `api.openai.com/v1` | If you already have an OpenAI account and want zero new accounts. |
| `anthropic` | `api.anthropic.com/v1` | Claude direct API. Fine for Pro tier; Sonnet 4.6 is a strong default. |
| `deepseek` | `api.deepseek.com/v1` | Direct DeepSeek — V4 Pro / Flash. Often the cheapest Pro on the planet right now. |
| `openrouter` | `openrouter.ai/api/v1` | One key, hundreds of models. Easy default if you want a single bill. |
| `groq` | `api.groq.com/openai/v1` | Fastest open-weights inference. Great for Workhorse. |
| `together` | `api.together.xyz/v1` | Together AI — solid open-weights catalog, good Llama / Qwen access. |
| `cerebras` | `api.cerebras.ai/v1` | Wafer-scale inference — extremely fast Llama / Qwen. |
| `nous-portal` | Nous Research portal | Hermes / DeepHermes models from the Hermes team. |
| `nvidia-nim` | NVIDIA NIM | Hosted NVIDIA models including Nemotron. |
| `zai` | Z.ai | GLM-4.6 / GLM-5 — strong open-weights Pro alternative. |
| `moonshot` | Moonshot AI | Kimi K2 / K2.6 direct. |
| `minimax` | MiniMax | MiniMax M-series. |
| `huggingface` | HuggingFace router | Anything on HuggingFace Inference Endpoints. |

### Aggregator (subscription bundles)

| Preset | What it is |
| --- | --- |
| `opencode-go` | OpenCode Go ($10/mo subscription) — open-weights bundle including DeepSeek V4 Pro/Flash, Kimi K2.6, GLM-5, Qwen3.6. **Cheapest path to frontier open-weights.** |
| `opencode-zen` | OpenCode Zen — premium bundle (Claude / GPT / Opus). Pay-as-you-go. |
| `ollama-cloud` | Ollama Cloud subscription — Llama / Mistral / DeepSeek hosted by Ollama. |

### Subscription auth (no API key — uses your existing seat)

| Preset | What it is |
| --- | --- |
| `codex-cli` | OpenAI Codex CLI — uses your **ChatGPT Plus / Pro / Max subscription**. You install `npm install -g @openai/codex` + run `codex login` once; Korpha subprocesses it. **No marginal cost** beyond your existing subscription, but quotas fill fast on heavy days. |
| `claude-code-cli` | Claude Code CLI — same shape but for **Claude Pro / Max**. Install with `curl -fsSL https://claude.ai/install.sh \| bash && claude`. |

### Local

| Preset | What it is |
| --- | --- |
| `local-ollama` | Local Ollama at `http://localhost:11434`. Free if you have the GPU. Default models: `llama3.1:8b` (Workhorse) / `llama3.1:70b` (Pro). |
| `custom` | Any other OpenAI-compat endpoint (vLLM, LM Studio, your own proxy, self-hosted vLLM at `http://10.0.0.5:8000/v1`, etc.). The wizard prompts for `base_url` + `name`. |

---

## The wizard flow (interactive)

```bash
korpha config
```

It asks, in order:

1. **Pick a provider** (numbered menu of all the presets above)
2. **(custom only)** Enter `base_url` + `name`
3. **API key** — pasted into your terminal once. Stored in
   `~/.korpha/providers.yaml` with the `api_key_env` indirection
   so the actual key sits in `~/.korpha/.env` not the YAML.
4. **Model name per tier** — defaults pre-filled per preset, you
   can override. E.g. `deepseek-v4-pro` for Pro, `deepseek-v4-flash`
   for Workhorse.
5. **Concurrency limit** — default 4 (parallel requests cap)
6. **Spend cap (USD)** — optional hard ceiling per account

That's it. Repeat for the second tier if you want a different
provider there.

---

## What gets written where

```
~/.korpha/
├── providers.yaml      ← which providers, which model per tier, caps
├── .env                ← actual API keys (chmod 600, gitignored from your homedir)
└── config.yaml         ← active dashboard theme + other UI prefs
```

`providers.yaml` references env-var names, not raw secrets:

```yaml
providers:
  - preset: opencode-go
    label: opencode-go-primary
    api_key_env: OPENCODE_GO_API_KEY    # ← reads .env
    tiers:
      workhorse: deepseek-v4-flash
      pro: deepseek-v4-pro
    concurrency_limit: 4
    spend_cap_usd: 25.00
```

You can edit either file by hand if you want — both are documented
schemas, no proprietary format. But the wizard is the Mike-friendly
path.

---

## Multi-provider chains (failover + cost optimization)

You can configure multiple providers. The pool tries them in order;
on `503` / `529` overload responses it auto-rotates to the next.

```yaml
providers:
  - preset: opencode-go            # Pro tier primary
    api_key_env: OPENCODE_GO_API_KEY
    tiers: { pro: deepseek-v4-pro, workhorse: deepseek-v4-flash }

  - preset: ollama-cloud           # Pro fallback if OpenCode Go has issues
    api_key_env: OLLAMA_CLOUD_API_KEY
    tiers: { pro: deepseek-v3.1:671b-cloud }

  - preset: groq                   # Workhorse fallback (fast + cheap)
    api_key_env: GROQ_API_KEY
    tiers: { workhorse: llama-3.1-8b-instant }
```

Order matters — earlier entries are tried first when no session
affinity exists. Multiple entries against the same preset are fine
(useful when you have multiple keys for higher rate limits).

---

## Recommended setups by budget

**$10/mo — single subscription, frontier open-weights**

```
Pro:        opencode-go    → deepseek-v4-pro
Workhorse:  opencode-go    → deepseek-v4-flash
```

Everything works on one $10 subscription. Recommended for solo
solopreneurs treating Korpha as their daily driver.

**$0 marginal — bring your own ChatGPT / Claude subscription**

```
Pro:        codex-cli      → codex-default (uses your ChatGPT Plus / Pro)
Workhorse:  groq           → llama-3.1-8b-instant ($0.05 / 1M tokens)
```

Subscription-paid Pro + dirt-cheap Workhorse. Quotas on the
subscription fill if you're heavy; the cheap workhorse keeps the
bulk drip flowing.

**$0 truly — local-only**

```
Pro:        local-ollama   → llama3.1:70b
Workhorse:  local-ollama   → llama3.1:8b
```

Free if you have a GPU. Slower than hosted; quality below frontier
open-weights — but offline-capable and zero data leaves your
machine.

**Pay-as-you-go API keys — no subscription**

```
Pro:        deepseek       → deepseek-reasoner
Workhorse:  deepseek       → deepseek-chat
```

DeepSeek direct is one of the cheapest frontier APIs available.
Workhorse calls run ~$0.001 per call.

---

## Troubleshooting

**"No provider configured"**
→ Run `korpha config` and add at least one. `korpha doctor`
shows whether one's been registered.

**"401 unauthorized" or "auth failed"**
→ Your API key in `~/.korpha/.env` is wrong / expired / revoked.
Check the key on the provider's dashboard; re-paste via
`korpha config-remove <label>` then `korpha config` again.

**"402 credits exhausted"**
→ Pay-as-you-go account out of credit. Top up at the provider's
billing page. Korpha will not silently retry on 402 — surfaces
the error to you.

**"429 rate-limited"**
→ Hit the provider's per-minute cap. Korpha auto-retries with
backoff; if it persists, lower `concurrency_limit` in your provider
entry, or add a second account with the same preset for parallelism.

**"503 / 529 overloaded"**
→ Provider is having a bad day. Korpha auto-rotates to the next
provider in your chain. If you only have one provider, the request
fails — add a fallback.

**Subscription auth not working (codex-cli / claude-code-cli)**
→ Confirm `codex login` / `claude` ran successfully outside
Korpha. `which codex` and `which claude` should both resolve
to a real binary on PATH. `korpha doctor` reports whether each
is installed + authed.

**Mixing camelCase and snake_case in providers.yaml**
→ Both are accepted (`api_key_env` and `apiKeyEnv` both work). If
something is being silently ignored, double-check the YAML
indentation — that's the usual culprit.

---

## Reference

- Schema source: [`korpha/inference/config.py`](../korpha/inference/config.py)
- Preset definitions: [`korpha/inference/providers/openai_compat.py`](../korpha/inference/providers/openai_compat.py)
- Wizard source: [`korpha/cli_config.py`](../korpha/cli_config.py)
- Run `korpha providers` to see what's currently configured
- Run `korpha doctor` for a full health check of provider + delegation
- Run `korpha config-remove <label>` to drop a misconfigured entry
