"""Workspaces — per-business filesystem isolation for code work.

Each business gets its own root directory. Each repo the business owns is
a subdirectory; the CTO can spawn parallel git worktrees off any repo so
multiple agents can edit the same codebase without stepping on each other.
"""
from korpha.workspaces.model import Repo
from korpha.workspaces.service import WorkspaceError, WorkspacesService

__all__ = ["Repo", "WorkspaceError", "WorkspacesService"]
