# Cost tracking + spend caps — knowing what your cofounder is spending

**Audience**: anyone who wants to keep an eye on LLM bills (or who's
worried they shouldn't have to).

Korpha tracks every LLM call: tokens in / tokens out / per-call
USD cost (computed from the provider's published rates). The tracker
runs whether you're using a $10/mo subscription or pay-as-you-go API
keys — even when marginal cost is $0, the input/output tokens are
recorded so you can see relative load.

---

## The cost pill (top-right of dashboard)

Every page in the dashboard has a small pill in the topbar:

> **$0.0312 today · saved $1.84 vs Sonnet**

- **Today** — running USD spend across all providers, since 00:00
  local time
- **Saved vs Sonnet** — counterfactual: if you'd run every call
  through Anthropic Sonnet at full rate, what would you have paid?
  The delta is your actual win. Hidden when delta < $0.01.

Click the pill → goes to `/app/costs` for the full view.

---

## The Costs page

`/app/costs` shows:

- **Today / This week / This month** rollups
- **Per-provider breakdown** — which account is doing what work
- **Per-tier breakdown** — Pro spend vs Workhorse spend (catch
  unintended Pro-tier overuse)
- **Top 10 most-expensive sessions** — drill into one to see the
  request that drove the cost
- **Daily-spend chart** — last 30 days

All cost rows are immutable — every LLM call writes a `Cost` row
to the audit log; the page just aggregates.

---

## CLI reading

```bash
korpha status               # includes today/week/month rollup
korpha status --costs       # cost-only view, more detail
```

---

## Setting spend caps

You can hard-cap spend per provider account in
`~/.korpha/providers.yaml`:

```yaml
providers:
  - preset: opencode-go
    label: opencode-go-primary
    api_key_env: OPENCODE_GO_API_KEY
    tiers:
      pro: deepseek-v4-pro
      workhorse: deepseek-v4-flash
    concurrency_limit: 4
    spend_cap_usd: 25.00          # ← hard ceiling per month
```

When the account hits the cap:

- New requests against this account fail with
  `SpendCapExceeded` at the inference layer
- The pool falls through to your next configured provider
  (if you have one)
- The cost pill shows a red `⚠ cap` badge until the next month rolls
  over OR you raise the cap

Caps are advisory by default — they prevent runaway loops, not
intentional spend. If you intentionally need to blow past the cap
for one task, raise it temporarily.

### Recommended starting caps

| Setup | Recommended cap |
| --- | --- |
| Solo solopreneur, daily use | $30/mo per account |
| Heavy daily use (multi-business, channel automations) | $100/mo per account |
| Team-of-2 use | $150-300/mo split across accounts |

Subscription accounts (`opencode-go`, `codex-cli`, `claude-code-cli`)
have their own platform-side quotas — Korpha's `spend_cap_usd` is
moot for those (cost is $0 in our tracker).

---

## "Saved vs Sonnet" — what the metric means

Sonnet 4.6 is our chosen reference rate (Anthropic's general-purpose
mid-tier). For every request, we compute:

```
sonnet_equivalent_cost = (input_tokens × $3.00 / 1M) + (output_tokens × $15.00 / 1M)
your_actual_cost       = (input_tokens × your_provider_input_rate) + ...
saved                  = sonnet_equivalent_cost - your_actual_cost
```

If you're on `opencode-go` (subscription) running DeepSeek V4 Pro,
your actual cost is $0 and the savings line shows the full Sonnet
equivalent. If you're on Sonnet directly, the savings line shows $0.
Negative deltas (Sonnet would've been cheaper) get hidden — we don't
shame you for picking quality.

Why Sonnet specifically: it's the most common "I just want a
sensible LLM" reference. Comparing against GPT-4o or Claude Opus
would be misleading on either end.

---

## Reasoning-model token accounting

Reasoning models (DeepSeek V4 Pro, Kimi K2.6, GLM-5, Claude with
extended thinking) emit **reasoning tokens** before visible output.
Korpha tracks these separately:

- `input_tokens` — what we sent
- `output_tokens` — what was returned, including reasoning
- `cached_tokens` — server-side prompt cache hits (some providers
  charge less for these)

The cost calc uses the provider's actual published rate per token
type. If a provider charges differently for reasoning vs visible
output, Korpha respects the split.

---

## Free tiers + "$0 marginal"

If you've configured a subscription provider (`opencode-go`,
`codex-cli`, `claude-code-cli`, `ollama-cloud`), per-call cost
shows as **$0.00** because the cost is rolled into your monthly
subscription, not metered per call.

That's true within reason — **subscriptions have quotas**. If you
DM the bot 500 times in an hour, you'll hit the quota and the
provider returns 429. Korpha falls through to the next provider
in your chain (if configured) or surfaces the error.

The $0 number is **honest accounting of marginal cost**, not a
claim of "free forever." See [`PROVIDERS.md`](PROVIDERS.md) for the
recommended setups that handle quota fills gracefully.

---

## Reference

- Cost tracker: [`korpha/inference/cost_tracker.py`](../korpha/inference/cost_tracker.py)
- Per-call cost computation: [`korpha/inference/providers/mock.py`](../korpha/inference/providers/mock.py)
  (the rate table lives here)
- Schema: every `Cost` row in the audit table has provider, account,
  tier, tokens (input/output/cached), USD, timestamp
- Dashboard: `/app/costs`
- API: `GET /me` (includes today's spend), full breakdown via
  the dashboard or DB
