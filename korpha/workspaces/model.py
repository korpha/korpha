"""Repo: a code repository owned by a business.

The CTO uses these to delegate code work — clones at `local_path`, spawns
worktrees off the default branch for each task. Tracking which repos a
business has lets us bound autonomy ("CTO may push to repos X, Y but not Z")
and keep activity attributed to the right business.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlmodel import Field, SQLModel

from korpha.db._base import primary_key_field, timestamp_field


class Repo(SQLModel, table=True):
    __tablename__ = "repo"

    id: UUID = primary_key_field()
    business_id: UUID = Field(foreign_key="business.id", index=True)
    name: str = Field(index=True, description="Short slug, unique within a business.")
    source_url: str | None = Field(
        default=None,
        description="Git URL to clone from. Null means a locally-initialized repo.",
    )
    local_path: str = Field(description="Absolute path to the checkout root.")
    default_branch: str = Field(default="main")
    created_at: datetime = timestamp_field()
