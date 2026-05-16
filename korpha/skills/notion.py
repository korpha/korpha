"""``notion.*`` skills — search, get/create/update pages, query databases.

Uses the Notion API directly via httpx (no SDK dep — Notion's REST API
is small enough that a third-party wrapper costs more than it saves,
and forces us onto someone else's release cadence).

Auth: an :class:`ExternalServiceAccount` with
``service=ExternalServiceKind.NOTION`` carrying ``{"api_key": "..."}``
in its encrypted credentials blob. The integration must be shared with
the target pages/databases in the Notion UI ("Connect to" → integration
name) — without that, the API returns 404 / 403 even for valid tokens.

Skills exposed:

  - ``notion.search``        — full-text search across the workspace
  - ``notion.get_page``      — fetch a page by id + its blocks
  - ``notion.create_page``   — create a page (in workspace or under
                                a parent page / database)
  - ``notion.update_page``   — update page properties
  - ``notion.append_blocks`` — append blocks to a page
  - ``notion.list_databases``— list databases the integration sees
  - ``notion.query_database``— filter / sort a database

Knowledge pack at ``korpha/knowledge_packs/productivity/notion/`` —
agents working on Notion automatically get the playbook in context.
"""
from __future__ import annotations

import json
import logging
from typing import Any
from uuid import UUID

import httpx

from korpha.credentials.model import (
    ExternalServiceAccount,
    ExternalServiceKind,
)
from korpha.credentials.resolver import resolve_credentials
from korpha.skills.registry import register
from korpha.skills.types import (
    Skill,
    SkillContext,
    SkillError,
    SkillProvenance,
    SkillResult,
    SkillSpec,
)

logger = logging.getLogger(__name__)


_API_BASE = "https://api.notion.com/v1"
_API_VERSION = "2025-09-03"
"""Pin the Notion-Version header. Notion ages out unsupported versions
slowly (years) but is strict about requiring the header; without it,
requests get a 400. Bump deliberately when consuming a new feature."""

_DEFAULT_TIMEOUT = 30.0


# ---------------------------------------------------------------- auth


