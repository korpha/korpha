# CLI reference — every `korpha` command

**Audience**: anyone who lives in the terminal.

```bash
korpha --help              # always your first stop
korpha <command> --help    # full options for any specific command
```

This page is a curated reference with examples — `--help` is the
canonical truth.

---

## Setup + onboarding

### `korpha init`

Initialize the local config and database. Creates `~/.korpha/`,
prompts for founder email + business name, hires the CEO.

```bash
korpha init
# prompts: email, display name, business name, business description

# Or non-interactive:
korpha init --email you@example.com --name "Mike" \
               --business "WidgetCo" --description "B2B SaaS"
```

### `korpha config`

Interactive provider wizard. Pick a provider preset (OpenCode Go,
DeepSeek, OpenRouter, Ollama, etc.), paste an API key, set models
per tier. See [`PROVIDERS.md`](PROVIDERS.md).

```bash
korpha config
```

### `korpha config-rankmyanswer-add`

Add a RankMyAnswer.com API key for GEO + SEO skills.

```bash
korpha config-rankmyanswer-add
```

### `korpha config-image-add`

Add an image-generation provider (Replicate / fal.ai / local SD /
Codex CLI).

```bash
korpha config-image-add
```

### `korpha config-remove <label>`

Remove a provider entry by its label.

```bash
korpha providers              # find the label
korpha config-remove openrouter-primary
```

### `korpha doctor`

Health check: providers, RankMyAnswer, coding delegation, image gen.
Prints fix commands inline. See [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md).

```bash
korpha doctor
```

### `korpha providers`

List currently-configured provider accounts with their tier mappings.

```bash
korpha providers
```

### `korpha db-migrate`

Apply Alembic database migrations. Usually only needed after `korpha`
upgrade.

```bash
korpha db-migrate
```

> The top-level `korpha migrate` namespace hosts the host-migration
> subgroup (`bundle` / `restore` / `inspect` / `check`); the Alembic
> schema command lives at `db-migrate` to avoid the collision.

---

## Talking to your cofounder

### `korpha ask <message>`

One-shot Q+A with the CEO. Auto-routes to the right skill if
applicable.

```bash
korpha ask "Help me pick a niche. I'm a Python dev with 5h/week and $2k savings."
```

Memory persists across `ask` calls — tell CEO a constraint once,
the next ask remembers.

### `korpha chat`

Interactive REPL with the CEO. Same auto-routing as `ask`.

```bash
korpha chat
> Help me pick a niche...
> (CEO replies + auto-routes to niche.find_micro_niches)
> What about pricing for the winner?
> ...
```

### `korpha propose <message>`

Asks CEO to produce a structured multi-task plan (rather than a
chat reply). Tasks get tagged `[CTO] / [CMO] / [COO]` and dispatched
to the Directors in parallel.

```bash
korpha propose "Plan a parallel push: ship a landing page, recruit interviewees, set up signup analytics."
```

### `korpha status`

Founder + business + agent roster + recent activity.

```bash
korpha status
korpha status --activity      # add the last 50 audit-log events
korpha status --costs         # add today/week/month spend rollup
korpha status --memory        # founder brief + thread context
```

---

## Approvals

### `korpha pending`

List pending approvals.

```bash
korpha pending
```

### `korpha approve <id>`

Approve an action.

```bash
korpha approve <approval_id>
```

### `korpha reject <id>`

Reject an action.

```bash
korpha reject <approval_id>
```

### `korpha execute <id>`

Explicit re-execute (rare; for retry-after-failure).

```bash
korpha execute <approval_id>
```

### `korpha blockers`

CoS digest + open blockers.

```bash
korpha blockers
```

---

## Skills

### `korpha skill list`

Browse all 17 built-in skills + any user-added YAML skills.

```bash
korpha skill list
```

### `korpha skill run <name>`

Invoke a skill directly. Each skill defines its own parameters via
`--arg` flags.

```bash
korpha skill run niche.find_micro_niches \
  --arg "skills=Python, FastAPI, Docker" \
  --arg "time_budget_hours=5" \
  --arg "savings_usd=2000"
```

See [`SKILLS.md`](SKILLS.md) for every skill's parameters.

---

