"""``cron.create_watchdog`` — agent stages an agentless cron job
behind the founder's approval gate.

Founder says "set up a 12h disk-space watchdog and ping me on
email if usage > 90%". The CEO calls this skill. We:

  1. Validate the requested config (cadence parses, channel is
     known, recipient looks plausible).
  2. Run a coarse safety scan over the script content (refuse
     ``rm -rf /``-style commands, network exfil patterns).
  3. Stage as an ``Approval`` with ``action_class=CODE_CHANGE``.
     The founder reviews + approves.
  4. The apply path writes the script + creates the ``ScriptCron``
     row. The next ``korpha tick`` (or running daemon) picks
     it up.

What this skill does NOT do:
  • Draft the script from scratch — caller hands us the content.
    The agent's drafting happens upstream via the LLM in the
    director loop; this skill is the structured deposit.
  • Run the script. That happens at the heartbeat tick.
  • Edit existing crons (yet — phase 2 if it bites).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from uuid import UUID

from korpha.approvals.model import ActionClass, Approval
from korpha.audit.model import InferenceTier
from korpha.scriptcron import parse_cadence
from korpha.scriptcron.model import ScriptCron
from korpha.skills.delta_lint import lint_text
from korpha.skills.registry import register
from korpha.skills.types import (
    Skill, SkillContext, SkillError, SkillProvenance, SkillResult, SkillSpec,
)


_CRON_SCRIPTS_DIR_NAME = "cron-scripts"

# Patterns the scanner refuses outright. Coarse but cheap. Founder
# still reviews each via approval — this is just the first gate so
# obvious wreckage is rejected before they even see the diff.
_FORBIDDEN_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"rm\s+-rf\s+/", "destructive 'rm -rf /'"),
    (r"rm\s+-rf\s+\$HOME", "destructive 'rm -rf $HOME'"),
    (r"rm\s+-rf\s+~", "destructive 'rm -rf ~'"),
    (r":\(\)\s*\{\s*:\|:&\s*\};", "fork bomb"),
    (r"mkfs\.", "filesystem format"),
    (r"dd\s+.*of=/dev/[sn]d", "raw-device dd write"),
    (r"chmod\s+-R\s+777\s+/", "world-writable root"),
    (r">\s*/dev/sda", "raw-device redirect"),
    (r"curl\s+[^|]*\|\s*(sh|bash|zsh)", "curl | sh pipe-to-shell"),
    (r"wget\s+[^|]*\|\s*(sh|bash|zsh)", "wget | sh pipe-to-shell"),
    (r"sudo\s+rm", "sudo destructive remove"),
    (r"history\s+-c", "history wipe"),
)


def _scan_script(content: str) -> list[str]:
    """Return a list of human-readable issues. Empty list = clean."""
    issues: list[str] = []
    for pattern, label in _FORBIDDEN_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE | re.MULTILINE):
            issues.append(label)
    return issues


_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,60}$")


def _scripts_dir(ctx: SkillContext) -> Path:
    """Where authored scripts live. Honors KORPHA_DATA_DIR for
    test isolation; production = ``~/.korpha/cron-scripts/``."""
    import os
    base = os.environ.get("KORPHA_DATA_DIR")
    return (
        (Path(base) / _CRON_SCRIPTS_DIR_NAME)
        if base
        else (Path.home() / ".korpha" / _CRON_SCRIPTS_DIR_NAME)
    )


class CreateWatchdogSkill(Skill):
    """Stage a new agentless script cron behind the approval gate."""

    spec = SkillSpec(
        name="cron.create_watchdog",
        description=(
            "Author an agentless cron job (a script that runs on a "
            "cadence and pings a channel with its stdout). Use when "
            "the founder asks for a watchdog ('alert me if memory > "
            "80%'), an RSS pull, a daily SaaS health check, or any "
            "programmatic recurring task that doesn't need the agent "
            "in the loop. Costs $0/tick — no LLM is invoked. The "
            "founder approves the script content + delivery target "
            "before it ships."
        ),
        parameters={
            "name": (
                "Short slug (e.g. 'memory-watchdog'). Used as the "
                "filename + display label. Letters/digits/_-./ only, "
                "max 60 chars."
            ),
            "script_content": (
                "The full script body (bash or python). The agent "
                "drafts this from the founder's request — keep it "
                "self-contained, no shebang assumptions. The host "
                "picks the interpreter from extension."
            ),
            "extension": (
                "File extension (.sh / .py). Drives the interpreter."
            ),
            "cadence": (
                "How often: 'every 5m' / 'every 12h' / 'every 1d'."
            ),
            "deliver": (
                "Channel for stdout: 'email' / 'telegram'. Empty = "
                "log-only (founder can still see runs in /app/cron)."
            ),
            "recipient": (
                "Email address or telegram chat_id. Required if "
                "deliver is set."
            ),
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        name = str(args.get("name") or "").strip()
        script_content = str(args.get("script_content") or "")
        extension = str(args.get("extension") or ".sh").strip().lower()
        cadence = str(args.get("cadence") or "").strip()
        deliver = (str(args.get("deliver") or "").strip().lower()) or None
        recipient = (str(args.get("recipient") or "").strip()) or None

        # ---- validation ----
        if not _SAFE_NAME_RE.match(name):
            raise SkillError(
                f"cron.create_watchdog: invalid name {name!r}. Use "
                "letters / digits / dot / dash / underscore only, "
                "1-60 chars, must start with alphanumeric."
            )
        if not script_content.strip():
            raise SkillError(
                "cron.create_watchdog: script_content required."
            )
        if extension not in (".sh", ".bash", ".py"):
            raise SkillError(
                f"cron.create_watchdog: extension {extension!r} not "
                "supported. Use .sh, .bash, or .py."
            )
        try:
            parse_cadence(cadence)
        except ValueError as exc:
            raise SkillError(
                f"cron.create_watchdog: bad cadence: {exc}"
            ) from exc
        if deliver and deliver not in ("email", "telegram"):
            raise SkillError(
                f"cron.create_watchdog: unknown channel {deliver!r}. "
                "Use 'email' or 'telegram'."
            )
        if deliver and not recipient:
            raise SkillError(
                f"cron.create_watchdog: deliver={deliver!r} needs a "
                "recipient (email address / chat_id)."
            )
        if recipient and not deliver:
            raise SkillError(
                "cron.create_watchdog: recipient set without deliver. "
                "Specify deliver= so we know which channel."
            )

        # ---- safety scan ----
        issues = _scan_script(script_content)
        if issues:
            raise SkillError(
                "cron.create_watchdog: script rejected by safety scan: "
                + "; ".join(issues)
                + ". Rephrase the script to avoid these patterns or "
                "drop them entirely."
            )

        # ---- post-write delta lint ----
        # Catches stray indent / unclosed brace / typo *before* the
        # founder sees the approval. Failure messages name line + col
        # so the LLM can self-correct on the next turn instead of
        # shipping broken cron that fails silently at execute time.
        lint = lint_text(
            script_content, suffix=extension, filename=f"{name}{extension}",
        )
        if not lint.ok:
            raise SkillError(
                f"cron.create_watchdog: {lint.render()}. Fix the syntax "
                "and re-author the cron."
            )

        # ---- stage approval ----
        role_id = ctx.invoking_agent_role_id
        size_kb = len(script_content.encode("utf-8")) / 1024
        deliver_label = (
            f"{deliver} → {recipient}"
            if deliver else "log-only (no push)"
        )
        summary = (
            f"Author cron job '{name}' ({extension}) running "
            f"{cadence}. Delivery: {deliver_label}. "
            f"Script: {size_kb:.1f} KB.\n\n"
            f"Preview (first 600 chars):\n"
            f"{script_content[:600]}"
            + ("\n…(truncated in preview)" if len(script_content) > 600 else "")
        )
        approval = Approval(
            business_id=ctx.business.id,
            agent_role_id=role_id,
            action_class=ActionClass.CODE_CHANGE,
            platform="cron",
            proposal_summary=summary,
            action_payload={
                "kind": "create_cron",
                "name": name,
                "script_content": script_content,
                "extension": extension,
                "cadence": cadence,
                "deliver_platform": deliver,
                "deliver_recipient": recipient,
            },
        )
        ctx.session.add(approval)
        ctx.session.commit()
        ctx.session.refresh(approval)

        return SkillResult(
            skill_name=self.spec.name,
            summary=(
                f"Drafted cron '{name}' ({cadence}, deliver: "
                f"{deliver_label}). Pending your approval — review "
                "in the Approvals queue or the chat will surface it."
            ),
            payload={
                "approval_id": str(approval.id),
                "name": name,
                "cadence": cadence,
                "deliver": deliver,
                "recipient": recipient,
                "script_size_bytes": len(script_content),
            },
            cost_usd=0.0,
        )


def apply_cron_proposal_from_approval(approval: Approval) -> Path:
    """Write the script + persist a ``ScriptCron`` row from an
    approved proposal. Returns the script's filesystem path so the
    apply-dispatcher can log it."""
    payload = approval.action_payload or {}
    if payload.get("kind") != "create_cron":
        raise ValueError(
            f"apply_cron_proposal: payload kind is "
            f"{payload.get('kind')!r}, expected 'create_cron'."
        )
    name = str(payload.get("name") or "")
    script_content = str(payload.get("script_content") or "")
    extension = str(payload.get("extension") or ".sh")
    cadence = str(payload.get("cadence") or "")
    deliver = payload.get("deliver_platform")
    recipient = payload.get("deliver_recipient")

    if not name or not script_content or not cadence:
        raise ValueError("apply_cron_proposal: payload missing fields")

    # Re-scan at apply time too. If a script was approved that contained
    # forbidden patterns (someone bypassed the staging path), refuse
    # the write — defense in depth.
    issues = _scan_script(script_content)
    if issues:
        raise ValueError(
            f"apply_cron_proposal: script failed re-scan: {issues}"
        )
    lint = lint_text(
        script_content, suffix=extension, filename=f"{name}{extension}",
    )
    if not lint.ok:
        raise ValueError(
            f"apply_cron_proposal: {lint.render()}"
        )

    # Build a scripts dir; KORPHA_DATA_DIR override gives test
    # isolation without DI.
    import os

    base = os.environ.get("KORPHA_DATA_DIR")
    target_dir = (
        (Path(base) / _CRON_SCRIPTS_DIR_NAME)
        if base
        else (Path.home() / ".korpha" / _CRON_SCRIPTS_DIR_NAME)
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    script_path = target_dir / f"{name}{extension}"
    script_path.write_text(script_content, encoding="utf-8")
    script_path.chmod(0o755)

    # Persist the ScriptCron row. We need a session — pull it from
    # the approval's session if present, else build a fresh one
    # against the configured engine.
    from sqlalchemy.orm import object_session
    from sqlmodel import Session

    sess = object_session(approval)
    own_session = False
    if sess is None:
        from korpha.db._session import get_engine
        sess = Session(get_engine())
        own_session = True

    try:
        job = ScriptCron(
            business_id=UUID(str(approval.business_id)),
            name=name,
            script_path=str(script_path),
            cadence=cadence,
            deliver_platform=deliver,
            deliver_recipient=recipient,
        )
        sess.add(job)
        sess.commit()
    finally:
        if own_session:
            sess.close()
    return script_path


register(CreateWatchdogSkill())


__all__ = [
    "CreateWatchdogSkill",
    "apply_cron_proposal_from_approval",
]
