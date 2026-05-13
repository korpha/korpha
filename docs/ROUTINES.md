# Routines + heartbeats — auto-scheduled work

**Audience**: anyone who wants Korpha to do something on a
schedule (weekly support triage, daily digest, monthly P&L review)
without you remembering to ask.

A **routine** is a scheduled invocation of a skill (or a custom
multi-step plan). The **heartbeat** loop wakes routines up on their
cron schedule, runs them, and writes the result to the audit log
+ surfaces any approvals.

---

## The default routines

These ship enabled out of the box (you can disable in settings):

| Name | Cadence | What it does |
| --- | --- | --- |
| `cos.daily_digest` | every day at 8am local | Chief of Staff aggregates the previous 24h: blockers, approvals waiting, KPI movements. Sends to your active channels (dashboard always; email/Telegram if configured). |
| `cos.weekly_review` | Monday 9am | Full weekly digest — one consolidated view of what shipped, what's pending, where the budget went. |
| `analytics.weekly_kpis` | Monday 9am | Aggregates revenue + spend + retention numbers for the digest. |
| `support.triage_inbox` | every hour | If support inbox is wired (`SUPPORT_INBOX_*` env vars), pulls new tickets and queues triage drafts. Off by default until configured. |
| `finance.weekly_review` | Monday 9am | Pulls last week's revenue/cost; produces the plain-English P&L summary. |

All of them surface their output via your configured channels — none
are silent. If a routine produces an approval (e.g. a triaged support
reply), it shows up the same way any other approval would.

---

## Cron syntax

Standard 5-field cron: `minute hour day month dayofweek`

```
*/15 * * * *   → every 15 minutes
0 9 * * 1      → Monday at 9am
0 8 * * *      → every day at 8am
0 0 1 * *      → first of every month at midnight
```

Korpha also accepts a few human-friendly aliases:

```
@daily     → 0 0 * * *
@weekly    → 0 0 * * 0
@monthly   → 0 0 1 * *
@hourly    → 0 * * * *
```

Times are **local** (your machine's timezone). If you want UTC,
prefix with `UTC:` — e.g. `UTC:0 14 * * *` for 14:00 UTC daily.

---

## Adding / editing a routine

Routines live at `~/.korpha/routines.yaml`:

```yaml
routines:
  - id: cos_daily_digest
    cadence: "0 8 * * *"
    skill: cos.compile_digest
    args: {}
    enabled: true
    target_channels:           # where to deliver the result
      - dashboard
      - email
      # - telegram

  - id: my_custom_weekly
    cadence: "0 18 * * 5"      # Friday 6pm
    skill: analytics.weekly_review
    args:
      include_charts: true
    enabled: true
    target_channels: [email]
```

After editing, restart `korpha server` (or send `SIGHUP` to the
process) — the heartbeat loop re-reads the file. Or use the
dashboard `/app/routines` page which writes the file for you.

### Per-routine tier + provider override

Each routine can pin its LLM calls to a specific tier and/or provider
account that's different from your global tier mapping. Useful when:

- Most routines should run on your cheap workhorse, but the
  *weekly review* deserves Pro-tier reasoning
- Your nightly memory summarizer shouldn't burn ChatGPT subscription
  quota — pin it to a cheap pay-as-you-go account
- You want one routine running on local Ollama (offline) while the
  rest hit your paid provider

Two new optional fields on each routine:

```yaml
routines:
  - id: weekly_strategic_review
    cadence: "0 18 * * 5"          # Friday 6pm
    skill: analytics.weekly_review
    tier_override: pro              # ← force Pro tier even if the skill defaults to Workhorse
    provider_label: deepseek-direct # ← pin to this specific account label
    enabled: true

  - id: nightly_summarizer
    cadence: "0 2 * * *"
    skill: memory.summarize
    tier_override: workhorse
    provider_label: groq-cheap      # save subscription for chat
    enabled: true
```

Both fields are optional and default to `None`. When unset, the
routine routes through your global tier mapping like any other call.

If the `provider_label` doesn't match a healthy account, Korpha
**logs a warning and falls back to normal routing** — a stale label
in your YAML never takes down a scheduled job.

The override is propagated automatically: routine → wakeup → handler →
`CompletionRequest.pinned_account_label`. Built-in handlers honor it
out of the box. Custom handlers can read
`HandlerContext.override_tier()` and `HandlerContext.override_pinned_label()`
and pass them on to whatever LLM calls they make.

### Disabling a routine

Either set `enabled: false` in the YAML, or click the toggle on
`/app/routines`. Disabling doesn't delete — re-enable to resume.

### Deleting a routine

Remove the entry from `routines.yaml`. The heartbeat loop drops it
on next reload. Audit log entries from past runs persist (immutable).

---

## Custom routines (calling skills you wrote)

If you've added your own skill (Python or YAML — see [`SKILLS.md`](SKILLS.md)
for the routes), you can schedule it the same way:

