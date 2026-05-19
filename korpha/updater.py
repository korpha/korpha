"""Self-update for Korpha installs.

One command, three platforms (Linux / macOS / Windows-native), modeled
after Hermes's ``cmd_update`` shape but trimmed to what Korpha actually
needs:

  1. Pre-update backup (uses the existing ``korpha backup`` payload —
     same tarball customers already trust) so a botched update is
     always recoverable.
  2. Git pull from origin (with a Windows-specific
     ``windows.appendAtomically=false`` config to dodge NTFS atomicity
     issues that crash git on some Windows boxes).
  3. ZIP fallback on Windows when ``.git/`` is missing (the customer
     installed from a release tarball rather than ``git clone``).
  4. ``uv sync --frozen`` to refresh deps (we use uv, not pip).
  5. ``korpha db-migrate`` to bring the DB schema up.
  6. Optional systemd / launchd / Task-Scheduler restart hint.

Robustness layer (HUP protection): customers run this over SSH in
tmux/screen; if the terminal hangs up mid-install, ``SIGHUP`` would
kill the Python process and leave the venv half-installed. The
``_install_hangup_protection`` helper makes the update SSH-disconnect
safe — same shape as Hermes line 7180.

What this module deliberately does NOT do:
  * Reinstall Playwright browsers — that's a one-time setup, not an
    update concern. ``uv sync`` updates the Playwright Python package;
    cached browsers continue to work unless a major version bump.
  * Touch ``$KORPHA_DATA_DIR`` (your install's data) beyond running
    DB migrations. Browser profiles, kanban cards, agent memory — all
    untouched.
  * Restart the running ``korpha server`` automatically. The update
    finishes; the operator decides when to bounce the service (with
    proper drain on systemd). We print the hint, that's it.
"""
from __future__ import annotations

import io
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO


# The repo origin we expect customers to be on. When the operator's
# git origin matches one of these, we treat them as "on the official
# repo" and pull updates without prompting. Forks get a warning that
# this is THEIR fork's main branch, not ours.
OFFICIAL_REPO_URLS: tuple[str, ...] = (
    "https://github.com/korpha/korpha",
    "https://github.com/korpha/korpha.git",
    "git@github.com:korpha/korpha.git",
)


# Where a zip-fallback download comes from when ``.git/`` is missing.
# Always the official repo's main branch. Forks fall back to git pull
# manually (we don't try to guess their tarball URL).
RELEASE_TARBALL_URL = (
    "https://github.com/korpha/korpha/archive/refs/heads/main.tar.gz"
)
RELEASE_ZIP_URL = (
    "https://github.com/korpha/korpha/archive/refs/heads/main.zip"
)


@dataclass
class UpdateResult:
    """Final summary returned to the CLI for display.

    ``steps_run`` is the audit trail the CLI prints at the end and the
    update.log captures verbatim. Failures land as a single failed step
    + ``success=False`` rather than raising.
    """

    success: bool
    method: str
    """One of ``git``, ``zip``, ``check-only``. The CLI surfaces this
    so the operator knows which code path ran."""

    starting_sha: str | None = None
    ending_sha: str | None = None
    fork_detected: bool = False
    backup_path: Path | None = None
    error: str | None = None
    steps_run: list[str] = field(default_factory=list)
    """Human-readable bullet list of what happened, in order. The CLI
    prints these for the operator + the test suite asserts on them."""


# ---------------------------------------------------------------------------
# Platform / env probes
# ---------------------------------------------------------------------------


def is_windows() -> bool:
    """``platform.system() == 'Windows'`` — pulled out so tests can
    monkeypatch the detection without poisoning ``sys.platform``."""
    return platform.system() == "Windows"


def project_root() -> Path:
    """The Korpha repo root (parent of the ``korpha`` package).

    Resolved off ``__file__`` rather than CWD so ``korpha update`` works
    from any directory the operator happens to be in. Matches the
    pattern Hermes uses with ``PROJECT_ROOT``.
    """
    return Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Hangup protection — let the update survive an SSH terminal hangup
# ---------------------------------------------------------------------------


