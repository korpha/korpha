"""Build the morning digest email content for a business.

The digest mirrors the dashboard home view but in email form: today's
spend, pending approvals, blockers needing attention, recent task
activity, and the cofounder's voice in 1-2 sentences. Designed for Mike
to read at breakfast and decide whether to dig in or trust the team.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlmodel import Session, select

from korpha.approvals.model import Approval, ApprovalStatus
from korpha.audit.model import Cost
from korpha.blockers.model import Blocker, BlockerStatus
from korpha.business.issues import format_ref
from korpha.business.model import Business, Task, TaskStatus
from korpha.db._base import as_utc, utcnow
from korpha.notifications.base import Notification

# Sonnet baseline for the savings line, matches dashboard.py.
_SONNET_INPUT_PER_1M = Decimal("3.00")
_SONNET_OUTPUT_PER_1M = Decimal("15.00")


@dataclass
class DigestSnapshot:
    """Numbers + items the digest renders. Kept separate from formatting
    so tests can assert on the data without parsing HTML."""

    business_name: str
    pending_approvals: int
    open_blockers: int
    in_progress_tasks: int
    today_spend_usd: float
    saved_vs_sonnet_usd: float
    open_blocker_titles: list[str]
    pending_approval_summaries: list[str]
    recent_task_lines: list[str]
    """Pre-formatted ``AIG-1 in_progress · Pick a micro-niche`` lines."""


def build_snapshot(
    session: Session, business: Business, *, recent_task_limit: int = 5
) -> DigestSnapshot:
    biz_id = business.id

    pending_approvals_rows = list(
        session.exec(
            select(Approval)
            .where(Approval.business_id == biz_id)
            .where(Approval.status == ApprovalStatus.PENDING)
        ).all()
    )
    open_blockers_rows = list(
        session.exec(
            select(Blocker)
            .where(Blocker.business_id == biz_id)
            .where(
                Blocker.status.in_(  # type: ignore[attr-defined]
                    [
                        BlockerStatus.OPEN,
                        BlockerStatus.TRIAGED,
                        BlockerStatus.AWAITING_FOUNDER,
                    ]
                )
            )
        ).all()
    )
    in_progress_tasks = len(
        list(
            session.exec(
                select(Task)
                .where(Task.business_id == biz_id)
                .where(Task.status == TaskStatus.IN_PROGRESS)
            ).all()
        )
    )
    recent_tasks = list(
        session.exec(
            select(Task)
            .where(Task.business_id == biz_id)
            .order_by(Task.updated_at.desc())  # type: ignore[attr-defined]
            .limit(recent_task_limit)
        ).all()
    )

    today_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    cost_rows = list(
        session.exec(select(Cost).where(Cost.business_id == biz_id)).all()
    )
    today_spend = Decimal("0")
    sonnet_total = Decimal("0")
    spend_total = Decimal("0")
    for c in cost_rows:
        spend_total += c.cost_usd
        sonnet_total += (
            Decimal(c.input_tokens) * _SONNET_INPUT_PER_1M / Decimal(1_000_000)
            + Decimal(c.output_tokens) * _SONNET_OUTPUT_PER_1M / Decimal(1_000_000)
        )
        created = as_utc(c.created_at)
        if created is not None and created >= today_start:
            today_spend += c.cost_usd
    saved = max(Decimal("0"), sonnet_total - spend_total)

    return DigestSnapshot(
        business_name=business.name,
        pending_approvals=len(pending_approvals_rows),
        open_blockers=len(open_blockers_rows),
        in_progress_tasks=in_progress_tasks,
        today_spend_usd=float(today_spend),
        saved_vs_sonnet_usd=float(saved),
        open_blocker_titles=[b.title for b in open_blockers_rows[:5]],
        pending_approval_summaries=[
            a.proposal_summary for a in pending_approvals_rows[:5]
        ],
        recent_task_lines=[
            f"{format_ref(business, t.ref_number)}  {t.status.value}  ·  {t.title}"
            for t in recent_tasks
        ],
    )


def render_digest(
    snap: DigestSnapshot, *, founder_name: str | None = None
) -> Notification:
    """Build the Notification (subject + text + html) from a snapshot."""
    salutation = f"Morning {founder_name}," if founder_name else "Morning,"
    subject = (
        f"{snap.business_name} digest · {snap.pending_approvals} approvals · "
        f"{snap.open_blockers} blockers"
    )

    text_lines = [
        salutation,
        "",
        f"  Pending approvals     {snap.pending_approvals}",
        f"  Open blockers         {snap.open_blockers}",
        f"  Tasks in progress     {snap.in_progress_tasks}",
        f"  Spend today           ${snap.today_spend_usd:.4f}",
    ]
    if snap.saved_vs_sonnet_usd > 0.01:
        text_lines.append(
            f"  Saved vs Sonnet       ${snap.saved_vs_sonnet_usd:.2f}"
        )
    if snap.pending_approval_summaries:
        text_lines += ["", "Approvals waiting on you:"]
        for s in snap.pending_approval_summaries:
            text_lines.append(f"  - {s}")
    if snap.open_blocker_titles:
        text_lines += ["", "Open blockers:"]
        for t in snap.open_blocker_titles:
            text_lines.append(f"  - {t}")
    if snap.recent_task_lines:
        text_lines += ["", "Recent tasks:"]
        for line in snap.recent_task_lines:
            text_lines.append(f"  {line}")
    text_lines += [
        "",
        "Open the dashboard to act:",
        "  http://localhost:8765/app/dashboard",
        "",
        "— Your Korpha cofounder",
    ]
    text_body = "\n".join(text_lines)

    html_body = _render_html(snap, salutation=salutation)
    return Notification(
        to="",  # caller sets
        subject=subject,
        text_body=text_body,
        html_body=html_body,
    )


def _render_html(snap: DigestSnapshot, *, salutation: str) -> str:
    def _li(items: list[str]) -> str:
        return "".join(f"<li>{_esc(s)}</li>" for s in items)

    saved_block = ""
    if snap.saved_vs_sonnet_usd > 0.01:
        saved_block = (
            f'<tr><td style="padding:4px 0;color:#7a8089">Saved vs Sonnet</td>'
            f'<td style="padding:4px 0;text-align:right;color:#6dcf80;font-variant-numeric:tabular-nums">'
            f"${snap.saved_vs_sonnet_usd:.2f}</td></tr>"
        )

    approvals_html = ""
    if snap.pending_approval_summaries:
        approvals_html = (
            f'<h3 style="font:600 13px ui-sans-serif;color:#e6e8eb;margin:18px 0 6px">'
            f"Approvals waiting on you</h3>"
            f'<ul style="padding-left:18px;margin:0;color:#c8ccd2">{_li(snap.pending_approval_summaries)}</ul>'
        )
    blockers_html = ""
    if snap.open_blocker_titles:
        blockers_html = (
            f'<h3 style="font:600 13px ui-sans-serif;color:#e6e8eb;margin:18px 0 6px">'
            f"Open blockers</h3>"
            f'<ul style="padding-left:18px;margin:0;color:#c8ccd2">{_li(snap.open_blocker_titles)}</ul>'
        )
    tasks_html = ""
    if snap.recent_task_lines:
        tasks_html = (
            f'<h3 style="font:600 13px ui-sans-serif;color:#e6e8eb;margin:18px 0 6px">'
            f"Recent tasks</h3>"
            f'<ul style="padding-left:18px;margin:0;color:#c8ccd2;'
            f'font-family:ui-monospace,Menlo,monospace;font-size:12.5px">'
            f"{_li(snap.recent_task_lines)}</ul>"
        )

    return f"""<!doctype html>
