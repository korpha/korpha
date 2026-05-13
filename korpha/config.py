"""Runtime configuration via environment variables.

Defaults assume a single-user, self-hosted install. Override anything
via ``KORPHA_*`` env vars or a ``.env`` file at cwd.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KORPHA_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    db_url: str | None = Field(
        default=None,
        description=(
            "SQLAlchemy URL. If unset, defaults to "
            "sqlite:///<data_dir>/korpha.db so running the CLI from any "
            "directory hits the same database. Set explicitly for Postgres."
        ),
    )
    data_dir: Path = Field(
        default=Path.home() / ".korpha",
        description="Persistent storage root for skills, secrets, attachments.",
    )
    log_level: str = Field(default="INFO")

    iteration_cap: int = Field(
        default=60,
        description="Max agent loop iterations per task (Hermes default).",
    )
    trust_envelope_default: int = Field(
        default=5,
        description="Consecutive approvals before Korpha offers auto-promotion.",
    )

    spend_cap_monthly_usd: float = Field(
        default=200.0,
        description="Default monthly spend cap. Hard stop above.",
    )

    display_currency: str = Field(
        default="USD",
        description=(
            "ISO 4217 currency code Mike sees in the UI (USD, EUR, GBP, "
            "CZK, JPY, …). Storage stays USD because LLM billing is USD. "
            "Display layer converts using usd_to_display_rate."
        ),
    )
    usd_to_display_rate: float = Field(
        default=1.0,
        description=(
            "How many display_currency units = 1 USD. e.g. EUR ~0.93, "
            "CZK ~23. Caps entered in the UI are converted back to USD "
            "for storage. Update occasionally; we don't fetch FX live."
        ),
    )

    @model_validator(mode="after")
    def _derive_db_url(self) -> "Settings":
        if not self.db_url:
            self.db_url = f"sqlite:///{self.data_dir.resolve()}/korpha.db"
        return self


def get_settings() -> Settings:
    return Settings()
