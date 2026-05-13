"""SharedResource + SharedResourceUsage tables.

A SharedResource is company-wide infrastructure (the AI model mesh,
OAuth CLI sessions, shared accounts) that any unit can consume.
Usage is logged per-(resource, consumer_unit) for attribution in
monthly review.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlmodel import Field, SQLModel

from korpha.db._base import (
    json_column, primary_key_field, timestamp_field,
)


class SharedResourceKind(StrEnum):
    """What flavor of shared infrastructure this row represents."""

    AI_MODEL = "ai_model"
    """Hosted model on the GPU mesh — z-image-turbo, Whisper, Kokoro,
    OmniVoice, bg-removal, etc. Mesh details (endpoint, auth) live in
    ``config``."""

    COMPUTE = "compute"
    """Generic compute pool — GPU cluster, container hosts, batch
    workers. For when an agent needs to run something heavy."""

    DOMAIN_POOL = "domain_pool"
    """Pool of pre-registered domains the founder can deploy to (e.g.
    for subdomain landing pages)."""

    HOST_POOL = "host_pool"
    """Shared VPS / hosting capacity."""

    SHARED_ACCOUNT = "shared_account"
    """A shared 3rd-party account (e.g. one Cloudflare account
    covering multiple lines). Specific service kind goes in
    ``config["service"]`` so the same row schema covers all
    shared accounts."""

    OAUTH_CLI = "oauth_cli"
    """OAuth-authorized CLI session. One per machine by physical
    constraint — Claude Code, Codex CLI, OpenCode, Cursor, Gemini
    CLI, ACPX, PI. Available in local install only; SaaS mode
    excludes these at enumeration time."""

    BROWSER = "browser"
    """Headless browser pool (Playwright / Chromium). Concurrency-
    gated because spinning up multiple Chromiums on a laptop is
    RAM-expensive. Default concurrency_limit=1 keeps Mike's machine
    responsive; agencies on bigger boxes can bump it from /app/units
    or `korpha browser set-concurrency`. ``config['max_concurrent']``
    is the semaphore count."""


class SharedResource(SQLModel, table=True):
    """One row per shared infrastructure asset."""

    __tablename__ = "shared_resource"

    id: UUID = primary_key_field()
    business_id: UUID = Field(foreign_key="business.id", index=True)

    kind: SharedResourceKind = Field(index=True)
    name: str = Field(
        index=True,
        description=(
            "Machine identifier — 'z-image-turbo', 'claude-code', "
            "'kokoro-tts'. Lower-snake or kebab."
        ),
    )
    label: str = Field(
        description=(
            "Human-readable. Shown in /app/units shared-resources "
            "panel and in monthly review attribution."
        ),
    )

    # Who built / hosts this. Nullable — some resources are rented
    # services without a "host" line (e.g. shared Cloudflare account).
    host_business_unit_id: UUID | None = Field(
        default=None, foreign_key="business_unit.id", index=True,
    )

    # Access details. For AI models: endpoint URL + auth config.
    # For OAuth CLIs: binary name + login state path. Schemaless so
    # plugins can ship whatever shape they need.
    endpoint: str | None = Field(default=None)
    config: dict[str, Any] = Field(
        default_factory=dict, sa_column=json_column(),
    )

    # How usage is attributed across consumers.
    # v1 default: track but don't charge back.
    cost_model: str = Field(default="tracked_not_charged")
    """One of: 'tracked_not_charged' | 'per_call' | 'metered'
    | 'fixed_monthly_with_credits' | 'subscription_quota'."""

    fixed_monthly_cost_usd: float | None = Field(default=None)

    # Deployment-mode gating. OAuth CLI resources set this to ["local"]
    # because SaaS deployments physically cannot share OAuth tokens.
    # Default ["local", "saas"] = available in both modes.
    available_in_modes: list[str] = Field(
        default_factory=lambda: ["local", "saas"],
        sa_column=json_column(),
    )

    # Subscription quota tracking — for OAuth CLI resources where the
    # constraint is a rolling window (Claude.ai 5h cap, ChatGPT Plus
    # message limit). None for unmetered/local resources.
    quota_window_seconds: int | None = Field(default=None)
    quota_limit_in_window: int | None = Field(default=None)
    quota_calls_in_window: int = Field(default=0)
    quota_window_started_at: datetime | None = Field(default=None)

    is_active: bool = Field(default=True, index=True)
    last_used_at: datetime | None = Field(default=None)
    created_at: datetime = timestamp_field()
    updated_at: datetime = timestamp_field()


class SharedResourceUsage(SQLModel, table=True):
    """Append-only usage log per (resource, consumer-unit) pair.

    Drives monthly-review attribution + cap enforcement. Never
    deleted; archived after 90 days via the existing
    ``korpha.audit.retention`` mechanism (PR followup).
    """

    __tablename__ = "shared_resource_usage"

    id: UUID = primary_key_field()
    resource_id: UUID = Field(
        foreign_key="shared_resource.id", index=True,
    )
    consumer_unit_id: UUID = Field(
        foreign_key="business_unit.id", index=True,
    )
    used_at: datetime = timestamp_field(index=True)
    units_consumed: float = Field(default=1.0)
    """How much capacity this call used — calls/tokens/seconds/etc.
    Resource-kind specific; the call site decides the meaning."""
    cost_attributed_usd: float = Field(default=0.0)
    """0 in v1 (tracked-not-charged). When chargeback enables, this
    is what the consumer's monthly P&L gets debited."""
    skill_name: str | None = Field(default=None, index=True)
    notes: str | None = Field(default=None)


__all__ = [
    "SharedResource",
    "SharedResourceKind",
    "SharedResourceUsage",
]
