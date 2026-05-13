# Codex CLI delegation — letting your CTO actually ship code

**Audience**: Founders who want code changes (not just plans), AND
have an OpenAI ChatGPT Plus / Pro / Max subscription.

The CTO Director scopes work into a "ship-this-week plan" but
doesn't write code itself by default — it produces a delegation
proposal. The `code.ship_via_codex` skill closes that loop: the
proposal goes to **Codex CLI** (using your ChatGPT subscription),
which runs in your repo, writes the code, runs tests, and reports
back a summary + diff for you to review.

---

## Why this exists

Without Codex CLI delegation, the CTO can:

- Produce plans
- Recommend boring/proven tech
- Delegate to designer / copywriter Workers
- Surface blockers

…but not actually edit code in your repo. The plan ends with you
copy-pasting into Codex / Cursor / Aider yourself.

With Codex CLI delegation, the CTO can:

- Take its own scoped task
- Dispatch it to `codex exec` in your repo
- Wait for the result
- Surface the diff via the approval queue for your review

End state: you go from "the cofounder gave me a plan" to "the
cofounder shipped a draft" — and you decide whether to commit it.

---

## Setup

1. **Install Codex CLI**:
   ```bash
   npm install -g @openai/codex
   ```

2. **Sign in (one-time, opens browser)**:
   ```bash
   codex login
   ```
   Uses your existing ChatGPT subscription auth — no new account.

3. **Verify**:
   ```bash
   korpha doctor
   # → ✓ Codex CLI: ready  (codex on PATH, auth file present)
   ```

That's it. The `code.ship_via_codex` skill is registered
automatically and the CTO will route to it when appropriate.

---

## How it gets invoked

You don't usually call this skill directly. It runs when:

1. You ask the CEO/CTO to do something concrete in code — *"add a
   /healthz endpoint to api/main.py"*
2. The CTO scopes it (which file, what change, what test)
3. The CTO produces an Approval with `ActionClass.SHIP_CODE`
4. You click ✓ Approve
5. Codex runs `codex exec` in your repo
6. Result lands as a NEW Approval — you see the diff summary, can
   decide to keep / discard

You can also call it explicitly:

```bash
korpha skill run code.ship_via_codex \
  --arg "prompt=Add /healthz to api/main.py returning {status: ok}; write a test." \
  --arg "cwd=/path/to/your/repo" \
  --arg "sandbox_mode=workspace-write"
```

---

## Sandbox modes

```yaml
sandbox_mode:
  read-only          → Codex can read but not modify files
                      Use for: review-only runs ("explain this code", "audit X")
  workspace-write    → Codex can edit files in cwd, run shell commands
                      Use for: most tasks (the default)
  danger-full-access → Codex can do anything: network, install, sudo, etc.
                      Use for: infra-edit work that needs network — RARE
```

Default is `workspace-write`. `danger-full-access` is exactly that —
prefer locking it to a specific task and immediately stepping back.

---

## What "shipping code" actually does

The `code.ship_via_codex` skill subprocesses Codex CLI like this:

```bash
codex exec \
  --sandbox workspace-write \
  --cwd /your/repo \
  "Add /healthz to api/main.py returning {status: ok}; write a test in tests/test_health.py"
```

Codex reads the codebase, makes changes, runs tests if asked,
prints a summary. Korpha:

- Captures stdout
- Surfaces it as the SkillResult `summary` (truncated to 1200 chars
  for the dashboard card; full output in `payload.codex_output`)
- Cost: $0 (Codex uses your subscription, not a per-call API)

The diff sits in your working tree — **Korpha does not commit**.
You review with `git diff`, decide to keep, throw away with
`git checkout -- .`, or stage selectively.

---

## What it can't do

- **Push to remote** — explicitly disallowed in the skill. Subscription-
  paid agents shipping commits to your remote repo would be the kind of
  thing that takes down companies.
- **Run with elevated privileges** — sandbox modes prevent sudo /
  privileged ops without `danger-full-access`.
- **Modify outside `cwd`** — Codex sandbox respects the working
  directory boundary by default.

---

## Recommended task patterns

### Good prompts (specific, scoped)

- ✓ "Add /healthz to api/main.py returning {'status': 'ok'}; write
   a test in tests/test_health.py"
- ✓ "Fix the typo on line 14 of README.md ('Stripee' → 'Stripe')"
- ✓ "Refactor the db/migrations/0042 file to use SQLModel instead
   of raw SQL; preserve behavior; tests should still pass"
- ✓ "Add a CLI command `korpha foo` that prints 'bar' — no
   arguments, no docstring needed"

### Bad prompts (vague)

- ✗ "Improve the codebase"
- ✗ "Make the dashboard better"
- ✗ "Add tests" (which? for what? to what coverage?)
- ✗ "Refactor X" (using what pattern? optimizing for what?)

The CTO will REFUSE vague delegations and ask you to scope first.
That's intended — Codex is an expensive (in time) tool to point at
fuzzy work.

---

## Multi-step coding loops

Codex internally runs multi-step loops (read → think → write → test
→ revise). Korpha's `max_tokens` floor for coding skills is
**128,000** to give Codex headroom for long loops. Set in
[`korpha/inference/limits.py`](../korpha/inference/limits.py)
as `DEFAULT_MAX_TOKENS_CODING`.

Override in `~/.korpha/providers.yaml` if you want:

```yaml
defaults:
  max_tokens_coding: 256000     # for very large refactors
```

---

## Subscription quotas

ChatGPT subscription plans have daily/monthly compute quotas. Heavy
delegation can fill them. If you hit a quota:

- The Codex CLI returns an error
- Korpha surfaces it as a SkillError ("Codex quota exhausted")
- The CTO falls back to producing a plain-text plan instead of
  shipping code

Workaround: keep your **Workhorse tier on a non-subscription
provider** (Groq, DeepSeek, OpenRouter) so most of the cofounder's
inference doesn't consume Codex quota. Only the actual code-write
calls hit Codex.

See [`PROVIDERS.md`](PROVIDERS.md) for the recommended split-tier
setup.

---

## Reference

- Skill: [`korpha/skills/code_deploy.py`](../korpha/skills/code_deploy.py)
- Codex CLI wrapper: [`korpha/delegation/codex.py`](../korpha/delegation/codex.py)
- Approval class: `ActionClass.SHIP_CODE` — always DRAFT mode (you
  always approve code changes manually; trust envelope doesn't
  auto-promote this)
- Tests: [`tests/test_code_deploy_skill.py`](../tests/test_code_deploy_skill.py)
- Codex CLI itself: https://github.com/openai/codex (or `npm view @openai/codex`)
