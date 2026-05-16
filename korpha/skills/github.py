"""``github.*`` skills — PRs, issues, repos, search via the REST API.

Hits ``api.github.com`` directly with a personal access token (PAT)
or fine-grained token from
https://github.com/settings/tokens. Fine-grained tokens need:
  - Contents: Read (for get_repo + list_commits)
  - Issues: Read + Write (for issue ops)
  - Pull requests: Read + Write (for PR ops)
  - Metadata: Read (always required)

Why REST not GraphQL? GitHub's REST API is exhaustively documented +
stable, and the operations these skills cover (CRUD on issues + PRs)
have direct REST endpoints. GraphQL wins for nested queries; we
don't need that here.
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


_API = "https://api.github.com"
_TIMEOUT = 30.0
_API_VERSION = "2022-11-28"


# ---------------------------------------------------------------- auth


def _resolve_token(ctx: SkillContext) -> str:
    try:
        resolved = resolve_credentials(
            ctx.session,
            business_unit_id=getattr(ctx, "business_unit_id", None),
            business_id=ctx.business.id,
            service=ExternalServiceKind.GITHUB,
        )
    except Exception as exc:  # noqa: BLE001
        raise SkillError(
            "GitHub token not configured. Create a personal access "
            "token at https://github.com/settings/tokens (free; pick "
            "'Tokens (classic)' for repo+workflow scope, or "
            "'Fine-grained tokens' for per-repo scoping with "
            "Contents:read + Issues:write + Pull requests:write). "
            "Then add at /app/credentials → GitHub."
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
            f"Failed to decrypt GitHub credentials: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(token, str) or not token.strip():
        raise SkillError(
            "GitHub credentials present but token field is empty."
        )
    return token.strip()


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": _API_VERSION,
        # GitHub recommends a UA so they can track usage; default
        # python-httpx UA is fine but we identify ourselves.
        "User-Agent": "korpha-agent",
    }


async def _request(
    method: str,
    path: str,
    *,
    token: str,
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> Any:
    url = f"{_API}{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.request(
                method, url,
                headers=_headers(token),
                json=json_body, params=params,
            )
    except httpx.HTTPError as exc:
        raise SkillError(
            f"GitHub transport: {type(exc).__name__}: {exc}"
        ) from exc
    if resp.status_code == 204:
        return None  # delete success returns no body
    if resp.status_code >= 400:
        # GitHub error body usually has {message, documentation_url}
        try:
            err = resp.json()
            msg = err.get("message", resp.text[:200])
            doc = err.get("documentation_url", "")
        except (json.JSONDecodeError, ValueError):
            msg, doc = resp.text[:200], ""
        suffix = f" (see {doc})" if doc else ""
        raise SkillError(
            f"GitHub API {resp.status_code}: {msg}{suffix}"
        )
    try:
        return resp.json()
    except json.JSONDecodeError as exc:
        raise SkillError(
            f"GitHub non-JSON: {resp.text[:200]}"
        ) from exc


def _split_repo(repo: str) -> tuple[str, str]:
    """Parse 'owner/repo' — raise SkillError if shape is wrong."""
    if not isinstance(repo, str) or "/" not in repo:
        raise SkillError(
            f"GitHub: repo must be 'owner/repo' form, got {repo!r}"
        )
    owner, name = repo.split("/", 1)
    return owner.strip(), name.strip()


# ---------------------------------------------------------------- skills


class GitHubGetRepoSkill(Skill):
    """Fetch repo metadata."""

    spec = SkillSpec(
        name="github.get_repo",
        description=(
            "Fetch GitHub repo metadata: stars, forks, primary "
            "language, license, default branch, topics, archived "
            "state. Use to confirm a repo exists + assess activity "
            "before integrating."
        ),
        parameters={"repo": "'owner/repo' form."},
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        owner, name = _split_repo(str(args.get("repo") or ""))
        token = _resolve_token(ctx)
        data = await _request(
            "GET", f"/repos/{owner}/{name}", token=token,
        )
        return SkillResult(
            skill_name="github.get_repo",
            summary=(
                f"{owner}/{name} — {data.get('stargazers_count', 0)} stars, "
                f"{data.get('language') or 'no primary lang'}"
            ),
            payload={
                "name": data.get("full_name"),
                "description": data.get("description"),
                "stars": data.get("stargazers_count"),
                "forks": data.get("forks_count"),
                "watchers": data.get("subscribers_count"),
                "open_issues": data.get("open_issues_count"),
                "language": data.get("language"),
                "license": (data.get("license") or {}).get("spdx_id"),
                "default_branch": data.get("default_branch"),
                "topics": data.get("topics", []),
                "archived": data.get("archived"),
                "pushed_at": data.get("pushed_at"),
                "html_url": data.get("html_url"),
            },
        )


class GitHubListPRsSkill(Skill):
    """List PRs on a repo."""

    spec = SkillSpec(
        name="github.list_prs",
        description=(
            "List pull requests on a repo. Filter by state "
            "(open/closed/all) and optional head/base branch."
        ),
        parameters={
            "repo": "'owner/repo'.",
            "state": (
                "Optional. 'open' (default), 'closed', or 'all'."
            ),
            "limit": "Optional, default 30, max 100.",
            "base": "Optional. Filter to PRs targeting this branch.",
        },
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        owner, name = _split_repo(str(args.get("repo") or ""))
        token = _resolve_token(ctx)
        state = str(args.get("state") or "open").lower()
        if state not in ("open", "closed", "all"):
            raise SkillError("state must be open/closed/all")
        params: dict[str, Any] = {
            "state": state,
            "per_page": min(int(args.get("limit") or 30), 100),
        }
        if args.get("base"):
            params["base"] = str(args["base"])
        data = await _request(
            "GET", f"/repos/{owner}/{name}/pulls",
            token=token, params=params,
        )
        prs = [
            {
                "number": p.get("number"),
                "title": p.get("title"),
                "state": p.get("state"),
                "user": (p.get("user") or {}).get("login"),
                "head": (p.get("head") or {}).get("ref"),
                "base": (p.get("base") or {}).get("ref"),
                "draft": p.get("draft"),
                "url": p.get("html_url"),
                "created_at": p.get("created_at"),
                "updated_at": p.get("updated_at"),
            }
            for p in (data or [])
        ]
        return SkillResult(
            skill_name="github.list_prs",
            summary=f"{len(prs)} {state} PR(s) in {owner}/{name}",
            payload={"prs": prs},
        )


class GitHubCreatePRSkill(Skill):
    """Open a new pull request."""

    spec = SkillSpec(
        name="github.create_pr",
        description=(
            "Open a new pull request. Branch must already exist on "
            "the remote (use github.list_commits to verify). Returns "
            "the new PR's number + URL."
        ),
        parameters={
            "repo": "'owner/repo'.",
            "title": "PR title.",
            "head": (
                "Branch with your changes. For PRs from forks use "
                "'fork_owner:branch'."
            ),
            "base": (
                "Branch you're merging into (usually 'main')."
            ),
            "body": "Optional. PR description (markdown).",
            "draft": "Optional. True opens as draft.",
        },
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        owner, name = _split_repo(str(args.get("repo") or ""))
        token = _resolve_token(ctx)
        title = str(args.get("title") or "").strip()
        head = str(args.get("head") or "").strip()
        base = str(args.get("base") or "").strip()
        if not (title and head and base):
            raise SkillError(
                "github.create_pr: title + head + base all required"
            )
        body: dict[str, Any] = {
            "title": title, "head": head, "base": base,
        }
        if args.get("body"):
            body["body"] = str(args["body"])
        if args.get("draft"):
            body["draft"] = bool(args["draft"])
        data = await _request(
            "POST", f"/repos/{owner}/{name}/pulls",
            token=token, json_body=body,
        )
        return SkillResult(
            skill_name="github.create_pr",
            summary=f"Opened #{data.get('number')}: {title}",
            payload={
                "number": data.get("number"),
                "url": data.get("html_url"),
                "state": data.get("state"),
            },
        )


class GitHubListIssuesSkill(Skill):
    """List issues on a repo (excluding PRs)."""

    spec = SkillSpec(
        name="github.list_issues",
        description=(
            "List issues on a repo. NOTE: GitHub's /issues endpoint "
            "returns PRs too; this skill filters them out. Filter by "
            "state and optional labels."
        ),
        parameters={
            "repo": "'owner/repo'.",
            "state": "Optional. open / closed / all.",
            "labels": (
                "Optional. List of label strings — all must match."
            ),
            "limit": "Optional, default 30, max 100.",
        },
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        owner, name = _split_repo(str(args.get("repo") or ""))
        token = _resolve_token(ctx)
        state = str(args.get("state") or "open").lower()
        if state not in ("open", "closed", "all"):
            raise SkillError("state must be open/closed/all")
        params: dict[str, Any] = {
            "state": state,
            "per_page": min(int(args.get("limit") or 30), 100),
        }
        labels = args.get("labels")
        if isinstance(labels, list) and labels:
            params["labels"] = ",".join(str(l) for l in labels)
        data = await _request(
            "GET", f"/repos/{owner}/{name}/issues",
            token=token, params=params,
        )
        # Strip PRs (they have a pull_request field).
        issues = [
            {
                "number": i.get("number"),
                "title": i.get("title"),
                "state": i.get("state"),
                "user": (i.get("user") or {}).get("login"),
                "labels": [
                    (l.get("name") if isinstance(l, dict) else l)
                    for l in (i.get("labels") or [])
                ],
                "comments": i.get("comments"),
                "url": i.get("html_url"),
                "created_at": i.get("created_at"),
                "updated_at": i.get("updated_at"),
            }
            for i in (data or [])
            if "pull_request" not in i
        ]
        return SkillResult(
            skill_name="github.list_issues",
            summary=f"{len(issues)} {state} issue(s) in {owner}/{name}",
            payload={"issues": issues},
        )


class GitHubCreateIssueSkill(Skill):
    """Create a new issue."""

    spec = SkillSpec(
        name="github.create_issue",
        description=(
            "File a new GitHub issue. Returns issue number + URL."
        ),
        parameters={
            "repo": "'owner/repo'.",
            "title": "Issue title.",
            "body": "Optional. Markdown description.",
            "labels": "Optional. List of label names (must exist).",
            "assignees": "Optional. List of GitHub usernames.",
        },
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        owner, name = _split_repo(str(args.get("repo") or ""))
        token = _resolve_token(ctx)
        title = str(args.get("title") or "").strip()
        if not title:
            raise SkillError("github.create_issue: title required")
        body: dict[str, Any] = {"title": title}
        if args.get("body"):
            body["body"] = str(args["body"])
        if isinstance(args.get("labels"), list):
            body["labels"] = [str(l) for l in args["labels"]]
        if isinstance(args.get("assignees"), list):
            body["assignees"] = [str(a) for a in args["assignees"]]
        data = await _request(
            "POST", f"/repos/{owner}/{name}/issues",
            token=token, json_body=body,
        )
        return SkillResult(
            skill_name="github.create_issue",
            summary=f"Filed #{data.get('number')}: {title}",
            payload={
                "number": data.get("number"),
                "url": data.get("html_url"),
            },
        )


class GitHubListCommitsSkill(Skill):
    """List recent commits on a repo."""

    spec = SkillSpec(
        name="github.list_commits",
        description=(
            "List commits on a repo (or a specific branch). Returns "
            "sha, message (first line), author, date, URL."
        ),
        parameters={
            "repo": "'owner/repo'.",
            "branch": "Optional. Defaults to repo's default branch.",
            "limit": "Optional, default 20, max 100.",
        },
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        owner, name = _split_repo(str(args.get("repo") or ""))
        token = _resolve_token(ctx)
        params: dict[str, Any] = {
            "per_page": min(int(args.get("limit") or 20), 100),
        }
        if args.get("branch"):
            params["sha"] = str(args["branch"])
        data = await _request(
            "GET", f"/repos/{owner}/{name}/commits",
            token=token, params=params,
        )
        commits = [
            {
                "sha": c.get("sha"),
                "message": (
                    ((c.get("commit") or {}).get("message") or "")
                    .splitlines()[0]
                    if (c.get("commit") or {}).get("message") else ""
                ),
                "author": (
                    ((c.get("commit") or {}).get("author") or {}).get("name")
                ),
                "date": (
                    ((c.get("commit") or {}).get("author") or {}).get("date")
                ),
                "url": c.get("html_url"),
            }
            for c in (data or [])
        ]
        return SkillResult(
            skill_name="github.list_commits",
            summary=f"{len(commits)} commit(s) from {owner}/{name}",
            payload={"commits": commits},
        )


class GitHubSearchCodeSkill(Skill):
    """Code search across GitHub or scoped to a repo."""

    spec = SkillSpec(
        name="github.search_code",
        description=(
            "Search code on GitHub. Use 'repo:owner/name' or "
            "'org:foo' qualifiers in the query to scope. Up to 30 "
            "matches with path + URL. Rate-limited to 30 req/min."
        ),
        parameters={
            "query": (
                "Search query — supports GitHub's code-search "
                "qualifiers (language:, path:, in:file, etc)."
            ),
            "limit": "Optional, default 20, max 100.",
        },
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        token = _resolve_token(ctx)
        q = str(args.get("query") or "").strip()
        if not q:
            raise SkillError("github.search_code: query required")
        data = await _request(
            "GET", "/search/code",
            token=token,
            params={
                "q": q,
                "per_page": min(int(args.get("limit") or 20), 100),
            },
        )
        items = (data or {}).get("items", []) or []
        hits = [
            {
                "name": i.get("name"),
                "path": i.get("path"),
                "repo": (i.get("repository") or {}).get("full_name"),
                "url": i.get("html_url"),
                "score": i.get("score"),
            }
            for i in items
        ]
        return SkillResult(
            skill_name="github.search_code",
            summary=(
                f"{len(hits)} match(es) of "
                f"{(data or {}).get('total_count', 0)} total"
            ),
            payload={
                "hits": hits,
                "total": (data or {}).get("total_count", 0),
                "incomplete": (data or {}).get("incomplete_results"),
            },
        )


def register_skills() -> None:
    register(GitHubGetRepoSkill())
    register(GitHubListPRsSkill())
    register(GitHubCreatePRSkill())
    register(GitHubListIssuesSkill())
    register(GitHubCreateIssueSkill())
    register(GitHubListCommitsSkill())
    register(GitHubSearchCodeSkill())


__all__ = [
    "GitHubCreateIssueSkill",
    "GitHubCreatePRSkill",
    "GitHubGetRepoSkill",
    "GitHubListCommitsSkill",
    "GitHubListIssuesSkill",
    "GitHubListPRsSkill",
    "GitHubSearchCodeSkill",
    "register_skills",
]
