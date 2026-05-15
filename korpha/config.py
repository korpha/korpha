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

    # ---- Context engine (CEO chat history shaping) ----
    # The original 20-message hardcoded cap was way too small for
    # 1M-context models. Hermes ships a token-budget compressor with
    # summary fallback; Korpha mirrors the same shape and exposes
    # the knobs here.
    context_engine: str = Field(
        default="compressor",
        description=(
            "Which context engine shapes CEO chat history before each "
            "LLM call. 'compressor' = Hermes-style head+tail+summary "
            "(default). 'passthrough' = no compaction (debugging)."
        ),
    )
    context_threshold_percent: float = Field(
        default=0.80,
        description=(
            "Compaction fires when estimated prompt tokens >= "
            "context_length * this. 0.80 leaves 20% headroom for "
            "the model's response and system overhead."
        ),
    )
    context_protect_first_n: int = Field(
        default=3,
        description=(
            "How many head messages always stay verbatim. The "
            "founder's original framing should fit in this window."
        ),
    )
    context_protect_last_n: int = Field(
        default=20,
        description=(
            "Minimum number of recent messages kept verbatim. Real "
            "tail size scales with the token budget; this is a floor."
        ),
    )
    context_summary_target_ratio: float = Field(
        default=0.20,
        description=(
            "Tail token budget as a fraction of threshold. 0.20 = "
            "protected tail can fill up to ~20% of threshold tokens."
        ),
    )
    context_summary_tokens_ceiling: int = Field(
        default=12_000,
        description="Hard cap on the summary's token budget.",
    )
    context_history_db_limit: int = Field(
        default=500,
        description=(
            "Max number of message rows to load from the DB per "
            "turn. Safety cap so a runaway conversation doesn't OOM "
            "the loader. Set high — engine handles real trimming."
        ),
    )

    # ---- Workforce auto-dispatch (kanban IN_PROGRESS → execution) ----
    # Without auto-dispatch, cards sit in IN_PROGRESS with no
    # executor running them. We have three triggers; pick one.
    workforce_auto_dispatch_mode: str = Field(
        default="inline",
        description=(
            "When to auto-dispatch IN_PROGRESS kanban cards through "
            "Workforce.dispatch. Options: 'inline' (default — "
            "kanban.fire_sprint triggers immediately after claim), "
            "'cron' (a ScriptCron preset polls every N min — "
            "install via `aigenteur cron add-card-dispatcher`), "
            "'hook' (POST_SKILL_CALL plugin hook fires after the "
            "skill), 'all' (all three; idempotency stamps prevent "
            "double-runs), 'off' (manual via /approvals/* only)."
        ),
    )
    workforce_auto_dispatch_stale_after_minutes: int = Field(
        default=30,
        description=(
            "A card already IN_PROGRESS for this many minutes "
            "without auto_dispatch stamp completion is treated as "
            "'still running' and skipped. Lower → faster retry on "
            "stuck cards. Higher → fewer duplicate runs."
        ),
    )
    workforce_auto_dispatch_max_cards: int = Field(
        default=12,
        description=(
            "Max cards dispatched per trigger fire. Prevents a "
            "huge bootstrap from spawning 50 parallel executors "
            "and busting the inference rate limit."
        ),
    )

    @model_validator(mode="after")
    def _derive_db_url(self) -> "Settings":
        if not self.db_url:
            self.db_url = f"sqlite:///{self.data_dir.resolve()}/korpha.db"
        return self


def get_settings() -> Settings:
    return Settings()
