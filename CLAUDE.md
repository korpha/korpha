# Project notes for Claude Code sessions

This file gets loaded as context at the start of every Claude Code
session opened in this repo (and its worktrees). Keep it short — the
goal is to bootstrap a fresh session into the project's conventions
without making them read everything.

## Multi-session relay (this project)

Sessions in this project coordinate via file-based inboxes at
`~/.claude/inboxes/{<role>}.md`. Roles: `main`, `marketro`, `andrew`,
`bugs`.

  - **`main`** — orchestrator + code edits. Lives in `aigenteur_agent/`.
  - **`marketro`** — drives the Marketro dashboard at `http://127.0.0.1:9000`
    via Playwright. Lives in `aigenteur_agent-marketro/` worktree.
    Dogfoods Marketro (the user's real multi-line business: KDP Activity
    Books, Evergreen T-shirts & Mugs, AI Animation Shorts, default).
  - **`andrew`** — drives the Andrew dashboard at
    `http://<andrew-mini-pc-lan-ip>:8765` via Playwright. Lives in
    `aigenteur_agent-andrew/` worktree. Dogfoods Andrew (the user's
    personal-brand X persona). Andrew is **never publicly the Korpha
    author** — independent coach positioning only.
  - **`bugs`** — fixes bugs reported by the driver sessions. Lives in
    `aigenteur_agent-bugs/` worktree. Commits to main branch; drivers
    pull + re-verify after the fix lands.

**At the start of every fresh session, before doing real work:**

1. Identify your role from `$PWD` (matches `aigenteur_agent-<role>`
   worktree naming) or from the user's bootstrap message.
2. Arm a `Monitor` on your inbox so peer messages arrive as
   task-notifications:

   ```
   Monitor:
     command: tail -F ~/.claude/inboxes/<your-role>.md 2>/dev/null
     persistent: true
     description: "<your-role> inbox relay"
   ```

   Skip if `TaskList` already shows a Monitor with this description.

3. Send to peers with:
   `~/.claude/bin/relay-send <to> "<subject>" "<body>"` (or pipe body
   via stdin). The `--from` flag overrides auto-detection.

**Reply convention:** acknowledge messages you act on by replying via
`relay-send` with `RE: <orig subject>`. Inbox files grow append-only;
rotate manually when >1 MB:
`mv inbox.md inbox.md.$(date +%F).log && touch inbox.md`.

**Recovery after reboot / session loss:** the full step-by-step
playbook (what survives a reset, infrastructure verification, per-
session bootstrap snippets, troubleshooting) lives in
`docs/MULTI_SESSION_RECOVERY.md`. Read it first if the orchestra
needs to come back up from cold.

**Driver sessions (`marketro`, `andrew`) do NOT edit code.** They drive
the dashboard via Playwright, file bug reports to `bugs`, verify fixes,
and report status to `main`. Code mutations go through `main` (design
calls) or `bugs` (fixes for confirmed defects).

## What lives where

  - `korpha/` — the Python package. Core code.
  - `korpha/api/` — FastAPI server + dashboard routes + templates.
  - `korpha/api/templates/` — Jinja2 dashboard templates.
  - `korpha/browser/` — Playwright provider + persistent profile store
    + visual fallback.
  - `korpha/social/` — social posting facade (per-platform per-business-line).
  - `korpha/migrate/` — machine-migration tooling (bundle/restore/inspect).
  - `korpha/updater.py` — `korpha update` self-update logic.
  - `scripts/install.{sh,ps1,cmd}` — first-time installers.
  - `tests/` — pytest. Run subsets like:
    `uv run pytest tests/test_social.py -q`.
  - `hermes/` — vendored reference for patterns we lift (NOT shipped
    as part of Korpha — read for inspiration, don't import from).

## Conventions worth knowing immediately

  - **uv, not pip.** All Python deps via `uv sync --frozen` /
    `uv run <command>`. `uv add <pkg>` to add a dep.
  - **No `cat`/`head`/`tail` in tool calls** — use the Read tool.
    `tail -F` for Monitors is the only exception.
  - **Always drive UI via Playwright, never curl.** Founder-facing
    tests must go through the actual dashboard.
  - **Verify your own UI changes via Playwright** before asking the
    user to look at something — don't make them check "does it load".
  - **`KORPHA_DATA_DIR` env var** controls where the data dir lives.
    Default: `~/.korpha/`. Marketro install at port 9000 uses the
    default; Andrew install uses its own.
  - **UI/CLI parity is required** — every user-facing capability must
    work in BOTH the dashboard AND the CLI. New CLI command needs a
    matching `/app/...` route.

## Memory + persistent context

The user has an auto-memory system at
`~/.claude/projects/-home-code4-aigenteur-agent/memory/`. The index is
`MEMORY.md` in that dir — load it for cross-session context (the
default `claude` session preloads it). Important rules captured there:
positioning (Andrew never the Korpha author publicly, no Skool
mentions), code style (max_tokens floors, open-weights-only for
recommendations), workflow (verify UI yourself, no manual ops for Mike).
