"""Publish a local skill spec to the AIgenteur hub.

Uses the cached hub session (from ``aigenteur skill hub-login``) to
POST a SubmitSkillBody to ``/api/v1/skills``. Returns the resulting
hub URL on success.

The mapping from a local ``SkillSpec`` (which only carries name +
description + parameters) to the richer hub fields (display_name,
tags, long_description, license) is done by passing extra metadata
explicitly — the caller (CLI or dashboard) supplies it. We don't
auto-derive tags from the dotted name because tags are how users
discover skills + we want them curated, not noisy.
"""
from __future__ import annotations

from typing import Any

import httpx

from korpha.skills_hub.hub_auth import HubSession


class HubPublishError(RuntimeError):
    """Publish failed — caller catches + surfaces to UI/CLI."""


def publish_skill(
    session: HubSession,
    *,
    name: str,
    display_name: str,
    description: str,
    long_description: str = "",
    license: str = "MIT",  # noqa: A002
    tags: list[str] | None = None,
    cofounder_protocol: bool = False,
    upstream_repo: str | None = None,
    upstream_path: str | None = None,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    """POST a skill submission. Raises HubPublishError on any non-201.

    Returns the parsed JSON response (SkillSummary shape) on success.
    The hub URL for the new skill is ``{base_url}/skills/{name}``.
    """
    body = {
        "name": name,
        "display_name": display_name,
        "description": description,
        "long_description": long_description,
        "license": license,
        "tags": tags or [],
        "cofounder_protocol": cofounder_protocol,
        "upstream_repo": upstream_repo,
        "upstream_path": upstream_path,
    }
    url = f"{session.base_url}/api/v1/skills"
    try:
        r = httpx.post(
            url,
            json=body,
            cookies=session.cookies(),
            timeout=timeout_seconds,
        )
    except httpx.HTTPError as exc:
        raise HubPublishError(f"network error: {exc}") from exc

    if r.status_code == 201:
        return r.json()
    if r.status_code == 401:
        raise HubPublishError(
            "hub session expired or invalid — re-run `aigenteur skill "
            "hub-login`."
        )
    if r.status_code == 403:
        raise HubPublishError(
            "email not verified on the hub yet. Click the link in the "
            "magic-link email first, then publish again."
        )
    if r.status_code == 409:
        raise HubPublishError(
            f"skill name {name!r} already exists on the hub. To "
            "republish, contact the maintainer (no in-app update flow "
            "yet)."
        )
    if r.status_code == 429:
        raise HubPublishError(
            "daily publish quota hit on the hub (5/day default). Try "
            "again tomorrow."
        )
    raise HubPublishError(f"hub returned {r.status_code}: {r.text[:200]}")


def hub_url_for(session: HubSession, name: str) -> str:
    return f"{session.base_url}/skills/{name}"


__all__ = ["HubPublishError", "hub_url_for", "publish_skill"]
