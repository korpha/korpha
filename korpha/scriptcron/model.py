"""SQLModel for agentless script cron jobs."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlmodel import Field, SQLModel

from korpha.db._base import primary_key_field, timestamp_field


class ScriptCronStatus(StrEnum):
    """Last-tick outcome — surfaced in CLI / dashboard listings."""

    NEVER_RUN = "never_run"
    OK = "ok"
    """Script exited 0 with output → delivered."""

    SILENT = "silent"
    """Script exited 0 with no stdout → tick succeeded, no message
    sent (watchdog pattern: no news is good news)."""

    FAILED = "failed"
    """Non-zero exit / timeout / interpreter missing. Error
    notification was pushed if delivery is configured."""


class ScriptCron(SQLModel, table=True):
    """One scheduled script. Per-business so Mike's two SaaS
    cofounder instances each have their own cron tables (multi-
    tenant)."""

    __tablename__ = "script_cron"

    id: UUID = primary_key_field()
    business_id: UUID = Field(foreign_key="business.id", index=True)
    # PR-INT-27: optional per-unit scope. When set, the cron run gets
    # SkillContext.business_unit_id wired so any memory.* / cooperation.*
    # / image.* calls during the run auto-scope to the unit's namespace.
    # None = company-wide cron (back-compat for all existing rows).
    business_unit_id: UUID | None = Field(
        default=None, foreign_key="business_unit.id", index=True,
    )

    name: str = Field(
        index=True,
        description="Short slug. Used in CLI listings + log lines.",
    )
    script_path: str = Field(
        description=(
            "Absolute path to the script. Interpreter picked from "
            "extension: .sh/.bash → /bin/bash, .py → sys.executable, "
            "everything else → exec directly (operator's responsibility "
            "to make it executable)."
        ),
    )
    cadence: str = Field(
        description=(
            "How often. Format: 'every Nm' / 'every Nh' / 'every Nd'. "
            "5m = run every 5 minutes."
        ),
    )

    deliver_platform: str | None = Field(
        default=None,
        description=(
            "Channel to push stdout to ('email' / 'telegram'). "
            "None = log to Activity only, no push."
        ),
    )
    deliver_recipient: str | None = Field(
        default=None,
        description=(
            "Who gets the push. Email address for email; chat_id "
            "for telegram. Required when deliver_platform is set."
        ),
    )

    enabled: bool = Field(default=True, index=True)
    last_run_at: datetime | None = Field(
        default=None, index=True,
        description="Last tick time (success or failure).",
    )
    last_status: ScriptCronStatus = Field(
        default=ScriptCronStatus.NEVER_RUN,
    )
    last_output: str = Field(
        default="",
        description=(
            "Last script output, capped at 4KB. Surfaced in the "
            "dashboard so the founder can see what was last sent."
        ),
    )
    last_error: str = Field(default="")
    last_summary: str = Field(
        default="",
        max_length=400,
        description=(
            "Bounded one-liner digest of the last run — first "
            "non-empty line of stdout (or stderr on failure), "
            "truncated to 200 chars, plus the duration/exit code "
            "if available. Renders inline in /app/cron so Mike "
            "can scan recent activity without expanding every job."
        ),
    )

    created_at: datetime = timestamp_field(index=True)
    updated_at: datetime = timestamp_field()
