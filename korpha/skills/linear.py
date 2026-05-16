"""``linear.*`` skills — issues, projects, teams via Linear's GraphQL API.

Linear has no REST API — everything is GraphQL at
``https://api.linear.app/graphql``. Auth = a personal API key from
Linear → Settings → Account → Security → Personal API keys.

We keep the GraphQL queries inline (small + readable) rather than
pulling a heavyweight GraphQL client. Each skill maps to one
operation Mike / agents commonly need:

  - ``linear.search_issues``    — find issues by text query
  - ``linear.create_issue``     — file a new issue in a team
  - ``linear.update_issue``     — change state / priority / assignee
  - ``linear.list_teams``       — discover team ids for create_issue
  - ``linear.list_projects``    — discover project ids
  - ``linear.get_issue``        — fetch one issue + comments
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from korpha.credentials.model import ExternalServiceKind
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


_API = "https://api.linear.app/graphql"
_TIMEOUT = 30.0


# ---------------------------------------------------------------- auth


def _resolve_token(ctx: SkillContext) -> str:
    """Decrypt the Linear API key from the credentials vault."""
    try:
        resolved = resolve_credentials(
            ctx.session,
            business_unit_id=getattr(ctx, "business_unit_id", None),
            business_id=ctx.business.id,
            service=ExternalServiceKind.LINEAR,
        )
    except Exception as exc:  # noqa: BLE001
        raise SkillError(
            "Linear API key not configured. Create one at "
            "Linear → Settings → Account → Security → Personal API "
            "keys (free, ~10 seconds). Then add at /app/credentials "
            "→ Linear. The token has full read+write on the workspace."
        ) from exc
    try:
        from korpha.secrets.crypto import decrypt_bytes, load_master_key
        master = load_master_key()
        blob = decrypt_bytes(
            resolved.account.credentials_encrypted, master,
        )
        data = json.loads(blob.decode("utf-8"))
        token = data.get("api_key") or data.get("token")
    except Exception as exc:  # noqa: BLE001
        raise SkillError(
            f"Failed to decrypt Linear credentials: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(token, str) or not token.strip():
        raise SkillError(
            "Linear credentials present but api_key field is empty."
        )
    return token.strip()


async def _gql(
    token: str,
    query: str,
    *,
    variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One GraphQL request. Raises SkillError on transport + GraphQL
    errors; the latter surface the actual ``errors[*].message`` so
    the agent can act (e.g. 'Issue not found' vs 'Authentication')."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                _API,
                headers={
                    # Linear's docs say to send the raw token (no
                    # 'Bearer'); they accept both but raw is the
                    # documented form.
                    "Authorization": token,
                    "Content-Type": "application/json",
                },
                json={"query": query, "variables": variables or {}},
            )
    except httpx.HTTPError as exc:
        raise SkillError(
            f"Linear transport: {type(exc).__name__}: {exc}"
        ) from exc
    if resp.status_code >= 400:
        raise SkillError(
            f"Linear HTTP {resp.status_code}: {resp.text[:200]}"
        )
    body = resp.json()
    if body.get("errors"):
        msgs = "; ".join(
            str(e.get("message", "?")) for e in body["errors"]
        )
        raise SkillError(f"Linear GraphQL: {msgs}")
    return body.get("data") or {}


# ---------------------------------------------------------------- skills


class LinearSearchIssuesSkill(Skill):
    """Full-text-style search across issues."""

    spec = SkillSpec(
        name="linear.search_issues",
        description=(
            "Search Linear issues by text. Returns up to 25 matches "
            "with id, identifier (PROJ-123), title, state, "
            "assignee, url. Use BEFORE creating a duplicate."
        ),
        parameters={
            "query": "Search term — matches title + description.",
            "limit": "Optional, default 25, max 50.",
        },
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        token = _resolve_token(ctx)
        q = str(args.get("query") or "").strip()
        if not q:
            raise SkillError("linear.search_issues: query required")
        limit = min(int(args.get("limit") or 25), 50)
        gql = """
        query Search($q: String!, $limit: Int!) {
          issueSearch(query: $q, first: $limit) {
            nodes {
              id identifier title url
              state { name type }
              assignee { name email }
              priority
              createdAt updatedAt
            }
          }
        }"""
        data = await _gql(token, gql, variables={"q": q, "limit": limit})
        nodes = (data.get("issueSearch") or {}).get("nodes") or []
        return SkillResult(
            skill_name="linear.search_issues",
            summary=f"Found {len(nodes)} Linear issue(s) for {q!r}",
            payload={"issues": nodes},
        )


class LinearGetIssueSkill(Skill):
    """Fetch one issue with full description + comments."""

    spec = SkillSpec(
        name="linear.get_issue",
        description=(
            "Fetch a Linear issue by identifier (e.g. 'PROJ-123') or "
            "by UUID. Returns title, description, state, priority, "
            "assignee, labels, and the first 20 comments."
        ),
        parameters={
            "identifier": (
                "Either 'PROJ-123' or the UUID. Identifier is the "
                "form Linear shows in URLs."
            ),
        },
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        token = _resolve_token(ctx)
        ident = str(args.get("identifier") or "").strip()
        if not ident:
            raise SkillError("linear.get_issue: identifier required")
        # Linear's `issue(id:)` accepts both the UUID and the
        # identifier — handy.
        gql = """
        query Issue($id: String!) {
          issue(id: $id) {
            id identifier title description url
            state { name type } priority
            assignee { name email }
            labels { nodes { name color } }
            createdAt updatedAt
            comments(first: 20) {
              nodes { id body user { name } createdAt }
            }
          }
        }"""
        data = await _gql(token, gql, variables={"id": ident})
        issue = data.get("issue")
        if issue is None:
            raise SkillError(f"linear.get_issue: {ident!r} not found")
        return SkillResult(
            skill_name="linear.get_issue",
            summary=f"Fetched {issue.get('identifier')}: {issue.get('title')}",
            payload={"issue": issue},
        )


class LinearCreateIssueSkill(Skill):
    """File a new issue."""

    spec = SkillSpec(
        name="linear.create_issue",
        description=(
            "Create a new Linear issue. Requires a team_id — call "
            "linear.list_teams first if you don't have one. Returns "
            "the created issue's id + identifier + URL."
        ),
        parameters={
            "team_id": (
                "Linear team UUID. Find via linear.list_teams."
            ),
            "title": "Issue title (required).",
            "description": (
                "Optional. Markdown supported in Linear."
            ),
            "priority": (
                "Optional. 0=No priority, 1=Urgent, 2=High, 3=Medium, "
                "4=Low. Default 0."
            ),
            "assignee_id": "Optional. User UUID.",
            "project_id": (
                "Optional. Project UUID (linear.list_projects)."
            ),
            "label_ids": (
                "Optional. List of label UUIDs."
            ),
        },
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        token = _resolve_token(ctx)
        team_id = str(args.get("team_id") or "").strip()
        title = str(args.get("title") or "").strip()
        if not team_id:
            raise SkillError("linear.create_issue: team_id required")
        if not title:
            raise SkillError("linear.create_issue: title required")
        input_obj: dict[str, Any] = {
            "teamId": team_id,
            "title": title,
        }
        if args.get("description"):
            input_obj["description"] = str(args["description"])
        if args.get("priority") is not None:
            input_obj["priority"] = int(args["priority"])
        if args.get("assignee_id"):
            input_obj["assigneeId"] = str(args["assignee_id"])
        if args.get("project_id"):
            input_obj["projectId"] = str(args["project_id"])
        labels = args.get("label_ids")
        if isinstance(labels, list) and labels:
            input_obj["labelIds"] = [str(l) for l in labels]

        gql = """
        mutation Create($input: IssueCreateInput!) {
          issueCreate(input: $input) {
            success
            issue { id identifier title url }
          }
        }"""
        data = await _gql(token, gql, variables={"input": input_obj})
        result = data.get("issueCreate") or {}
        if not result.get("success"):
            raise SkillError("Linear refused issue create")
        issue = result.get("issue") or {}
        return SkillResult(
            skill_name="linear.create_issue",
            summary=f"Created {issue.get('identifier')}: {title}",
            payload={"issue": issue},
        )


class LinearUpdateIssueSkill(Skill):
    """Change state / priority / assignee on an issue."""

    spec = SkillSpec(
        name="linear.update_issue",
        description=(
            "Update an existing Linear issue's mutable fields. Pass "
            "only the fields you want to change."
        ),
        parameters={
            "issue_id": "UUID or 'PROJ-123' identifier.",
            "state_id": "Optional. WorkflowState UUID for new state.",
            "priority": "Optional. 0-4.",
            "assignee_id": "Optional.",
            "title": "Optional. Rename the issue.",
            "description": "Optional. Replace description.",
        },
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        token = _resolve_token(ctx)
        issue_id = str(args.get("issue_id") or "").strip()
        if not issue_id:
            raise SkillError("linear.update_issue: issue_id required")
        update: dict[str, Any] = {}
        if args.get("state_id"):
            update["stateId"] = str(args["state_id"])
        if args.get("priority") is not None:
            update["priority"] = int(args["priority"])
        if args.get("assignee_id"):
            update["assigneeId"] = str(args["assignee_id"])
        if args.get("title"):
            update["title"] = str(args["title"])
        if args.get("description") is not None:
            update["description"] = str(args["description"])
        if not update:
            raise SkillError(
                "linear.update_issue: pass at least one field to change"
            )
        gql = """
        mutation Update($id: String!, $input: IssueUpdateInput!) {
          issueUpdate(id: $id, input: $input) {
            success
            issue { id identifier title url state { name } }
          }
        }"""
        data = await _gql(
            token, gql,
            variables={"id": issue_id, "input": update},
        )
        result = data.get("issueUpdate") or {}
        if not result.get("success"):
            raise SkillError("Linear refused issue update")
        issue = result.get("issue") or {}
        return SkillResult(
            skill_name="linear.update_issue",
            summary=f"Updated {issue.get('identifier')}",
            payload={"issue": issue},
        )


class LinearListTeamsSkill(Skill):
    """Discover team ids — required input for create_issue."""

    spec = SkillSpec(
        name="linear.list_teams",
        description=(
            "List all teams the API key can see. Returns id, key "
            "(e.g. 'PROJ'), name. Use to grab a team_id for "
            "create_issue."
        ),
        parameters={},
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        token = _resolve_token(ctx)
        gql = """
        query Teams {
          teams(first: 100) {
            nodes { id key name description }
          }
        }"""
        data = await _gql(token, gql)
        teams = (data.get("teams") or {}).get("nodes") or []
        return SkillResult(
            skill_name="linear.list_teams",
            summary=f"{len(teams)} team(s) accessible",
            payload={"teams": teams},
        )


class LinearListProjectsSkill(Skill):
    """List projects across teams."""

    spec = SkillSpec(
        name="linear.list_projects",
        description=(
            "List projects (optionally filtered to one team). "
            "Returns id, name, state, target_date. Use to grab a "
            "project_id for create_issue."
        ),
        parameters={
            "team_id": (
                "Optional. UUID. Filter to projects owned by this "
                "team."
            ),
        },
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        token = _resolve_token(ctx)
        team_id = args.get("team_id")
        if team_id:
            gql = """
            query TeamProjects($team: String!) {
              team(id: $team) {
                projects(first: 100) {
                  nodes { id name state targetDate url }
                }
              }
            }"""
            data = await _gql(token, gql, variables={"team": str(team_id)})
            nodes = (
                ((data.get("team") or {}).get("projects") or {}).get("nodes")
                or []
            )
        else:
            gql = """
            query Projects {
              projects(first: 100) {
                nodes { id name state targetDate url }
              }
            }"""
            data = await _gql(token, gql)
            nodes = (data.get("projects") or {}).get("nodes") or []
        return SkillResult(
            skill_name="linear.list_projects",
            summary=f"{len(nodes)} project(s)",
            payload={"projects": nodes},
        )


def register_skills() -> None:
    register(LinearSearchIssuesSkill())
    register(LinearGetIssueSkill())
    register(LinearCreateIssueSkill())
    register(LinearUpdateIssueSkill())
    register(LinearListTeamsSkill())
    register(LinearListProjectsSkill())


__all__ = [
    "LinearCreateIssueSkill",
    "LinearGetIssueSkill",
    "LinearListProjectsSkill",
    "LinearListTeamsSkill",
    "LinearSearchIssuesSkill",
    "LinearUpdateIssueSkill",
    "register_skills",
]