<html><body style="margin:0;padding:24px;background:#0c0d10;color:#e6e8eb;font:14px/1.55 ui-sans-serif">
<div style="max-width:560px;margin:0 auto;background:#15171c;border:1px solid #232730;border-radius:12px;padding:24px">
  <div style="font:600 16px ui-sans-serif;color:#e6e8eb">{_esc(snap.business_name)} <span style="color:#7a8089;font-weight:400">· morning digest</span></div>
  <div style="color:#9aa0a8;margin:6px 0 18px">{_esc(salutation)}</div>
  <table style="width:100%;border-collapse:collapse;font:13px ui-sans-serif">
    <tr><td style="padding:4px 0;color:#7a8089">Pending approvals</td><td style="padding:4px 0;text-align:right;font-variant-numeric:tabular-nums">{snap.pending_approvals}</td></tr>
    <tr><td style="padding:4px 0;color:#7a8089">Open blockers</td><td style="padding:4px 0;text-align:right;font-variant-numeric:tabular-nums">{snap.open_blockers}</td></tr>
    <tr><td style="padding:4px 0;color:#7a8089">Tasks in progress</td><td style="padding:4px 0;text-align:right;font-variant-numeric:tabular-nums">{snap.in_progress_tasks}</td></tr>
    <tr><td style="padding:4px 0;color:#7a8089">Spend today</td><td style="padding:4px 0;text-align:right;font-variant-numeric:tabular-nums">${snap.today_spend_usd:.4f}</td></tr>
    {saved_block}
  </table>
  {approvals_html}
  {blockers_html}
  {tasks_html}
  <div style="margin-top:24px;padding-top:14px;border-top:1px solid #232730;color:#7a8089;font-size:12px">
    <a href="http://localhost:8765/app/dashboard" style="color:#5e9eff;text-decoration:none">Open the dashboard →</a>
  </div>
  <div style="margin-top:8px;color:#5a6068;font-size:11px">— Your Korpha cofounder</div>
</div>
</body></html>"""


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


__all__ = ["DigestSnapshot", "build_snapshot", "render_digest"]
