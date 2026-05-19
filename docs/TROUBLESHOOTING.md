# Troubleshooting + `korpha doctor`

**Audience**: anyone who hit something that didn't work.

Start here:

```bash
korpha doctor
```

That single command checks every major dependency and prints
**what's wrong + the exact command to fix it**. Below covers what
each check means + common errors not surfaced by `doctor`.

---

## What `korpha doctor` checks

```
Korpha health check

  ✓ Inference provider: configured
    (run `korpha providers` to see which)

  ✓ RankMyAnswer (GEO + SEO): configured

  Coding delegation (optional)
    Lets the CTO ship code. Skip if you only want planning + drafting.
  ○ Claude Code: installed but not signed in
      → claude
  ✓ Codex CLI: ready  (codex on PATH, auth file present)
```

Symbols:

| Symbol | Meaning |
| --- | --- |
| `✓` | Configured + working |
| `○` | Not configured (yellow — needs your action if you want this feature) |
| `·` | Not configured (gray — purely optional, no action needed) |
| `✗` | Configured but broken (red — fix needed) |

Each `○` / `✗` line is followed by an indented `→` arrow with the
exact command to fix it.

---

## Common errors + fixes

### "No provider configured. Run `korpha config` first."

You haven't added any LLM provider. Run `korpha config` and pick
one. See [`PROVIDERS.md`](PROVIDERS.md) for the deep dive.

### "401 unauthorized" / "auth failed"

API key is wrong / expired / revoked.

```bash
korpha providers              # find the label of the broken account
korpha config-remove <label>  # drop it
korpha config                 # re-add with the correct key
```

### "402 credits exhausted"

Pay-as-you-go provider is out of credit. Top up at the provider's
billing page. Korpha surfaces 402 directly — does not silent-retry.

### "429 rate-limited"

You hit the provider's per-minute cap. Korpha auto-retries with
exponential backoff. If it persists:

- Lower `concurrency_limit` in `~/.korpha/providers.yaml` for
  that account
- Add a second account with the same preset (different key) for
  parallelism
- Switch to a higher-tier plan with the provider

### "503 / 529 overloaded"

Provider is having a bad day. Korpha auto-rotates to the next
provider in your chain. If you only have one provider, the request
fails — add a fallback. See [`PROVIDERS.md`](PROVIDERS.md) for
multi-provider chains.

### "Empty response from model"

Almost always a `max_tokens` floor issue with reasoning models —
DeepSeek V4 Pro / Kimi K2.6 / GLM-5 spend chain-of-thought tokens
before producing visible output. Korpha ships with 16k floor for
agents, 128k for coding. If you've manually overridden the floor in
`~/.korpha/providers.yaml`:

```yaml
defaults:
  max_tokens_normal: 16000     # don't drop below this for reasoning models
  max_tokens_coding: 128000
```

Bump back up if you accidentally dropped it.

### "FTS5 not available" (memory search)

SQLite was built without FTS5. Either:

- Reinstall SQLite with FTS5: `brew install sqlite` on macOS,
  `apt install libsqlite3-dev` + reinstall Python on Linux
- OR disable memory search: Korpha falls back to LIKE-based
  search automatically; the warning is informational only

### "alembic head mismatch"

DB schema is out of date.

```bash
korpha db-migrate
```

If it errors, the schema may have drifted. Backup
`~/.korpha/korpha.db` first, then:

```bash
cp ~/.korpha/korpha.db ~/.korpha/korpha.db.bak
korpha db-migrate --force
```

### "browser automation: playwright not installed"

```bash
uv pip install playwright
playwright install chromium
```

Or install the agent-browser CLI as the second backend:
`npm install -g @anthropic/agent-browser`. Korpha auto-discovers
either.

### "no codex on PATH" when CTO tries to ship code

```bash
npm install -g @openai/codex
codex login   # one-time, opens a browser
```

### "claude code: installed but not signed in"

```bash
claude   # opens a browser for Claude Pro / Max login
```

### Subscription quota hit (codex-cli / claude-code-cli)

Subscription auth has a daily / monthly quota that fills fast on
heavy workloads. When it does:

- Either switch the affected tier to an API-key provider for the rest
  of the day (`korpha config`)
- Or add a cheap workhorse (Groq / DeepSeek) so most calls don't
  hit the subscription

The split-tier design (Pro = subscription, Workhorse = cheap API)
exists exactly for this case.

### Dashboard says "Inference provider: configured" when it shouldn't

Pre-`a16edc98`-fix bug. Make sure you're on a recent build —
`load_dotenv()` walks up from cwd now (not from package source),
so a fresh install with no providers reports correctly. If still
broken: `korpha --help` should show command list; if commands
are missing, your install is partial — re-run the curl installer.

### Skill fails with "Skill X not configured"

For skills that require an external service (RankMyAnswer, Resend,
Stripe), you need to configure the integration first:

```bash
korpha config-rankmyanswer-add        # for geo_seo.* skills
echo 'RESEND_API_KEY=re_...' >> ~/.korpha/.env  # for outreach.send_cold_email
echo 'STRIPE_API_KEY=sk_...' >> ~/.korpha/.env  # for commerce.create_payment_link
```

`korpha doctor` reports which integrations are configured.

---

## Where logs go

```
~/.korpha/
├── korpha.db         ← SQLite database (sessions, agents, approvals, audit)
├── activity.log         ← human-readable activity stream (tail -f for live)
└── crashes/             ← stack traces from any uncaught exception
```

Live tail:

```bash
tail -f ~/.korpha/activity.log
```

For the immutable audit log (every approval, skill run, message),
use the dashboard `/app/activity` page or `korpha status --activity`.

---

## Reset to clean state

If everything's broken and you want to start fresh:

```bash
# Backup first (always!)
cp -r ~/.korpha ~/.korpha.backup-$(date +%Y%m%d)

# Wipe state, keep providers config
rm ~/.korpha/korpha.db
korpha init   # re-creates DB + asks for founder + business

# OR full reset (re-do provider setup too)
rm -rf ~/.korpha/
korpha init
korpha config
```

The install doesn't ship "factory reset" as a CLI subcommand
deliberately — losing state by accident is worse than typing two
`rm` commands intentionally.

---

## Still stuck

- **GitHub Issues**: [github.com/korpha/korpha/issues](https://github.com/korpha/korpha/issues)
  — search first; open new issue with `korpha doctor` output
  pasted in
- **GitHub Discussions**: [github.com/korpha/korpha/discussions](https://github.com/korpha/korpha/discussions)
  — `#troubleshooting` category; people in your situation are usually
  there
- **Logs to share** — `korpha doctor` + the last 30 lines of
  `~/.korpha/activity.log` (scrub keys before posting publicly)

Issues with full repro (commands you ran + output you got) get
fixed fastest.