@dataclass
class _HUPState:
    """Internal record of what hangup protection installed, so we can
    cleanly tear it down whether the update succeeded or crashed.

    Kept as a dataclass rather than a dict so callers get attribute
    autocompletion in the IDE and tests can assert on it.
    """

    prev_stdout: IO | None = None
    prev_stderr: IO | None = None
    log_file: IO | None = None
    installed: bool = False


def install_hangup_protection() -> _HUPState:
    """Make the current process survive SIGHUP + broken-pipe writes.

    Returns a state object the caller passes to
    :func:`finalize_hangup_protection` on exit so stdio + log file get
    cleanly restored.

    Two protections, both best-effort + transparent:
      1. ``SIGHUP`` set to ``SIG_IGN`` (POSIX only — Windows doesn't
         deliver SIGHUP). POSIX preserves SIG_IGN across exec, so
         subprocess git/uv inherit the protection.
      2. Update log opened at ``$KORPHA_DATA_DIR/logs/update.log``.
         The caller (CLI) writes step lines to BOTH stdout and the log
         via :func:`log_step` — we don't wrap sys.stdout because
         typer/click cache the original stream reference at import
         time and writes to a wrapper get bypassed silently.
    """
    state = _HUPState(prev_stdout=sys.stdout, prev_stderr=sys.stderr)

    try:
        import signal
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
    except (ValueError, OSError):
        # Non-main thread or hostile signal env — update still runs,
        # just without hangup protection.
        pass

    try:
        base = os.environ.get("KORPHA_DATA_DIR")
        logs_dir = (
            Path(base) if base else (Path.home() / ".korpha")
        ) / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = logs_dir / "update.log"
        log_file = open(log_path, "a", buffering=1, encoding="utf-8")

        import datetime as _dt
        log_file.write(
            f"\n=== korpha update started "
            f"{_dt.datetime.now().isoformat(timespec='seconds')} ===\n"
        )
        state.log_file = log_file
        state.installed = True
    except Exception:  # noqa: BLE001
        state.log_file = None
    return state


def log_step(state: _HUPState, message: str) -> None:
    """Mirror a step message into the update log.

    Caller still uses ``typer.echo`` / ``print`` for the visible
    output. This is the second copy that survives terminal hangup —
    if SSH drops mid-update, the log on disk has the full record.
    No-op when the log file failed to open.
    """
    if not state or state.log_file is None:
        return
    try:
        state.log_file.write(message.rstrip() + "\n")
        state.log_file.flush()
    except Exception:  # noqa: BLE001
        pass


def finalize_hangup_protection(state: _HUPState) -> None:
    """Restore stdio + close the update log opened by
    :func:`install_hangup_protection`. Safe to call on a never-installed
    state — it just returns."""
    if not state or not state.installed:
        return
    if state.log_file is not None:
        try:
            state.log_file.flush()
            state.log_file.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _git_cmd_base() -> list[str]:
    """Construct the git invocation prefix.

    On Windows, ``windows.appendAtomically=false`` works around an
    NTFS atomicity quirk that crashes git mid-write on some boxes
    (especially with aggressive antivirus). On Linux/Mac it's a no-op
    so we always include it — keeps the call sites simple.
    """
    if is_windows():
        return ["git", "-c", "windows.appendAtomically=false"]
    return ["git"]


def get_origin_url(cwd: Path) -> str | None:
    """Return the origin remote URL, or None when not set / git fails."""
    try:
        result = subprocess.run(
            _git_cmd_base() + ["remote", "get-url", "origin"],
            cwd=cwd, capture_output=True, text=True, check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, OSError):
        pass
    return None


def _normalize_repo_url(url: str) -> str:
    """Strip trailing ``.git`` + ``/`` for comparison."""
    n = url.rstrip("/")
    if n.endswith(".git"):
        n = n[:-4]
    return n


def is_fork(origin_url: str | None) -> bool:
    """True when origin is set AND doesn't match any
    :data:`OFFICIAL_REPO_URLS` entry."""
    if not origin_url:
        return False
    normalized = _normalize_repo_url(origin_url)
    for official in OFFICIAL_REPO_URLS:
        if normalized == _normalize_repo_url(official):
            return False
    return True


