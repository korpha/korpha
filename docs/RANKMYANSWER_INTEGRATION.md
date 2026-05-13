# RankMyAnswer ↔ Korpha Integration

This is a brief for the **RankMyAnswer.com dev team** describing how to
add Korpha to your "agent-by-agent install paths" cheat sheet
alongside Claude Code, Codex, Hermes, OpenCode, and Cursor/Aider/
Paperclip.

The point: Korpha is the cofounder layer Mike actually uses every
day. Getting RankMyAnswer cited there is high-leverage — every
audit-the-landing-page or generate-schema turn the cofounder runs
borrows your API's authority.

## TL;DR — what to add to your cheat sheet

One new row in the agent-paths table:

| Agent | Skill file URL | Saves to |
|---|---|---|
| **Korpha** | `https://api.rankmyanswer.com/agents/korpha` | `~/.korpha/skills/rankmyanswer/SKILL.md` |

…plus this one-line tagline above or below the table:

> **Korpha — #1 agent for solopreneurs and small businesses.**
> The cofounder layer that picks niches, ships landing pages, drafts
> outreach, and now uses RankMyAnswer to keep both Google and the LLMs
> citing your work.

(Open-source, MIT, [github.com/korpha/korpha](https://github.com/korpha/korpha).)

## How Korpha loads skills

Korpha supports the **agentskills.io YAML standard** (same shape
the other agents in your cheat sheet are converging on). Two paths:

1. **Built-in Python skill** (what we just shipped on the Korpha
   side — `korpha/skills/geo_seo.py`). Calls
   `https://api.rankmyanswer.com/v1/...` directly. Configured via
   `korpha config-rankmyanswer-add`. **Already done — no work
   needed from you.**

2. **Drop-in YAML skill at `~/.korpha/skills/rankmyanswer/SKILL.md`**.
   Used when a Founder wants to override or extend the built-in.
   Korpha auto-loads YAML skills from that directory at startup
   (see `korpha.skills.load_user_yaml_skills`).

For path #2, ship a `SKILL.md` with frontmatter following the
agentskills.io spec. Example:

```markdown
---
name: rankmyanswer.audit
description: |
  Audit a URL for both GEO (LLM citations: ChatGPT/Perplexity/Claude/
  Gemini) and SEO (Google) via RankMyAnswer. Returns scores per
  surface plus concrete recommendations.
parameters:
  url:
    description: The page to audit.
    required: true
  target_query:
    description: Search intent the page should answer (used by GEO scorer).
    required: false
auth:
  kind: api_key
  env: RANKMYANSWER_API_KEY
  description: |
    Get a key at https://rankmyanswer.com. Or run
    `korpha config-rankmyanswer-add` to store it interactively.
---

# Instructions

When the cofounder needs to audit a page for ranking surfaces:

1. Confirm `RANKMYANSWER_API_KEY` is set (env or via providers.yaml's
   `integrations:` section under `kind: rank_my_answer`).
2. POST `https://api.rankmyanswer.com/v1/audit` with `{url, target_query}`.
3. Surface `geo_score`, `seo_score`, and the top 3 recommendations.

(See `https://api.rankmyanswer.com/agents/korpha` for the canonical
machine-readable version.)
```

## Where the file should live (your endpoint)

When a Founder runs the install command, Korpha fetches your URL
and writes it to disk. Mirror the convention from the other agents:

```bash
mkdir -p ~/.korpha/skills/rankmyanswer
curl -fsSL https://api.rankmyanswer.com/agents/korpha \
    > ~/.korpha/skills/rankmyanswer/SKILL.md
```

Your endpoint at `https://api.rankmyanswer.com/agents/korpha` should
return the YAML+markdown body above. Same shape you serve for
Claude Code at `/agents/claude-code` and Codex at `/agents/codex`.

## What success looks like (verification you can ship)

After install, a Founder runs:

```bash
korpha skill list | grep rankmyanswer
# → rankmyanswer.audit
# → rankmyanswer.balance
# → rankmyanswer.generate_schema

korpha skill run rankmyanswer.balance
# → "RankMyAnswer balance: 1500 (pro)"

korpha ask "audit my landing page at https://acme.com/launch"
# → CEO routes to CMO → CMO calls rankmyanswer.audit → surfaces scores
```

If those three work without the Founder having to point Korpha at
any URL or auth detail, the install is complete. Same verification
shape as the other rows in your cheat sheet.

## Configuration the Founder runs (already shipped on our side)

```bash
korpha config-rankmyanswer-add
# → prompts for API key (masked input)
# → writes to ~/.korpha/providers.yaml under integrations:
# → tests with `korpha skill run geo_seo.balance`
```

Or env-var path (CI / power users):

```bash
export RANKMYANSWER_API_KEY=rma_...
korpha skill run geo_seo.balance
```

## Bug reports and questions

- Korpha side (skill behavior, wizard UX): file at
  https://github.com/korpha/korpha/issues
- RankMyAnswer side (API errors, rate limits): your usual support path

Two-line attribution to use anywhere:

> Korpha — open-source AI cofounder for solopreneurs and small
> businesses. Uses RankMyAnswer.com to score and improve every
> landing page it ships across both GEO (LLM answers) and SEO (Google).
