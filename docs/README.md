# Korpha Documentation

This is the docs index. Start with the [project README](../README.md)
if you're brand new — it covers what Korpha is and how to install
it. The pages below go deeper.

> **Heads up**: Korpha is in active development. Specs are stable,
> the live eval harness scores 100% on the canonical baseline, but
> rough edges still show up — please open an issue if you hit one.

---

## For end users

Practical how-to guides for the people running Korpha day-to-day.

| Doc | What it covers |
| --- | --- |
| [**Provider setup**](PROVIDERS.md) | Pick LLMs for your cofounder. Wizard walkthrough; OpenCode Go vs OpenRouter vs Ollama vs subscription auth; multi-account fallback chains; recommended setups by budget. |
| [**Skills reference**](SKILLS.md) | What each of the 17 built-in skills does, parameters, when CEO auto-routes vs manual. |
| [**Approvals + trust envelope**](APPROVALS.md) | The Approve / Deny / Discuss flow. How the trust envelope auto-promotes agents from "draft proposals" to "execute and log" once you've shown trust on an action class. Core UX. |
| [**Channels guide**](CHANNELS.md) | Telegram bot setup; Discord app + bot; email outbound (Resend); per-channel approval flow. |
| [**Themes**](THEMES.md) | Switch between built-in themes, drop in your own YAML, share with others. |
| [**Costs + spend caps**](COSTS.md) | Reading the cost pill, setting per-account caps, what "saved vs Sonnet" means. |
| [**Routines + heartbeats**](ROUTINES.md) | Auto-scheduled work, cron syntax, how to add/edit/disable. |
| [**MCP servers**](MCP.md) | Wire external tools (filesystem, GitHub, Postgres, etc.) into your cofounder via Model Context Protocol. |
| [**Codex CLI delegation**](CODEX_DELEGATION.md) | Letting the CTO actually ship code via your ChatGPT subscription. |
| [**Memory + FTS5**](MEMORY.md) | What persists across sessions, how retrieval works, exporting / clearing memory. |
| [**Troubleshooting**](TROUBLESHOOTING.md) | `korpha doctor` interpretation, common errors, log locations, reset to clean state. |
| [**Eval baselines**](eval-baselines/README.md) | Canonical 100% / 96.2% scores against DeepSeek V4 Pro / Flash. How to reproduce, what the numbers mean. |

---

## For theme authors

| Doc | What it covers |
| --- | --- |
| [**Theme Protocol**](THEME_PROTOCOL.md) | Full author guide for the dashboard theme YAML format. Schema reference, validation rules, sharing mechanics. |
| [**Theme Contest**](THEME_CONTEST.md) | Quarterly community contest where the top 3 user-submitted themes ship as built-ins in the next release. Rules, schedule, judging. |

---

## For partners (Cofounder Protocol)

| Doc | What it covers |
| --- | --- |
| [**Cofounder Protocol**](COFOUNDER_PROTOCOL.md) | Full v1 spec for partner manifests. Schema, validation, install/list/uninstall flow. |
| [**RankMyAnswer integration brief**](RANKMYANSWER_INTEGRATION.md) | Reference: the original brief sent to RankMyAnswer's dev team — example of what landing in core looks like for the first canonical Cofounder Protocol partner. |

---

## Reference docs

| Doc | What it covers |
| --- | --- |
| [**CLI reference**](CLI_REFERENCE.md) | Every `korpha` subcommand with examples. |
| [**API reference**](API_REFERENCE.md) | All HTTP endpoints with curl examples + request/response shapes. |

---

## For developers / contributors

Internal-facing docs for people working on Korpha itself.

| Doc | What it covers |
| --- | --- |
| [**Architecture**](../ARCHITECTURE.md) | System design, module boundaries, data model. The "where does this code live and why" reference. |
| [**Brief**](../BRIEF.md) | The product source-of-truth — what Korpha is, who Mike is, what shipping looks like. Read this before proposing roadmap changes. |
| [**Prompt Audit**](PROMPT_AUDIT.md) | The prompt-pattern audit that drove the Round-2 prompt lift to 100% on the eval. Records what was lifted from Paperclip / Hermes / OpenClaw. |
| [**Progress**](../PROGRESS.md) | Build log — what shipped, when, why. Append-only history. |
| [**Next Steps**](../NEXT_STEPS.md) | Prioritized roadmap. Phase 2/3/4 buckets with status. |

---

## Quick links by question

**"How do I install Korpha?"** → [README, Quickstart section](../README.md#quickstart)

**"How do I add a provider / set up an API key?"** → [`PROVIDERS.md`](PROVIDERS.md)

**"What can my cofounder actually do?"** → [`SKILLS.md`](SKILLS.md)

**"How do I approve / deny actions?"** → [`APPROVALS.md`](APPROVALS.md)

**"How do I get notifications on my phone / in Discord / by email?"** → [`CHANNELS.md`](CHANNELS.md)

**"How do I change the dashboard look?"** → [`THEMES.md`](THEMES.md)

**"How do I cap LLM spend?"** → [`COSTS.md`](COSTS.md)

**"How do I schedule recurring work?"** → [`ROUTINES.md`](ROUTINES.md)

**"How do I make my cofounder edit code?"** → [`CODEX_DELEGATION.md`](CODEX_DELEGATION.md)

**"How do I add external tools (GitHub, Slack, Postgres)?"** → [`MCP.md`](MCP.md)

**"What does the cofounder remember? Can I clear it?"** → [`MEMORY.md`](MEMORY.md)

**"Something's broken — where do I start?"** → [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md)

**"Full CLI command list?"** → [`CLI_REFERENCE.md`](CLI_REFERENCE.md)

**"HTTP API endpoints?"** → [`API_REFERENCE.md`](API_REFERENCE.md) (or live at `/docs` when running)

**"My SaaS wants to integrate"** → [`COFOUNDER_PROTOCOL.md`](COFOUNDER_PROTOCOL.md)

**"How good are the agents?"** → [`docs/eval-baselines/`](eval-baselines/README.md)

**"How do I report a bug / suggest a feature?"** → [GitHub Issues](https://github.com/korpha/korpha/issues)

**"Where do I get help / hang out?"** → [GitHub Discussions](https://github.com/korpha/korpha/discussions)
