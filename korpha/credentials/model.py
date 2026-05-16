"""ExternalServiceAccount — unified credential storage scoped to BusinessUnit.

Stores encrypted credentials for any external service the agents call.
Replaces the dataclass-only ``ProviderAccount`` long-term (PR4 adds
this alongside it; future PRs migrate LLM routing onto it too).

Credentials are encrypted via the existing secrets vault (#208) before
storage; the row never holds plaintext. Spending caps + rate limit
metadata + usage counters land on the row so the resolver can skip
exhausted accounts without a separate join.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlmodel import Field, SQLModel

from korpha.db._base import (
    json_column, primary_key_field, timestamp_field,
)


class ExternalServiceKind(StrEnum):
    """What service the credentials authenticate against."""

    # LLM / AI
    LLM_OPENAI_COMPAT = "llm_openai_compat"
    """Any OpenAI-compatible LLM API — covers the 17 env-fallback
    presets from #212 (DeepSeek, Groq, Together, OpenRouter, etc.)."""
    LLM_ANTHROPIC = "llm_anthropic"
    """Native Anthropic Messages API. Different from claude-code
    OAuth CLI (which is a SharedResource, not an account)."""
    LLM_GOOGLE = "llm_google"
    """Google AI Studio / Gemini API."""

    # Non-LLM AI services
    IMAGE_GEN = "image_gen"
    """Stable Diffusion API, Ideogram, etc. — non-LLM image gen
    that isn't on the shared GPU mesh."""
    TTS = "tts"
    STT = "stt"

    # Commerce + payments
    STRIPE = "stripe"
    PAYPAL = "paypal"
    LEMON_SQUEEZY = "lemon_squeezy"
    PADDLE = "paddle"
    JVZOO = "jvzoo"
    WARRIOR_PLUS = "warrior_plus"

    # Email / messaging
    RESEND = "resend"
    SENDGRID = "sendgrid"
    MAILGUN = "mailgun"
    POSTMARK = "postmark"

    # Publishing platforms
    KDP_API = "kdp_api"
    PRINTFUL = "printful"
    PRINTIFY = "printify"
    ETSY = "etsy"
    GUMROAD = "gumroad"
    TEACHABLE = "teachable"
    THINKIFIC = "thinkific"
    KAJABI = "kajabi"

    # ESP / list mgmt
    CONVERTKIT = "convertkit"
    BEEHIIV = "beehiiv"
    MAILERLITE = "mailerlite"
    GETRESPONSE = "getresponse"
    AWEBER = "aweber"

    # Infra
    CLOUDFLARE = "cloudflare"
    VERCEL = "vercel"
    FLY = "fly"
    RAILWAY = "railway"
    VPS_HOST = "vps_host"
    DOMAIN_REGISTRAR = "domain_registrar"
    SUPABASE = "supabase"
    NEON = "neon"

    # Productivity / SaaS tools
    NOTION = "notion"
    """Notion integration token (https://notion.so/my-integrations).
    Required for the ``notion.*`` skill family — search, get/create/
    update pages, query databases. Pages + databases must be
    explicitly shared with the integration in the Notion UI."""

    LINEAR = "linear"
    """Linear personal API key (Linear app → Settings → Account →
    Security → Personal API keys). Used by the ``linear.*`` skill
    family — issues, projects, teams."""

    GITHUB = "github"
    """GitHub personal access token (https://github.com/settings/
    tokens) or fine-grained token. Used by the ``github.*`` skill
    family — PRs, issues, repos, search, commits. Fine-grained tokens
    must include 'Contents:read' + 'Issues:write' + 'Pull requests:
    write' for the target repos."""

    # Custom — community-defined service kinds
    CUSTOM = "custom"


class ExternalServiceAccount(SQLModel, table=True):
    """One credentialed account for one service, scoped to a unit.

    Resolution: when an agent calls an external service, the resolver
    walks up the BusinessUnit tree (leaf → root) looking for an
    active uncapped account matching the requested service. If found,
    returns it. If not, falls through to the company-wide account
    (business_unit_id IS NULL). If still not found, raises.
    """

    __tablename__ = "external_service_account"

    id: UUID = primary_key_field()
    business_id: UUID = Field(foreign_key="business.id", index=True)
    business_unit_id: UUID | None = Field(
        default=None, foreign_key="business_unit.id", index=True,
        description=(
            "Null = company-wide default for this Business. "
            "Set = scoped to one unit (or its descendants, via the "
            "resolver's tree-walk fallback)."
        ),
    )

    service: ExternalServiceKind = Field(index=True)
    label: str = Field(
        description=(
            "Human-readable. Shown in /app/credentials. "
            "e.g. 'Korpha Stripe — main', 'Romance OpenAI key'."
        ),
    )

    # Encrypted JSON blob: {"api_key": "...", "secret": "...", ...}.
    # Decrypted at call time via the secrets vault (#208).
    credentials_encrypted: bytes = Field(
        description="Fernet-shaped AEAD-encrypted JSON credentials blob.",
    )

    # Provider-specific metadata (account ID, base URL override,
    # workspace name, etc.). Never holds secrets.
    provider_meta: dict[str, Any] = Field(
        default_factory=dict, sa_column=json_column(),
    )

    # Spending guardrails. Resolver skips accounts whose cap is hit.
    spending_cap_usd_per_month: Decimal | None = Field(
        default=None, max_digits=12, decimal_places=4,
    )
    spending_used_this_month_usd: Decimal = Field(
        default=Decimal("0"), max_digits=12, decimal_places=4,
    )
    spending_cap_resets_at: datetime | None = Field(default=None)

    # Rate limit hints (optional). Used by the inference layer to
    # back off before the upstream returns 429.
    rate_limit_meta: dict[str, Any] | None = Field(
        default=None, sa_column=json_column(),
    )

    is_active: bool = Field(default=True, index=True)
    last_used_at: datetime | None = Field(default=None)
    last_error_at: datetime | None = Field(default=None)
    last_error_message: str | None = Field(default=None)

    created_at: datetime = timestamp_field()
    updated_at: datetime = timestamp_field()


__all__ = [
    "ExternalServiceAccount",
    "ExternalServiceKind",
]
