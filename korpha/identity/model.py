"""Founder — the human user. One per install."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlmodel import Field, SQLModel

from korpha.db._base import json_column, primary_key_field, timestamp_field


class Founder(SQLModel, table=True):
    __tablename__ = "founder"

    id: UUID = primary_key_field()
    email: str = Field(index=True, unique=True)
    display_name: str | None = Field(default=None)
    timezone: str = Field(default="UTC")
    preferences: dict[str, Any] = Field(default_factory=dict, sa_column=json_column())
    active_business_id: UUID | None = Field(
        default=None,
        foreign_key="business.id",
        description=(
            "Which business CLI / API operations target by default. NULL means "
            "'use the only business that exists', which keeps single-business "
            "installs working without explicit switching."
        ),
    )
    created_at: datetime = timestamp_field()
    updated_at: datetime = timestamp_field()