```yaml
routines:
  - id: my_thing
    cadence: "@daily"
    skill: my_namespace.do_thing
    args:
      param_a: "value"
    enabled: true
    target_channels: [dashboard]
```

The skill needs to be registered in `default_registry` at startup
(automatic for both built-in Python skills and YAML skills under
`~/.korpha/skills/`).

---

## Multi-step routines (chained skills)

For "run skill A, then B with A's output", define a chain:

```yaml
routines:
  - id: weekly_growth_loop
    cadence: "0 9 * * 1"
    chain:
      - skill: analytics.weekly_review
        args: {}
        save_as: kpis
      - skill: growth.draft_content_plan
        args:
          audience: "indie devs"
          based_on: "{{ kpis.summary }}"
        save_as: plan
    target_channels: [dashboard, email]
```

`save_as` puts the skill's payload into the chain's local context
so later steps can reference it via `{{ <name>.<key> }}` template
syntax.

Use sparingly — long chains turn into multi-minute jobs. Two-three
steps is the sweet spot.

---

## Heartbeat internals (for the curious)

The heartbeat loop is a single async task that:

1. Loads `routines.yaml` on startup + on SIGHUP
2. Computes the next-fire time for every enabled routine
3. Sleeps until the earliest one
4. Wakes, fires that routine, writes activity log entry
5. Recomputes next-fire and repeats

Routines are **coalesced** — if you suspend the laptop and the
schedule passes, when the loop resumes it fires each missed routine
**at most once**, not N times. (Daily digest doesn't run 4× when
you wake up Monday after a long weekend.)

---

## Troubleshooting

**Routines don't fire**
→ Confirm `korpha server` is running. The heartbeat loop is part
of the server process; if the server's down, no routines fire.
Check `korpha status` — it shows the heartbeat loop's last
tick time.

**My routine fires but doesn't deliver to email**
→ Confirm Resend is configured (`RESEND_API_KEY` + verified domain).
Run `korpha email-test --to you@example.com` to validate the
channel works in isolation. If yes, check `target_channels: [email]`
is set on the routine YAML.

**Routine fires too often / not enough**
→ Cron syntax is unforgiving. Test your expression at
[crontab.guru](https://crontab.guru) before setting it.

**I disabled a routine but it still fires once more**
→ That's the run that was already in flight when you disabled.
Subsequent fires are suppressed.

---

## Reference

- Routine engine: [`korpha/heartbeats/`](../korpha/heartbeats/)
- YAML schema: [`korpha/heartbeats/config.py`](../korpha/heartbeats/config.py)
- Dashboard: `/app/routines`
- CLI: `korpha status` (shows heartbeat liveness)

For one-off work that should fire ONCE in the future (not on a
recurring schedule), use the `wakeup queue` instead — exposed via
the `cofounder.schedule_wakeup` API. Routines are for recurring
work.