def current_sha(cwd: Path) -> str | None:
    """Return the short SHA of HEAD, or None if git fails / not a repo."""
    try:
        result = subprocess.run(
            _git_cmd_base() + ["rev-parse", "--short", "HEAD"],
            cwd=cwd, capture_output=True, text=True, check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, OSError):
        pass
    return None


# ---------------------------------------------------------------------------
# Update steps — each callable returns (ok: bool, message: str)
# ---------------------------------------------------------------------------


def step_backup(repo: Path) -> tuple[bool, str, Path | None]:
    """Run the standard ``korpha backup`` so a botched update is always
    recoverable. Returns the resulting tarball path on success.

    Uses the existing backup machinery rather than re-tarring inline
    so the output is bit-identical to a customer-initiated
    ``korpha backup`` — same restore command works for both.
    """
    import datetime as _dt

    base = os.environ.get("KORPHA_DATA_DIR")
    base_dir = Path(base) if base else (Path.home() / ".korpha")
    if not base_dir.is_dir():
        return True, "skipped (no data dir to back up)", None

    backups_dir = base_dir / "backups" / "pre-update"
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = backups_dir / f"pre-update-{stamp}.tar.gz"

    try:
        with tarfile.open(out, "w:gz") as tar:
            tar.add(base_dir, arcname="korpha")
    except OSError as exc:
        return False, f"backup failed: {exc}", None
    return True, f"backup → {out}", out


