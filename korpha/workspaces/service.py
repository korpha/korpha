"""WorkspacesService — disk layout + git worktree lifecycle for businesses.

Layout:

    {root}/
      {business_short}/
        repos/
          {repo_name}/         <- main checkout (`git clone` lands here)
        worktrees/
          {repo_name}/{branch} <- ephemeral parallel checkout

The `{root}` defaults to `~/.korpha/workspaces` but can be overridden
via the constructor — useful for tests (tmp_path) and for power users
who want to mount the workspace root on a different volume.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlmodel import Session, select

from korpha.workspaces.model import Repo


class WorkspaceError(RuntimeError):
    """Filesystem or git operation failed in a way the caller should handle."""


_BUSINESS_DIR_LEN = 12
_SLUG_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


@dataclass
class WorkspacesService:
    session: Session
    root: Path
    """Top-level directory holding every business workspace."""

    git_binary: str = "git"
    """Override for tests / non-standard installs."""

    @classmethod
    def default_root(cls) -> Path:
        return Path.home() / ".korpha" / "workspaces"

    def root_for(self, business_id: UUID) -> Path:
        return self.root / business_id.hex[:_BUSINESS_DIR_LEN]

    def ensure_root(self, business_id: UUID) -> Path:
        path = self.root_for(business_id)
        (path / "repos").mkdir(parents=True, exist_ok=True)
        (path / "worktrees").mkdir(parents=True, exist_ok=True)
        return path

    def register_repo(
        self,
        *,
        business_id: UUID,
        name: str,
        source_url: str | None = None,
        default_branch: str = "main",
        clone: bool = True,
    ) -> Repo:
        """Create the on-disk checkout + persist a Repo row.

        - When ``source_url`` is given and ``clone=True``, runs
          ``git clone source_url <path>``.
        - When ``source_url`` is None, runs ``git init`` so the CTO can start
          a fresh codebase from scratch.
        - Idempotent: re-registering the same name returns the existing Repo
          (does NOT re-clone).
        """
        slug = _slugify(name)
        if not slug:
            raise WorkspaceError(f"empty repo name after slugifying: {name!r}")

        existing = self.session.exec(
            select(Repo)
            .where(Repo.business_id == business_id)
            .where(Repo.name == slug)
        ).first()
        if existing is not None:
            return existing

        root = self.ensure_root(business_id)
        repo_path = root / "repos" / slug
        if repo_path.exists():
            raise WorkspaceError(
                f"path {repo_path} already exists but no Repo row claims it"
            )

        if source_url and clone:
            self._run_git(
                ["clone", source_url, str(repo_path)],
                cwd=root,
            )
        else:
            repo_path.mkdir(parents=True)
            self._run_git(["init", "-b", default_branch], cwd=repo_path)

        row = Repo(
            business_id=business_id,
            name=slug,
            source_url=source_url,
            local_path=str(repo_path),
            default_branch=default_branch,
        )
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return row

    def get_repo(self, business_id: UUID, name: str) -> Repo:
        slug = _slugify(name)
        repo = self.session.exec(
            select(Repo)
            .where(Repo.business_id == business_id)
            .where(Repo.name == slug)
        ).first()
        if repo is None:
            raise WorkspaceError(f"no repo named {slug!r} for business {business_id}")
        return repo

    def list_repos(self, business_id: UUID) -> list[Repo]:
        return list(
            self.session.exec(
                select(Repo).where(Repo.business_id == business_id)
            ).all()
        )

    def create_worktree(
        self,
        *,
        business_id: UUID,
        repo_name: str,
        branch: str,
        base: str | None = None,
    ) -> Path:
        """Spawn a git worktree for parallel work on ``branch``.

        Creates the branch off ``base`` (default: repo's default branch) if
        it doesn't exist yet. Returns the worktree path. Raises
        WorkspaceError if the worktree path already exists.
        """
        repo = self.get_repo(business_id, repo_name)
        branch_slug = _slugify(branch)
        if not branch_slug:
            raise WorkspaceError(f"empty branch name after slugifying: {branch!r}")

        worktree_root = self.root_for(business_id) / "worktrees" / repo.name
        worktree_root.mkdir(parents=True, exist_ok=True)
        worktree_path = worktree_root / branch_slug
        if worktree_path.exists():
            raise WorkspaceError(f"worktree {worktree_path} already exists")

        base_branch = base or repo.default_branch
        # `git worktree add -b <new> <path> <base>` creates the branch from base.
        self._run_git(
            ["worktree", "add", "-b", branch_slug, str(worktree_path), base_branch],
            cwd=Path(repo.local_path),
        )
        return worktree_path

    def remove_worktree(
        self,
        *,
        business_id: UUID,
        repo_name: str,
        branch: str,
        force: bool = False,
    ) -> None:
        """Delete a worktree and its branch. ``force=True`` discards
        uncommitted changes — use with care."""
        repo = self.get_repo(business_id, repo_name)
        branch_slug = _slugify(branch)
        worktree_path = self.root_for(business_id) / "worktrees" / repo.name / branch_slug
        if not worktree_path.exists():
            return

        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(worktree_path))
        self._run_git(args, cwd=Path(repo.local_path))

    def purge_business(self, business_id: UUID) -> None:
        """Delete the entire on-disk workspace for a business. Does NOT
        touch DB rows — caller is responsible for that. Idempotent."""
        path = self.root_for(business_id)
        if path.exists():
            shutil.rmtree(path)

    def _run_git(self, args: list[str], *, cwd: Path) -> str:
        proc = subprocess.run(
            [self.git_binary, *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise WorkspaceError(
                f"git {' '.join(args)} failed (rc={proc.returncode}): "
                f"{(proc.stderr or proc.stdout).strip()[:500]}"
            )
        return proc.stdout


def _slugify(value: str) -> str:
    return _SLUG_RE.sub("-", value.strip()).strip("-")


__all__ = ["WorkspaceError", "WorkspacesService"]
