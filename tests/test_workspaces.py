"""WorkspacesService tests — repo init, worktree spawn/remove, slugging.

Each test runs against a tmp_path so we don't touch ~/.korpha. Git is
required at runtime; the conftest skips these tests when git isn't on PATH.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from sqlmodel import Session

from korpha.business.model import Business
from korpha.workspaces.model import Repo
from korpha.workspaces.service import WorkspaceError, WorkspacesService

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="git binary not available"
)


@pytest.fixture
def workspaces(session: Session, tmp_path: Path) -> WorkspacesService:
    svc = WorkspacesService(session=session, root=tmp_path / "ws")
    # `git init` and `git worktree add` need git author config.
    subprocess.run(
        ["git", "config", "--global", "--add", "safe.directory", "*"],
        check=False,
        capture_output=True,
    )
    return svc


def _git(*args: str, cwd: Path) -> None:
    env = {
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@test.invalid",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@test.invalid",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env=env,
    )


def test_ensure_root_creates_layout(
    workspaces: WorkspacesService, business: Business
) -> None:
    root = workspaces.ensure_root(business.id)
    assert (root / "repos").exists()
    assert (root / "worktrees").exists()


def test_register_fresh_repo_runs_git_init(
    workspaces: WorkspacesService, business: Business, session: Session
) -> None:
    repo = workspaces.register_repo(
        business_id=business.id,
        name="widget-site",
        source_url=None,
    )
    assert repo.name == "widget-site"
    assert Path(repo.local_path).exists()
    assert (Path(repo.local_path) / ".git").exists()

    rows = workspaces.list_repos(business.id)
    assert len(rows) == 1
    assert rows[0].id == repo.id


def test_register_repo_idempotent(
    workspaces: WorkspacesService, business: Business
) -> None:
    a = workspaces.register_repo(business_id=business.id, name="site")
    b = workspaces.register_repo(business_id=business.id, name="site")
    assert a.id == b.id


def test_slugifies_repo_name(
    workspaces: WorkspacesService, business: Business
) -> None:
    repo = workspaces.register_repo(business_id=business.id, name="My Cool Site!")
    assert repo.name == "My-Cool-Site"


def test_create_and_remove_worktree(
    workspaces: WorkspacesService, business: Business
) -> None:
    repo = workspaces.register_repo(business_id=business.id, name="r")
    # We need a commit on the default branch before `worktree add` can find it.
    repo_path = Path(repo.local_path)
    (repo_path / "README.md").write_text("seed", encoding="utf-8")
    _git("add", ".", cwd=repo_path)
    _git(
        "-c",
        "user.email=a@b",
        "-c",
        "user.name=t",
        "commit",
        "-m",
        "init",
        cwd=repo_path,
    )

    wt = workspaces.create_worktree(
        business_id=business.id, repo_name="r", branch="feature/x"
    )
    assert wt.exists()
    assert (wt / "README.md").exists()

    workspaces.remove_worktree(
        business_id=business.id, repo_name="r", branch="feature/x"
    )
    assert not wt.exists()


def test_create_worktree_collision_errors(
    workspaces: WorkspacesService, business: Business
) -> None:
    repo = workspaces.register_repo(business_id=business.id, name="r")
    repo_path = Path(repo.local_path)
    (repo_path / "README.md").write_text("seed", encoding="utf-8")
    _git("add", ".", cwd=repo_path)
    _git(
        "-c",
        "user.email=a@b",
        "-c",
        "user.name=t",
        "commit",
        "-m",
        "init",
        cwd=repo_path,
    )
    workspaces.create_worktree(business_id=business.id, repo_name="r", branch="x")
    with pytest.raises(WorkspaceError):
        workspaces.create_worktree(business_id=business.id, repo_name="r", branch="x")


def test_get_repo_unknown_raises(
    workspaces: WorkspacesService, business: Business
) -> None:
    with pytest.raises(WorkspaceError):
        workspaces.get_repo(business.id, "nope")


def test_purge_business_removes_workspace(
    workspaces: WorkspacesService, business: Business
) -> None:
    workspaces.register_repo(business_id=business.id, name="r")
    workspaces.purge_business(business.id)
    assert not workspaces.root_for(business.id).exists()


def test_business_workspaces_isolated(
    workspaces: WorkspacesService, business: Business, session: Session, founder
) -> None:
    """Different businesses get different workspace roots."""
    other_biz = Business(
        founder_id=founder.id, name="OtherCo", description="another"
    )
    session.add(other_biz)
    session.commit()
    session.refresh(other_biz)

    a = workspaces.register_repo(business_id=business.id, name="r")
    b = workspaces.register_repo(business_id=other_biz.id, name="r")
    assert a.local_path != b.local_path
    # Same name allowed across businesses.
    assert workspaces.get_repo(business.id, "r").id == a.id
    assert workspaces.get_repo(other_biz.id, "r").id == b.id


def test_repo_row_persists(
    workspaces: WorkspacesService, business: Business, session: Session
) -> None:
    workspaces.register_repo(business_id=business.id, name="x")
    from sqlmodel import select

    rows = session.exec(select(Repo)).all()
    assert len(rows) == 1