def _resolve_token(ctx: SkillContext) -> str:
    """Pull the Notion integration token from the credentials vault.

    Raises :class:`SkillError` with an actionable blocker message
    (URL + steps + free-tier note) when no token is configured."""
    try:
        resolved = resolve_credentials(
            ctx.session,
            business_unit_id=getattr(ctx, "business_unit_id", None),
            business_id=ctx.business.id,
            service=ExternalServiceKind.NOTION,
        )
    except Exception as exc:  # noqa: BLE001
        raise SkillError(
            "Notion integration token not configured. "
            "Create one at https://www.notion.so/my-integrations "
            "(takes ~30 seconds, free) — name it 'AIgenteur' and copy "
            "the secret. Then add it via /app/credentials → Notion. "
            "After setup, share each page/database with the integration "
            "via 'Connect to' in the page menu, otherwise the API "
            "returns 404."
        ) from exc

    try:
        from korpha.secrets.crypto import (
            decrypt_bytes, load_master_key,
        )
        master = load_master_key()
        blob = decrypt_bytes(
            resolved.account.credentials_encrypted, master,
        )
        data = json.loads(blob.decode("utf-8"))
        token = data.get("api_key") or data.get("token")
    except Exception as exc:  # noqa: BLE001
        raise SkillError(
            f"Failed to decrypt Notion credentials: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    if not isinstance(token, str) or not token.strip():
        raise SkillError(
            "Notion credentials present but ``api_key`` field is empty. "
            "Re-add the token at /app/credentials → Notion."
        )
    return token.strip()


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": _API_VERSION,
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------- transport


async def _request(
    method: str,
    path: str,
    *,
    token: str,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One Notion API call. Returns parsed JSON or raises SkillError."""
    url = f"{_API_BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            resp = await client.request(
                method, url,
                headers=_headers(token),
                json=json_body,
                params=params,
            )
    except httpx.HTTPError as exc:
        raise SkillError(
            f"Notion transport error: {type(exc).__name__}: {exc}"
        ) from exc

    if resp.status_code >= 400:
        # Surface Notion's structured error body — usually has a useful
        # ``code`` + ``message`` that explains why (e.g. unauthorized
        # because the integration wasn't shared with the resource).
        try:
            err = resp.json()
            code = err.get("code", "unknown")
            message = err.get("message", resp.text[:200])
        except (json.JSONDecodeError, ValueError):
            code, message = "non_json", resp.text[:200]
        raise SkillError(
            f"Notion API {resp.status_code} ({code}): {message}"
        )
    try:
        return resp.json()
    except json.JSONDecodeError as exc:
        raise SkillError(
            f"Notion returned non-JSON: {resp.text[:200]}"
        ) from exc


# ---------------------------------------------------------------- skills


class NotionSearchSkill(Skill):
    """Full-text search across pages + databases the integration sees."""

    spec = SkillSpec(
        name="notion.search",
        description=(
            "Search Notion for pages or databases matching a query. "
            "Only finds resources the integration has been shared "
            "with. Returns a list of {id, title, url, last_edited}. "
            "Use this BEFORE creating a duplicate page."
        ),
        parameters={
            "query": (
                "Search term. Notion does substring + token matching "
                "on titles and body content. Empty string lists all "
                "accessible resources."
            ),
            "filter_type": (
                "Optional. 'page' or 'database' to narrow results. "
                "Default: both."
            ),
            "page_size": (
                "Optional, default 10, max 100."
            ),
        },
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        token = _resolve_token(ctx)
        body: dict[str, Any] = {
            "query": str(args.get("query") or "").strip(),
            "page_size": min(int(args.get("page_size") or 10), 100),
        }
        ft = str(args.get("filter_type") or "").strip().lower()
        if ft in ("page", "database"):
            body["filter"] = {"value": ft, "property": "object"}
        data = await _request("POST", "/search", token=token, json_body=body)
        results = []
        for r in data.get("results", []):
            results.append({
                "id": r.get("id"),
                "object": r.get("object"),
                "url": r.get("url"),
                "title": _extract_title(r),
                "last_edited": r.get("last_edited_time"),
            })
        return SkillResult(
            skill_name="notion.search",
            summary=f"Found {len(results)} Notion result(s)",
            payload={"results": results, "raw_count": len(data.get("results", []))},
        )


class NotionGetPageSkill(Skill):
    """Fetch a page's properties + block children in one call."""

    spec = SkillSpec(
        name="notion.get_page",
        description=(
            "Fetch a Notion page by id, including its first 100 "
            "child blocks. Returns the full property map + a flat "
            "list of block summaries (id, type, plain_text)."
        ),
        parameters={
            "page_id": "The Notion page UUID (with or without hyphens).",
        },
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        page_id = _clean_id(args.get("page_id"))
        if not page_id:
            raise SkillError("notion.get_page: page_id required")
        token = _resolve_token(ctx)
        page = await _request(
            "GET", f"/pages/{page_id}", token=token,
        )
        blocks_resp = await _request(
            "GET", f"/blocks/{page_id}/children",
            token=token, params={"page_size": 100},
        )
        block_summaries = [
            {
                "id": b.get("id"),
                "type": b.get("type"),
                "plain_text": _block_plain_text(b),
            }
            for b in blocks_resp.get("results", [])
        ]
        return SkillResult(
            skill_name="notion.get_page",
            summary=(
                f"Fetched page {page_id[:8]} "
                f"with {len(block_summaries)} block(s)"
            ),
            payload={
                "page": page,
                "blocks": block_summaries,
                "has_more_blocks": blocks_resp.get("has_more", False),
            },
        )


class NotionCreatePageSkill(Skill):
    """Create a new page (under a database or parent page)."""

    spec = SkillSpec(
        name="notion.create_page",
        description=(
            "Create a new Notion page. ``parent`` is either "
            "{'database_id': '...'} (the page becomes a row) or "
            "{'page_id': '...'} (the page becomes a child). "
            "``title`` is required. ``properties`` map matches the "
            "parent database's schema; ``children`` is a list of "
            "Notion blocks to seed the page body."
        ),
        parameters={
            "parent": (
                "Required. {'database_id': 'uuid'} or "
                "{'page_id': 'uuid'}."
            ),
            "title": "Page title (string).",
            "properties": (
                "Optional. Database-scoped property map (e.g. "
                "{'Status': {'select': {'name': 'Todo'}}}). "
                "Title is auto-filled from ``title`` arg."
            ),
            "children": (
                "Optional. List of Notion block dicts to populate "
                "the page body. See notion.append_blocks docs for "
                "block shapes (paragraph, heading_1, bullet_list_item, "
                "code, etc.)."
            ),
            "title_property": (
                "Optional. Name of the title property in the parent "
                "database. Defaults to 'Name' (Notion's default for "
                "new databases) but Notion databases can rename it."
            ),
        },
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        token = _resolve_token(ctx)
        parent = args.get("parent")
        if not isinstance(parent, dict):
            raise SkillError(
                "notion.create_page: parent={'database_id': ...} or "
                "{'page_id': ...} required"
            )
        title = str(args.get("title") or "").strip()
        if not title:
            raise SkillError("notion.create_page: title required")
        title_property = str(args.get("title_property") or "Name")

        properties = dict(args.get("properties") or {})
        properties[title_property] = {
            "title": [{"text": {"content": title}}],
        }
        body: dict[str, Any] = {
            "parent": parent,
            "properties": properties,
        }
        children = args.get("children")
        if isinstance(children, list) and children:
            body["children"] = children

        data = await _request("POST", "/pages", token=token, json_body=body)
        return SkillResult(
            skill_name="notion.create_page",
            summary=f"Created Notion page: {title}",
            payload={
                "id": data.get("id"),
                "url": data.get("url"),
                "created_time": data.get("created_time"),
            },
        )


class NotionUpdatePageSkill(Skill):
    """Update properties on an existing page."""

    spec = SkillSpec(
        name="notion.update_page",
        description=(
            "Update a Notion page's properties (NOT its block body — "
            "use notion.append_blocks for content). ``properties`` is "
            "the partial map of fields to change."
        ),
        parameters={
            "page_id": "The page UUID.",
            "properties": "Property partial map.",
            "archived": "Optional. True archives the page.",
        },
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        page_id = _clean_id(args.get("page_id"))
        if not page_id:
            raise SkillError("notion.update_page: page_id required")
        token = _resolve_token(ctx)
        body: dict[str, Any] = {}
        if isinstance(args.get("properties"), dict):
            body["properties"] = args["properties"]
        if "archived" in args:
            body["archived"] = bool(args["archived"])
        if not body:
            raise SkillError(
                "notion.update_page: pass at least one of properties "
                "or archived"
            )
        data = await _request(
            "PATCH", f"/pages/{page_id}", token=token, json_body=body,
        )
        return SkillResult(
            skill_name="notion.update_page",
            summary=f"Updated page {page_id[:8]}",
            payload={"id": data.get("id"), "url": data.get("url")},
        )


class NotionAppendBlocksSkill(Skill):
    """Append blocks to a page or block container."""

    spec = SkillSpec(
        name="notion.append_blocks",
        description=(
            "Append a list of Notion blocks to a page or block "
            "container. Each block is a dict like "
            "{'type': 'paragraph', 'paragraph': {'rich_text': "
            "[{'text': {'content': 'hello'}}]}}. See SKILL.md pack "
            "(productivity/notion) for block-type reference."
        ),
        parameters={
            "block_id": (
                "The container to append into — usually a page id "
                "(which acts as a block container)."
            ),
            "children": "List of block dicts.",
        },
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        block_id = _clean_id(args.get("block_id"))
        children = args.get("children")
        if not block_id:
            raise SkillError("notion.append_blocks: block_id required")
        if not isinstance(children, list) or not children:
            raise SkillError(
                "notion.append_blocks: children must be a non-empty list"
            )
        token = _resolve_token(ctx)
        data = await _request(
            "PATCH", f"/blocks/{block_id}/children",
            token=token, json_body={"children": children},
        )
        appended = data.get("results", [])
        return SkillResult(
            skill_name="notion.append_blocks",
            summary=f"Appended {len(appended)} block(s) to {block_id[:8]}",
            payload={"appended_count": len(appended)},
        )


class NotionListDatabasesSkill(Skill):
    """List databases the integration can see."""

    spec = SkillSpec(
        name="notion.list_databases",
        description=(
            "List databases the integration has access to. Returns "
            "a list of {id, title, url}. Use this to find database "
            "ids for notion.query_database / notion.create_page."
        ),
        parameters={
            "query": "Optional substring filter on database title.",
        },
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        token = _resolve_token(ctx)
        body: dict[str, Any] = {
            "filter": {"value": "database", "property": "object"},
            "page_size": 100,
        }
        q = str(args.get("query") or "").strip()
        if q:
            body["query"] = q
        data = await _request("POST", "/search", token=token, json_body=body)
        rows = [
            {
                "id": r.get("id"),
                "title": _extract_title(r),
                "url": r.get("url"),
            }
            for r in data.get("results", [])
        ]
        return SkillResult(
            skill_name="notion.list_databases",
            summary=f"Found {len(rows)} database(s)",
            payload={"databases": rows},
        )


class NotionQueryDatabaseSkill(Skill):
    """Query a database with optional filter + sort."""

    spec = SkillSpec(
        name="notion.query_database",
        description=(
            "Query a Notion database (data source). Returns "
            "matching pages with their properties. See "
            "https://developers.notion.com/reference/post-database-query "
            "for filter / sort shapes."
        ),
        parameters={
            "database_id": "The database UUID.",
            "filter": "Optional. Notion filter expression dict.",
            "sorts": "Optional. List of sort expressions.",
            "page_size": "Optional, default 25, max 100.",
        },
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        db_id = _clean_id(args.get("database_id"))
        if not db_id:
            raise SkillError(
                "notion.query_database: database_id required"
            )
        token = _resolve_token(ctx)
        body: dict[str, Any] = {
            "page_size": min(int(args.get("page_size") or 25), 100),
        }
        if isinstance(args.get("filter"), dict):
            body["filter"] = args["filter"]
        if isinstance(args.get("sorts"), list):
            body["sorts"] = args["sorts"]
        data = await _request(
            "POST", f"/databases/{db_id}/query",
            token=token, json_body=body,
        )
        results = data.get("results", [])
        return SkillResult(
            skill_name="notion.query_database",
            summary=f"Returned {len(results)} row(s) from database",
            payload={
                "rows": [
                    {
                        "id": r.get("id"),
                        "url": r.get("url"),
                        "properties": r.get("properties", {}),
                    }
                    for r in results
                ],
                "has_more": data.get("has_more", False),
            },
        )


# ---------------------------------------------------------------- helpers


def _clean_id(raw: Any) -> str | None:
    """Notion accepts ids with or without hyphens. Normalize to hyphen
    form (8-4-4-4-12) so URL building stays clean."""
    if not isinstance(raw, str):
        return None
    s = raw.strip().replace("-", "")
    if len(s) != 32:
        return raw.strip() if raw.strip() else None
    return f"{s[0:8]}-{s[8:12]}-{s[12:16]}-{s[16:20]}-{s[20:32]}"


def _extract_title(resource: dict[str, Any]) -> str:
    """Notion stores titles in two different places depending on the
    resource type (page vs database). Try both."""
    # Database has a top-level title array
    title_arr = resource.get("title")
    if isinstance(title_arr, list) and title_arr:
        first = title_arr[0]
        if isinstance(first, dict):
            return str(first.get("plain_text", "")).strip()
    # Page stores title under properties.<title-prop>.title[]
    props = resource.get("properties") or {}
    for prop in props.values():
        if not isinstance(prop, dict) or prop.get("type") != "title":
            continue
        title_list = prop.get("title") or []
        if title_list and isinstance(title_list[0], dict):
            return str(title_list[0].get("plain_text", "")).strip()
    return "(untitled)"


def _block_plain_text(block: dict[str, Any]) -> str:
    """Pull readable text out of any block type we know about."""
    btype = block.get("type")
    if not btype:
        return ""
    payload = block.get(btype) or {}
    rich = payload.get("rich_text") or []
    out: list[str] = []
    for r in rich:
        if isinstance(r, dict):
            t = r.get("plain_text") or r.get("text", {}).get("content", "")
            if t:
                out.append(t)
    return "".join(out)


def register_skills() -> None:
    register(NotionSearchSkill())
    register(NotionGetPageSkill())
    register(NotionCreatePageSkill())
    register(NotionUpdatePageSkill())
    register(NotionAppendBlocksSkill())
    register(NotionListDatabasesSkill())
    register(NotionQueryDatabaseSkill())


__all__ = [
    "NotionAppendBlocksSkill",
    "NotionCreatePageSkill",
    "NotionGetPageSkill",
    "NotionListDatabasesSkill",
    "NotionQueryDatabaseSkill",
    "NotionSearchSkill",
    "NotionUpdatePageSkill",
    "register_skills",
]