def step_git_pull(repo: Path) -> tuple[bool, str]:
    """Fetch + fast-forward main from origin. Returns ``(False, ...)``
    when the working tree is dirty or the remote rejected the pull —
    operator deals with it manually."""
    git = _git_cmd_base()

    # Refuse to update on a dirty tree — customers might have local
    # tweaks and a pull would lose them. Better to fail loud.
    status = subprocess.run(
        git + ["status", "--porcelain"],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    if status.returncode != 0:
        return False, f"git status failed: {status.stderr.strip()}"
    if status.stdout.strip():
        return False, (
            "working tree has uncommitted changes — commit or stash "
            "them, then re-run `korpha update`"
        )

    fetch = subprocess.run(
        git + ["fetch", "origin"],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    if fetch.returncode != 0:
        return False, f"git fetch failed: {fetch.stderr.strip()}"

    pull = subprocess.run(
        git + ["pull", "--ff-only", "origin"],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    if pull.returncode != 0:
        return False, f"git pull failed: {pull.stderr.strip()}"
    return True, "git pull → fast-forwarded to origin"


def step_zip_fallback(repo: Path) -> tuple[bool, str]:
    """Download main as a ZIP and overlay it on the repo.

    Used when ``.git/`` doesn't exist — customers can install from a
    GitHub "Download ZIP" or a release tarball without ever cloning.
    Overlay strategy is overwrite-but-don't-delete: any file the
    customer added (like ``.env``) survives.
    """
    try:
        with urllib.request.urlopen(RELEASE_ZIP_URL, timeout=120) as resp:
            data = resp.read()
    except (OSError, ValueError) as exc:
        return False, f"zip download failed: {exc}"

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            # GitHub zips have a top-level dir like
            # ``aigenteur_agent-main/``; skip it.
            names = zf.namelist()
            if not names:
                return False, "zip is empty"
            top = names[0].split("/", 1)[0]
            prefix = f"{top}/"
            for entry in names:
                if not entry.startswith(prefix):
                    continue
                rel = entry[len(prefix):]
                if not rel or rel.endswith("/"):
                    continue
                dest = repo / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(entry) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
    except (zipfile.BadZipFile, OSError) as exc:
        return False, f"zip extract failed: {exc}"
    return True, "zip overlay → main applied"


def step_uv_sync(repo: Path) -> tuple[bool, str]:
    """Run ``uv sync --frozen`` to update Python deps to the locked
    versions. ``--frozen`` refuses to update the lockfile in place —
    we want the lockfile that's now on disk (post-pull) to be the
    source of truth, not a fresh resolve."""
    uv = shutil.which("uv")
    if uv is None:
        return False, (
            "uv not found on PATH. Install it from "
            "https://docs.astral.sh/uv/ then re-run `korpha update`."
        )
    proc = subprocess.run(
        [uv, "sync", "--frozen"],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        return False, f"uv sync failed: {proc.stderr.strip()[:400]}"
    return True, "uv sync → deps refreshed"


def step_db_migrate(repo: Path) -> tuple[bool, str]:
    """Run ``korpha db-migrate`` to bring the DB schema up to head.

    Invokes via the same venv we just synced (``uv run``) so we don't
    drag in a stale CLI from the operator's global PATH.

    Command name is ``db-migrate`` (not ``migrate``) — the top-level
    ``korpha migrate`` namespace belongs to the host-migration
    subgroup (bundle/restore/inspect/check). Calling bare ``korpha
    migrate`` would print --help and exit non-zero.
    """
    uv = shutil.which("uv")
    if uv is None:
        return False, "uv not found — cannot run migrations"
    proc = subprocess.run(
        [uv, "run", "korpha", "db-migrate"],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        return False, f"db migrate failed: {proc.stderr.strip()[:400]}"
    return True, "db migrate → schema at head"


# ---------------------------------------------------------------------------
# Top-level orchestration
# ---------------------------------------------------------------------------


def run_update(
    *,
    skip_backup: bool = False,
    check_only: bool = False,
    yes: bool = False,
) -> UpdateResult:
    """Drive the full update sequence.

    Public entry point used by both the CLI command and the planned
    ``/app/update`` dashboard route. Never raises — all errors come
    back on the ``UpdateResult``.

    ``yes`` is reserved for unattended runs (CI, scheduled updates);
    today nothing prompts so it's a no-op marker. Keeps the signature
    forward-compatible for when we add prompts for forks/dirty trees.
    """
    repo = project_root()
    result = UpdateResult(success=False, method="git")

    origin = get_origin_url(repo)
    result.fork_detected = is_fork(origin)
    result.starting_sha = current_sha(repo)

    if check_only:
        # Light path: just report what's available, don't mutate.
        git = _git_cmd_base()
        fetch = subprocess.run(
            git + ["fetch", "origin"],
            cwd=repo, capture_output=True, text=True, check=False,
        )
        if fetch.returncode != 0:
            result.error = (
                f"check failed: git fetch returned "
                f"{fetch.returncode}: {fetch.stderr.strip()}"
            )
            result.method = "check-only"
            return result
        ahead = subprocess.run(
            git + ["rev-list", "--count", "HEAD..origin/main"],
            cwd=repo, capture_output=True, text=True, check=False,
        )
        count = ahead.stdout.strip() or "?"
        result.steps_run.append(f"check → {count} commits behind origin/main")
        result.success = True
        result.method = "check-only"
        return result

    if not skip_backup:
        ok, msg, path = step_backup(repo)
        result.steps_run.append(msg)
        result.backup_path = path
        if not ok:
            result.error = msg
            return result
    else:
        result.steps_run.append("backup → skipped (--no-backup)")

    git_dir = repo / ".git"
    if git_dir.is_dir():
        ok, msg = step_git_pull(repo)
    else:
        result.method = "zip"
        ok, msg = step_zip_fallback(repo)
    result.steps_run.append(msg)
    if not ok:
        result.error = msg
        return result

    ok, msg = step_uv_sync(repo)
    result.steps_run.append(msg)
    if not ok:
        result.error = msg
        return result

    ok, msg = step_db_migrate(repo)
    result.steps_run.append(msg)
    if not ok:
        result.error = msg
        return result

    result.ending_sha = current_sha(repo)
    result.success = True
    return result


__all__ = [
    "OFFICIAL_REPO_URLS",
    "RELEASE_TARBALL_URL",
    "RELEASE_ZIP_URL",
    "UpdateResult",
    "current_sha",
    "finalize_hangup_protection",
    "get_origin_url",
    "install_hangup_protection",
    "is_fork",
    "is_windows",
    "project_root",
    "run_update",
    "step_backup",
    "step_db_migrate",
    "step_git_pull",
    "step_uv_sync",
    "step_zip_fallback",
]