## Server (the dashboard)

### `korpha server`

Start the FastAPI server hosting the web dashboard at
`http://localhost:8765`. Includes the heartbeat loop (so routines
fire while the server is up).

```bash
korpha server
korpha server --port 9000           # custom port
korpha server --host 0.0.0.0        # bind all interfaces (LAN access)
```

For background / persistence: use `tmux`, `systemd`, `nohup`, or
similar. Korpha doesn't ship its own daemon harness.

---

## Channels

### `korpha channel-run <telegram | discord>`

Run a channel adapter (long-running). Pipes channel messages into
the agent system. See [`CHANNELS.md`](CHANNELS.md).

```bash
korpha channel-run telegram
korpha channel-run discord
```

### `korpha email-test --to <email>`

Send a test email via Resend to verify configuration.

```bash
korpha email-test --to you@example.com
```

### `korpha email-digest --to <email>`

Manually fire the daily digest.

```bash
korpha email-digest --to you@example.com
```

---

## Browser automation

### `korpha browser-do <instruction> --url <url>`

Run a browser task — agent navigates and acts.

```bash
korpha browser-do "click the signup button and screenshot the form" \
  --url https://example.com
```

### `korpha browser-test`

Smoke test of the browser provider.

```bash
korpha browser-test
```

---

## MCP servers

### `korpha mcp-list`

Show configured MCP servers + their tools. See [`MCP.md`](MCP.md).

```bash
korpha mcp-list
korpha mcp-list --tools        # full tool listing
```

---

## Multi-business (managing multiple ventures from one install)

### `korpha business-list`

```bash
korpha business-list
```

### `korpha business-create --name <name>`

Spin up a new business profile (separate cofounder, separate
budget, separate audit log).

```bash
korpha business-create --name "OtherVenture" --description "Different niche"
```

### `korpha business-switch <name>`

Switch the active business — subsequent commands operate on this one.

```bash
korpha business-switch widgetco
```

### `korpha business-export --to <file>`

Export current business — secrets-scrubbed, portable.

```bash
korpha business-export --to /tmp/widgetco.tar.gz
```

### `korpha business-import --from <file>`

Import a previously-exported business (different machine, restore
backup, etc.).

```bash
korpha business-import --from /tmp/widgetco.tar.gz
```

---

## Cofounder Protocol (third-party partners)

### `korpha cofounder install <url-or-path>`

Install a third-party Cofounder Protocol manifest. See
[`COFOUNDER_PROTOCOL.md`](COFOUNDER_PROTOCOL.md).

```bash
korpha cofounder install https://rankmyanswer.com/.well-known/cofounder.yaml
```

### `korpha cofounder list`

Show installed partners.

```bash
korpha cofounder list
```

### `korpha cofounder uninstall <name>`

```bash
korpha cofounder uninstall rank_my_answer
```

---

## Eval harness

### `korpha eval`

Score role prompts against the deterministic fixtures.

```bash
korpha eval                          # all roles, Pro tier
korpha eval --role ceo               # one role
korpha eval --tier workhorse          # Workhorse tier
korpha eval --runs 3                  # 3-run majority averaging
korpha eval --max-tokens 32000        # custom budget for A/B
korpha eval --json > out.json         # machine-readable output
```

See [`docs/eval-baselines/`](eval-baselines/) for canonical
baselines.

---

## Plugins

### `korpha plugins-list`

Show installed plugins (capability-gated out-of-process workers).

```bash
korpha plugins-list
```

---

## Demo

### `korpha demo`

In-memory end-to-end demo (no persistent DB). Useful for showing
Korpha to someone in a quick session without committing to a
local install.

```bash
korpha demo
```

---

## All commands at a glance

```
init                          status                channel-run
config                        ask                   email-test
config-rankmyanswer-add        chat                  email-digest
config-image-add               propose               browser-do
config-remove                  pending               browser-test
doctor                         approve               mcp-list
providers                      reject                business-list
migrate                        execute               business-create
                               blockers              business-switch
server                         skill list            business-export
                               skill run             business-import
eval                           plugins-list
                               cofounder install
                               cofounder list
                               cofounder uninstall
                               demo
```

`korpha <command> --help` for every option.
