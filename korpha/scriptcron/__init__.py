"""Agentless script cron — schedule scripts that ping a channel
with their stdout. No LLM in the loop = $0 cost per tick.

Inspired by Hermes PR #19709 "feat(cron): no_agent mode for
script-only cron jobs (watchdog pattern)". The pitch from the PR
description: "Why not utilize the gateway and hermes' cron to
access things that don't need to cost an agent's time across any
messenger service you have connected? Just have your agent set
up cronjobs that need no agent in the loop, like running a
systems diagnostic script that reports info to you every 12
hours, pulls in an RSS feed, and send it over to you on telegram."

Korpha version specifics:
  - Persisted in the existing DB (SQLite/Postgres) via
    ``ScriptCron`` SQLModel
  - Delivery reuses commit #152's ``_PLATFORM_SENDERS``
    (email / telegram), so adding a new channel = one entry
  - Cadence is a simple "every Nm/h/d" string parsed into a
    timedelta — keeps the contract to what Mike will actually
    use, no full crontab parser yet
  - Empty stdout → silent tick (watchdog pattern: no news is
    good news, don't spam Mike's phone every 5 min)
  - Non-zero exit OR script timeout → ALERT: ❌ message gets
    pushed (broken watchdogs shouldn't fail silently)
"""
from korpha.scriptcron.model import ScriptCron, ScriptCronStatus
from korpha.scriptcron.runner import (
    DEFAULT_TIMEOUT_SECONDS,
    RunOutcome,
    parse_cadence,
    run_due_jobs,
    run_job,
)

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "RunOutcome",
    "ScriptCron",
    "ScriptCronStatus",
    "parse_cadence",
    "run_due_jobs",
    "run_job",
]
