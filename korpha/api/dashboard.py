"""Server-rendered dashboard routes (Linear-inspired shell).

Each handler resolves the active Founder + Business via the same
``_founder_business`` helper the API endpoints use, queries whatever this
view needs, and renders a Jinja2 template that extends ``base.html``.

Cost is intentionally NOT a top-level left-nav item — it's reached via
the always-visible top-bar pill. Mike sees today's spend at a glance
without it being in his face.

NOTE: this module deliberately does NOT use ``from __future__ import
annotations``. FastAPI uses runtime type introspection to detect
``Depends`` markers; future-stringified annotations break dependency
injection silently (same caveat as server.py).
"""
import contextlib
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from korpha.approvals.model import Approval, ApprovalStatus
from korpha.audit.model import Activity, Cost, InferenceTier
from korpha.business.issues import find_task_by_ref, format_ref
from korpha.business.model import Business, Goal, Task, TaskStatus
from korpha.cofounder.model import (
    AgentRole,
    Message,
    Thread,
    ThreadPlatform,
    ThreadStatus,
)
from korpha.db._base import as_utc, utcnow
from korpha.heartbeats.model import Routine
from korpha.identity.model import Founder
from korpha.inference.cost_tracker import CostTracker
from korpha.skills import (
    SkillContext,
    SkillError,
)
from korpha.skills import (
    default_registry as skills_registry,
)

# Sonnet-baseline pricing used for the "saved vs Claude Sonnet" pill copy.
# Sonnet 4.6 list prices ($/Mtok): input 3.00, output 15.00.
_SONNET_INPUT_PER_1M = Decimal("3.00")
_SONNET_OUTPUT_PER_1M = Decimal("15.00")


_TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


# Last autonomy tick result per business — populated by the
# /app/autonomy/tick handler so the panel can render
# "Last tick: fired X" without persisting a new table. Lost on
# process restart, which is fine — it's a UX nicety, not a record.
_LAST_AUTONOMY_TICK: dict[Any, dict[str, Any]] = {}


def _agent_created_root() -> Path:
    """Where authored skills live on disk. Honors KORPHA_SKILLS_DIR
    so tests can point at tmp_path."""
    import os
    env = os.getenv("KORPHA_SKILLS_DIR")
    base = Path(env) if env else Path.home() / ".korpha" / "skills"
    return base / "agent_created"


def _enumerate_authored_skills() -> list[dict[str, Any]]:
    """List every skill the agent authored, both YAML and Python.

    YAML skills land at ``agent_created/<name_with_underscores>/manifest.yaml``;
    Python skills at ``agent_created/python/<name_with_underscores>/skill.py``.
    Returns a stable-sorted list of dicts keyed for the template:

        {kind: "yaml" | "python",
         slug: "<dir_name>",
         name: "<dotted skill name>",
         description: "<from manifest>",
         dir: Path,
         primary_file: Path,
         size_bytes: int,
         mtime: datetime}
    """
    import yaml
    from datetime import UTC, datetime as _dt
    root = _agent_created_root()
    out: list[dict[str, Any]] = []
    if not root.exists():
        return out
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if child.name == "python":
            for py_dir in sorted(child.iterdir()):
                if not py_dir.is_dir():
                    continue
                skill_py = py_dir / "skill.py"
                if not skill_py.is_file():
                    continue
                manifest_data: dict[str, Any] = {}
                manifest_path = py_dir / "manifest.yaml"
                if manifest_path.exists():
                    try:
                        manifest_data = yaml.safe_load(
                            manifest_path.read_text(encoding="utf-8")
                        ) or {}
                        if not isinstance(manifest_data, dict):
                            manifest_data = {}
                    except yaml.YAMLError:
                        manifest_data = {}
                stat = skill_py.stat()
                out.append({
                    "kind": "python",
                    "slug": py_dir.name,
                    "name": str(
                        manifest_data.get("name") or
                        py_dir.name.replace("__", ".")
                    ),
                    "description": str(
                        manifest_data.get("description") or
                        "(no description)"
                    ),
                    "dir": py_dir,
                    "primary_file": skill_py,
                    "size_bytes": stat.st_size,
                    "mtime": _dt.fromtimestamp(stat.st_mtime, tz=UTC),
                })
            continue
        # YAML path: agent_created/<name>/manifest.yaml
        manifest_path = child / "manifest.yaml"
        if not manifest_path.is_file():
            continue
        try:
            manifest_data = yaml.safe_load(
                manifest_path.read_text(encoding="utf-8")
            ) or {}
            if not isinstance(manifest_data, dict):
                manifest_data = {}
        except yaml.YAMLError:
            manifest_data = {}
        stat = manifest_path.stat()
        out.append({
            "kind": "yaml",
            "slug": child.name,
            "name": str(
                manifest_data.get("name") or
                child.name.replace("__", ".")
            ),
            "description": str(
                manifest_data.get("description") or
                "(no description)"
            ),
            "dir": child,
            "primary_file": manifest_path,
            "size_bytes": stat.st_size,
            "mtime": _dt.fromtimestamp(stat.st_mtime, tz=UTC),
        })
    return out


def _find_authored_skill(kind: str, slug: str) -> dict[str, Any] | None:
    """Resolve (kind, slug) to a single authored-skill record.

    Returns None when the skill doesn't exist OR when ``slug`` would
    escape the agent_created root via path traversal (".." / absolute
    path). The caller redirects on None — never raises.

    The slug is the on-disk directory name (underscored), not the
    dotted skill name. We compare child paths to the resolved root
    so symlinks / relative segments don't sneak past.
    """
    if "/" in slug or ".." in slug or slug.startswith("."):
        return None
    if kind not in ("yaml", "python"):
        return None
    for entry in _enumerate_authored_skills():
        if entry["kind"] == kind and entry["slug"] == slug:
            try:
                resolved = entry["dir"].resolve()
                root = _agent_created_root().resolve()
                resolved.relative_to(root)
            except (ValueError, OSError):
                return None
            return entry
    return None


def _human_bytes(n: int) -> str:
    """Compact byte-formatter for /app/disk + flash messages."""
    if n < 0:
        n = 0
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(n)
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024
        idx += 1
    if idx == 0:
        return f"{int(size)} {units[idx]}"
    return f"{size:.1f} {units[idx]}"


def _needs_onboarding(business: Business) -> bool:
    """True when Day-0 intake hasn't run for this business yet.

    We treat "no goal recorded" as the gate rather than "brief dict is
    empty", because some legacy callers may have stuck a placeholder in
    there. The goal field is the load-bearing one downstream skills read.

    Founders who skipped the brief form (POST /app/onboard/skip) carry
    a ``skipped_intake`` marker so the dashboard gate doesn't bounce
    them back into the form on every page load. They're free to chat
    directly with the CEO — typical use case: pasting a roster of
    multiple ideas the CEO bundles into Lines via hr.start_business_line.
    """
    brief = business.founder_brief or {}
    if brief.get("skipped_intake"):
        return False
    return not str(brief.get("goal") or "").strip()


# Placeholder sentinel matching cli._BOOTSTRAP_PLACEHOLDER_EMAIL. Kept
# in sync by hand — the alternative is a config import that pulls all
# of typer into the dashboard module, which isn't worth it for two
# string constants.
_BOOTSTRAP_PLACEHOLDER_EMAIL = "founder@localhost.invalid"


def _needs_identity_setup(founder: Founder, business: Business) -> bool:
    """True when `korpha server` auto-bootstrapped the DB but the
    Founder hasn't filled in their real identity yet via the web
    wizard. We gate on the placeholder email — once it's a real
    address we trust the user finished the welcome step.

    Empty email also counts: an upgrade path where someone wiped
    their identity but didn't set anything new lands here too."""
    email = (founder.email or "").strip().lower()
    return not email or email == _BOOTSTRAP_PLACEHOLDER_EMAIL


def build_dashboard_router(
    require_session: Callable[[], Session],
    founder_business: Callable[[Session], tuple[Founder, Business]],
    cost_tracker_factory: Callable[[], CostTracker] | None = None,
    engine_factory: Callable[[], Any] | None = None,
) -> APIRouter:
    """Construct the dashboard router. The two dependency functions are
    handed in so this module can stay decoupled from server.py's closures.

    ``cost_tracker_factory`` is needed for routes that run skills (Day-0
    intake). Passed as a factory so each request gets its own tracker
    backed by the request-scoped session in the closure.

    ``engine_factory`` is needed for background tasks that have to open
    their own session (the request session is closed by the time the
    task runs). Currently used by the post-pick-niche skill chain.
    """
    router = APIRouter(include_in_schema=False)

    def _theme_context() -> dict[str, Any]:
        """Resolve the active theme into CSS-var + font-link strings
        the base template injects into <head>. Failures fall back to
        the default theme — a broken user YAML must never blank-page
        the dashboard."""
        from korpha.themes import (
            BUILTIN_THEMES,
            DEFAULT_THEME,
            DashboardThemesError,
            get_active_theme_name,
            list_themes,
            load_theme_by_name,
        )
        from korpha.themes.css import render_font_link, render_theme_css_vars

        active_name = get_active_theme_name()
        try:
            theme = load_theme_by_name(active_name)
        except DashboardThemesError:
            theme = DEFAULT_THEME
        return {
            "active_theme": theme,
            "active_theme_name": theme.name,
            "theme_css_vars": render_theme_css_vars(theme),
            "theme_font_link": render_font_link(theme),
            "available_themes": list_themes(),
            "_theme_builtin_names": set(BUILTIN_THEMES.keys()),
        }

    def _ctx(session: Session, **extra: Any) -> dict[str, Any]:
        founder, business = founder_business(session)
        agents = session.exec(
            select(AgentRole)
            .where(AgentRole.business_id == business.id)
            .where(AgentRole.is_active.is_(True))  # type: ignore[attr-defined]
        ).all()
        return {
            "founder": founder,
            "business": business,
            "agents": list(agents),
            "live_agents": 0,  # filled in once heartbeat tracking lands
            **_theme_context(),
            **extra,
        }

    @router.get("/dashboard", response_class=HTMLResponse, response_model=None)
    def dashboard(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse | RedirectResponse:
        try:
            ctx = _ctx(session)
            biz = ctx["business"]
            founder = ctx["founder"]
            # First-first-run gate: `korpha server` auto-bootstrapped
            # with placeholder identity, but the founder hasn't filled
            # in their real name/email/business yet via the welcome
            # form. Send them there before the brief textarea.
            if _needs_identity_setup(founder, biz):
                return RedirectResponse(
                    "/app/welcome", status_code=status.HTTP_303_SEE_OTHER
                )
            # First-run gate: if Day-0 intake hasn't been captured yet
            # the dashboard would just be empty noise. Send the Founder
            # to the upfront chooser ("I have ideas" vs "discover with
            # me"). The chooser is responsible for routing them on.
            if _needs_onboarding(biz):
                return RedirectResponse(
                    "/app/start", status_code=status.HTTP_303_SEE_OTHER
                )
            ctx["all_agents"] = _agents_with_status(session, biz, ctx["agents"])
            ctx["kpis"] = _compute_kpis(session, biz.id)
            ctx["recent_events"] = _recent_events(session, biz.id, limit=6)
            ctx["recent_tasks"] = _recent_tasks(session, biz, ctx["agents"], limit=6)
            ctx["charts"] = _compute_charts(session, biz.id)
            ctx["active"] = "dashboard"
            # First-day banner: count pending approvals produced by the
            # post-pick-niche chain. Hidden once the Founder has acted on
            # any of them so we don't keep nagging.
            chain_count, has_stripe = _first_day_chain_summary(session, biz.id)
            ctx["first_day_chain_count"] = chain_count
            ctx["first_day_has_stripe"] = has_stripe
            return templates.TemplateResponse(request, "dashboard.html", ctx)
        finally:
            session.close()

    @router.get("/welcome", response_class=HTMLResponse, response_model=None)
    def welcome_form(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse | RedirectResponse:
        """First-first-run identity capture. Shown automatically when
        `korpha server` auto-bootstrapped the DB with placeholder
        identity — once the founder enters a real email we redirect
        to the brief screen. Manual revisits after setup land on
        /app/settings instead."""
        try:
            ctx = _ctx(session, active="welcome")
            founder = ctx["founder"]
            biz = ctx["business"]
            if not _needs_identity_setup(founder, biz):
                # Already set up — don't show the wizard a second time.
                return RedirectResponse(
                    "/app/dashboard", status_code=status.HTTP_303_SEE_OTHER
                )
            ctx["existing_name"] = founder.display_name or ""
            ctx["existing_email"] = (
                "" if (founder.email or "").lower() == _BOOTSTRAP_PLACEHOLDER_EMAIL
                else (founder.email or "")
            )
            ctx["existing_business"] = (
                "" if biz.name == "My Business" else biz.name
            )
            return templates.TemplateResponse(request, "welcome.html", ctx)
        finally:
            session.close()

    @router.get(
        "/restore-from-cloud",
        response_class=HTMLResponse,
        response_model=None,
    )
    def restore_from_cloud_form(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse | RedirectResponse:
        """Form for the fresh-install / rebuilding scenario: founder
        has B2 (or R2/S3/MinIO) credentials, lost their old machine,
        wants to pull their data back into this install. Different
        from /app/backups/offdisk/configure because this runs BEFORE
        off-disk is set up, and chains configure + restore in one
        click."""
        try:
            ctx = _ctx(session, active="restore")
            if _needs_identity_setup(ctx["founder"], ctx["business"]):
                return RedirectResponse(
                    "/app/welcome", status_code=status.HTTP_303_SEE_OTHER
                )
            # Pre-populate from existing off-disk config if Mike got
            # this far before (maybe he configured backup, then wants
            # to test restore without retyping creds).
            from korpha.backup.offdisk import current_status
            existing = current_status() or {}
            ctx["existing"] = existing
            ctx["error"] = request.query_params.get("error") or ""
            return templates.TemplateResponse(
                request, "restore_from_cloud.html", ctx,
            )
        finally:
            session.close()

    @router.post(
        "/restore-from-cloud",
        response_class=HTMLResponse,
        response_model=None,
    )
    def restore_from_cloud_submit(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        provider: Annotated[str, Form()] = "",
        bucket: Annotated[str, Form()] = "",
        region: Annotated[str, Form()] = "",
        account_id: Annotated[str, Form()] = "",
        endpoint_override: Annotated[str, Form()] = "",
        access_key_id: Annotated[str, Form()] = "",
        secret_access_key: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        """Two-step in one handler: configure off-disk with the
        provided creds (so future syncs work too), then pull the
        latest snapshot + WAL down via litestream restore."""
        try:
            from korpha.backup.install import (
                install_litestream, litestream_path,
            )
            from korpha.backup.offdisk import (
                configure_offdisk, restore_from_offdisk,
                verify_credentials,
            )

            try:
                cfg = configure_offdisk(
                    provider=provider,
                    bucket=bucket,
                    region=region,
                    access_key_id=access_key_id,
                    secret_access_key=secret_access_key,
                    account_id=account_id or None,
                    endpoint_override=endpoint_override or None,
                )
            except ValueError as exc:
                return RedirectResponse(
                    f"/app/restore-from-cloud?error={str(exc)[:120]}",
                    status_code=status.HTTP_303_SEE_OTHER,
                )

            # Optional creds check — non-fatal if awscli isn't on PATH;
            # litestream will surface its own errors otherwise.
            ok, vmsg = verify_credentials(
                cfg,
                access_key_id=access_key_id,
                secret_access_key=secret_access_key,
            )
            if not ok:
                return RedirectResponse(
                    f"/app/restore-from-cloud?error="
                    f"Bucket+creds+rejected:+{vmsg[:80]}",
                    status_code=status.HTTP_303_SEE_OTHER,
                )

            # Install litestream if it's not on PATH — same one-click
            # path the dashboard backup setup uses.
            if litestream_path() is None:
                inst_res = install_litestream()
                if not inst_res.ok:
                    return RedirectResponse(
                        f"/app/restore-from-cloud?error="
                        f"litestream+install+failed:+{inst_res.message[:80]}",
                        status_code=status.HTTP_303_SEE_OTHER,
                    )

            ok, msg = restore_from_offdisk(cfg)
            if not ok:
                return RedirectResponse(
                    f"/app/restore-from-cloud?error={msg[:120]}",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            return RedirectResponse(
                f"/app/backups?flash=Restored+from+{bucket}+—+"
                f"restart+the+server+now+to+load+your+recovered+data."
                f"+API+keys+(Stripe/Resend/etc)+need+re-entry+from+each+"
                f"provider's+dashboard.",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        finally:
            session.close()

    @router.get("/start", response_class=HTMLResponse, response_model=None)
    def start_chooser(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse | RedirectResponse:
        """Upfront fork between the two onboarding paths:
        - "I have ideas already" → POST /app/onboard/skip → /app/chat
          (paste your roster, CEO bundles into Lines + spawns VPs)
        - "Discover with me" → /app/onboard (brief textarea + niche
          discovery + pick one)
        Identity must be captured first (the welcome step)."""
        try:
            ctx = _ctx(session, active="start")
            if _needs_identity_setup(ctx["founder"], ctx["business"]):
                return RedirectResponse(
                    "/app/welcome", status_code=status.HTTP_303_SEE_OTHER
                )
            # Already past intake — don't gate them again.
            biz = ctx["business"]
            brief = biz.founder_brief or {}
            if brief.get("goal") or brief.get("skipped_intake"):
                return RedirectResponse(
                    "/app/dashboard", status_code=status.HTTP_303_SEE_OTHER
                )
            return templates.TemplateResponse(request, "start.html", ctx)
        finally:
            session.close()

    @router.post("/welcome", response_class=HTMLResponse, response_model=None)
    def welcome_submit(
        request: Request,
        founder_name: Annotated[str, Form()] = "",
        founder_email: Annotated[str, Form()] = "",
        business_name: Annotated[str, Form()] = "",
        session: Annotated[Session, Depends(require_session)] = None,  # type: ignore[assignment]
    ) -> HTMLResponse | RedirectResponse:
        """Capture real founder identity. Validates email looks
        plausible; re-renders the form with an error otherwise so
        we don't silently accept a typo."""
        try:
            founder, business = founder_business(session)
            n = founder_name.strip()
            e = founder_email.strip()
            b = business_name.strip()

            def _render_error(msg: str) -> HTMLResponse:
                ctx = _ctx(session, active="welcome")
                ctx["existing_name"] = n
                ctx["existing_email"] = e
                ctx["existing_business"] = b
                ctx["error"] = msg
                return templates.TemplateResponse(
                    request, "welcome.html", ctx,
                )

            if not n:
                return _render_error("Your name is required.")
            if not e or "@" not in e or "." not in e.split("@", 1)[-1]:
                return _render_error(
                    "That doesn't look like a valid email address."
                )
            if not b:
                return _render_error("Your business name is required.")

            founder.display_name = n
            founder.email = e
            business.name = b
            # Update the root DEFAULT BusinessUnit name to match so
            # the org tree label tracks the business.
            from korpha.business_units.model import BusinessUnit as _BU
            root = session.exec(
                select(_BU)
                .where(_BU.business_id == business.id)
                .where(_BU.parent_id.is_(None))  # type: ignore[attr-defined]
            ).first()
            if root is not None:
                root.name = b
                session.add(root)
            session.add(founder)
            session.add(business)
            session.commit()
            return RedirectResponse(
                "/app/start", status_code=status.HTTP_303_SEE_OTHER
            )
        finally:
            session.close()

    @router.get("/onboard", response_class=HTMLResponse, response_model=None)
    def onboard_form(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse | RedirectResponse:
        """Day-0 intake screen. Shown automatically when business.founder_brief
        is empty; reachable manually from /app/settings to re-do the brief."""
        try:
            ctx = _ctx(session, active="onboard")
            # If the founder hit /onboard directly without finishing
            # the identity step, bounce them.
            if _needs_identity_setup(ctx["founder"], ctx["business"]):
                return RedirectResponse(
                    "/app/welcome", status_code=status.HTTP_303_SEE_OTHER
                )
            existing = ctx["business"].founder_brief or {}
            ctx["existing_brief"] = existing
            ctx["existing_answer"] = existing.get("raw_answer", "")
            return templates.TemplateResponse(request, "onboard.html", ctx)
        finally:
            session.close()

    @router.post("/onboard/skip", response_class=HTMLResponse, response_model=None)
    def onboard_skip(
        session: Annotated[Session, Depends(require_session)],
    ) -> RedirectResponse:
        """Skip the brief textarea + niche-discovery autoflow. Used by
        founders who already have a roster of ideas to paste at the CEO
        (Paperclip-style flow). Marks the business with skipped_intake
        so the dashboard stops bouncing back here, then drops them in
        chat where the CEO can call hr.start_business_line per Line."""
        try:
            _founder, business = founder_business(session)
            current = business.founder_brief or {}
            business.founder_brief = {
                **current,
                "skipped_intake": True,
            }
            session.add(business)
            session.commit()
            return RedirectResponse(
                "/app/chat", status_code=status.HTTP_303_SEE_OTHER
            )
        finally:
            session.close()

    @router.post("/onboard", response_class=HTMLResponse, response_model=None)
    async def onboard_submit(
        request: Request,
        answer: Annotated[str, Form(...)],
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse | RedirectResponse:
        """Run founder.intake_brief, persist, redirect to dashboard.

        On LLM error or unparseable response we re-render the form with
        the error message so the Founder can retry — much better than a
        500 page on the conversion-critical first screen."""
        try:
            answer = answer.strip()
            if not answer:
                ctx = _ctx(session, active="onboard")
                ctx["existing_answer"] = ""
                ctx["error"] = "Tell us what you want — even a rough sentence."
                return templates.TemplateResponse(request, "onboard.html", ctx)

            if cost_tracker_factory is None:
                ctx = _ctx(session, active="onboard")
                ctx["existing_answer"] = answer
                ctx["error"] = (
                    "No LLM provider configured. Set OLLAMA_CLOUD_API_KEY "
                    "(or another provider) and reload."
                )
                return templates.TemplateResponse(request, "onboard.html", ctx)

            founder, business = founder_business(session)
            try:
                tracker = cost_tracker_factory()
            except HTTPException as exc:
                # The factory raises 503 when no provider is configured.
                # On the conversion-critical first screen we'd rather
                # re-render the form with the actionable message than
                # surface a raw 503 page.
                ctx = _ctx(session, active="onboard")
                ctx["existing_answer"] = answer
                ctx["error"] = (
                    "No LLM provider configured. Set OLLAMA_CLOUD_API_KEY "
                    f"(or another provider) and reload. [{exc.detail}]"
                )
                return templates.TemplateResponse(request, "onboard.html", ctx)
            skill_ctx = SkillContext(
                business=business,
                founder=founder,
                session=session,
                cost_tracker=tracker,
            )
            try:
                await skills_registry.run(
                    "founder.intake_brief",
                    ctx=skill_ctx,
                    args={"answer": answer},
                )
            except SkillError as exc:
                ctx = _ctx(session, active="onboard")
                ctx["existing_answer"] = answer
                ctx["error"] = (
                    "We couldn't structure that into a brief. Try rephrasing "
                    f"with concrete numbers (goal, hours/week, savings). "
                    f"[{exc}]"
                )
                return templates.TemplateResponse(request, "onboard.html", ctx)

            # Take the Founder to the "captured + working" page that auto-
            # triggers niche discovery. Skipping straight to the dashboard
            # would lose the BRIEF.md "0:30 — visible work happening" beat.
            return RedirectResponse(
                "/app/onboard/done", status_code=status.HTTP_303_SEE_OTHER
            )
        finally:
            session.close()

    @router.get("/onboard/done", response_class=HTMLResponse, response_model=None)
    def onboard_done(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse | RedirectResponse:
        """Step 2 of intake: brief is captured. Auto-triggers niche
        discovery via HTMX so the Founder watches their cofounder think
        instead of staring at a 'thanks' page."""
        try:
            _founder, business = founder_business(session)
            if _needs_onboarding(business):
                # Direct hit on /onboard/done before submitting brief —
                # bounce them to step 1.
                return RedirectResponse(
                    "/app/onboard", status_code=status.HTTP_303_SEE_OTHER
                )
            ctx = _ctx(session, active="onboard")
            ctx["brief"] = business.founder_brief or {}
            return templates.TemplateResponse(request, "onboard_done.html", ctx)
        finally:
            session.close()

    @router.get("/onboard/niche-fragment", response_class=HTMLResponse)
    async def onboard_niche_fragment(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse:
        """HTMX target. Runs niche.find_micro_niches with founder_brief
        defaults, returns an HTML fragment with the niche cards. Errors
        come back as a small inline alert so the user can retry without
        leaving the onboard flow."""
        try:
            founder, business = founder_business(session)
            ctx_extra = {
                "brief": business.founder_brief or {},
                "candidates": [],
                "recommended_index": 0,
                "rationale": "",
                "error": None,
            }
            if cost_tracker_factory is None:
                ctx_extra["error"] = "No LLM provider configured."
                return templates.TemplateResponse(
                    request,
                    "_onboard_niches.html",
                    {"request": request, **ctx_extra},
                )
            try:
                tracker = cost_tracker_factory()
            except HTTPException as exc:
                ctx_extra["error"] = (
                    f"No LLM provider configured. [{exc.detail}]"
                )
                return templates.TemplateResponse(
                    request,
                    "_onboard_niches.html",
                    {"request": request, **ctx_extra},
                )
            skill_ctx = SkillContext(
                business=business,
                founder=founder,
                session=session,
                cost_tracker=tracker,
            )
            try:
                result = await skills_registry.run(
                    "niche.find_micro_niches", ctx=skill_ctx, args={}
                )
            except SkillError as exc:
                ctx_extra["error"] = f"Niche discovery failed: {exc}"
                return templates.TemplateResponse(
                    request,
                    "_onboard_niches.html",
                    {"request": request, **ctx_extra},
                )
            payload = result.payload
            ctx_extra["candidates"] = payload.get("candidates") or []
            ctx_extra["recommended_index"] = int(
                payload.get("recommended_index") or 0
            )
            ctx_extra["rationale"] = str(payload.get("rationale") or "")
            return templates.TemplateResponse(
                request,
                "_onboard_niches.html",
                {"request": request, **ctx_extra},
            )
        finally:
            session.close()

    @router.post("/onboard/pick-niche", response_class=HTMLResponse, response_model=None)
    def onboard_pick_niche(
        request: Request,
        background_tasks: BackgroundTasks,
        session: Annotated[Session, Depends(require_session)],
        name: Annotated[str, Form()] = "",
        value_prop: Annotated[str, Form()] = "",
        validation_experiment: Annotated[str, Form()] = "",
        target_avatar: Annotated[str, Form()] = "",
        price_band: Annotated[str, Form()] = "",
        line_kind: Annotated[str, Form()] = "",
    ) -> HTMLResponse | RedirectResponse:
        """Founder picks one of the proposed niches. Creates a Goal,
        seeds the first validation Task, and moves the Business out of
        IDEA. The seeded Task means the dashboard has real content from
        minute one — no "your inbox is empty" wasteland."""
        try:
            _founder, business = founder_business(session)
            name = name.strip()
            if not name:
                # Bad request — bounce them back to the proposal page.
                return RedirectResponse(
                    "/app/onboard/done", status_code=status.HTTP_303_SEE_OTHER
                )
            from korpha.business.issues import allocate_task_ref
            from korpha.business.model import (
                BusinessStatus,
                Goal,
                GoalStatus,
                Task,
                TaskPriority,
                TaskStatus,
            )

            goal = Goal(
                business_id=business.id,
                title=f"Validate: {name}",
                description=value_prop.strip() or None,
                status=GoalStatus.ACTIVE,
            )
            session.add(goal)

            experiment = validation_experiment.strip()
            if experiment:
                task = Task(
                    business_id=business.id,
                    title=f"Run: {experiment}",
                    description=(
                        f"First validation experiment for the {name!r} "
                        "niche. Auto-seeded from the Day-0 onboarding "
                        "flow — your cofounder picked this as the "
                        "fastest way to learn whether the niche has legs."
                    ),
                    status=TaskStatus.PENDING,
                    priority=TaskPriority.NORMAL,
                    ref_number=allocate_task_ref(session, business.id),
                )
                session.add(task)

            if business.status == BusinessStatus.IDEA:
                business.status = BusinessStatus.VALIDATING
                session.add(business)
            session.commit()

            # Fan out: validate / landing / outreach skills run in the
            # background, each producing a pending Approval. By the time
            # the Founder is done admiring the dashboard, the queue has
            # the BRIEF.md "deliverables already drafted" beat.
            if (
                cost_tracker_factory is not None
                and engine_factory is not None
            ):
                from korpha.onboarding import run_post_pick_niche_chain

                niche_payload: dict[str, Any] = {
                    "name": name,
                    "value_prop": value_prop.strip(),
                    "target_avatar": target_avatar.strip(),
                    "validation_experiment": experiment,
                    "price_band": price_band.strip(),
                }
                _engine = engine_factory()
                # Normalize line_kind. The niche skill emits one of
                # pod/kdp/info/saas/affiliate/agency per candidate;
                # anything else (or empty) means "stay in DEFAULT" and
                # the chain will skip the Line spawn.
                _valid_lines = {
                    "pod", "kdp", "info", "saas", "affiliate", "agency",
                }
                _line = line_kind.strip().lower()
                _line = _line if _line in _valid_lines else None
                background_tasks.add_task(
                    run_post_pick_niche_chain,
                    engine=_engine,
                    business_id=business.id,
                    niche=niche_payload,
                    cost_tracker_factory=cost_tracker_factory,
                    line_kind=_line,
                )
            return RedirectResponse(
                "/app/dashboard", status_code=status.HTTP_303_SEE_OTHER
            )
        finally:
            session.close()

    @router.get("/inbox", response_class=HTMLResponse)
    def inbox(request: Request, session: Annotated[Session, Depends(require_session)]) -> HTMLResponse:
        try:
            ctx = _ctx(session, active="inbox")
            return templates.TemplateResponse(request, "inbox.html", ctx)
        finally:
            session.close()

    @router.post("/chat/new", response_class=HTMLResponse, response_model=None)
    def chat_new(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> RedirectResponse:
        """Archive the current web thread + return to a fresh chat.

        We close the existing ACTIVE web threads (status → closed) so
        ``/app/chat`` loads with no history and the empty-state CTAs
        appear again. Old messages are NOT deleted — they're still
        searchable via memory and visible in /app/activity.
        """
        try:
            founder, business = founder_business(session)
            web_threads = list(
                session.exec(
                    select(Thread)
                    .where(Thread.business_id == business.id)
                    .where(Thread.founder_id == founder.id)
                    .where(Thread.platform == ThreadPlatform.WEB)
                    .where(Thread.status == ThreadStatus.ACTIVE)
                ).all()
            )
            for t in web_threads:
                t.status = ThreadStatus.CLOSED
                session.add(t)
            session.commit()
            return RedirectResponse(
                "/app/chat", status_code=status.HTTP_303_SEE_OTHER
            )
        finally:
            session.close()

    def _load_thread_messages(
        session: Session, thread_ids: list[Any], limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Project messages from a set of threads to template-ready dicts.

        We don't return SQLModel instances because Pydantic rejects
        ``setattr`` of unknown fields (we'd need that for role title
        denormalization). Plain dicts also keep the template branch-free.
        """
        if not thread_ids:
            return []
        msg_rows = list(
            session.exec(
                select(Message)
                .where(Message.thread_id.in_(thread_ids))  # type: ignore[attr-defined]
                .order_by(Message.created_at)
            ).all()
        )
        role_titles: dict[Any, str] = {}
        out: list[dict[str, Any]] = []
        for m in msg_rows[-limit:]:
            if m.sender_role_id and m.sender_role_id not in role_titles:
                role = session.get(AgentRole, m.sender_role_id)
                role_titles[m.sender_role_id] = (
                    role.title if role else "Cofounder"
                )
            title = (
                role_titles.get(m.sender_role_id) if m.sender_role_id
                else "Cofounder"
            )
            out.append({
                "sender_type_value": (
                    m.sender_type.value if m.sender_type else "system"
                ),
                "sender_role_title": title or "Cofounder",
                "content": m.content,
                "created_at": as_utc(m.created_at) or m.created_at,
            })
        return out

    def _founder_web_threads(
        session: Session, business_id: Any, founder_id: Any,
        *, status_in: list[ThreadStatus] | None = None,
    ) -> list[Thread]:
        stmt = (
            select(Thread)
            .where(Thread.business_id == business_id)
            .where(Thread.founder_id == founder_id)
            .where(Thread.platform == ThreadPlatform.WEB)
            .order_by(Thread.last_message_at.desc())  # type: ignore[attr-defined]
        )
        if status_in is not None:
            stmt = stmt.where(Thread.status.in_(status_in))  # type: ignore[attr-defined]
        return list(session.exec(stmt).all())

    @router.get("/chat", response_class=HTMLResponse)
    def chat(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse:
        """Founder ↔ cofounder chat — loads the active web thread.

        Submission goes to /ask/stream (SSE); the template's inline JS
        handles the streaming render. When there are no active threads
        the empty-state CTAs render so the user always has a clear next
        step.
        """
        try:
            ctx = _ctx(session, active="chat")
            founder, business = founder_business(session)
            web_threads = _founder_web_threads(
                session, business.id, founder.id,
                status_in=[ThreadStatus.ACTIVE],
            )
            messages = _load_thread_messages(
                session, [t.id for t in web_threads]
            )
            ctx["messages"] = messages
            ctx["thread_id"] = None  # current active — /ask/stream picks it
            ctx["is_archived"] = False
            ctx["has_history"] = bool(_founder_web_threads(
                session, business.id, founder.id,
                status_in=[ThreadStatus.CLOSED],
            ))
            biz_created = as_utc(business.created_at)
            ctx["day_number"] = (
                max(0, (utcnow() - biz_created).days)
                if biz_created else 0
            )
            return templates.TemplateResponse(request, "chat.html", ctx)
        finally:
            session.close()

    @router.get("/chat/history", response_class=HTMLResponse)
    def chat_history(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse:
        """List every past web conversation so the founder can revisit
        anything they discussed before."""
        try:
            ctx = _ctx(session, active="chat-history")
            founder, business = founder_business(session)
            threads = _founder_web_threads(
                session, business.id, founder.id, status_in=None,
            )
            # Build a preview per thread: first founder message + last
            # exchange timestamp + total turn count.
            previews: list[dict[str, Any]] = []
            for t in threads:
                msg_count_row = session.exec(
                    select(Message).where(Message.thread_id == t.id)
                ).all()
                msg_count = len(msg_count_row)
                first_msg = next(
                    (
                        m for m in sorted(
                            msg_count_row, key=lambda x: x.created_at
                        )
                        if m.sender_type
                        and m.sender_type.value == "founder"
                    ),
                    None,
                )
                preview_text = (
                    first_msg.content if first_msg else
                    "(no founder messages)"
                )
                if len(preview_text) > 140:
                    preview_text = preview_text[:137].rstrip() + "…"
                last_msg_at = as_utc(t.last_message_at) or t.last_message_at
                previews.append({
                    "id": str(t.id),
                    "topic": t.topic or "Untitled",
                    "status": t.status.value,
                    "is_active": t.status == ThreadStatus.ACTIVE,
                    "preview": preview_text,
                    "msg_count": msg_count,
                    "last_message_at": last_msg_at,
                })
            ctx["thread_previews"] = previews
            return templates.TemplateResponse(
                request, "chat_history.html", ctx
            )
        finally:
            session.close()

    @router.get(
        "/chat/{thread_id}", response_class=HTMLResponse, response_model=None,
    )
    def chat_thread(
        thread_id: UUID,
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse | RedirectResponse:
        """View a specific past chat thread. Closed threads render in
        archived mode (no input until resumed); active threads behave
        like /app/chat."""
        try:
            ctx = _ctx(session, active="chat")
            founder, business = founder_business(session)
            thread = session.get(Thread, thread_id)
            # Authorize: thread must belong to this founder + business.
            if (
                thread is None
                or thread.business_id != business.id
                or thread.founder_id != founder.id
            ):
                return RedirectResponse(
                    "/app/chat/history",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            messages = _load_thread_messages(session, [thread.id], limit=200)
            ctx["messages"] = messages
            ctx["thread_id"] = str(thread.id)
            ctx["is_archived"] = thread.status == ThreadStatus.CLOSED
            ctx["thread_topic"] = thread.topic or "Untitled chat"
            ctx["has_history"] = True
            biz_created = as_utc(business.created_at)
            ctx["day_number"] = (
                max(0, (utcnow() - biz_created).days)
                if biz_created else 0
            )
            return templates.TemplateResponse(request, "chat.html", ctx)
        finally:
            session.close()

    @router.post(
        "/chat/{thread_id}/resume", response_class=HTMLResponse,
        response_model=None,
    )
    def chat_thread_resume(
        thread_id: UUID,
        session: Annotated[Session, Depends(require_session)],
    ) -> RedirectResponse:
        """Reopen an archived thread. Closes any other active web threads
        first so the founder always has at most one active conversation —
        the /ask/stream router would otherwise pick a stale one."""
        try:
            founder, business = founder_business(session)
            target = session.get(Thread, thread_id)
            if (
                target is None
                or target.business_id != business.id
                or target.founder_id != founder.id
            ):
                return RedirectResponse(
                    "/app/chat/history",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            # Close any currently-active web thread so resuming this one
            # doesn't create two actives.
            for other in _founder_web_threads(
                session, business.id, founder.id,
                status_in=[ThreadStatus.ACTIVE],
            ):
                if other.id != target.id:
                    other.status = ThreadStatus.CLOSED
                    session.add(other)
            target.status = ThreadStatus.ACTIVE
            session.add(target)
            session.commit()
            return RedirectResponse(
                "/app/chat", status_code=status.HTTP_303_SEE_OTHER
            )
        finally:
            session.close()

    @router.get("/issues", response_class=HTMLResponse)
    def issues(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        q: str | None = None,
        status: str | None = None,
        agent: str | None = None,
    ) -> HTMLResponse:
        try:
            ctx = _ctx(session, active="issues")
            issues_data, total = _list_issues(
                session,
                ctx["business"],
                ctx["agents"],
                q=q,
                status_filter=status,
                agent_filter=agent,
            )
            ctx["issues"] = issues_data
            ctx["total_issues"] = total
            ctx["status_options"] = [
                {"value": s.value, "label": s.value.replace("_", " ")}
                for s in TaskStatus
            ]
            ctx["query"] = {
                "q": q or "",
                "status": status or "",
                "agent": agent or "",
                "has_filters": bool(q or status or agent),
            }
            return templates.TemplateResponse(request, "issues.html", ctx)
        finally:
            session.close()

    @router.get("/issues/{ref}", response_class=HTMLResponse)
    def issue_detail(
        ref: str,
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse:
        try:
            ctx = _ctx(session, active="issues")
            task = find_task_by_ref(session, ctx["business"], ref)
            if task is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, f"issue {ref} not found")
            ctx["issue"] = _format_issue(task, ctx["business"], ctx["agents"], session)
            children_rows = list(
                session.exec(
                    select(Task)
                    .where(Task.business_id == ctx["business"].id)
                    .where(Task.parent_task_id == task.id)
                    .order_by(Task.created_at.asc())  # type: ignore[attr-defined]
                ).all()
            )
            ctx["children"] = [
                _format_issue_brief(c, ctx["business"]) for c in children_rows
            ]
            return templates.TemplateResponse(request, "issue_detail.html", ctx)
        finally:
            session.close()

    @router.get("/routines", response_class=HTMLResponse)
    def routines(
        request: Request, session: Annotated[Session, Depends(require_session)]
    ) -> HTMLResponse:
        try:
            ctx = _ctx(session, active="routines")
            rows = session.exec(
                select(Routine).where(Routine.business_id == ctx["business"].id)
            ).all()
            ctx["routines"] = [_format_routine(r) for r in rows]
            return templates.TemplateResponse(request, "routines.html", ctx)
        finally:
            session.close()

    @router.get("/goals", response_class=HTMLResponse)
    def goals(request: Request, session: Annotated[Session, Depends(require_session)]) -> HTMLResponse:
        try:
            ctx = _ctx(session, active="goals")
            rows = session.exec(
                select(Goal).where(Goal.business_id == ctx["business"].id)
            ).all()
            ctx["goals"] = [_format_goal(g) for g in rows]
            return templates.TemplateResponse(request, "goals.html", ctx)
        finally:
            session.close()

    @router.get("/agents", response_class=HTMLResponse)
    def agents(request: Request, session: Annotated[Session, Depends(require_session)]) -> HTMLResponse:
        try:
            ctx = _ctx(session, active="agents")
            ctx["all_agents"] = [_format_agent(a) for a in ctx["agents"]]
            return templates.TemplateResponse(request, "agents.html", ctx)
        finally:
            session.close()

    @router.get("/agents/{agent_id}", response_class=HTMLResponse)
    def agent_detail(
        agent_id: str,
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse:
        try:
            ctx = _ctx(session, active="agents", active_agent_id=agent_id)
            biz = ctx["business"]
            try:
                aid = UUID(agent_id)
            except ValueError as exc:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found") from exc
            agent_row = session.get(AgentRole, aid)
            if agent_row is None or agent_row.business_id != biz.id:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")

            # Live status: running iff this agent has produced an Activity
            # event in the last LIVE_WINDOW. Task assignment alone doesn't
            # count — an idle agent with a queued task is still idle.
            threshold = utcnow() - _LIVE_WINDOW
            recent_activity = session.exec(
                select(Activity)
                .where(Activity.business_id == biz.id)
                .where(Activity.actor_id == agent_row.id)
                .where(Activity.created_at >= threshold)
                .limit(1)
            ).first()
            current_task = session.exec(
                select(Task)
                .where(Task.business_id == biz.id)
                .where(Task.assigned_to_role_id == agent_row.id)
                .where(Task.status == TaskStatus.IN_PROGRESS)
                .order_by(Task.updated_at.desc())  # type: ignore[attr-defined]
                .limit(1)
            ).first()

            ctx["agent"] = _format_agent(agent_row)
            ctx["agent"]["status_label"] = (
                "running" if recent_activity else "idle"
            )
            ctx["agent"]["status_class"] = (
                "running" if recent_activity else "done"
            )
            ctx["agent"]["current_task"] = (
                {
                    "ref": format_ref(biz, current_task.ref_number),
                    "title": current_task.title,
                }
                if current_task
                else None
            )

            # Recent issues assigned to this agent.
            assigned = list(
                session.exec(
                    select(Task)
                    .where(Task.business_id == biz.id)
                    .where(Task.assigned_to_role_id == agent_row.id)
                    .order_by(Task.updated_at.desc())  # type: ignore[attr-defined]
                    .limit(8)
                ).all()
            )
            ctx["assigned_issues"] = [
                _format_issue_brief(t, biz, ctx["agents"]) for t in assigned
            ]

            # Activity events attributed to this agent.
            agent_activity = list(
                session.exec(
                    select(Activity)
                    .where(Activity.business_id == biz.id)
                    .where(Activity.actor_id == agent_row.id)
                    .order_by(Activity.created_at.desc())  # type: ignore[attr-defined]
                    .limit(12)
                ).all()
            )
            ctx["agent_events"] = [
                _format_activity(e, session) for e in agent_activity
            ]

            # Costs scoped to this agent.
            ctx["agent_costs"] = _agent_costs(session, biz.id, agent_row.id)

            # Run activity chart (14d) for this agent.
            ctx["agent_run_chart"] = _agent_run_chart(session, biz.id, agent_row.id)

            return templates.TemplateResponse(request, "agent_detail.html", ctx)
        finally:
            session.close()

    @router.get("/org", response_class=HTMLResponse)
    def org(request: Request, session: Annotated[Session, Depends(require_session)]) -> HTMLResponse:
        try:
            ctx = _ctx(session, active="org")
            ctx["org_tree"] = _build_org_tree(ctx["agents"])
            return templates.TemplateResponse(request, "org.html", ctx)
        finally:
            session.close()

    @router.get("/approvals/{approval_id}/preview", response_class=HTMLResponse)
    def approval_preview(
        request: Request,
        approval_id: UUID,
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse:
        """Render a landing-copy approval as the actual landing page so
        the Founder can see what they're approving before clicking yes.

        Other kinds (validation, outreach drafts) currently fall through
        to the standard approval card preview — there's no useful
        "render" for those."""
        try:
            _founder, business = founder_business(session)
            approval = session.exec(
                select(Approval)
                .where(Approval.id == approval_id)
                .where(Approval.business_id == business.id)
            ).first()
            if approval is None:
                raise HTTPException(404, "Approval not found")
            payload = approval.action_payload or {}
            kind = str(payload.get("kind") or "")
            raw_result = payload.get("result")
            result: dict[str, Any] = raw_result if isinstance(raw_result, dict) else {}

            if kind == "landing_copy":
                ctx = {
                    "request": request,
                    "headline": str(result.get("headline") or ""),
                    "subhead": str(result.get("subhead") or ""),
                    "social_proof": str(result.get("social_proof_line") or ""),
                    "cta_label": str(result.get("primary_cta") or "Get early access"),
                    "cta_verb": str(result.get("cta_verb") or "Sign up"),
                    "objections": (
                        result.get("objection_handlers") or []
                        if isinstance(result.get("objection_handlers"), list)
                        else []
                    ),
                    "business_name": business.name,
                    "approval_id": str(approval.id),
                    "niche_name": str(payload.get("niche_name") or ""),
                }
                return templates.TemplateResponse(request, "landing_preview.html", ctx)

            if kind == "validation_report":
                scores = result.get("scores") if isinstance(result.get("scores"), dict) else {}
                ctx = {
                    "request": request,
                    "verdict": str(result.get("verdict") or "improve").lower(),
                    "overall": result.get("overall") or 0,
                    "scores": scores,
                    "strengths": result.get("strengths") or [],
                    "concerns": result.get("concerns") or [],
                    "kill_test": str(result.get("kill_test") or ""),
                    "improvement_path": str(result.get("improvement_path") or ""),
                    "niche_name": str(payload.get("niche_name") or ""),
                    "business_name": business.name,
                }
                return templates.TemplateResponse(request, "validation_preview.html", ctx)

            if kind == "outreach_drafts":
                raw_variants = result.get("variants")
                variants: list[Any] = raw_variants if isinstance(raw_variants, list) else []
                # Normalise dict-shaped variants so the template can rely
                # on .subject / .body / .angle fields.
                normalised = [
                    {
                        "angle": str(v.get("angle") or "") if isinstance(v, dict) else "",
                        "subject": str(v.get("subject") or "") if isinstance(v, dict) else "",
                        "body": str(v.get("body") or "") if isinstance(v, dict) else "",
                    }
                    for v in variants
                ]
                ctx = {
                    "request": request,
                    "variants": normalised,
                    "personalization_template": str(result.get("personalization_template") or ""),
                    "follow_up_subject": str(result.get("follow_up_subject") or ""),
                    "niche_name": str(payload.get("niche_name") or ""),
                    "business_name": business.name,
                    "founder_name": ceo_display_name(session, business.id) or "Your cofounder",
                }
                return templates.TemplateResponse(request, "outreach_preview.html", ctx)

            raise HTTPException(
                400,
                f"Approval kind {kind!r} has no preview view "
                f"(landing_copy / validation_report / outreach_drafts only)",
            )
        finally:
            session.close()

    @router.get("/approvals", response_class=HTMLResponse)
    def approvals(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        tab: str = "pending",
    ) -> HTMLResponse:
        try:
            ctx = _ctx(session, active="approvals")
            biz_id = ctx["business"].id
            stmt = (
                select(Approval)
                .where(Approval.business_id == biz_id)
                .order_by(Approval.created_at.desc())  # type: ignore[attr-defined]
            )
            if tab != "all":
                stmt = stmt.where(Approval.status == ApprovalStatus.PENDING)
                tab = "pending"
            rows = list(session.exec(stmt).all())
            pending_count = len(
                list(
                    session.exec(
                        select(Approval)
                        .where(Approval.business_id == biz_id)
                        .where(Approval.status == ApprovalStatus.PENDING)
                    ).all()
                )
            )
            ctx["approvals"] = [_format_approval(a) for a in rows]
            ctx["tab"] = tab
            ctx["pending_count"] = pending_count
            return templates.TemplateResponse(request, "approvals.html", ctx)
        finally:
            session.close()

    @router.post(
        "/approvals/{approval_id}/approve",
        response_class=HTMLResponse,
        response_model=None,
    )
    async def approvals_approve_post(
        approval_id: str,
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        comment: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        """Founder approves an approval from the dashboard, optionally
        with a comment. Wraps the same gate.decide path the JSON API
        and CLI use — same dispatch side-effects (Stripe / email / skill
        author / cron / etc.) fire here too."""
        try:
            from uuid import UUID

            from korpha.approvals.gate import ApprovalGate, Decision

            ctx = _ctx(session, active="approvals")
            try:
                aid = UUID(approval_id)
            except ValueError:
                return RedirectResponse(
                    "/app/approvals?error=Bad+approval+id",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            note = (comment or "").strip() or None
            gate = ApprovalGate(session)
            try:
                result = gate.decide(
                    approval_id=aid,
                    decision=Decision.APPROVE,
                    decided_by_founder_id=ctx["founder"].id,
                    modification_note=note,
                )
            except KeyError:
                return RedirectResponse(
                    "/app/approvals?error=Approval+not+found",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            except Exception as exc:  # noqa: BLE001
                return RedirectResponse(
                    f"/app/approvals?error={str(exc)[:80].replace(' ', '+')}",
                    status_code=status.HTTP_303_SEE_OTHER,
                )

            # Mirror the post-approve dispatch that /approvals/{id}/approve
            # does in server.py — without this, approving from /app would
            # be a no-op for Stripe / cron / skill-author payload kinds.
            payload = result.approval.action_payload or {}
            payload_kind = payload.get("kind")
            try:
                if payload_kind == "author_skill":
                    from korpha.skills.meta import (
                        apply_skill_proposal_from_approval,
                    )
                    apply_skill_proposal_from_approval(result.approval)
                elif payload_kind == "author_python_skill":
                    from korpha.skills.meta import (
                        apply_python_skill_proposal_from_approval,
                    )
                    apply_python_skill_proposal_from_approval(result.approval)
                elif payload_kind == "create_cron":
                    from korpha.skills.cron_author import (
                        apply_cron_proposal_from_approval,
                    )
                    apply_cron_proposal_from_approval(result.approval)
                else:
                    from korpha.approvals.dispatch import (
                        dispatch_by_action_class,
                    )
                    dr = await dispatch_by_action_class(
                        session, result.approval, ctx["business"],
                    )
                    if dr is not None and not dr.ok:
                        result.approval.action_payload = {
                            **(result.approval.action_payload or {}),
                            "dispatch_error": dr.message,
                        }
                        session.add(result.approval)
                        session.commit()
            except Exception as exc:  # noqa: BLE001
                import logging
                logging.getLogger(__name__).warning(
                    "approval dashboard approve: dispatch raised",
                    exc_info=True,
                )
                result.approval.action_payload = {
                    **(result.approval.action_payload or {}),
                    "dispatch_error": (
                        f"internal dispatch crash: {type(exc).__name__}: {exc}"
                    ),
                }
                session.add(result.approval)
                session.commit()

            return RedirectResponse(
                f"/app/approvals?approved={approval_id[:8]}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        finally:
            session.close()

    @router.post(
        "/approvals/{approval_id}/reject",
        response_class=HTMLResponse,
        response_model=None,
    )
    def approvals_reject_post(
        approval_id: str,
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        comment: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        """Founder rejects an approval, optionally with a comment
        explaining why. Comment lands in modification_note on the row."""
        try:
            from uuid import UUID

            from korpha.approvals.gate import ApprovalGate, Decision

            ctx = _ctx(session, active="approvals")
            try:
                aid = UUID(approval_id)
            except ValueError:
                return RedirectResponse(
                    "/app/approvals?error=Bad+approval+id",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            note = (comment or "").strip() or None
            gate = ApprovalGate(session)
            try:
                gate.decide(
                    approval_id=aid,
                    decision=Decision.REJECT,
                    decided_by_founder_id=ctx["founder"].id,
                    modification_note=note,
                )
            except KeyError:
                return RedirectResponse(
                    "/app/approvals?error=Approval+not+found",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            except Exception as exc:  # noqa: BLE001
                return RedirectResponse(
                    f"/app/approvals?error={str(exc)[:80].replace(' ', '+')}",
                    status_code=status.HTTP_303_SEE_OTHER,
                )

            return RedirectResponse(
                f"/app/approvals?rejected={approval_id[:8]}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        finally:
            session.close()

    @router.get("/skills", response_class=HTMLResponse)
    def skills_view(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        q: str = "",
    ) -> HTMLResponse:
        """Marketplace-style browse: every loaded skill grouped by
        provenance + searchable. Mike sees the inventory of tools
        the cofounder can call. ``q=...`` filters by name +
        description substring."""
        try:
            from korpha.skills.types import SkillProvenance

            ctx = _ctx(session, active="skills")
            query = (q or "").strip().lower()

            specs = sorted(
                skills_registry.list_specs(), key=lambda s: s.name,
            )
            if query:
                specs = [
                    s for s in specs
                    if query in s.name.lower()
                    or query in s.description.lower()
                ]

            def _domain(name: str) -> str:
                return name.split(".", 1)[0] if "." in name else "other"

            buckets: dict[str, list[dict]] = {}
            for s in specs:
                provenance = (
                    s.provenance.value
                    if isinstance(s.provenance, SkillProvenance)
                    else str(s.provenance or "builtin")
                )
                buckets.setdefault(provenance, []).append({
                    "name": s.name,
                    "description": s.description,
                    "tier": getattr(
                        s.default_tier, "value", str(s.default_tier or ""),
                    ),
                    "domain": _domain(s.name),
                    "param_count": len(s.parameters or {}),
                })

            ordering = ["builtin", "user_authored", "agent_authored"]
            ordered_buckets = [
                {
                    "provenance": p,
                    "label": {
                        "builtin": "Built-in",
                        "user_authored": "Your YAML skills",
                        "agent_authored": "Agent-authored",
                    }.get(p, p),
                    "blurb": {
                        "builtin": (
                            "Ship with Korpha. Pre-approved, "
                            "never auto-archived."
                        ),
                        "user_authored": (
                            "YAML manifests you dropped in "
                            "~/.korpha/skills/."
                        ),
                        "agent_authored": (
                            "Drafted by the agent through "
                            "meta.author_skill, approved through "
                            "the gate, written to disk."
                        ),
                    }.get(p, ""),
                    "skills": buckets[p],
                }
                for p in ordering if p in buckets
            ]
            for p, skills in buckets.items():
                if p not in ordering:
                    ordered_buckets.append({
                        "provenance": p, "label": p.replace("_", " ").title(),
                        "blurb": "", "skills": skills,
                    })

            ctx["query"] = query
            ctx["buckets"] = ordered_buckets
            ctx["total"] = len(specs)
            return templates.TemplateResponse(request, "skills.html", ctx)
        finally:
            session.close()

    @router.get("/skills/authored", response_class=HTMLResponse)
    def skills_authored_view(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse:
        """Surface every skill the agent has authored on disk so Mike
        can review or remove them. Reads ``~/.korpha/skills/
        agent_created/`` directly — the loader's runtime registry is
        a side effect of these files, but the disk is the authoritative
        source of truth."""
        try:
            ctx = _ctx(session, active="skills-authored")
            ctx["authored_skills"] = _enumerate_authored_skills()
            return templates.TemplateResponse(
                request, "skills_authored.html", ctx,
            )
        finally:
            session.close()

    @router.get(
        "/skills/authored/{kind}/{slug}/source",
        response_class=HTMLResponse,
        response_model=None,
    )
    def skills_authored_source(
        kind: str,
        slug: str,
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse | RedirectResponse:
        """Show the source of one authored skill — manifest.yaml or
        skill.py — so Mike can audit before deleting / re-using."""
        try:
            ctx = _ctx(session, active="skills-authored")
            entry = _find_authored_skill(kind, slug)
            if entry is None:
                return RedirectResponse(
                    "/app/skills/authored",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            # Read the source content here rather than in the template —
            # Jinja's {% include %} only resolves against the template
            # loader, not arbitrary on-disk paths.
            try:
                ctx["source_text"] = entry["primary_file"].read_text(
                    encoding="utf-8",
                )
            except OSError:
                ctx["source_text"] = (
                    "(could not read file — check permissions)"
                )
            ctx["entry"] = entry
            return templates.TemplateResponse(
                request, "skills_authored_source.html", ctx,
            )
        finally:
            session.close()

    @router.post(
        "/skills/authored/{kind}/{slug}/delete",
        response_class=HTMLResponse,
        response_model=None,
    )
    def skills_authored_delete(
        kind: str,
        slug: str,
        session: Annotated[Session, Depends(require_session)],
    ) -> RedirectResponse:
        """Delete an authored skill from disk + drop it from the
        running registry. Path-traversal-guarded by routing through
        ``_find_authored_skill`` (which validates the slug against
        the agent_created tree before touching anything)."""
        try:
            entry = _find_authored_skill(kind, slug)
            if entry is None:
                return RedirectResponse(
                    "/app/skills/authored",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            import shutil
            shutil.rmtree(entry["dir"], ignore_errors=False)
            # Drop from the in-memory registry too, otherwise
            # /app/skills still lists it until restart.
            from korpha.skills import default_registry
            if entry["name"] in default_registry.skills:
                del default_registry.skills[entry["name"]]
            return RedirectResponse(
                "/app/skills/authored",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        finally:
            session.close()

    @router.get("/activity", response_class=HTMLResponse)
    def activity(
        request: Request, session: Annotated[Session, Depends(require_session)]
    ) -> HTMLResponse:
        try:
            ctx = _ctx(session, active="activity")
            ctx["events"] = _recent_events(session, ctx["business"].id, limit=80)
            return templates.TemplateResponse(request, "activity.html", ctx)
        finally:
            session.close()

    @router.get("/memory", response_class=HTMLResponse)
    def memory_view(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        q: str = "",
    ) -> HTMLResponse:
        """List the founder's long-term memories. Optional ?q= filters
        via the same scoring logic as the recall skill."""
        try:
            from korpha.memory.db_backend import DbLongTermMemory
            from korpha.memory.model import LongTermMemoryEntry

            ctx = _ctx(session, active="memory")
            mem = DbLongTermMemory(session)
            search_term = (q or "").strip()
            if search_term:
                entries = _sync_search(
                    mem, ctx["business"].id,
                    ctx["founder"].id, search_term,
                )
            else:
                # Empty query → list everything for this scope, newest first
                rows = list(session.exec(
                    select(LongTermMemoryEntry)
                    .where(
                        LongTermMemoryEntry.business_id
                        == ctx["business"].id,
                    )
                    .where(
                        LongTermMemoryEntry.founder_id
                        == ctx["founder"].id,
                    )
                    .order_by(
                        LongTermMemoryEntry.created_at.desc(),  # type: ignore[attr-defined]
                    )
                ).all())
                entries = [_row_to_view(r) for r in rows]

            ctx["entries"] = entries
            ctx["query"] = search_term
            return templates.TemplateResponse(
                request, "memory.html", ctx,
            )
        finally:
            session.close()

    @router.post(
        "/memory/{memory_id}/forget",
        response_class=HTMLResponse,
        response_model=None,
    )
    def memory_forget_post(
        memory_id: str,
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> RedirectResponse:
        """Drop a stored memory. Multi-tenant: refuses if the row
        isn't owned by the current business + founder."""
        try:
            from uuid import UUID as _UUID

            from korpha.memory.model import LongTermMemoryEntry

            ctx = _ctx(session, active="memory")
            try:
                mem_uuid = _UUID(memory_id)
            except ValueError:
                return RedirectResponse(
                    "/app/memory?error=bad_id",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            row = session.get(LongTermMemoryEntry, mem_uuid)
            if (
                row is None
                or row.business_id != ctx["business"].id
                or row.founder_id != ctx["founder"].id
            ):
                return RedirectResponse(
                    "/app/memory?error=not_found",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            session.delete(row)
            session.commit()
            return RedirectResponse(
                "/app/memory?forgot=1",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        finally:
            session.close()

    @router.get("/cron", response_class=HTMLResponse)
    def cron_view(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse:
        """List agentless script cron jobs for this business."""
        try:
            from korpha.scriptcron.model import ScriptCron

            ctx = _ctx(session, active="cron")
            jobs = list(session.exec(
                select(ScriptCron)
                .where(ScriptCron.business_id == ctx["business"].id)
                .order_by(ScriptCron.created_at.desc())  # type: ignore[attr-defined]
            ).all())
            ctx["jobs"] = jobs
            return templates.TemplateResponse(
                request, "cron.html", ctx,
            )
        finally:
            session.close()

    @router.get("/cron/new", response_class=HTMLResponse)
    def cron_new_view(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse:
        """Render the create-cron form with sensible defaults."""
        try:
            ctx = _ctx(session, active="cron")
            ctx["form"] = {
                "name": "",
                "extension": ".sh",
                "cadence": "every 1h",
                "script_content": "",
                "deliver": "",
                "recipient": "",
            }
            ctx["error"] = None
            return templates.TemplateResponse(
                request, "cron_new.html", ctx,
            )
        finally:
            session.close()

    @router.post(
        "/cron/new",
        response_class=HTMLResponse,
        response_model=None,
    )
    def cron_new_post(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        name: Annotated[str, Form()] = "",
        extension: Annotated[str, Form()] = ".sh",
        cadence: Annotated[str, Form()] = "every 1h",
        script_content: Annotated[str, Form()] = "",
        deliver: Annotated[str, Form()] = "",
        recipient: Annotated[str, Form()] = "",
    ) -> HTMLResponse | RedirectResponse:
        """Create a cron from the browser form. Same semantics as
        the CLI — founder editing their own dashboard, no approval
        gate. The safety scanner from cron.create_watchdog still
        runs so obviously-bad scripts are rejected before write."""
        try:
            from pathlib import Path as _P
            import os as _os

            from korpha.scriptcron import parse_cadence
            from korpha.scriptcron.model import ScriptCron
            from korpha.skills.cron_author import (
                _CRON_SCRIPTS_DIR_NAME, _SAFE_NAME_RE, _scan_script,
            )

            ctx = _ctx(session, active="cron")
            form = {
                "name": name.strip(),
                "extension": (extension or ".sh").strip().lower(),
                "cadence": cadence.strip(),
                "script_content": script_content,
                "deliver": (deliver or "").strip().lower(),
                "recipient": (recipient or "").strip(),
            }
            ctx["form"] = form

            def _err(msg: str, code: int = 400):
                ctx["error"] = msg
                return templates.TemplateResponse(
                    request, "cron_new.html", ctx, status_code=code,
                )

            if not _SAFE_NAME_RE.match(form["name"]):
                return _err(
                    "Name must be 1–60 chars, alphanumeric + ._- only, "
                    "starting with a letter or digit.",
                )
            if form["extension"] not in (".sh", ".bash", ".py"):
                return _err("Extension must be .sh, .bash, or .py.")
            if not form["script_content"].strip():
                return _err("Script body cannot be empty.")
            try:
                parse_cadence(form["cadence"])
            except ValueError as exc:
                return _err(f"Bad cadence: {exc}")
            if form["deliver"] and form["deliver"] not in (
                "email", "telegram",
            ):
                return _err(
                    "Deliver must be 'email' or 'telegram' (or empty).",
                )
            if form["deliver"] and not form["recipient"]:
                return _err(
                    "Recipient is required when a delivery channel is set.",
                )
            if form["recipient"] and not form["deliver"]:
                return _err(
                    "Delivery channel required when recipient is set.",
                )

            issues = _scan_script(form["script_content"])
            if issues:
                return _err(
                    "Script rejected by safety scan: "
                    + "; ".join(issues)
                    + ". Rewrite to avoid these patterns.",
                )

            existing = session.exec(
                select(ScriptCron)
                .where(ScriptCron.business_id == ctx["business"].id)
                .where(ScriptCron.name == form["name"])
            ).first()
            if existing is not None:
                return _err(
                    f"A cron named {form['name']!r} already exists. "
                    "Pick a different name or delete the existing one.",
                )

            base = _os.environ.get("KORPHA_DATA_DIR")
            scripts_dir = (
                (_P(base) / _CRON_SCRIPTS_DIR_NAME)
                if base
                else (_P.home() / ".korpha" / _CRON_SCRIPTS_DIR_NAME)
            )
            scripts_dir.mkdir(parents=True, exist_ok=True)
            script_path = scripts_dir / f"{form['name']}{form['extension']}"
            script_path.write_text(
                form["script_content"], encoding="utf-8",
            )
            script_path.chmod(0o755)

            job = ScriptCron(
                business_id=ctx["business"].id,
                name=form["name"],
                script_path=str(script_path),
                cadence=form["cadence"],
                deliver_platform=form["deliver"] or None,
                deliver_recipient=form["recipient"] or None,
            )
            session.add(job); session.commit()
            return RedirectResponse(
                f"/app/cron?created={form['name']}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        finally:
            session.close()

    @router.post(
        "/cron/{job_id}/run-now",
        response_class=HTMLResponse,
        response_model=None,
    )
    def cron_run_now_post(
        job_id: str,
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> RedirectResponse:
        """Trigger a job immediately, ignoring its cadence."""
        try:
            import asyncio as _aio
            from uuid import UUID as _UUID

            from korpha.scriptcron import run_job
            from korpha.scriptcron.model import ScriptCron

            ctx = _ctx(session, active="cron")
            try:
                jid = _UUID(job_id)
            except ValueError:
                return RedirectResponse(
                    "/app/cron?error=bad_id",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            job = session.get(ScriptCron, jid)
            if (
                job is None
                or job.business_id != ctx["business"].id
            ):
                return RedirectResponse(
                    "/app/cron?error=not_found",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            outcome = _aio.run(run_job(session, job))
            return RedirectResponse(
                f"/app/cron?ran={job.name}&status={outcome.status.value}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        finally:
            session.close()

    @router.post(
        "/cron/{job_id}/toggle",
        response_class=HTMLResponse,
        response_model=None,
    )
    def cron_toggle_post(
        job_id: str,
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> RedirectResponse:
        """Flip enabled — pause a noisy watchdog without deleting it."""
        try:
            from uuid import UUID as _UUID

            from korpha.scriptcron.model import ScriptCron

            ctx = _ctx(session, active="cron")
            try:
                jid = _UUID(job_id)
            except ValueError:
                return RedirectResponse(
                    "/app/cron?error=bad_id",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            job = session.get(ScriptCron, jid)
            if (
                job is None
                or job.business_id != ctx["business"].id
            ):
                return RedirectResponse(
                    "/app/cron?error=not_found",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            job.enabled = not job.enabled
            session.add(job); session.commit()
            return RedirectResponse(
                f"/app/cron?toggled={'on' if job.enabled else 'off'}&"
                f"name={job.name}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        finally:
            session.close()

    @router.post(
        "/cron/{job_id}/delete",
        response_class=HTMLResponse,
        response_model=None,
    )
    def cron_delete_post(
        job_id: str,
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> RedirectResponse:
        """Permanently delete a cron job."""
        try:
            from uuid import UUID as _UUID

            from korpha.scriptcron.model import ScriptCron

            ctx = _ctx(session, active="cron")
            try:
                jid = _UUID(job_id)
            except ValueError:
                return RedirectResponse(
                    "/app/cron?error=bad_id",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            job = session.get(ScriptCron, jid)
            if (
                job is None
                or job.business_id != ctx["business"].id
            ):
                return RedirectResponse(
                    "/app/cron?error=not_found",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            name = job.name
            session.delete(job); session.commit()
            return RedirectResponse(
                f"/app/cron?deleted={name}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        finally:
            session.close()

    @router.get("/jobs", response_class=HTMLResponse)
    def jobs_view(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse:
        """In-flight + recently-completed background jobs for this
        business. Multi-tenant: filtered by business_id."""
        try:
            from korpha.jobs import job_registry

            ctx = _ctx(session, active="jobs")
            jobs = job_registry.list(
                business_id=str(ctx["business"].id),
            )
            ctx["jobs"] = jobs
            return templates.TemplateResponse(
                request, "jobs.html", ctx,
            )
        finally:
            session.close()

    @router.post(
        "/jobs/{job_id}/cancel",
        response_class=HTMLResponse,
        response_model=None,
    )
    def jobs_cancel_post(
        job_id: str,
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> RedirectResponse:
        """Cancel a running job. Multi-tenant: refuses if the job
        isn't owned by this business."""
        try:
            from korpha.jobs import job_registry

            ctx = _ctx(session, active="jobs")
            j = job_registry.get(job_id)
            if (
                j is None
                or j.business_id != str(ctx["business"].id)
            ):
                return RedirectResponse(
                    "/app/jobs?error=not_found",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            cancelled = job_registry.cancel(job_id)
            return RedirectResponse(
                f"/app/jobs?cancelled={'1' if cancelled else '0'}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        finally:
            session.close()

    @router.get("/kanban", response_class=HTMLResponse)
    def kanban_view(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse:
        """C-suite kanban board grouped by column. Cards owned by
        a role that has an actively-running director attempt get
        marked ``live`` so the IN_PROGRESS column shows real-time
        activity (workforce ↔ kanban feedback loop)."""
        try:
            from korpha.cofounder.workforce import (
                list_running_subagents,
            )
            from korpha.kanban import KanbanBoard
            from korpha.kanban.model import TRANSITIONS

            ctx = _ctx(session, active="kanban")
            board = KanbanBoard(session)
            snapshot = board.board_snapshot(ctx["business"].id)

            # PR-INT-7: unit filter ribbon. ?unit=<uuid> narrows the
            # board to cards scoped to that BusinessUnit. ?unit=__none__
            # shows only the unscoped (company-wide) cards.
            unit_filter_raw = request.query_params.get("unit")
            unit_filter: UUID | None = None
            unit_filter_none = unit_filter_raw == "__none__"
            if unit_filter_raw and not unit_filter_none:
                try:
                    unit_filter = UUID(unit_filter_raw)
                except ValueError:
                    unit_filter = None
            if unit_filter is not None:
                snapshot = {
                    col: [c for c in cards if c.business_unit_id == unit_filter]
                    for col, cards in snapshot.items()
                }
            elif unit_filter_none:
                snapshot = {
                    col: [c for c in cards if c.business_unit_id is None]
                    for col, cards in snapshot.items()
                }

            mine_filter = request.query_params.get("mine") == "1"
            ctx["mine_filter"] = mine_filter
            # Build the ribbon: all units, plus a "company-wide" pseudo
            from korpha.business_units.board import BusinessUnitBoard
            ctx["unit_filter_options"] = list(
                BusinessUnitBoard(session).list_for_business(
                    ctx["business"].id
                )
            )
            ctx["unit_filter_id"] = (
                str(unit_filter) if unit_filter else (
                    "__none__" if unit_filter_none else None
                )
            )

            # Cross-reference IN_PROGRESS owners against the running
            # subagent registry. We tag the *card* (not just the
            # column) so the template can render a per-card pulse.
            live_pairs = {
                (s["business_id"], s["role_type"])
                for s in list_running_subagents()
            }
            biz_id_str = str(ctx["business"].id)
            live_card_ids: set = set()
            for c in snapshot.values():
                for card in c:
                    if (
                        card.owner_role
                        and (biz_id_str, card.owner_role) in live_pairs
                        and card.column.value == "in_progress"
                    ):
                        live_card_ids.add(card.id)
            # Pull artifacts in one query, group by card so the
            # template can render them inline without N+1.
            from korpha.kanban.artifacts import CardArtifact

            artifacts_by_card: dict = {}
            for art in session.exec(
                select(CardArtifact).where(
                    CardArtifact.business_id == ctx["business"].id,
                )
            ).all():
                artifacts_by_card.setdefault(
                    art.card_id, [],
                ).append(art)

            # Count open blockers per card so the board can surface
            # 'this card is waiting on Mike' visually.
            from korpha.blockers.model import Blocker, BlockerStatus
            blocker_counts_by_card: dict = {}
            for b in session.exec(
                select(Blocker)
                .where(Blocker.business_id == ctx["business"].id)
                .where(Blocker.kanban_card_id.is_not(None))  # type: ignore[union-attr]
                .where(Blocker.status.in_([  # type: ignore[union-attr]
                    BlockerStatus.OPEN,
                    BlockerStatus.TRIAGED,
                    BlockerStatus.AWAITING_FOUNDER,
                ]))
            ).all():
                blocker_counts_by_card[b.kanban_card_id] = (
                    blocker_counts_by_card.get(b.kanban_card_id, 0) + 1
                )

            # ?mine=1 narrows the board to "cards waiting on Mike" so
            # the founder can land on /app/kanban?mine=1 and see only
            # the stuff blocking the team. Two definitions of
            # "waiting on me":
            #   - Card sits in REVIEW (needs accept/reject verdict).
            #   - Card has an open Blocker (any column).
            # Computed AFTER blocker_counts so we can scope by blocker
            # presence; composes cleanly with the unit filter above.
            if mine_filter:
                from korpha.kanban.model import KanbanColumn as _KC
                snapshot = {
                    col: [
                        c for c in cards
                        if col == _KC.REVIEW
                        or (blocker_counts_by_card.get(c.id, 0) > 0)
                    ]
                    for col, cards in snapshot.items()
                }

            ctx["snapshot"] = snapshot
            ctx["transitions"] = TRANSITIONS
            ctx["live_card_ids"] = live_card_ids
            ctx["artifacts_by_card"] = artifacts_by_card
            ctx["blocker_counts_by_card"] = blocker_counts_by_card
            return templates.TemplateResponse(
                request, "kanban.html", ctx,
            )
        finally:
            session.close()

    @router.post(
        "/kanban/new",
        response_class=HTMLResponse,
        response_model=None,
    )
    def kanban_new_post(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        title: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        """Quick-add a card straight to BACKLOG. The CEO / specify
        flow fills in body + acceptance criteria later."""
        try:
            from korpha.kanban import (
                CreateCardInput, KanbanBoard, KanbanError,
            )

            ctx = _ctx(session, active="kanban")
            title = (title or "").strip()
            if not title:
                return RedirectResponse(
                    "/app/kanban?error=Title+required",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            board = KanbanBoard(session)
            try:
                card = board.create(CreateCardInput(
                    business_id=ctx["business"].id,
                    title=title,
                    created_by_founder_id=ctx["founder"].id,
                ))
            except KanbanError as exc:
                return RedirectResponse(
                    f"/app/kanban?error={str(exc).replace(' ', '+')}",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            return RedirectResponse(
                f"/app/kanban?created={card.title.replace(' ', '+')}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        finally:
            session.close()

    @router.post(
        "/kanban/{card_id}/move",
        response_class=HTMLResponse,
        response_model=None,
    )
    def kanban_move_post(
        card_id: str,
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        to_column: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        """Founder-driven column transition. Surfaces KanbanError
        messages back to the panel via ?error=."""
        try:
            from uuid import UUID

            from korpha.kanban import KanbanBoard, KanbanError
            from korpha.kanban.model import KanbanCard, KanbanColumn

            ctx = _ctx(session, active="kanban")
            try:
                cid = UUID(card_id)
            except ValueError:
                return RedirectResponse(
                    "/app/kanban?error=Bad+card+id",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            try:
                col = KanbanColumn(to_column)
            except ValueError:
                return RedirectResponse(
                    "/app/kanban?error=Bad+target+column",
                    status_code=status.HTTP_303_SEE_OTHER,
                )

            card = session.get(KanbanCard, cid)
            if card is None or card.business_id != ctx["business"].id:
                return RedirectResponse(
                    "/app/kanban?error=Card+not+found",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            board = KanbanBoard(session)
            try:
                board.move(
                    cid, col, actor_founder_id=ctx["founder"].id,
                )
            except KanbanError as exc:
                return RedirectResponse(
                    f"/app/kanban?error={str(exc).replace(' ', '+')}",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            return RedirectResponse(
                f"/app/kanban?moved={col.value}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        finally:
            session.close()

    @router.get("/kanban/{card_id}", response_class=HTMLResponse)
    def kanban_card_detail(
        card_id: str,
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse:
        """Per-card view: title, body, acceptance criteria, attached
        blockers (with respond form), artifacts, comments, history.
        Mike's primary surface for unblocking work."""
        try:
            from uuid import UUID

            from korpha.blockers.model import (
                Blocker, BlockerStatus,
            )
            from korpha.kanban.artifacts import CardArtifact
            from korpha.kanban.model import KanbanCard
            from korpha.kanban.relations import KanbanCardComment

            ctx = _ctx(session, active="kanban")
            try:
                cid = UUID(card_id)
            except ValueError:
                return RedirectResponse(
                    "/app/kanban?error=Bad+card+id",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            card = session.get(KanbanCard, cid)
            if card is None or card.business_id != ctx["business"].id:
                return RedirectResponse(
                    "/app/kanban?error=Card+not+found",
                    status_code=status.HTTP_303_SEE_OTHER,
                )

            # Open blockers attached to this card (newest first).
            open_blockers = list(session.exec(
                select(Blocker)
                .where(Blocker.kanban_card_id == cid)
                .where(Blocker.status.in_([  # type: ignore[union-attr]
                    BlockerStatus.OPEN,
                    BlockerStatus.TRIAGED,
                    BlockerStatus.AWAITING_FOUNDER,
                ]))
                .order_by(Blocker.submitted_at.desc())  # type: ignore[union-attr]
            ).all())
            # Resolved blockers (for context — what's already answered).
            resolved_blockers = list(session.exec(
                select(Blocker)
                .where(Blocker.kanban_card_id == cid)
                .where(Blocker.status.in_([  # type: ignore[union-attr]
                    BlockerStatus.RESOLVED,
                    BlockerStatus.RESOLVED_BY_COS,
                ]))
                .order_by(Blocker.resolved_at.desc())  # type: ignore[union-attr]
            ).all())

            # Artifacts (REVIEW evidence, generated work, URLs).
            artifacts = list(session.exec(
                select(CardArtifact)
                .where(CardArtifact.card_id == cid)
                .order_by(CardArtifact.created_at.desc())  # type: ignore[union-attr]
            ).all())

            # Comments (Hermes-style audit trail).
            comments = list(session.exec(
                select(KanbanCardComment)
                .where(KanbanCardComment.card_id == cid)
                .order_by(KanbanCardComment.created_at.asc())  # type: ignore[union-attr]
            ).all())

            ctx["card"] = card
            ctx["open_blockers"] = open_blockers
            ctx["resolved_blockers"] = resolved_blockers
            ctx["artifacts"] = artifacts
            ctx["comments"] = comments
            return templates.TemplateResponse(
                request, "kanban_detail.html", ctx,
            )
        finally:
            session.close()

    @router.post(
        "/kanban/{card_id}/blockers/{blocker_id}/respond",
        response_class=HTMLResponse,
        response_model=None,
    )
    async def kanban_blocker_respond_post(
        card_id: str,
        blocker_id: str,
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        response: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        """Founder submits the answer / decision / info that unblocks
        a card. Marks the blocker resolved, drops a card comment with
        the resolution, clears the auto_dispatch cooldown stamp, and
        kicks off an immediate re-dispatch so the Director sees the
        new context."""
        try:
            from uuid import UUID

            from korpha.blockers.queue import BlockerQueue
            from korpha.kanban import KanbanBoard
            from korpha.kanban.model import KanbanCard, KanbanColumn
            from korpha.kanban.relations import KanbanCardComment

            ctx = _ctx(session, active="kanban")
            try:
                cid = UUID(card_id)
                bid = UUID(blocker_id)
            except ValueError:
                return RedirectResponse(
                    "/app/kanban?error=Bad+id",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            card = session.get(KanbanCard, cid)
            if card is None or card.business_id != ctx["business"].id:
                return RedirectResponse(
                    "/app/kanban?error=Card+not+found",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            answer = (response or "").strip()
            if not answer:
                return RedirectResponse(
                    f"/app/kanban/{cid}?error=Response+is+required",
                    status_code=status.HTTP_303_SEE_OTHER,
                )

            queue = BlockerQueue(session=session)
            try:
                resolved = queue.mark_resolved(
                    bid,
                    resolution=answer,
                    resolved_by_founder_id=ctx["founder"].id,
                )
            except KeyError:
                return RedirectResponse(
                    f"/app/kanban/{cid}?error=Blocker+not+found",
                    status_code=status.HTTP_303_SEE_OTHER,
                )

            # Drop a comment on the card so the next Director attempt
            # sees the resolution as inline context.
            session.add(KanbanCardComment(
                card_id=cid,
                business_id=ctx["business"].id,
                author_kind="founder",
                author_founder_id=ctx["founder"].id,
                body=f"[Founder unblocked: {resolved.title}] {answer}",
            ))
            session.commit()

            # Clear the cooldown stamp + bounce IN_PROGRESS cards back
            # to READY so the next "go" can re-fire from a clean state.
            meta = dict(card.metadata_json or {})
            stamp_dropped = meta.pop("auto_dispatch_at", None) is not None
            if stamp_dropped:
                card.metadata_json = meta
                session.add(card)
            if card.column == KanbanColumn.IN_PROGRESS:
                board = KanbanBoard(session)
                try:
                    board.move(
                        cid, KanbanColumn.READY,
                        actor_founder_id=ctx["founder"].id,
                        note=f"unblocked by founder: {resolved.title}",
                    )
                except Exception:  # noqa: BLE001
                    pass
            session.commit()

            return RedirectResponse(
                f"/app/kanban/{cid}?unblocked=1",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        finally:
            session.close()

    @router.get("/blockers", response_class=HTMLResponse)
    def blockers_inbox(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse:
        """Mike's inbox of every open blocker — grouped by card so he
        can see 'these 5 cards need answers' at a glance. Each row
        deep-links into the card detail view to respond."""
        try:
            from korpha.blockers.model import Blocker, BlockerStatus
            from korpha.kanban.model import KanbanCard

            ctx = _ctx(session, active="blockers")
            biz_id = ctx["business"].id
            open_rows = list(session.exec(
                select(Blocker)
                .where(Blocker.business_id == biz_id)
                .where(Blocker.status.in_([  # type: ignore[union-attr]
                    BlockerStatus.OPEN,
                    BlockerStatus.TRIAGED,
                    BlockerStatus.AWAITING_FOUNDER,
                ]))
                .where(Blocker.deduped_into_id.is_(None))  # type: ignore[union-attr]
                .order_by(Blocker.submitted_at.desc())  # type: ignore[union-attr]
            ).all())

            # Lookup card titles in one query
            card_ids = {b.kanban_card_id for b in open_rows if b.kanban_card_id}
            cards_by_id: dict = {}
            if card_ids:
                for card in session.exec(
                    select(KanbanCard).where(KanbanCard.id.in_(card_ids))  # type: ignore[union-attr]
                ).all():
                    cards_by_id[card.id] = card

            grouped_attached: list = []
            unattached: list = []
            seen_card_ids: list = []
            attached_by_card: dict = {}
            for b in open_rows:
                if b.kanban_card_id and b.kanban_card_id in cards_by_id:
                    attached_by_card.setdefault(b.kanban_card_id, []).append(b)
                    if b.kanban_card_id not in seen_card_ids:
                        seen_card_ids.append(b.kanban_card_id)
                else:
                    unattached.append(b)
            for cid in seen_card_ids:
                grouped_attached.append({
                    "card": cards_by_id[cid],
                    "blockers": attached_by_card[cid],
                })

            ctx["grouped_attached"] = grouped_attached
            ctx["unattached"] = unattached
            ctx["total_open"] = len(open_rows)
            return templates.TemplateResponse(
                request, "blockers.html", ctx,
            )
        finally:
            session.close()

    @router.get("/weekly", response_class=HTMLResponse)
    def weekly_view(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse:
        """Mike's Monday-morning view: what shipped, what's stuck,
        what's pending review, where the spend went — last 7 days."""
        try:
            from datetime import datetime, timedelta, timezone
            from decimal import Decimal

            from korpha.approvals.model import Approval, ApprovalStatus
            from korpha.audit.model import Cost
            from korpha.blockers.model import Blocker, BlockerStatus
            from korpha.kanban.model import (
                KanbanCard, KanbanCardEvent, KanbanColumn,
            )

            ctx = _ctx(session, active="weekly")
            biz_id = ctx["business"].id
            now = datetime.now(tz=timezone.utc)
            window_start = now - timedelta(days=7)
            window_prev = now - timedelta(days=14)

            # Cards shipped to DONE in the last 7 days. We use the
            # KanbanCardEvent log so a card that went DONE → BACKLOG
            # → DONE counts the most recent transition correctly.
            shipped_cards = list(session.exec(
                select(KanbanCard)
                .where(KanbanCard.business_id == biz_id)
                .where(KanbanCard.column == KanbanColumn.DONE)
                .where(KanbanCard.moved_at >= window_start)
                .order_by(KanbanCard.moved_at.desc())  # type: ignore[attr-defined]
            ).all())

            shipped_prev = list(session.exec(
                select(KanbanCard)
                .where(KanbanCard.business_id == biz_id)
                .where(KanbanCard.column == KanbanColumn.DONE)
                .where(KanbanCard.moved_at >= window_prev)
                .where(KanbanCard.moved_at < window_start)
            ).all())

            in_progress_cards = list(session.exec(
                select(KanbanCard)
                .where(KanbanCard.business_id == biz_id)
                .where(KanbanCard.column == KanbanColumn.IN_PROGRESS)
                .order_by(KanbanCard.moved_at.desc())  # type: ignore[attr-defined]
            ).all())

            review_cards = list(session.exec(
                select(KanbanCard)
                .where(KanbanCard.business_id == biz_id)
                .where(KanbanCard.column == KanbanColumn.REVIEW)
                .order_by(KanbanCard.moved_at.desc())  # type: ignore[attr-defined]
            ).all())

            pending_approvals = len(list(session.exec(
                select(Approval)
                .where(Approval.business_id == biz_id)
                .where(Approval.status == ApprovalStatus.PENDING)
            ).all()))

            top_blockers = list(session.exec(
                select(Blocker)
                .where(Blocker.business_id == biz_id)
                .where(Blocker.status == BlockerStatus.OPEN)
                .order_by(Blocker.submitted_at.desc())  # type: ignore[attr-defined]
            ).all())[:5]

            # Cost rollup last 7d.
            costs = list(session.exec(
                select(Cost)
                .where(Cost.business_id == biz_id)
                .where(Cost.created_at >= window_start)
            ).all())
            spend_total = float(
                sum((c.cost_usd for c in costs), Decimal("0"))
            )
            tiers: dict[str, dict] = {}
            workhorse_cost = Decimal("0")
            for c in costs:
                t = c.tier.value
                bucket = tiers.setdefault(t, {
                    "tier": t, "calls": 0, "tokens": 0,
                    "cost": Decimal("0"),
                })
                bucket["calls"] += 1
                bucket["tokens"] += c.input_tokens + c.output_tokens
                bucket["cost"] += c.cost_usd
                if t == "workhorse":
                    workhorse_cost += c.cost_usd
            spend_by_tier = sorted(
                ({"tier": v["tier"], "calls": v["calls"],
                  "tokens": v["tokens"], "cost": float(v["cost"])}
                 for v in tiers.values()),
                key=lambda r: -r["cost"],
            )
            workhorse_pct = (
                int((workhorse_cost / max(Decimal("0.000001"),
                    Decimal(str(spend_total)))) * 100)
                if spend_total > 0 else 0
            )

            shipped_count = len(shipped_cards)
            prev_count = len(shipped_prev)
            if prev_count == 0 and shipped_count == 0:
                shipped_delta = "first week of activity"
            elif prev_count == 0:
                shipped_delta = "vs 0 last week"
            else:
                diff = shipped_count - prev_count
                if diff > 0:
                    shipped_delta = f"+{diff} vs last week"
                elif diff < 0:
                    shipped_delta = f"{diff} vs last week"
                else:
                    shipped_delta = "same as last week"

            # Friendly greeting based on hour-of-day.
            hour = now.hour
            if hour < 12:
                greeting = "Good morning, Mike."
            elif hour < 17:
                greeting = "Afternoon check-in."
            else:
                greeting = "Evening recap."
            display_name = (
                ctx["founder"].display_name or ctx["founder"].email
            )
            greeting = greeting.replace("Mike", display_name.split(" ")[0])

            from korpha.liveness import classify_kanban_signals
            stuck_signals = classify_kanban_signals(
                session, biz_id, now=now,
            )

            ctx.update({
                "greeting": greeting,
                "window_start": window_start,
                "window_end": now,
                "shipped_cards": shipped_cards,
                "shipped_count": shipped_count,
                "shipped_delta": shipped_delta,
                "in_progress_cards": in_progress_cards,
                "review_cards": review_cards,
                "pending_approvals": pending_approvals,
                "open_blockers": len(list(session.exec(
                    select(Blocker)
                    .where(Blocker.business_id == biz_id)
                    .where(Blocker.status == BlockerStatus.OPEN)
                ).all())),
                "top_blockers": top_blockers,
                "spend_total": spend_total,
                "spend_by_tier": spend_by_tier,
                "spend_breakdown": {
                    "workhorse_pct": workhorse_pct,
                },
                "stuck_signals": stuck_signals,
                "stuck_critical_count": sum(
                    1 for s in stuck_signals
                    if s.severity == "critical"
                ),
            })
            return templates.TemplateResponse(
                request, "weekly.html", ctx,
            )
        finally:
            session.close()

    @router.get("/team", response_class=HTMLResponse)
    def team_view(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse:
        """Org chart with hire/fire controls. Mirrors the
        `korpha team` CLI."""
        try:
            from korpha.cofounder.model import AgentRole

            ctx = _ctx(session, active="team")
            rows = list(session.exec(
                select(AgentRole)
                .where(AgentRole.business_id == ctx["business"].id)
                .where(AgentRole.is_active)
            ).all())
            ctx["c_suite"] = sorted(
                [r for r in rows if r.role_type.value in (
                    "ceo", "cto", "cmo", "coo", "chief_of_staff",
                )],
                key=lambda r: ["ceo", "cto", "cmo", "coo",
                               "chief_of_staff"].index(r.role_type.value)
                if r.role_type.value in [
                    "ceo", "cto", "cmo", "coo", "chief_of_staff",
                ] else 99,
            )
            ctx["workers"] = sorted(
                [r for r in rows if r.role_type.value == "worker"],
                key=lambda r: r.hired_at,
            )
            return templates.TemplateResponse(
                request, "team.html", ctx,
            )
        finally:
            session.close()

    @router.post(
        "/team/hire",
        response_class=HTMLResponse,
        response_model=None,
    )
    def team_hire_post(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        specialty: Annotated[str, Form()] = "",
        description: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        try:
            from korpha.cofounder.hiring import HiringService
            from korpha.cofounder.model import RoleType

            ctx = _ctx(session, active="team")
            spec = (specialty or "").strip().lower()
            if not spec or " " in spec or len(spec) > 60:
                return RedirectResponse(
                    "/app/team?error=Specialty+must+be+lowercase+one-token",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            desc = (description or "").strip() or None
            if desc and len(desc) > 1000:
                desc = desc[:1000]
            HiringService(session).hire(
                ctx["business"].id, RoleType.WORKER,
                title=spec.replace("-", " ").title(),
                specialty=spec,
                description=desc,
                source="dashboard:hire",
            )
            return RedirectResponse(
                f"/app/team?hired={spec}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        finally:
            session.close()

    @router.post(
        "/team/{role_id}/fire",
        response_class=HTMLResponse,
        response_model=None,
    )
    def team_fire_post(
        role_id: str,
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> RedirectResponse:
        try:
            from uuid import UUID

            from korpha.cofounder.hiring import HiringService
            from korpha.cofounder.model import AgentRole, RoleType

            ctx = _ctx(session, active="team")
            try:
                rid = UUID(role_id)
            except ValueError:
                return RedirectResponse(
                    "/app/team?error=Bad+role+id",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            role = session.get(AgentRole, rid)
            if role is None or role.business_id != ctx["business"].id:
                return RedirectResponse(
                    "/app/team?error=Role+not+found",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            if role.role_type != RoleType.WORKER:
                return RedirectResponse(
                    "/app/team?error=Refuses+to+fire+C-suite",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            label = role.title or role.specialty or str(role.id)[:8]
            HiringService(session).fire(rid, reason="dashboard")
            return RedirectResponse(
                f"/app/team?fired={label}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        finally:
            session.close()

    # ---------------------------------------------------------------
    # PR-INT-7: /app/units + /app/credentials
    # ---------------------------------------------------------------

    @router.get("/units", response_class=HTMLResponse)
    def units_view(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse:
        """Org tree of BusinessUnits — lines, types, audiences. Each
        unit links to its credentials / kanban filter."""
        try:
            from korpha.business_units.board import BusinessUnitBoard
            from korpha.business_units.context import (
                CANONICAL_LINE_KINDS,
            )
            from korpha.cofounder.model import AgentRole

            ctx = _ctx(session, active="units")
            board = BusinessUnitBoard(session)
            units = list(board.list_for_business(ctx["business"].id))
            # Parent name lookup
            parent_names = {u.id: u.name for u in units}
            # VP title lookup for owned units
            owner_ids = [
                u.owner_agent_role_id for u in units
                if u.owner_agent_role_id is not None
            ]
            vp_titles: dict = {}
            if owner_ids:
                for role in session.exec(
                    select(AgentRole).where(AgentRole.id.in_(owner_ids))  # type: ignore[attr-defined]
                ).all():
                    vp_titles[role.id] = role.title
            ctx["units"] = units
            ctx["parent_names"] = parent_names
            ctx["vp_titles"] = vp_titles
            ctx["line_kinds"] = CANONICAL_LINE_KINDS
            return templates.TemplateResponse(
                request, "units.html", ctx,
            )
        finally:
            session.close()

    @router.post(
        "/units/start",
        response_class=HTMLResponse,
        response_model=None,
    )
    def units_start_post(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        kind: Annotated[str, Form()] = "",
        name: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        """Start a business line via hr.start_business_line skill so
        the VP is auto-hired + the Line Pack is applied."""
        try:
            from korpha.business_units.context import (
                CANONICAL_LINE_KINDS,
            )

            kind = (kind or "").strip().lower()
            valid_kinds = {k["value"] for k in CANONICAL_LINE_KINDS}
            if kind not in valid_kinds:
                return RedirectResponse(
                    f"/app/units?error=Unknown+line+kind+{kind}",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            ctx = _ctx(session)
            display = (name or kind.upper()).strip()
            # Call the hr.start_business_line skill so the same
            # plumbing fires (VP hire, line pack apply, namespace
            # provision) as via chat.
            import asyncio

            from korpha.inference.cost_tracker import CostTracker
            from korpha.inference.pool import InferencePool
            from korpha.skills import default_registry
            from korpha.skills.types import SkillContext

            skill = default_registry.skills.get("hr.start_business_line")
            if skill is None:
                return RedirectResponse(
                    "/app/units?error=hr+skill+unavailable",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            sctx = SkillContext(
                business=ctx["business"], founder=ctx["founder"],
                session=session,
                cost_tracker=CostTracker(pool=InferencePool(
                    providers=[], accounts=[],
                )),
            )
            try:
                asyncio.run(skill.run(
                    ctx=sctx, args={"kind": kind, "name": display},
                ))
            except Exception as exc:  # noqa: BLE001
                return RedirectResponse(
                    f"/app/units?error={str(exc)[:80]}",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            return RedirectResponse(
                f"/app/units?started={display}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        finally:
            session.close()

    @router.get("/credentials", response_class=HTMLResponse)
    def credentials_view(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse:
        """Surface per-unit credentials + the OAuth CLI pool."""
        try:
            from korpha.business_units.board import BusinessUnitBoard
            from korpha.credentials.model import (
                ExternalServiceAccount,
            )
            from korpha.shared_resources.model import (
                SharedResource, SharedResourceKind,
            )

            ctx = _ctx(session, active="credentials")
            biz_id = ctx["business"].id
            # OAuth CLI pool
            ctx["oauth_clis"] = list(session.exec(
                select(SharedResource).where(
                    SharedResource.business_id == biz_id,
                    SharedResource.kind == SharedResourceKind.OAUTH_CLI,
                )
            ).all())
            # Per-unit API accounts
            board = BusinessUnitBoard(session)
            units = list(board.list_for_business(biz_id))
            accounts_by_unit: dict = {}
            for acc in session.exec(
                select(ExternalServiceAccount).where(
                    ExternalServiceAccount.business_id == biz_id,
                )
            ).all():
                accounts_by_unit.setdefault(
                    acc.business_unit_id, [],
                ).append(acc)
            units_with_creds = []
            for u in units:
                accs = accounts_by_unit.get(u.id, [])
                if accs:
                    units_with_creds.append(
                        {"unit": u, "accounts": accs},
                    )
            # Also surface the company-wide default account bucket
            default_accs = accounts_by_unit.get(None, [])
            if default_accs:
                class _DefaultUnit:
                    name = "(Company default)"
                    class kind:
                        value = "default"
                units_with_creds.insert(
                    0, {"unit": _DefaultUnit(), "accounts": default_accs},
                )
            ctx["units_with_creds"] = units_with_creds
            # PR-INT-18: form options
            ctx["unit_options"] = units
            from korpha.credentials.model import ExternalServiceKind
            ctx["service_kinds"] = [k.value for k in ExternalServiceKind]
            # PR-XAI-1: subscription-OAuth status
            from korpha.inference.xai_oauth import is_configured as _xai_ok
            ctx["xai_oauth_configured"] = _xai_ok()
            return templates.TemplateResponse(
                request, "credentials.html", ctx,
            )
        finally:
            session.close()

    @router.post("/credentials/xai-oauth/start")
    def credentials_xai_oauth_start(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> JSONResponse:
        """Kick off the xAI OAuth loopback sign-in flow in a
        background thread + return immediately. The flow opens
        Mike's browser to ``auth.x.ai``; he signs in; the loopback
        server captures the callback + writes tokens to the vault.

        Returns JSON ``{started: true}`` — the UI polls
        ``/app/credentials/xai-oauth/status`` for completion."""
        try:
            import threading
            from korpha.inference.xai_oauth import begin_login

            def _run():
                try:
                    begin_login(open_browser=True, timeout_seconds=300)
                except Exception:  # noqa: BLE001
                    import logging
                    logging.getLogger(__name__).warning(
                        "xai-oauth loopback failed", exc_info=True,
                    )

            t = threading.Thread(target=_run, daemon=True)
            t.start()
            return JSONResponse({"started": True})
        finally:
            session.close()

    @router.get("/credentials/xai-oauth/status")
    def credentials_xai_oauth_status(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> JSONResponse:
        """Poll endpoint for the in-progress OAuth flow."""
        try:
            from korpha.inference.xai_oauth import is_configured
            return JSONResponse({"configured": is_configured()})
        finally:
            session.close()

    @router.post("/credentials/xai-oauth/logout")
    def credentials_xai_oauth_logout(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> RedirectResponse:
        """Clear stored xAI OAuth tokens."""
        try:
            from korpha.inference.xai_oauth import logout
            logout()
            return RedirectResponse("/app/credentials", status_code=303)
        finally:
            session.close()

    @router.post(
        "/credentials/new",
        response_class=HTMLResponse,
        response_model=None,
    )
    def credentials_new_post(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        service: Annotated[str, Form()] = "",
        label: Annotated[str, Form()] = "",
        api_key: Annotated[str, Form()] = "",
        business_unit: Annotated[str, Form()] = "",
        cap: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        """PR-INT-18: save a per-unit credential from the dashboard.

        Encrypts the api_key blob using the local secrets-vault master
        key, resolves the optional unit by UUID, persists an
        ExternalServiceAccount row. The resolver tree-walk (PR4) will
        find it on next lookup."""
        try:
            from decimal import Decimal
            import json as _json

            from korpha.business_units.context import resolve_unit_id
            from korpha.credentials.model import (
                ExternalServiceAccount, ExternalServiceKind,
            )
            from korpha.secrets.crypto import (
                encrypt_bytes, load_master_key,
            )

            ctx = _ctx(session)
            biz_id = ctx["business"].id

            service = (service or "").strip()
            label = (label or "").strip()
            api_key = (api_key or "").strip()
            if not service or not label or not api_key:
                return RedirectResponse(
                    "/app/credentials?error=service+label+and+api_key+required",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            try:
                service_kind = ExternalServiceKind(service)
            except ValueError:
                return RedirectResponse(
                    f"/app/credentials?error=Unknown+service+{service}",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            unit_id = None
            unit_str = (business_unit or "").strip()
            if unit_str:
                try:
                    unit_id = resolve_unit_id(session, biz_id, unit_str)
                except ValueError as exc:
                    return RedirectResponse(
                        f"/app/credentials?error={str(exc)[:80]}",
                        status_code=status.HTTP_303_SEE_OTHER,
                    )
            cap_dec: Decimal | None = None
            cap_str = (cap or "").strip()
            if cap_str:
                try:
                    cap_dec = Decimal(cap_str)
                except Exception:  # noqa: BLE001
                    return RedirectResponse(
                        "/app/credentials?error=Spending+cap+must+be+a+number",
                        status_code=status.HTTP_303_SEE_OTHER,
                    )

            try:
                import os as _os
                data_dir = Path(
                    _os.getenv("KORPHA_DATA_DIR")
                    or _os.path.expanduser("~/.korpha")
                )
                master_key_path = data_dir / "secrets" / "master.key"
                master = load_master_key(master_key_path)
            except Exception as exc:  # noqa: BLE001
                return RedirectResponse(
                    f"/app/credentials?error=vault+key+unavailable:+{str(exc)[:60]}",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            plaintext = _json.dumps(
                {"api_key": api_key}, separators=(",", ":"),
            ).encode("utf-8")
            credentials_blob = encrypt_bytes(plaintext, master)

            session.add(ExternalServiceAccount(
                business_id=biz_id,
                business_unit_id=unit_id,
                service=service_kind,
                label=label,
                credentials_encrypted=credentials_blob,
                spending_cap_usd_per_month=cap_dec,
                is_active=True,
            ))
            session.commit()
            return RedirectResponse(
                f"/app/credentials?saved={label}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        finally:
            session.close()

    # ---------------------------------------------------------------
    # Backups (Layer 1 — local rotating snapshots)
    # ---------------------------------------------------------------

    @router.get("/backups", response_class=HTMLResponse)
    def backups_view(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse:
        try:
            from korpha.backup import BackupKind, list_backups
            from korpha.backup.offdisk import (
                PROVIDERS, current_status, replicator_status,
            )
            ctx = _ctx(session, active="backups")
            ctx["snapshots"] = list_backups(kind=BackupKind.DB_SNAPSHOT)
            ctx["bundles"] = list_backups(kind=BackupKind.FULL_BUNDLE)
            ctx["providers"] = PROVIDERS
            ctx["offdisk_status"] = current_status()
            ctx["replicator"] = replicator_status()
            # Surface whether the litestream binary is available so
            # the page can render a one-click install button instead
            # of failing silently when the founder hits Start.
            from korpha.backup.install import litestream_path
            _ls = litestream_path()
            ctx["litestream_installed"] = _ls is not None
            ctx["litestream_path"] = str(_ls) if _ls else ""
            return templates.TemplateResponse(
                request, "backups.html", ctx,
            )
        finally:
            session.close()

    @router.post(
        "/backups/offdisk/configure",
        response_class=HTMLResponse,
        response_model=None,
    )
    def backups_offdisk_configure(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        provider: Annotated[str, Form()] = "",
        bucket: Annotated[str, Form()] = "",
        region: Annotated[str, Form()] = "",
        account_id: Annotated[str, Form()] = "",
        access_key_id: Annotated[str, Form()] = "",
        secret_access_key: Annotated[str, Form()] = "",
        endpoint_override: Annotated[str, Form()] = "",
        verify: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        """Off-disk backup setup wizard — non-technical Mike clicks
        through this; no shell required."""
        try:
            from korpha.backup.offdisk import (
                configure_offdisk, start_replicator, verify_credentials,
            )
            try:
                cfg = configure_offdisk(
                    provider=provider,
                    bucket=bucket,
                    region=region,
                    access_key_id=access_key_id,
                    secret_access_key=secret_access_key,
                    account_id=account_id or None,
                    endpoint_override=endpoint_override or None,
                )
            except ValueError as exc:
                return RedirectResponse(
                    f"/app/backups?error={str(exc)[:120]}",
                    status_code=status.HTTP_303_SEE_OTHER,
                )

            verified_msg = ""
            if verify:
                ok, msg = verify_credentials(
                    cfg,
                    access_key_id=access_key_id,
                    secret_access_key=secret_access_key,
                )
                verified_msg = f" — {msg}"
                if not ok:
                    return RedirectResponse(
                        f"/app/backups?error="
                        f"Saved+but+verify+failed:+{msg[:80]}",
                        status_code=status.HTTP_303_SEE_OTHER,
                    )

            ok, msg, pid = start_replicator(cfg)
            if not ok:
                # Auto-recover the common "binary missing" first-time
                # case. New founders haven't installed litestream yet;
                # rather than make them click around, install in-line
                # then retry start. Other errors (config, perms,
                # network) bubble up as before.
                from korpha.backup.install import (
                    install_litestream, litestream_path,
                )
                if litestream_path() is None:
                    install_res = install_litestream()
                    if install_res.ok:
                        ok, msg, pid = start_replicator(cfg)
                if not ok:
                    return RedirectResponse(
                        f"/app/backups?error=Saved+but+couldn%27t+start:"
                        f"+{msg[:80]}",
                        status_code=status.HTTP_303_SEE_OTHER,
                    )
            return RedirectResponse(
                f"/app/backups?flash=Off-disk+backup+active"
                f"+→+{cfg.bucket}{verified_msg}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        finally:
            session.close()

    @router.post(
        "/backups/install-litestream",
        response_class=HTMLResponse,
        response_model=None,
    )
    def backups_install_litestream(
        session: Annotated[Session, Depends(require_session)],
    ) -> RedirectResponse:
        """Mike-friendly one-click install for the litestream binary.

        Off-disk backup requires litestream on PATH. Rather than make
        the founder hunt for the install command, we download the
        pinned release directly into ~/.local/bin and verify the
        SHA-256. The next 'Start replicator' click then Just Works.
        """
        try:
            from korpha.backup.install import install_litestream
            result = install_litestream()
            key = "flash" if result.ok else "error"
            return RedirectResponse(
                f"/app/backups?{key}={result.message[:120]}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        finally:
            session.close()

    @router.post(
        "/backups/offdisk/restore",
        response_class=HTMLResponse,
        response_model=None,
    )
    def backups_offdisk_restore(
        session: Annotated[Session, Depends(require_session)],
    ) -> RedirectResponse:
        """Disaster-recovery: pull latest snapshot+WAL from off-disk
        replica back into korpha.db. Used when the live DB is lost
        or corrupted but the off-disk replica is intact.

        Builds a fresh OffDiskConfig from the persisted status file
        (same path the toggle/Start handler uses) so this works even
        when the replicator daemon isn't currently running.
        """
        try:
            from korpha.backup.offdisk import (
                OffDiskConfig, current_status, restore_from_offdisk,
            )
            import os as _os
            status_dict = current_status()
            if status_dict is None:
                return RedirectResponse(
                    "/app/backups?error=Off-disk+not+configured+yet",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            data_dir = Path(
                _os.getenv("KORPHA_DATA_DIR")
                or _os.path.expanduser("~/.korpha")
            )
            cfg = OffDiskConfig(
                provider=status_dict["provider"],
                bucket=status_dict["bucket"],
                endpoint=status_dict.get("endpoint", ""),
                region=status_dict.get("region", ""),
                creds_path=data_dir / "secrets" / "litestream-s3.creds.enc",
                config_path=data_dir / "litestream.yml",
                runner_path=data_dir / "litestream-run.sh",
            )
            ok, msg = restore_from_offdisk(cfg, data_dir=data_dir)
            key = "flash" if ok else "error"
            return RedirectResponse(
                f"/app/backups?{key}={msg[:120]}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        finally:
            session.close()

    @router.post(
        "/backups/offdisk/toggle",
        response_class=HTMLResponse,
        response_model=None,
    )
    def backups_offdisk_toggle(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        action: Annotated[str, Form()] = "start",
    ) -> RedirectResponse:
        try:
            from korpha.backup.offdisk import (
                OffDiskConfig, PROVIDERS, current_status, start_replicator,
                stop_replicator,
            )
            import os as _os
            if action == "stop":
                ok, msg = stop_replicator()
                flash = "Replicator stopped" if ok else f"Error: {msg}"
            else:
                status_dict = current_status()
                if status_dict is None:
                    return RedirectResponse(
                        "/app/backups?error=Off-disk+not+configured+yet",
                        status_code=status.HTTP_303_SEE_OTHER,
                    )
                # Reconstruct enough OffDiskConfig to call start
                data_dir = Path(
                    _os.getenv("KORPHA_DATA_DIR")
                    or _os.path.expanduser("~/.korpha")
                )
                cfg = OffDiskConfig(
                    provider=status_dict["provider"],
                    bucket=status_dict["bucket"],
                    endpoint=status_dict.get("endpoint", ""),
                    region=status_dict.get("region", ""),
                    creds_path=data_dir / "secrets" / "litestream-s3.creds.enc",
                    config_path=data_dir / "litestream.yml",
                    runner_path=data_dir / "litestream-run.sh",
                )
                ok, msg, pid = start_replicator(cfg)
                flash = (
                    f"Replicator started (pid {pid})" if ok
                    else f"Error: {msg}"
                )
            key = "flash" if "stop" in flash.lower() or "started" in flash.lower() else "error"
            return RedirectResponse(
                f"/app/backups?{key}={flash[:80]}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        finally:
            session.close()

    @router.post(
        "/backups/offdisk/disconnect",
        response_class=HTMLResponse,
        response_model=None,
    )
    def backups_offdisk_disconnect(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> RedirectResponse:
        try:
            from korpha.backup.offdisk import (
                _config_status_path, stop_replicator,
            )
            import os as _os
            data_dir = Path(
                _os.getenv("KORPHA_DATA_DIR")
                or _os.path.expanduser("~/.korpha")
            )
            stop_replicator()
            # Drop the status file + creds + config — but keep the
            # secrets vault entry encrypted on disk in case the user
            # changes their mind.
            for p in [
                _config_status_path(data_dir),
                data_dir / "litestream.yml",
                data_dir / "litestream-run.sh",
                data_dir / "litestream.pid",
            ]:
                p.unlink(missing_ok=True)
            return RedirectResponse(
                "/app/backups?flash=Off-disk+disconnected.+Local+snapshots+continue.",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        finally:
            session.close()

    @router.post(
        "/backups/snapshot",
        response_class=HTMLResponse,
        response_model=None,
    )
    def backups_snapshot_post(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        full: Annotated[str, Form()] = "0",
    ) -> RedirectResponse:
        try:
            from korpha.backup import take_db_snapshot, take_full_backup
            if str(full) == "1":
                info = take_full_backup()
                label = f"full bundle {info.path.name}"
            else:
                info = take_db_snapshot()
                label = f"db snapshot {info.path.name}"
            return RedirectResponse(
                f"/app/backups?flash={label}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        except Exception as exc:  # noqa: BLE001
            return RedirectResponse(
                f"/app/backups?error={str(exc)[:80]}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        finally:
            session.close()

    @router.post(
        "/backups/restore",
        response_class=HTMLResponse,
        response_model=None,
    )
    def backups_restore_post(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        snapshot: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        try:
            from korpha.backup import restore_db_snapshot
            if not snapshot.strip():
                return RedirectResponse(
                    "/app/backups?error=Snapshot+name+required",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            try:
                target = restore_db_snapshot(snapshot.strip())
            except FileNotFoundError as exc:
                return RedirectResponse(
                    f"/app/backups?error={str(exc)[:80]}",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            return RedirectResponse(
                f"/app/backups?flash=Restored+{snapshot.strip()}+"
                f"—+server+restart+recommended",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        finally:
            session.close()

    @router.get("/disk", response_class=HTMLResponse)
    def disk_view(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse:
        """Storage breakdown + vacuum trigger. Reuses the same
        helpers ``korpha disk`` CLI does so the numbers always
        match between surfaces."""
        try:
            from pathlib import Path as _P

            from korpha.checkpoints.v2 import disk_breakdown
            from korpha.config import get_settings

            ctx = _ctx(session, active="disk")
            rows: list[tuple[str, int, str]] = []
            data_root = _P(
                __import__("os").environ.get("KORPHA_DATA_DIR")
                or str(_P.home() / ".korpha")
            )

            # Main DB size
            try:
                db_url = get_settings().db_url
                if db_url.startswith("sqlite:///"):
                    db_path = _P(db_url[len("sqlite:///"):])
                    if db_path.is_file():
                        rows.append((
                            "Main DB (sqlite)",
                            db_path.stat().st_size,
                            str(db_path),
                        ))
            except Exception:  # noqa: BLE001
                pass

            try:
                bd = disk_breakdown()
                rows.append((
                    f"Checkpoint blobs ({bd['blob_count']} files)",
                    bd["blob_bytes"],
                    str(data_root / "checkpoints" / "blobs"),
                ))
                for w in bd["workspaces"]:
                    rows.append((
                        f"Workspace '{w['slug']}'",
                        w["v1_bytes"] + w["manifest_bytes"],
                        str(data_root / "checkpoints" / w["slug"]),
                    ))
            except Exception:  # noqa: BLE001
                pass

            for label, sub in (
                ("Agent-authored skills", "skills"),
                ("Cron scripts", "cron-scripts"),
                ("Background job logs", "jobs"),
            ):
                p = data_root / sub
                if p.is_dir():
                    size = sum(
                        f.stat().st_size for f in p.rglob("*")
                        if f.is_file()
                    )
                    rows.append((label, size, str(p)))

            # Audit archive
            try:
                from korpha.audit.retention import (
                    archive_size_breakdown,
                )
                ab = archive_size_breakdown()
                if ab["total_bytes"] > 0:
                    rows.append((
                        f"Audit archive ({len(ab['files'])} files)",
                        ab["total_bytes"],
                        str(data_root / "archive"),
                    ))
            except Exception:  # noqa: BLE001
                pass

            total = sum(r[1] for r in rows)
            ctx["rows"] = [
                (
                    label, _human_bytes(size), loc,
                    int((size / total) * 100) if total else 0,
                )
                for label, size, loc in rows
            ]
            ctx["total_human"] = _human_bytes(total)
            return templates.TemplateResponse(
                request, "disk.html", ctx,
            )
        finally:
            session.close()

    @router.post(
        "/disk/vacuum",
        response_class=HTMLResponse,
        response_model=None,
    )
    def disk_vacuum_post(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> RedirectResponse:
        """Run checkpoint v2 GC + sqlite VACUUM. Best-effort; we
        report what was reclaimed via redirect query params so the
        founder gets immediate feedback."""
        try:
            import sqlite3
            from pathlib import Path as _P

            from korpha.checkpoints.v2 import vacuum
            from korpha.config import get_settings

            stats = vacuum()
            db_reclaimed = 0
            try:
                db_url = get_settings().db_url
                if db_url.startswith("sqlite:///"):
                    db_path = _P(db_url[len("sqlite:///"):])
                    if db_path.is_file():
                        before = db_path.stat().st_size
                        conn = sqlite3.connect(str(db_path))
                        try:
                            conn.execute("VACUUM")
                            conn.commit()
                        finally:
                            conn.close()
                        after = db_path.stat().st_size
                        db_reclaimed = max(0, before - after)
            except Exception:  # noqa: BLE001
                pass

            return RedirectResponse(
                "/app/disk?vacuumed=1"
                f"&reclaimed={_human_bytes(stats['bytes_reclaimed'])}"
                f"&blobs={stats['blobs_deleted']}"
                f"&tmp={stats['tmp_swept']}"
                f"&db_reclaimed={_human_bytes(db_reclaimed)}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        finally:
            session.close()

    @router.get("/checkpoints", response_class=HTMLResponse)
    def checkpoints_view(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse:
        """List workspace snapshots — one section per Repo. Click
        restore to undo a Codex run without dropping to terminal."""
        try:
            from korpha.checkpoints import list_checkpoints
            from korpha.workspaces.model import Repo

            ctx = _ctx(session, active="checkpoints")
            repos = list(session.exec(
                select(Repo).where(Repo.business_id == ctx["business"].id)
            ).all())
            sections: list[dict] = []
            for repo in repos:
                cps = list_checkpoints(repo.local_path)
                sections.append({
                    "repo": repo,
                    "checkpoints": cps,
                })
            ctx["sections"] = sections
            return templates.TemplateResponse(
                request, "checkpoints.html", ctx,
            )
        finally:
            session.close()

    @router.post(
        "/checkpoints/{repo_id}/{snapshot_id}/restore",
        response_class=HTMLResponse,
        response_model=None,
    )
    def checkpoints_restore_post(
        repo_id: str,
        snapshot_id: str,
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse | RedirectResponse:
        """Restore a snapshot via the dashboard. Auto-takes a
        pre-restore snapshot (the manager's default) so the founder
        can redo if the restore was a mistake."""
        try:
            from uuid import UUID as _UUID

            from korpha.checkpoints import (
                CheckpointError, restore,
            )
            from korpha.workspaces.model import Repo

            ctx = _ctx(session, active="checkpoints")
            try:
                repo_uuid = _UUID(repo_id)
            except ValueError:
                ctx["error"] = "bad repo id"
                ctx["sections"] = []
                return templates.TemplateResponse(
                    request, "checkpoints.html", ctx, status_code=400,
                )
            repo = session.get(Repo, repo_uuid)
            if repo is None or repo.business_id != ctx["business"].id:
                ctx["error"] = (
                    "Repo not found or not owned by this business."
                )
                ctx["sections"] = []
                return templates.TemplateResponse(
                    request, "checkpoints.html", ctx, status_code=404,
                )
            try:
                pre = restore(repo.local_path, snapshot_id)
            except CheckpointError as exc:
                ctx["error"] = f"Restore failed: {exc}"
                ctx["sections"] = []
                return templates.TemplateResponse(
                    request, "checkpoints.html", ctx, status_code=500,
                )
            # 303 → GET so a refresh doesn't re-trigger restore
            return RedirectResponse(
                f"/app/checkpoints?restored={snapshot_id}&pre={pre.id}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        finally:
            session.close()

    @router.get("/insights", response_class=HTMLResponse)
    def insights_view(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        days: int = 7,
    ) -> HTMLResponse:
        """Founder-facing 'cost / hours saved' aggregate. The screenshot
        money shot for Skool meetups + retention reports."""
        try:
            from korpha.insights import compute_insights
            # Clamp to a reasonable range so a hostile / fat-fingered
            # ?days=99999 doesn't query 274 years of nothing.
            days_clamped = max(1, min(365, int(days)))
            ctx = _ctx(session, active="insights")
            ctx["insights"] = compute_insights(
                session,
                business_id=ctx["business"].id,
                window_days=days_clamped,
            )
            ctx["window_choices"] = [1, 7, 30, 90]
            ctx["selected_days"] = days_clamped
            return templates.TemplateResponse(request, "insights.html", ctx)
        finally:
            session.close()

    @router.get("/settings", response_class=HTMLResponse)
    def settings_view(
        request: Request, session: Annotated[Session, Depends(require_session)]
    ) -> HTMLResponse:
        try:
            ctx = _ctx(session, active="settings")
            return templates.TemplateResponse(request, "settings.html", ctx)
        finally:
            session.close()

    @router.get("/costs", response_class=HTMLResponse)
    def costs(request: Request, session: Annotated[Session, Depends(require_session)]) -> HTMLResponse:
        try:
            ctx = _ctx(session, active="costs")
            ctx["spend"] = _compute_spend(session, ctx["business"].id)
            ctx["by_tier"] = _spend_by_tier(session, ctx["business"].id)
            return templates.TemplateResponse(request, "costs.html", ctx)
        finally:
            session.close()

    @router.get("/partials/sidebar-live-counts", response_class=HTMLResponse)
    def sidebar_live_counts(
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse:
        """Tiny fragment that the base template polls every 10s. Live count
        means agents that have produced an Activity event in the last
        LIVE_WINDOW seconds — i.e. *actually doing something right now*,
        not just having work assigned to them."""
        try:
            ctx = _ctx(session)
            biz_id = ctx["business"].id
            threshold = utcnow() - _LIVE_WINDOW
            recent_actors = session.exec(
                select(Activity.actor_id)
                .where(Activity.business_id == biz_id)
                .where(Activity.created_at >= threshold)
                .where(Activity.actor_id.is_not(None))  # type: ignore[union-attr]
            ).all()
            live = len({a for a in recent_actors if a is not None})

            pending_approvals = len(
                list(
                    session.exec(
                        select(Approval)
                        .where(Approval.business_id == biz_id)
                        .where(Approval.status == ApprovalStatus.PENDING)
                    ).all()
                )
            )
            html = f'''
<a href="/app/dashboard" class="nav-item nav-item-live"
   hx-swap-oob="outerHTML:.nav-item-live">
  <span class="icon">⬚</span>
  <span class="label">Dashboard</span>
  {f'<span class="badge badge-live">{live} live</span>' if live else ''}
</a>
<a href="/app/approvals" class="nav-item nav-item-approvals"
   hx-swap-oob="outerHTML:.nav-item-approvals">
  <span class="icon">✓</span>
  <span class="label">Approvals</span>
  {f'<span class="badge badge-pending">{pending_approvals}</span>' if pending_approvals else ''}
</a>
            '''.strip()
            return HTMLResponse(html)
        finally:
            session.close()

    @router.get("/partials/cost-pill", response_class=HTMLResponse)
    def cost_pill(session: Annotated[Session, Depends(require_session)]) -> HTMLResponse:
        try:
            _, business = founder_business(session)
            spend = _compute_spend(session, business.id)
            today = float(spend["today"])
            saved = float(spend["saved_vs_sonnet"])
            if saved > 0.01:
                savings_html = (
                    f'<span class="cost-pill-savings">·&nbsp;saved ${saved:.2f} '
                    f"vs Sonnet</span>"
                )
            else:
                savings_html = ""
            html = (
                f'<span class="cost-pill-amount">${today:.4f}</span>'
                f'<span class="cost-pill-suffix">today</span>{savings_html}'
            )
            return HTMLResponse(html)
        finally:
            session.close()

    # ---------------------------------------------------------------- Providers
    # Mike-friendly in-dashboard provider wizard. Mirrors the
    # `korpha config` CLI flow as HTML forms — non-technical
    # Founders never have to open a terminal.

    def _provider_error(msg: str) -> str:
        """Render a visible HTMX-friendly error card for the provider form."""
        from html import escape
        return (
            '<div class="card provider-form-card">'
            '<div class="provider-empty" style="color:var(--red);">'
            f'⚠ {escape(msg)}'
            '</div></div>'
        )

    @router.get("/settings/providers/list", response_class=HTMLResponse)
    def providers_list_partial() -> HTMLResponse:
        """Render the configured-providers list. Reads providers.yaml
        directly so the partial is a snapshot of the truth, not a
        cached state."""
        from korpha.inference.config import (
            ProviderConfigError,
            config_path,
            load_from_yaml,
        )

        rows: list[str] = []
        try:
            loaded = load_from_yaml()
        except ProviderConfigError as exc:
            rows.append(
                f'<div class="provider-empty">⚠ providers.yaml has an error: '
                f'{str(exc)[:200]}</div>'
            )
            return HTMLResponse(f'<div class="card">{"".join(rows)}</div>')

        if loaded is None or not loaded.accounts:
            rows.append(
                '<div class="provider-empty">No providers configured yet. '
                'Click "+ Add provider" below to wire one up.</div>'
            )
        else:
            # Aggregate tier coverage across every configured account.
            # Used to render the missing-vision nudge banner above the
            # list when no provider serves the VISION tier.
            covered_tiers: set[str] = set()
            for account in loaded.accounts:
                for tier in account.tier_models:
                    covered_tiers.add(tier.value)

            from korpha.api.server import _data_dir as _server_data_dir
            nudge_dismissed = (
                Path(_server_data_dir()) / ".vision-nudge-dismissed"
            ).exists()
            if "vision" not in covered_tiers and not nudge_dismissed:
                rows.append(
                    '<div class="provider-row vision-nudge"'
                    ' style="background:rgba(255,200,0,0.08);'
                    'border-left:3px solid var(--accent);padding:10px 14px;'
                    'margin-bottom:6px;border-radius:6px;">'
                    '  <div class="provider-meta">'
                    '    <span class="provider-meta-name">⚠ No vision model configured</span>'
                    '    <span class="provider-meta-detail">'
                    '      Your agents can\'t see images yet (screenshots, product photos, '
                    '      competitor pages, OCR). Recommended: NVIDIA Nemotron 3 Nano Omni '
                    '      — free on OpenRouter, also on NVIDIA NIM. Optional but very '
                    '      useful for marketing + design work.'
                    '    </span>'
                    '  </div>'
                    '  <div style="display:flex;gap:6px;">'
                    '    <button class="provider-remove-btn"'
                    '            hx-get="/app/settings/providers/new?preset=openrouter"'
                    '            hx-target="#provider-form"'
                    '            hx-swap="innerHTML"'
                    '            style="border-color:var(--accent);color:var(--accent);">'
                    '      + Add OpenRouter (free vision)'
                    '    </button>'
                    '    <button class="provider-remove-btn"'
                    '            hx-get="/app/settings/providers/new?preset=nvidia-nim"'
                    '            hx-target="#provider-form"'
                    '            hx-swap="innerHTML"'
                    '            style="border-color:var(--accent);color:var(--accent);">'
                    '      + Add NVIDIA NIM'
                    '    </button>'
                    '    <form method="post" action="/app/settings/providers/dismiss-vision-nudge"'
                    '          hx-post="/app/settings/providers/dismiss-vision-nudge"'
                    '          hx-target="#providers-panel" hx-swap="innerHTML"'
                    '          style="display:inline;">'
                    '      <button type="submit" class="provider-remove-btn">Skip</button>'
                    '    </form>'
                    '  </div>'
                    '</div>'
                )

            # Render each configured account row with per-tier badges
            # so Mike sees at a glance which tiers each provider covers.
            for account in loaded.accounts:
                covered = {t.value for t in account.tier_models}
                badges_html = "".join(
                    f'<span style="display:inline-block;padding:2px 6px;'
                    f'border-radius:3px;font-size:10px;margin-right:4px;'
                    f'background:{"rgba(80,200,120,0.15)" if t in covered else "rgba(150,150,150,0.1)"};'
                    f'color:{"var(--green, #5fb878)" if t in covered else "var(--text-faint)"};">'
                    f'{("✓" if t in covered else "·")} {t}'
                    f'</span>'
                    for t in ("workhorse", "pro", "vision")
                )
                tiers = ", ".join(
                    f"{t.value}={m}" for t, m in sorted(
                        account.tier_models.items(), key=lambda kv: kv[0].value
                    )
                )
                label = account.label or account.provider_name
                cap = (
                    f' · cap ${account.spend_cap_usd:.2f}/mo'
                    if account.spend_cap_usd
                    else ''
                )
                rows.append(
                    f'<div class="provider-row">'
                    f'  <div class="provider-meta">'
                    f'    <span class="provider-meta-name">{label}</span>'
                    f'    <span style="margin:4px 0;">{badges_html}</span>'
                    f'    <span class="provider-meta-detail">'
                    f'      {account.provider_name} · {tiers}{cap}'
                    f'    </span>'
                    f'  </div>'
                    f'  <button class="provider-remove-btn"'
                    f'          hx-post="/app/settings/providers/{label}/remove"'
                    f'          hx-target="#providers-panel"'
                    f'          hx-swap="innerHTML"'
                    f'          hx-confirm="Remove provider {label!r}?">'
                    f'    Remove'
                    f'  </button>'
                    f'</div>'
                )
        path_str = str(config_path())
        rows.append(
            f'<div style="padding-top:8px;font-size:11px;color:var(--text-faint);">'
            f'  Stored at <code>{path_str}</code>'
            f'</div>'
        )
        return HTMLResponse(f'<div class="card">{"".join(rows)}</div>')

    # Recommended presets — the short list Mike sees up-front. Hosted
    # cloud providers with sensible cost-ascending defaults + local
    # Ollama as the OSS-first $0 path. Everything else (self-hosted
    # variants, custom endpoints, regional providers) lives under the
    # Advanced expander so the main form stays tidy.
    _RECOMMENDED_PRESETS: tuple[str, ...] = (
        "opencode-go",
        "ollama-cloud",
        "openrouter",
        "nvidia-nim",
        "deepseek",
        "local-ollama",
        "codex-cli",
        "claude-code-cli",
    )

    # Per-preset hint shown under the picker so Mike understands what
    # he's signing up for. Empty string = no hint.
    _PRESET_HINTS: dict[str, str] = {
        "local-ollama": (
            "Free, but needs ~24 GB VRAM (Linux/Windows GPU) "
            "or a Mac with at least 24 GB unified RAM for usable "
            "quality. Install Ollama first: https://ollama.com"
        ),
        "lm-studio": (
            "Free, but needs ~24 GB VRAM or a Mac with at least "
            "24 GB unified RAM. Install LM Studio and start its "
            "Local Server first: https://lmstudio.ai"
        ),
        "codex-cli": (
            "No API key needed — uses your existing ChatGPT login "
            "via Codex CLI. Install + log in first: https://docs.openai.com/codex"
        ),
        "claude-code-cli": (
            "No API key needed — uses your existing Anthropic login "
            "via Claude Code CLI. Install + log in first: "
            "https://docs.claude.com/en/docs/claude-code/setup"
        ),
    }

    @router.get("/settings/providers/new", response_class=HTMLResponse)
    def provider_form_partial(
        preset: str = "",
        scope: str = "main",
    ) -> HTMLResponse:
        """Render the add-provider form. When ``preset`` is set in the
        query string, pre-fill the workhorse / pro / vision model
        fields with the known good defaults for that preset — Mike
        doesn't need to know "what's a valid model id for ollama-cloud."
        He can still override the values; they're starting points.

        ``scope`` controls the preset list shown:
        - ``main`` (default): only recommended presets (the Mike path)
        - ``advanced``: every preset including self-hosted, custom,
          Anthropic/OpenAI direct, LM Studio, local Ollama. Used by
          the collapsed Advanced expander on /app/settings.
        """
        from korpha.inference.providers.openai_compat import (
            PROVIDER_PRESETS,
            SUBSCRIPTION_PRESETS,
        )
        from korpha.inference.env_fallback import get_preset_defaults

        is_advanced = scope == "advanced"
        if is_advanced:
            # Everything not already in Recommended, plus 'custom' for
            # arbitrary OpenAI-compat endpoints (vLLM, custom shims, …).
            advanced = sorted(
                (set(PROVIDER_PRESETS) | {"custom"})
                - set(_RECOMMENDED_PRESETS)
            )
            ordered = advanced
            default_preset = "local-ollama"
        else:
            ordered = list(_RECOMMENDED_PRESETS)
            default_preset = "opencode-go"

        chosen = preset if preset in ordered else default_preset
        options = "".join(
            f'<option value="{p}"{" selected" if p == chosen else ""}>{p}</option>'
            for p in ordered
        )

        defaults = get_preset_defaults(chosen) or {}
        workhorse_default = defaults.get("workhorse_model", "")
        pro_default = defaults.get("pro_model", "")
        vision_default = defaults.get("vision_model", "")
        # Subscription presets don't take a key — show a "(none needed)"
        # placeholder instead of a "paste sk-..." prompt.
        is_subscription = chosen in SUBSCRIPTION_PRESETS
        key_placeholder = (
            "(none needed — uses your local CLI login)"
            if is_subscription
            else "paste your API key — encrypted at rest"
        )
        key_disabled = "disabled" if is_subscription else ""

        # Suggest a sensible default label so Mike doesn't have to
        # invent one — he can still edit before submitting.
        default_label = f"{chosen}-primary" if chosen != "custom" else ""

        # Custom preset needs explicit name + base_url; self-hosted
        # presets (local-ollama, lm-studio) take an optional host
        # override. Both are surfaced inline so power users coming
        # from Hermes/OpenClaw can wire their own endpoint without
        # editing YAML by hand.
        needs_endpoint = chosen == "custom"
        is_self_hosted = chosen in ("local-ollama", "lm-studio")
        host_hint = ""
        if chosen == "local-ollama":
            host_hint = "http://localhost:11434"
        elif chosen == "lm-studio":
            host_hint = "http://localhost:1234"

        scope_hidden = f'<input type="hidden" name="scope" value="{scope}" />'
        preset_hint = _PRESET_HINTS.get(chosen, "")
        preset_hint_html = (
            f'<div class="provider-form-hint" '
            f'style="grid-column:2;margin-top:-4px;color:var(--text-faint);">'
            f'{preset_hint}</div>'
            if preset_hint else ""
        )

        endpoint_fields = ""
        if needs_endpoint:
            endpoint_fields = """
              <label class="provider-form-label" for="provider_name">Provider id</label>
              <input name="provider_name" id="provider_name" class="provider-form-input"
                     placeholder="e.g. my-vllm" required />

              <label class="provider-form-label" for="base_url">Base URL</label>
              <input name="base_url" id="base_url" class="provider-form-input"
                     placeholder="https://api.example.com/v1" required />
              <div class="provider-form-hint">
                The OpenAI-compatible chat-completions endpoint. Anthropic native
                speaks this dialect at api.anthropic.com/v1. Most local servers
                (vLLM, llama.cpp, TGI, …) work out of the box.
              </div>
            """
        elif is_self_hosted:
            endpoint_fields = f"""
              <label class="provider-form-label" for="base_url">Host (optional)</label>
              <input name="base_url" id="base_url" class="provider-form-input"
                     placeholder="{host_hint}" />
              <div class="provider-form-hint">
                Leave blank for the default. Override if your daemon
                runs on a different port or remote host.
              </div>
            """

        html = f"""
        <div class="card provider-form-card">
          <form hx-post="/app/settings/providers"
                hx-target="#providers-panel"
                hx-swap="innerHTML"
                hx-on::after-request="if (event.detail.successful) document.getElementById('provider-form').innerHTML = ''">
            {scope_hidden}
            <div class="provider-form-grid">
              <label class="provider-form-label" for="preset">Provider</label>
              <select name="preset" id="preset" class="provider-form-select" required
                      hx-get="/app/settings/providers/new?scope={scope}"
                      hx-target="#provider-form"
                      hx-swap="innerHTML"
                      hx-include="[name=preset]"
                      hx-trigger="change">
                {options}
              </select>
              {preset_hint_html}

              <label class="provider-form-label" for="label">Label</label>
              <input name="label" id="label" class="provider-form-input"
                     value="{default_label}"
                     placeholder="e.g. opencode-go-primary" required />

              <label class="provider-form-label" for="api_key">API key</label>
              <input name="api_key" id="api_key" class="provider-form-input"
                     type="password" placeholder="{key_placeholder}" {key_disabled} />
              <div class="provider-form-hint">
                Leave blank for subscription-auth presets (codex-cli, claude-code-cli)
                and most local servers that don't require auth.
              </div>
              {endpoint_fields}

              <label class="provider-form-label" for="workhorse_model">Workhorse model</label>
              <input name="workhorse_model" id="workhorse_model" class="provider-form-input"
                     value="{workhorse_default}"
                     placeholder="e.g. deepseek-v4-flash" />

              <label class="provider-form-label" for="pro_model">Pro model</label>
              <input name="pro_model" id="pro_model" class="provider-form-input"
                     value="{pro_default}"
                     placeholder="e.g. deepseek-v4-pro" />

              <label class="provider-form-label" for="vision_model">Vision model</label>
              <input name="vision_model" id="vision_model" class="provider-form-input"
                     value="{vision_default}"
                     placeholder="(optional — leave blank if this provider doesn't host one)" />
              <div class="provider-form-hint">
                These are sensible defaults for this provider. Override any of them
                with a model id you prefer. Leave blank to skip that tier.
              </div>

              <label class="provider-form-label" for="spend_cap">Spend cap (USD/mo)</label>
              <input name="spend_cap" id="spend_cap" class="provider-form-input"
                     type="number" step="0.01" min="0" placeholder="optional" />
            </div>

            <div class="provider-form-actions">
              <button type="button" class="provider-form-cancel"
                      onclick="document.getElementById('provider-form').innerHTML = ''">
                Cancel
              </button>
              <button type="submit" class="provider-form-submit">
                Add provider
              </button>
            </div>
          </form>
        </div>
        """
        return HTMLResponse(html)

    @router.post("/settings/providers", response_class=HTMLResponse)
    def provider_create(
        preset: Annotated[str, Form()],
        label: Annotated[str, Form()],
        api_key: Annotated[str, Form()] = "",
        pro_model: Annotated[str, Form()] = "",
        workhorse_model: Annotated[str, Form()] = "",
        vision_model: Annotated[str, Form()] = "",
        spend_cap: Annotated[str, Form()] = "",
        provider_name: Annotated[str, Form()] = "",
        base_url: Annotated[str, Form()] = "",
    ) -> HTMLResponse:
        """Append a new provider entry to providers.yaml.

        Validation + dedupe rules:
        - Apply preset defaults for any blank model field so a Mike who
          submits without overriding still gets workable model strings.
        - Reject the POST cleanly (with a visible error) if the final
          entry has no tiers — never silently write broken YAML.
        - If a provider entry with the same ``label`` already exists,
          replace it instead of appending. No duplicate accumulation
          on retry.
        """
        from korpha.inference.config_writer import (
            append_provider_entry, remove_provider_entry,
        )
        from korpha.inference.env_fallback import get_preset_defaults
        from korpha.inference.providers.openai_compat import (
            SUBSCRIPTION_PRESETS,
        )

        label = label.strip()
        if not label:
            return HTMLResponse(
                _provider_error("label is required"),
                status_code=400,
            )

        # Apply preset defaults for any blank field. Mike picks
        # "opencode-go" → workhorse + pro auto-fill if he didn't
        # override.
        defaults = get_preset_defaults(preset) or {}
        workhorse_final = (
            workhorse_model.strip() or defaults.get("workhorse_model", "")
        )
        pro_final = pro_model.strip() or defaults.get("pro_model", "")
        vision_final = vision_model.strip() or defaults.get("vision_model", "")

        tiers: dict[str, str] = {}
        if workhorse_final:
            tiers["workhorse"] = workhorse_final
        if pro_final:
            tiers["pro"] = pro_final
        if vision_final:
            tiers["vision"] = vision_final

        if not tiers:
            return HTMLResponse(
                _provider_error(
                    f"No model tiers set. Pick a known preset (e.g. "
                    f"opencode-go) or fill at least one model field "
                    f"(workhorse / pro / vision)."
                ),
                status_code=400,
            )

        entry: dict[str, Any] = {
            "preset": preset,
            "label": label,
            "tiers": tiers,
        }

        # Custom preset: name + base_url are mandatory. Self-hosted
        # presets (local-ollama / lm-studio) accept an optional
        # base_url override and don't need an api_key.
        is_self_hosted = preset in ("local-ollama", "lm-studio")
        if preset == "custom":
            if not provider_name.strip():
                return HTMLResponse(
                    _provider_error("Provider id is required for custom endpoint."),
                    status_code=400,
                )
            if not base_url.strip():
                return HTMLResponse(
                    _provider_error("Base URL is required for custom endpoint."),
                    status_code=400,
                )
            entry["name"] = provider_name.strip()
            entry["base_url"] = base_url.strip()
        elif is_self_hosted and base_url.strip():
            entry["base_url"] = base_url.strip()

        # API-key validation. Subscription + self-hosted presets are
        # the only ones that may legitimately omit a key.
        if api_key.strip():
            entry["api_key"] = api_key.strip()
        elif (
            preset not in SUBSCRIPTION_PRESETS
            and not is_self_hosted
            and preset != "custom"
        ):
            return HTMLResponse(
                _provider_error(
                    f"API key required for preset '{preset}'. "
                    f"Subscription presets (codex-cli / claude-code-cli) "
                    f"and self-hosted presets (local-ollama / lm-studio) "
                    f"are the only ones that work without a key."
                ),
                status_code=400,
            )
        if spend_cap.strip():
            with contextlib.suppress(ValueError):
                entry["spend_cap_usd"] = float(spend_cap)

        # Dedupe by label: drop any existing entry with the same label
        # before appending so retries replace rather than accumulate.
        with contextlib.suppress(Exception):
            remove_provider_entry(label)

        try:
            append_provider_entry(entry)
        except Exception as exc:
            return HTMLResponse(
                _provider_error(f"Failed to save: {str(exc)[:200]}"),
                status_code=500,
            )
        return providers_list_partial()

    @router.post(
        "/settings/providers/{label}/remove", response_class=HTMLResponse
    )
    def provider_remove(label: str) -> HTMLResponse:
        from korpha.inference.config_writer import remove_provider_entry

        remove_provider_entry(label)
        return providers_list_partial()

    @router.post(
        "/settings/providers/dismiss-vision-nudge",
        response_class=HTMLResponse,
    )
    def provider_dismiss_vision_nudge() -> HTMLResponse:
        """Persist Mike's "skip" choice on the missing-vision nudge so
        it doesn't reappear every page render. Stored as a marker file
        in the data dir — survives restarts, simple to undo by deleting
        the file."""
        from korpha.api.server import _data_dir as _server_data_dir
        marker = Path(_server_data_dir()) / ".vision-nudge-dismissed"
        with contextlib.suppress(OSError):
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
        return providers_list_partial()

    @router.get("/settings/themes/edit", response_class=HTMLResponse, response_model=None)
    def theme_editor(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse:
        """Visual theme editor — color pickers + density + radius +
        live preview pane. Saves to ~/.korpha/dashboard-themes/."""
        try:
            ctx = _ctx(session, active="settings")
            return templates.TemplateResponse(request, "theme_editor.html", ctx)
        finally:
            session.close()

    @router.post("/settings/themes/save", response_class=HTMLResponse, response_model=None)
    def theme_save(
        name: Annotated[str, Form()],
        label: Annotated[str, Form()],
        description: Annotated[str, Form()],
        background: Annotated[str, Form()],
        midground: Annotated[str, Form()],
        warm_glow_color: Annotated[str, Form()],
        warm_glow_opacity: Annotated[str, Form()],
        primary: Annotated[str, Form()],
        success: Annotated[str, Form()],
        warning: Annotated[str, Form()],
        destructive: Annotated[str, Form()],
        radius: Annotated[str, Form()],
        density: Annotated[str, Form()],
    ) -> HTMLResponse:
        """Persist the editor's theme as YAML to
        ``~/.korpha/dashboard-themes/<name>.yaml``. Validates via
        the theme parser (so shipping a bad value gets a clear error)
        before writing."""
        import os
        import re
        from pathlib import Path

        import yaml

        from korpha.themes import parse_theme
        from korpha.themes.types import ThemeValidationError

        # Build the warm_glow rgba from picker color + slider alpha
        try:
            alpha = max(0.0, min(1.0, float(warm_glow_opacity)))
        except ValueError:
            alpha = 0.06
        m = re.match(r"^#([0-9a-fA-F]{6})$", warm_glow_color.strip())
        if m:
            h = m.group(1)
            r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
            warm_glow = f"rgba({r}, {g}, {b}, {alpha:.3f})"
        else:
            warm_glow = "rgba(94, 158, 255, 0.06)"

        body = {
            "name": name,
            "label": label,
            "description": description,
            "palette": {
                "background": background,
                "midground": midground,
                "foreground": {"hex": "#ffffff", "alpha": 0},
                "warm_glow": warm_glow,
                "noise_opacity": 0,
            },
            "layout": {"radius": radius, "density": density},
            "color_overrides": {
                "primary": primary,
                "accent": primary,
                "success": success,
                "warning": warning,
                "destructive": destructive,
            },
        }

        # Validate before writing — gives the editor user a clear
        # error inline instead of a broken YAML on disk.
        try:
            parse_theme(body)
        except ThemeValidationError as exc:
            return HTMLResponse(
                f'<div style="color:var(--red);font-size:12px;">'
                f'⚠ Invalid theme: {str(exc)[:300]}'
                f'</div>',
                status_code=400,
            )

        # Write to ~/.korpha/dashboard-themes/<name>.yaml
        data_dir = (
            Path(os.environ["KORPHA_DATA_DIR"]).expanduser()
            if os.getenv("KORPHA_DATA_DIR")
            else Path.home() / ".korpha"
        )
        target = data_dir / "dashboard-themes" / f"{name}.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            yaml.safe_dump(body, sort_keys=False, default_flow_style=False),
            encoding="utf-8",
        )

        return HTMLResponse(
            f'<div style="color:var(--green);font-size:12px;">'
            f'✓ Saved to {target}. '
            f'<a href="/app/dashboard" style="color:var(--accent);">Pick from the topbar palette icon →</a>'
            f'</div>'
        )

    @router.post("/theme", response_class=HTMLResponse, response_model=None)
    def set_theme_form(
        request: Request,
        name: Annotated[str, Form()],
    ) -> RedirectResponse:
        """Form-submit handler for the topbar theme picker. Redirects
        back to wherever the user came from so the new theme paints
        immediately on their current page."""
        import contextlib

        from korpha.themes import (
            DashboardThemesError,
            set_active_theme_name,
        )

        # Bad theme name — silently fall back without crashing. The
        # picker can only submit names from the menu, so this fires
        # only on a stale tab where a user theme YAML was deleted
        # between page-load and click.
        with contextlib.suppress(DashboardThemesError):
            set_active_theme_name(name)
        referer = request.headers.get("referer") or "/app/dashboard"
        return RedirectResponse(referer, status_code=status.HTTP_303_SEE_OTHER)

    # ---------------------------------------------------------------
    # PR-C: /app/budgets — per-business / per-line / tier caps
    # ---------------------------------------------------------------

    @router.get("/budgets", response_class=HTMLResponse)
    def budgets_view(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse:
        try:
            from korpha.budgets import BudgetService
            from korpha.budgets.currency import format_amount
            from korpha.budgets.service import _spent_in_window
            from korpha.business_units.board import BusinessUnitBoard
            from korpha.config import get_settings

            ctx = _ctx(session, active="budgets")
            biz_id = ctx["business"].id
            svc = BudgetService(session)
            policies = svc.list_for_business(biz_id)
            board = BusinessUnitBoard(session)
            units = list(board.list_for_business(biz_id))
            unit_names = {u.id: u.name for u in units}

            rows = []
            for p in policies:
                spent = _spent_in_window(session, p)
                target = unit_names.get(p.business_unit_id) if p.business_unit_id else None
                rows.append({
                    "id": p.id,
                    "scope": p.scope.value,
                    "window": p.window.value,
                    "target": target or (p.tier or "—"),
                    "label": p.label or "—",
                    "limit_display": format_amount(p.limit_usd),
                    "spent_display": format_amount(spent),
                    "pct": min(100, int(float(spent) / max(float(p.limit_usd), 0.0001) * 100)),
                    "active": p.is_active,
                    "paused_reason": p.paused_reason or "",
                })

            ctx["policies"] = rows
            ctx["units"] = units
            ctx["currency"] = get_settings().display_currency or "USD"
            ctx["rate"] = get_settings().usd_to_display_rate or 1.0
            return templates.TemplateResponse(
                request, "budgets.html", ctx,
            )
        finally:
            session.close()

    @router.post(
        "/budgets/create",
        response_class=HTMLResponse,
        response_model=None,
    )
    def budgets_create(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        scope: Annotated[str, Form()] = "business",
        limit: Annotated[float, Form()] = 0,
        window: Annotated[str, Form()] = "day",
        unit_id: Annotated[str, Form()] = "",
        tier: Annotated[str, Form()] = "",
        label: Annotated[str, Form()] = "",
        currency_mode: Annotated[str, Form()] = "display",
    ) -> RedirectResponse:
        try:
            from decimal import Decimal
            from uuid import UUID
            from korpha.budgets import BudgetScope, BudgetService, BudgetWindow
            from korpha.budgets.currency import display_to_usd

            if limit <= 0:
                return RedirectResponse(
                    "/app/budgets?error=limit+must+be+%3E+0",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            try:
                scope_val = BudgetScope(scope)
                window_val = BudgetWindow(window)
            except ValueError as exc:
                return RedirectResponse(
                    f"/app/budgets?error={str(exc)[:60]}",
                    status_code=status.HTTP_303_SEE_OTHER,
                )

            limit_usd = (
                Decimal(str(limit)) if currency_mode == "usd"
                else display_to_usd(Decimal(str(limit)))
            )

            ctx = _ctx(session, active="budgets")
            biz_id = ctx["business"].id

            kwargs: dict = {
                "business_id": biz_id,
                "scope": scope_val,
                "window": window_val,
                "limit_usd": limit_usd,
                "label": label,
            }
            if scope_val == BudgetScope.BUSINESS_UNIT and unit_id:
                kwargs["business_unit_id"] = UUID(unit_id)
            if scope_val == BudgetScope.TIER and tier:
                kwargs["tier"] = tier

            try:
                BudgetService(session).create(**kwargs)
            except ValueError as exc:
                return RedirectResponse(
                    f"/app/budgets?error={str(exc)[:80]}",
                    status_code=status.HTTP_303_SEE_OTHER,
                )

            return RedirectResponse(
                "/app/budgets?flash=Cap+created",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        finally:
            session.close()

    @router.post(
        "/budgets/{policy_id}/pause",
        response_class=HTMLResponse,
        response_model=None,
    )
    def budgets_pause(
        request: Request,
        policy_id: UUID,
        session: Annotated[Session, Depends(require_session)],
    ) -> RedirectResponse:
        try:
            from korpha.budgets import BudgetService
            try:
                BudgetService(session).pause(policy_id)
            except KeyError:
                return RedirectResponse(
                    "/app/budgets?error=not+found",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            return RedirectResponse(
                "/app/budgets?flash=Paused",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        finally:
            session.close()

    @router.post(
        "/budgets/{policy_id}/resume",
        response_class=HTMLResponse,
        response_model=None,
    )
    def budgets_resume(
        request: Request,
        policy_id: UUID,
        session: Annotated[Session, Depends(require_session)],
    ) -> RedirectResponse:
        try:
            from korpha.budgets import BudgetService
            try:
                BudgetService(session).resume(policy_id)
            except KeyError:
                return RedirectResponse(
                    "/app/budgets?error=not+found",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            return RedirectResponse(
                "/app/budgets?flash=Resumed",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        finally:
            session.close()

    @router.post(
        "/budgets/{policy_id}/delete",
        response_class=HTMLResponse,
        response_model=None,
    )
    def budgets_delete(
        request: Request,
        policy_id: UUID,
        session: Annotated[Session, Depends(require_session)],
    ) -> RedirectResponse:
        try:
            from korpha.budgets.model import BudgetPolicy
            policy = session.get(BudgetPolicy, policy_id)
            if policy is None:
                return RedirectResponse(
                    "/app/budgets?error=not+found",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            session.delete(policy)
            session.commit()
            return RedirectResponse(
                "/app/budgets?flash=Deleted",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        finally:
            session.close()

    # ---------------------------------------------------------------
    # Autonomy panel — mode selector + caps + force-tick
    # ---------------------------------------------------------------

    @router.get("/autonomy", response_class=HTMLResponse)
    def autonomy_view(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse:
        try:
            from korpha.budgets.currency import format_amount
            from korpha.cofounder import autonomy as autonomy_mod
            from korpha.config import get_settings

            ctx = _ctx(session, active="autonomy")
            business = ctx["business"]
            snap = autonomy_mod.evaluate(session, business=business)

            settings = get_settings()
            currency = settings.display_currency or "USD"
            paused_reasons_human = {
                "mode_off": "autonomy is off — only manual 'go' fires work",
                "iterations_reached": "daily iteration cap reached",
                "daily_budget_reached": "daily $ cap reached",
                "monthly_budget_reached": "monthly $ cap reached",
            }
            snap_view = {
                "mode": snap.mode,
                "mode_label": {
                    "off": "Off (manual go only)",
                    "iterations": "Max iterations / day",
                    "daily_budget": "Max budget / day",
                    "monthly_only": "Monthly cap only",
                }.get(snap.mode.value, snap.mode.value),
                "paused": snap.paused,
                "paused_reason": snap.paused_reason,
                "paused_reason_human": paused_reasons_human.get(
                    snap.paused_reason or "", snap.paused_reason or "",
                ),
                "iterations_today": snap.iterations_today,
                "iterations_cap": snap.iterations_cap,
                "spent_today_display": format_amount(snap.spent_today_usd),
                "spent_month_display": format_amount(snap.spent_month_usd),
                "daily_cap_display": (
                    format_amount(snap.daily_cap_usd)
                    if snap.daily_cap_usd is not None else None
                ),
                "monthly_cap_display": (
                    format_amount(snap.monthly_cap_usd)
                    if snap.monthly_cap_usd is not None else None
                ),
                "daily_cap_form_value": (
                    f"{snap.daily_cap_usd:.2f}"
                    if snap.daily_cap_usd is not None else None
                ),
                "monthly_cap_form_value": (
                    f"{snap.monthly_cap_usd:.2f}"
                    if snap.monthly_cap_usd is not None else None
                ),
            }

            snap_view["throttle_rows"] = [
                {
                    "window": ts.throttle.window.value,
                    "count": ts.count,
                    "limit": ts.throttle.limit,
                    "pct": min(100, int(ts.pct_used * 100)),
                    "paused": ts.is_paused,
                    "label": ts.throttle.label or "—",
                }
                for ts in snap.throttle_statuses
            ]
            snap_view["credit_pool"] = (
                {
                    "balance": snap.credit_pool.balance,
                    "monthly_grant": snap.credit_pool.monthly_grant,
                    "next_refill_at": (
                        snap.credit_pool.next_refill_at.isoformat()[:19]
                        if snap.credit_pool.next_refill_at else None
                    ),
                    "lifetime_debited": snap.credit_pool.lifetime_debited,
                    "lifetime_purchased": snap.credit_pool.lifetime_purchased,
                }
                if snap.credit_pool is not None else None
            )

            ctx["snap"] = snap_view
            ctx["currency"] = currency
            ctx["last_tick"] = _LAST_AUTONOMY_TICK.get(business.id)
            return templates.TemplateResponse(
                request, "autonomy.html", ctx,
            )
        finally:
            session.close()

    @router.post(
        "/autonomy/set",
        response_class=HTMLResponse,
        response_model=None,
    )
    def autonomy_set(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        mode: Annotated[str, Form()] = "off",
        daily_max_iterations: Annotated[str, Form()] = "",
        daily_limit: Annotated[str, Form()] = "",
        monthly_limit: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        try:
            from decimal import Decimal, InvalidOperation
            from korpha.business.model import AutonomyMode
            from korpha.cofounder import autonomy as autonomy_mod

            ctx = _ctx(session, active="autonomy")
            business = ctx["business"]

            try:
                mode_val = AutonomyMode(mode)
            except ValueError:
                return RedirectResponse(
                    f"/app/autonomy?error=invalid+mode:+{mode}",
                    status_code=status.HTTP_303_SEE_OTHER,
                )

            def _maybe_decimal(raw: str) -> Decimal | None:
                raw = (raw or "").strip()
                if not raw:
                    return None
                try:
                    val = Decimal(raw)
                except (InvalidOperation, ValueError):
                    return None
                return val if val > 0 else None

            iter_val: int | None = None
            if mode_val == AutonomyMode.ITERATIONS:
                try:
                    iter_val = int((daily_max_iterations or "0").strip())
                except ValueError:
                    iter_val = 0
                if iter_val <= 0:
                    return RedirectResponse(
                        "/app/autonomy?error=daily+max+iterations+must+be+%3E+0",
                        status_code=status.HTTP_303_SEE_OTHER,
                    )

            try:
                autonomy_mod.set_mode(
                    session, business=business, mode=mode_val,
                    daily_max_iterations=iter_val,
                )
            except ValueError as exc:
                return RedirectResponse(
                    f"/app/autonomy?error={str(exc)[:80]}",
                    status_code=status.HTTP_303_SEE_OTHER,
                )

            daily_cap = _maybe_decimal(daily_limit)
            monthly_cap = _maybe_decimal(monthly_limit)

            # In daily_budget mode we always want a daily cap.
            if mode_val == AutonomyMode.DAILY_BUDGET and daily_cap is None:
                return RedirectResponse(
                    "/app/autonomy?error=daily+limit+required+for+daily_budget+mode",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            # monthly_only intentionally does NOT require a monthly cap:
            # for open-weights / subscription / local setups (Codex CLI,
            # Claude Code, Ollama) the $ math is fake — we hit the
            # subscription rate limit or local-GPU bandwidth instead.
            # Leaving the cap blank means "no $ cap, just grind until
            # the cascade naturally throttles."

            # When switching out of daily_budget, clear the daily cap
            # so the prior policy doesn't keep hard-stopping. The
            # monthly cap is kept across mode switches because Mike
            # likely wants a sticky monthly guardrail regardless of
            # day-to-day choice.
            if mode_val not in (
                AutonomyMode.DAILY_BUDGET,
            ):
                autonomy_mod.upsert_daily_cap(
                    session, business=business, limit_usd=None,
                )
            else:
                autonomy_mod.upsert_daily_cap(
                    session, business=business, limit_usd=daily_cap,
                )
            autonomy_mod.upsert_monthly_cap(
                session, business=business, limit_usd=monthly_cap,
            )

            return RedirectResponse(
                "/app/autonomy?flash=Saved",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        finally:
            session.close()

    @router.post(
        "/autonomy/tick",
        response_class=HTMLResponse,
        response_model=None,
    )
    async def autonomy_force_tick(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> RedirectResponse:
        try:
            from korpha.cofounder.autonomy_daemon import run_tick
            from korpha.identity.model import Founder as _Founder
            from sqlmodel import select as _select

            ctx = _ctx(session, active="autonomy")
            business = ctx["business"]
            founder = session.exec(
                _select(_Founder).where(
                    _Founder.id == business.founder_id,
                )
            ).first()
            if founder is None:
                return RedirectResponse(
                    "/app/autonomy?error=founder+missing",
                    status_code=status.HTTP_303_SEE_OTHER,
                )

            # Build the same CostTracker pieces the running server uses.
            try:
                from korpha.api.server import _build_pool_pieces
                from korpha.inference import InferencePool as _Pool
                from korpha.inference.cost_tracker import (
                    CostTracker as _Tracker,
                )
                providers_list, accounts_list = _build_pool_pieces()
                pool = _Pool(
                    providers=providers_list, accounts=accounts_list,
                )
                tracker = _Tracker(pool=pool)
            except Exception:  # noqa: BLE001
                tracker = None  # tick still runs; dispatch may no-op

            tr = await run_tick(
                session=session, business=business,
                founder=founder, cost_tracker=tracker,
            )
            _LAST_AUTONOMY_TICK[business.id] = {
                "fired_count": tr.fired_count,
                "dispatched_count": tr.dispatched_count,
                "skipped_reason": tr.skipped_reason,
            }
            msg = (
                f"Fired+{tr.fired_count}+cards,+dispatched+"
                f"{tr.dispatched_count}"
            )
            if tr.skipped_reason:
                msg = f"Skipped:+{tr.skipped_reason}"
            return RedirectResponse(
                f"/app/autonomy?flash={msg}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        finally:
            session.close()

    # ---------------------------------------------------------------
    # Plugins panel — list discovered plugins + bundled load status
    # ---------------------------------------------------------------

    @router.get("/plugins", response_class=HTMLResponse)
    def plugins_view(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse:
        try:
            from korpha.plugins.hooks import HookKind, hook_registry
            from korpha.plugins.loader import (
                BUNDLED_PLUGINS_DIR,
                discover_all_plugins,
            )

            ctx = _ctx(session, active="plugins")
            mans = discover_all_plugins()
            bundled_path = str(BUNDLED_PLUGINS_DIR.resolve())
            rows = []
            for m in mans:
                source = str(m.source_path.resolve())
                if source.startswith(bundled_path):
                    origin = "bundled"
                elif "site-packages" in source:
                    origin = "pip"
                else:
                    origin = "user"
                rows.append({
                    "name": m.name,
                    "version": m.version,
                    "description": m.description,
                    "permissions": sorted(m.permissions),
                    "source_path": source,
                    "origin": origin,
                })
            ctx["plugins"] = rows
            ctx["hook_listener_counts"] = {
                kind.value: len(hook_registry.listeners(kind))
                for kind in HookKind
            }
            return templates.TemplateResponse(
                request, "plugins.html", ctx,
            )
        finally:
            session.close()

    # ---------------------------------------------------------------
    # Knowledge packs panel — browse Hermes-style SKILL.md playbooks
    # ---------------------------------------------------------------

    @router.get("/knowledge", response_class=HTMLResponse)
    def knowledge_view(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse:
        try:
            from collections import Counter

            from korpha.knowledge_packs import (
                available_categories,
                available_packs,
            )

            ctx = _ctx(session, active="knowledge")
            packs = available_packs()
            counts = Counter(p.category for p in packs)
            ctx["packs"] = packs
            ctx["categories"] = available_categories()
            ctx["counts"] = dict(counts)
            return templates.TemplateResponse(
                request, "knowledge.html", ctx,
            )
        finally:
            session.close()

    @router.get(
        "/knowledge/{category}/{name}", response_class=HTMLResponse,
    )
    def knowledge_pack_detail(
        request: Request,
        category: str,
        name: str,
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse:
        try:
            from fastapi import HTTPException

            from korpha.knowledge_packs import get_pack

            slug = f"{category}/{name}"
            pack = get_pack(slug)
            if pack is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"knowledge pack {slug!r} not found",
                )
            ctx = _ctx(session, active="knowledge")
            ctx["pack"] = pack
            return templates.TemplateResponse(
                request, "knowledge_detail.html", ctx,
            )
        finally:
            session.close()

    # ---------------------------------------------------------------
    # PR-A/B: /app/inference — cascade tuning
    # ---------------------------------------------------------------

    @router.get("/inference", response_class=HTMLResponse)
    def inference_view(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse:
        try:
            from korpha.inference.codex_runtime import status as codex_status
            from korpha.inference.config import load_from_yaml
            from korpha.inference.env_fallback import (
                detect_configured_providers,
            )
            ctx = _ctx(session, active="inference")
            cfg = load_from_yaml()
            if cfg is not None:
                accounts = list(cfg.accounts)
                source = "providers.yaml"
            else:
                pairs = detect_configured_providers()
                accounts = [a for _, a in pairs]
                source = "env vars"
            accounts.sort(
                key=lambda a: (a.priority, a.label or a.provider_name),
            )
            ctx["accounts"] = accounts
            ctx["source"] = source
            ctx["codex_runtime"] = codex_status()
            return templates.TemplateResponse(
                request, "inference.html", ctx,
            )
        finally:
            session.close()

    @router.post(
        "/inference/codex-runtime/toggle",
        response_class=HTMLResponse,
        response_model=None,
    )
    def inference_codex_runtime_toggle(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        action: Annotated[str, Form()] = "",
    ) -> RedirectResponse:
        """One-click flip of the Codex runtime — adds or removes the
        codex-cli entry from providers.yaml at top priority. Mirrors
        Hermes /codex-runtime."""
        try:
            from korpha.inference.codex_runtime import disable, enable
            act = (action or "").strip().lower()
            if act in ("on", "enable", "true"):
                result = enable()
            elif act in ("off", "disable", "false"):
                result = disable()
            else:
                return RedirectResponse(
                    "/app/inference?error=Bad+action+(use+on+or+off)",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            from urllib.parse import quote
            flash = "Codex runtime " + (
                "enabled" if result.enabled else "disabled"
            )
            if not result.codex_binary_ok and act in ("on", "enable", "true"):
                return RedirectResponse(
                    f"/app/inference?error={quote(result.detail)}",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            return RedirectResponse(
                f"/app/inference?flash={quote(flash)}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        finally:
            session.close()

    @router.post(
        "/inference/openrouter-free/add",
        response_class=HTMLResponse,
        response_model=None,
    )
    def inference_openrouter_free_add(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        keys: Annotated[str, Form()] = "",
        pro_model: Annotated[str, Form()] = "deepseek/deepseek-chat-v4:free",
        workhorse_model: Annotated[str, Form()] = "meta-llama/llama-3.3-70b-instruct:free",
    ) -> RedirectResponse:
        """Bulk-add N OpenRouter free-tier keys from a textarea
        (one key per line). Each becomes its own ProviderAccount
        under the 'openrouter-free' preset; the cascade rotates
        through them on 429 and de-dupes against any already in
        providers.yaml.

        Hard-pinned to :free models — rejected at config load if
        the operator ever tries to point them at a paid model.
        """
        from pathlib import Path
        from urllib.parse import quote
        import yaml

        try:
            # Validate the model suffix up-front so Mike sees the
            # error before we write anything.
            for label_field, val in (
                ("PRO model", pro_model), ("WORKHORSE model", workhorse_model),
            ):
                if not val.endswith(":free"):
                    msg = (
                        f"{label_field} {val!r} is not a :free model — "
                        f"openrouter-free only accepts ids ending in ':free'."
                    )
                    return RedirectResponse(
                        f"/app/inference?error={quote(msg)}",
                        status_code=status.HTTP_303_SEE_OTHER,
                    )

            raw_keys = [
                line.strip()
                for line in (keys or "").splitlines()
                if line.strip()
            ]
            if not raw_keys:
                return RedirectResponse(
                    "/app/inference?error=Paste+at+least+one+key.",
                    status_code=status.HTTP_303_SEE_OTHER,
                )

            yaml_path = Path.home() / ".korpha" / "providers.yaml"
            if yaml_path.exists():
                existing = yaml.safe_load(yaml_path.read_text()) or {}
            else:
                existing = {}
            providers = list(existing.get("providers") or [])
            existing_keys: set[str] = {
                str(p.get("api_key") or "") for p in providers
                if p.get("preset") == "openrouter-free"
            }
            already_count = len(existing_keys)

            added = 0
            duplicates = 0
            for k in raw_keys:
                if k in existing_keys:
                    duplicates += 1
                    continue
                existing_keys.add(k)
                providers.append({
                    "preset": "openrouter-free",
                    "label": (
                        f"openrouter-free-{already_count + added + 1}"
                    ),
                    "tiers": {
                        "pro": pro_model,
                        "workhorse": workhorse_model,
                    },
                    "api_key": k,
                    # Free-tier quota: ignore OpenRouter's tiny
                    # retry_after on free-tier 429 and wait for the
                    # daily reset boundary (00:00 UTC). Without this
                    # the router honors retry_after → re-trips the
                    # limit immediately → infinite loop.
                    "free_tier_quota": {
                        "window_kind": "daily",
                        "reset_utc": "00:00",
                    },
                })
                added += 1

            existing["providers"] = providers
            yaml_path.parent.mkdir(parents=True, exist_ok=True)
            yaml_path.write_text(yaml.safe_dump(existing, sort_keys=False))

            flash_parts = [f"Added {added} OpenRouter free key(s)"]
            if duplicates:
                flash_parts.append(f"({duplicates} duplicate(s) skipped)")
            flash_parts.append("— restart server to pick them up.")
            return RedirectResponse(
                f"/app/inference?flash={quote(' '.join(flash_parts))}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        finally:
            session.close()

    @router.post(
        "/inference/update",
        response_class=HTMLResponse,
        response_model=None,
    )
    def inference_update(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        label: Annotated[str, Form()] = "",
        priority: Annotated[int, Form()] = 100,
        retries_before_swap: Annotated[int, Form()] = 1,
        free_tier_window: Annotated[str, Form()] = "",
        free_tier_reset_utc: Annotated[str, Form()] = "00:00",
    ) -> RedirectResponse:
        try:
            from korpha.inference.config_writer import update_provider_entry

            fields: dict = {
                "priority": max(1, int(priority)),
                "retries_before_swap": max(0, int(retries_before_swap)),
            }
            if free_tier_window.strip() in ("daily", "hourly", "monthly"):
                fields["free_tier_quota"] = {
                    "window_kind": free_tier_window.strip(),
                    "reset_utc": free_tier_reset_utc or "00:00",
                }
            elif free_tier_window.strip() == "none":
                fields["free_tier_quota"] = None

            ok = update_provider_entry(label, fields)
            if not ok:
                return RedirectResponse(
                    f"/app/inference?error=no+provider+{label}",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            return RedirectResponse(
                f"/app/inference?flash=Updated+{label}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        finally:
            session.close()

    # ---------------------------------------------------------------
    # PR-D: /app/browser — concurrency-gated pool
    # ---------------------------------------------------------------

    @router.get("/browser", response_class=HTMLResponse)
    def browser_view(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> HTMLResponse:
        try:
            from korpha.browser.pool import get_status, hydrate_from_db
            hydrate_from_db()
            ctx = _ctx(session, active="browser")
            ctx["pool"] = get_status()
            return templates.TemplateResponse(
                request, "browser_pool.html", ctx,
            )
        finally:
            session.close()

    @router.post(
        "/browser/concurrency",
        response_class=HTMLResponse,
        response_model=None,
    )
    def browser_set_concurrency(
        request: Request,
        session: Annotated[Session, Depends(require_session)],
        n: Annotated[int, Form()] = 1,
    ) -> RedirectResponse:
        try:
            import asyncio
            from korpha.browser.pool import persist_concurrency

            if n < 1:
                return RedirectResponse(
                    "/app/browser?error=concurrency+must+be+%3E%3D+1",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            asyncio.run(persist_concurrency(n))
            return RedirectResponse(
                f"/app/browser?flash=Concurrency+set+to+{n}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        finally:
            session.close()

    return router


# ────────────────────────── data helpers ──────────────────────────


def _compute_kpis(session: Session, business_id: Any) -> dict[str, Any]:
    agents_enabled = len(
        session.exec(
            select(AgentRole)
            .where(AgentRole.business_id == business_id)
            .where(AgentRole.is_active.is_(True))  # type: ignore[attr-defined]
        ).all()
    )
    tasks_in_progress = len(
        session.exec(
            select(Task)
            .where(Task.business_id == business_id)
            .where(Task.status == TaskStatus.IN_PROGRESS)
        ).all()
    )
    tasks_blocked = len(
        session.exec(
            select(Task)
            .where(Task.business_id == business_id)
            .where(Task.status == TaskStatus.BLOCKED)
        ).all()
    )
    approvals_pending = len(
        session.exec(
            select(Approval)
            .where(Approval.business_id == business_id)
            .where(Approval.status == ApprovalStatus.PENDING)
        ).all()
    )
    spend = _compute_spend(session, business_id)
    return {
        "agents_enabled": agents_enabled,
        "agents_paused": 0,
        "tasks_in_progress": tasks_in_progress,
        "tasks_blocked": tasks_blocked,
        "approvals_pending": approvals_pending,
        "spend_month": float(spend["month"]),
    }


def _compute_spend(session: Session, business_id: Any) -> dict[str, Any]:
    rows = list(
        session.exec(select(Cost).where(Cost.business_id == business_id)).all()
    )
    now = utcnow()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    today_total = Decimal("0")
    month_total = Decimal("0")
    grand_total = Decimal("0")
    today_calls = 0
    month_calls = 0
    total_calls = 0
    sonnet_total = Decimal("0")

    for row in rows:
        created = as_utc(row.created_at) or now
        cost = row.cost_usd
        grand_total += cost
        total_calls += 1
        if created >= month_start:
            month_total += cost
            month_calls += 1
        if created >= today_start:
            today_total += cost
            today_calls += 1
        # What this row would have cost on Sonnet pricing.
        sonnet_total += (
            Decimal(row.input_tokens) * _SONNET_INPUT_PER_1M / Decimal(1_000_000)
            + Decimal(row.output_tokens) * _SONNET_OUTPUT_PER_1M / Decimal(1_000_000)
        )

    saved = max(Decimal("0"), sonnet_total - grand_total)
    return {
        "today": today_total,
        "today_calls": today_calls,
        "month": month_total,
        "month_calls": month_calls,
        "total": grand_total,
        "total_calls": total_calls,
        "saved_vs_sonnet": saved,
    }


def _spend_by_tier(session: Session, business_id: Any) -> list[dict[str, Any]]:
    rows = session.exec(select(Cost).where(Cost.business_id == business_id)).all()
    bucket: dict[InferenceTier, dict[str, Any]] = {}
    for r in rows:
        b = bucket.setdefault(
            r.tier,
            {
                "tier": r.tier.value,
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost": Decimal("0"),
            },
        )
        b["calls"] += 1
        b["input_tokens"] += r.input_tokens
        b["output_tokens"] += r.output_tokens
        b["cost"] += r.cost_usd
    out = list(bucket.values())
    for row in out:
        row["cost"] = float(row["cost"])
    out.sort(key=lambda r: r["tier"])
    return out


def _list_issues(
    session: Session,
    business: Business,
    agents: list[AgentRole],
    *,
    q: str | None = None,
    status_filter: str | None = None,
    agent_filter: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Render Tasks as Linear-style issue rows.

    Returns ``(rows, total_unfiltered)`` so the toolbar can show "12 of 84".
    Tasks without a ref_number sort to the bottom — they're legacy rows
    that haven't been backfilled yet."""
    stmt = (
        select(Task)
        .where(Task.business_id == business.id)
        .order_by(Task.ref_number.desc().nullslast())  # type: ignore[union-attr]
    )

    total = len(list(session.exec(stmt).all()))

    if status_filter:
        stmt = stmt.where(Task.status == status_filter)
    if agent_filter:
        try:
            agent_uuid = UUID(agent_filter)
            stmt = stmt.where(Task.assigned_to_role_id == agent_uuid)
        except ValueError:
            pass
    if q:
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(func_lower(Task.title).like(like))

    rows = list(session.exec(stmt).all())
    return [_format_issue_brief(t, business, agents) for t in rows], total


def func_lower(col: Any) -> Any:
    """Tiny helper so the search uses case-insensitive matching."""
    from sqlalchemy import func as _f

    return _f.lower(col)


def _format_issue_brief(
    t: Task, business: Business, agents: list[AgentRole] | None = None
) -> dict[str, Any]:
    agent_label = None
    if agents and t.assigned_to_role_id is not None:
        for a in agents:
            if a.id == t.assigned_to_role_id:
                agent_label = a.title
                break
    return {
        "ref": format_ref(business, t.ref_number),
        "title": t.title,
        "status_class": _task_status_class(t.status),
        "status_label": t.status.value.replace("_", " "),
        "created_label": _humanize_time(t.created_at),
        "agent_label": agent_label,
    }


def _format_issue(
    t: Task,
    business: Business,
    agents: list[AgentRole],
    session: Session,
) -> dict[str, Any]:
    """Full detail-page payload."""
    brief = _format_issue_brief(t, business, agents)
    parent_ref: str | None = None
    if t.parent_task_id is not None:
        parent = session.get(Task, t.parent_task_id)
        if parent is not None and parent.business_id == business.id:
            parent_ref = format_ref(business, parent.ref_number)
    return {
        **brief,
        "description": t.description,
        "priority": t.priority.value,
        "parent_ref": parent_ref,
        "updated_label": _humanize_time(t.updated_at),
        "completed_label": _humanize_time(t.completed_at) if t.completed_at else None,
    }


def _format_routine(r: Routine) -> dict[str, Any]:
    return {
        "name": r.name,
        "kind": r.kind,
        "schedule_value": r.schedule_value,
        "enabled": r.enabled,
        "last_fired_label": _humanize_time(r.last_fired_at) if r.last_fired_at else "never",
    }


def _format_goal(g: Goal) -> dict[str, Any]:
    return {
        "title": g.title,
        "status_class": "active" if g.status.value == "active" else "done",
        "status_label": g.status.value,
    }


def _format_agent(a: AgentRole) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "title": a.title,
        "role_type": a.role_type.value.upper(),
        "specialty": a.specialty,
        "is_active": a.is_active,
        "inference_tier_default": a.inference_tier_default,
        "hired_label": _humanize_time(a.hired_at),
    }


_LIVE_WINDOW = timedelta(seconds=60)
"""How recently an agent must have done something to count as live. The
heartbeat-fired Activity row is the truest signal that an agent is mid-
execution; mere task assignment doesn't count."""


def _agents_with_status(
    session: Session, business: Business, agents: list[AgentRole]
) -> list[dict[str, Any]]:
    """Format agents for the home dashboard.

    Two distinct signals:
      - ``status_label`` = "running" only when the agent has produced an
        Activity event in the last LIVE_WINDOW (i.e. it's actually mid-
        execution right now). Otherwise "idle". A task being assigned ≠
        live; the agent has to be doing something.
      - ``current_task_*`` fields = the in-progress task assigned to this
        agent, if any. Shown as 'current focus' on the card regardless of
        live status, so Mike sees what's queued for them even when idle.
    """
    threshold = utcnow() - _LIVE_WINDOW
    out: list[dict[str, Any]] = []
    for a in agents:
        current = session.exec(
            select(Task)
            .where(Task.business_id == business.id)
            .where(Task.assigned_to_role_id == a.id)
            .where(Task.status == TaskStatus.IN_PROGRESS)
            .order_by(Task.updated_at.desc())  # type: ignore[attr-defined]
            .limit(1)
        ).first()

        recent_activity = session.exec(
            select(Activity)
            .where(Activity.business_id == business.id)
            .where(Activity.actor_id == a.id)
            .where(Activity.created_at >= threshold)
            .limit(1)
        ).first()

        formatted = _format_agent(a)
        if current is not None:
            formatted["current_task_ref"] = format_ref(business, current.ref_number)
            formatted["current_task_title"] = current.title
        else:
            formatted["current_task_ref"] = None
            formatted["current_task_title"] = None

        if recent_activity is not None:
            formatted["status_label"] = "running"
            formatted["status_class"] = "running"
        else:
            formatted["status_label"] = "idle"
            formatted["status_class"] = "done"
        out.append(formatted)
    return out


def _recent_tasks(
    session: Session, business: Business, agents: list[AgentRole], *, limit: int
) -> list[dict[str, Any]]:
    rows = list(
        session.exec(
            select(Task)
            .where(Task.business_id == business.id)
            .order_by(Task.updated_at.desc())  # type: ignore[attr-defined]
            .limit(limit)
        ).all()
    )
    return [_format_issue_brief(t, business, agents) for t in rows]


def _build_org_tree(agents: list[AgentRole]) -> list[dict[str, Any]]:
    """Group agents into the implicit cofounder hierarchy:

        Tier 1 — CEO
        Tier 2 — C-suite (CTO / CMO / COO) + Chief of Staff
        Tier 3 — Workers

    AgentRole has no explicit parent_id today, so the tree is derived
    from role_type. When parent_id lands the helper can return real
    edges instead of tiered groups."""
    from korpha.cofounder.model import RoleType

    tier_for: dict[RoleType, int] = {
        RoleType.CEO: 1,
        RoleType.CTO: 2,
        RoleType.CMO: 2,
        RoleType.COO: 2,
        RoleType.CHIEF_OF_STAFF: 2,
        RoleType.WORKER: 3,
    }
    tier_labels = {1: "CEO", 2: "C-suite", 3: "Workers"}

    grouped: dict[int, list[dict[str, Any]]] = {1: [], 2: [], 3: []}
    for a in agents:
        tier = tier_for.get(a.role_type, 3)
        grouped[tier].append(_format_agent(a))

    return [
        {"label": tier_labels[t], "nodes": grouped[t]}
        for t in (1, 2, 3)
        if grouped[t]
    ]


def _format_approval(a: Approval) -> dict[str, Any]:
    """Render an Approval with a friendly action preview. Nested payload
    keys are surfaced as preview rows so Mike can see *what* the agent
    wants to do without expanding raw JSON."""
    status_class_map = {
        "pending": "pending",
        "approved": "active",
        "rejected": "blocked",
        "executed": "done",
        "expired": "done",
    }
    payload = a.action_payload or {}
    preview_lines: list[dict[str, str]] = []
    kind_tag: str | None = None
    dispatch_error: str | None = None
    side_effect_url: str | None = None
    if isinstance(payload, dict):
        # Chain-produced approvals carry a "kind" + "result" structure.
        # Pull the load-bearing field out of result so it shows up in
        # the preview without the Founder having to expand raw JSON.
        kind = str(payload.get("kind") or "")
        result = payload.get("result") if isinstance(payload.get("result"), dict) else None
        if kind and result is not None:
            kind_tag = kind.replace("_", " ")
            preview_lines.extend(_preview_for_chain_kind(kind, result))
        else:
            for k, v in payload.items():
                if k.lower() in ("text", "body", "content", "message", "subject"):
                    preview_lines.insert(
                        0, {"key": k, "value": _shorten(str(v), 240)}
                    )
                elif isinstance(v, str | int | float | bool):
                    preview_lines.append({"key": k, "value": _shorten(str(v), 120)})
        # Side-effect outcome surfacing: a failed dispatch must be
        # loud, not buried in payload JSON. dispatch_error covers the
        # pre-run failures (missing key, bad payload); execute_error +
        # send_error are the legacy fields the CLI executors wrote.
        for err_key in ("dispatch_error", "execute_error", "send_error"):
            err_val = payload.get(err_key)
            if err_val:
                dispatch_error = str(err_val)
                break
        # Success URL: lift Stripe link / .ics URL so Mike can click
        # straight from the approval card.
        for url_key in (
            "stripe_payment_link_url",
            "ics_download_url",
            "ics_add_to_google_url",
        ):
            url_val = payload.get(url_key)
            if url_val:
                side_effect_url = str(url_val)
                break
    return {
        "id": str(a.id),
        "summary": a.proposal_summary,
        "status": a.status.value,
        "status_class": status_class_map.get(a.status.value, "pending"),
        "action_class_label": a.action_class.value.replace("_", " "),
        "platform": a.platform,
        "created_label": _humanize_time(a.created_at),
        "preview_lines": preview_lines,
        "kind_tag": kind_tag,
        "dispatch_error": dispatch_error,
        "side_effect_url": side_effect_url,
        "modification_note": a.modification_note,
    }


def _preview_for_chain_kind(kind: str, result: dict[str, Any]) -> list[dict[str, str]]:
    """Format a chain-produced Approval payload into glanceable preview
    rows. We pick 2-3 fields per kind that tell the story without
    drowning the card in JSON."""
    lines: list[dict[str, str]] = []
    if kind == "validation_report":
        if result.get("verdict"):
            lines.append({"key": "Verdict", "value": str(result["verdict"]).upper()})
        if result.get("overall") is not None:
            lines.append({"key": "Score", "value": f"{result['overall']}/10"})
        if result.get("kill_test"):
            lines.append(
                {"key": "Kill test", "value": _shorten(str(result["kill_test"]), 180)}
            )
    elif kind == "landing_copy":
        if result.get("headline"):
            lines.append(
                {"key": "Headline", "value": _shorten(str(result["headline"]), 160)}
            )
        if result.get("subhead"):
            lines.append(
                {"key": "Sub", "value": _shorten(str(result["subhead"]), 200)}
            )
        if result.get("primary_cta"):
            lines.append({"key": "CTA", "value": str(result["primary_cta"])})
    elif kind == "outreach_drafts":
        variants = result.get("variants") or []
        if isinstance(variants, list) and variants:
            first = variants[0] if isinstance(variants[0], dict) else {}
            if first.get("subject"):
                lines.append(
                    {"key": "Subject", "value": _shorten(str(first["subject"]), 120)}
                )
            if first.get("body"):
                lines.append(
                    {"key": "Body", "value": _shorten(str(first["body"]), 240)}
                )
            lines.append({"key": "Variants", "value": f"{len(variants)} drafts"})
    return lines


def ceo_display_name(session: Session, business_id: UUID) -> str | None:
    """Resolve the Founder's display name for use as the From line in
    the outreach preview. Falls back to email when display_name is empty."""
    biz = session.exec(
        select(Business).where(Business.id == business_id)
    ).first()
    if biz is None:
        return None
    founder = session.exec(
        select(Founder).where(Founder.id == biz.founder_id)
    ).first()
    if founder is None:
        return None
    return founder.display_name or founder.email


def _first_day_chain_summary(session: Session, business_id: UUID) -> tuple[int, bool]:
    """Return (count_of_pending_chain_approvals, includes_stripe).

    "Chain approval" = produced by the post-pick-niche fan-out, identified
    by ``action_payload.kind`` being one of the chain kinds. Banner shows
    while at least one of these is still pending.
    """
    chain_kinds = {
        "validation_report",
        "landing_copy",
        "outreach_drafts",
        "create_payment_link",
    }
    rows = session.exec(
        select(Approval)
        .where(Approval.business_id == business_id)
        .where(Approval.status == ApprovalStatus.PENDING)
    ).all()
    matching = [
        r for r in rows
        if isinstance(r.action_payload, dict)
        and str(r.action_payload.get("kind") or "") in chain_kinds
    ]
    has_stripe = any(
        str(r.action_payload.get("kind") or "") == "create_payment_link"
        for r in matching
    )
    return len(matching), has_stripe


def _shorten(text: str, limit: int) -> str:
    s = text.strip()
    if len(s) <= limit:
        return s
    return s[: limit - 1] + "…"


def _agent_costs(
    session: Session, business_id: Any, agent_id: Any
) -> dict[str, Any]:
    rows = list(
        session.exec(
            select(Cost)
            .where(Cost.business_id == business_id)
            .where(Cost.agent_role_id == agent_id)
        ).all()
    )
    if not rows:
        return {
            "calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total": 0.0,
            "saved_vs_sonnet": 0.0,
        }
    total = Decimal("0")
    sonnet = Decimal("0")
    input_t = 0
    output_t = 0
    for r in rows:
        total += r.cost_usd
        input_t += r.input_tokens
        output_t += r.output_tokens
        sonnet += (
            Decimal(r.input_tokens) * _SONNET_INPUT_PER_1M / Decimal(1_000_000)
            + Decimal(r.output_tokens) * _SONNET_OUTPUT_PER_1M / Decimal(1_000_000)
        )
    return {
        "calls": len(rows),
        "input_tokens": input_t,
        "output_tokens": output_t,
        "total": float(total),
        "saved_vs_sonnet": float(max(Decimal("0"), sonnet - total)),
    }


def _agent_run_chart(
    session: Session, business_id: Any, agent_id: Any
) -> list[dict[str, Any]]:
    today = utcnow().date()
    days = [today - timedelta(days=i) for i in range(13, -1, -1)]
    by_day: dict[str, int] = {d.isoformat(): 0 for d in days}
    earliest = days[0]
    rows = list(
        session.exec(
            select(Activity)
            .where(Activity.business_id == business_id)
            .where(Activity.actor_id == agent_id)
        ).all()
    )
    for row in rows:
        created = as_utc(row.created_at)
        if created is None or created.date() < earliest:
            continue
        key = created.date().isoformat()
        if key in by_day:
            by_day[key] += 1
    return [
        {"label": d.strftime("%m/%d"), "value": by_day[d.isoformat()]} for d in days
    ]


def _compute_charts(session: Session, business_id: Any) -> dict[str, Any]:
    """Build the four mini charts the home view shows.

    Each chart returns a list of (label, value) rows that the template
    renders as a tiny inline-SVG bar strip — no chart library, no JS.
    """
    today = utcnow().date()
    days = [today - timedelta(days=i) for i in range(13, -1, -1)]

    # Run Activity: daily count of Activity rows.
    activity_rows = list(
        session.exec(select(Activity).where(Activity.business_id == business_id)).all()
    )
    by_day: dict[str, int] = {d.isoformat(): 0 for d in days}
    earliest = days[0]
    for row in activity_rows:
        created = as_utc(row.created_at)
        if created is None or created.date() < earliest:
            continue
        key = created.date().isoformat()
        if key in by_day:
            by_day[key] += 1
    run_activity = [
        {"label": d.strftime("%m/%d"), "value": by_day[d.isoformat()]} for d in days
    ]

    # Issues by Status — current snapshot.
    status_rows = list(
        session.exec(select(Task).where(Task.business_id == business_id)).all()
    )
    by_status: dict[str, int] = {}
    for t in status_rows:
        key = t.status.value
        by_status[key] = by_status.get(key, 0) + 1
    status_order = ["pending", "in_progress", "blocked", "done", "cancelled"]
    issues_by_status = [
        {"label": k.replace("_", " "), "value": by_status.get(k, 0), "key": k}
        for k in status_order
        if by_status.get(k, 0) > 0
    ]

    # Spend by day (last 14 days).
    cost_rows = list(
        session.exec(select(Cost).where(Cost.business_id == business_id)).all()
    )
    spend_by_day: dict[str, float] = {d.isoformat(): 0.0 for d in days}
    for c in cost_rows:
        created = as_utc(c.created_at)
        if created is None or created.date() < earliest:
            continue
        key = created.date().isoformat()
        if key in spend_by_day:
            spend_by_day[key] += float(c.cost_usd)
    spend = [
        {"label": d.strftime("%m/%d"), "value": spend_by_day[d.isoformat()]}
        for d in days
    ]

    return {
        "run_activity": run_activity,
        "issues_by_status": issues_by_status,
        "spend": spend,
    }


def _row_to_view(row: Any) -> dict[str, Any]:
    """Render a LongTermMemoryEntry as a dict for the template."""
    return {
        "id": str(row.id),
        "text": row.text,
        "tags": list(row.tags or ()),
        "created_at": row.created_at,
        "score": None,
    }


def _sync_search(
    mem: Any, business_id: Any, founder_id: Any, query: str,
) -> list[dict[str, Any]]:
    """Synchronous wrapper around the async search so the dashboard
    route doesn't need its own event loop. The DB-backed memory is
    sync under the hood; awaiting it is just protocol cosmetics."""
    import asyncio as _aio

    from korpha.memory import MemoryQuery

    async def _go():
        return await mem.search(MemoryQuery(
            business_id=business_id, founder_id=founder_id,
            text=query, limit=200,
        ))

    try:
        loop = _aio.get_event_loop()
    except RuntimeError:
        loop = _aio.new_event_loop()
        _aio.set_event_loop(loop)
    if loop.is_running():
        # Already in a loop (FastAPI's) — open a fresh thread loop.
        # In practice this branch is hit by tests under pytest-asyncio;
        # FastAPI's sync routes get their own loop via the worker.
        new_loop = _aio.new_event_loop()
        try:
            entries = new_loop.run_until_complete(_go())
        finally:
            new_loop.close()
    else:
        entries = loop.run_until_complete(_go())
    return [
        {
            "id": str(e.id),
            "text": e.text,
            "tags": list(e.tags),
            "created_at": e.created_at,
            "score": e.score,
        }
        for e in entries
    ]


def _recent_events(
    session: Session, business_id: Any, *, limit: int = 20
) -> list[dict[str, Any]]:
    """Recent Activity rows, with consecutive same-(actor, event_type) runs
    coalesced into one entry showing a count. Cuts the noise level when
    one agent fires the same event many times in a row."""
    # Pull more raw rows than ``limit`` so the coalescing doesn't shrink
    # the visible list below the requested size.
    rows = list(
        session.exec(
            select(Activity)
            .where(Activity.business_id == business_id)
            .order_by(Activity.created_at.desc())  # type: ignore[attr-defined]
            .limit(limit * 4)
        ).all()
    )
    out: list[dict[str, Any]] = []
    last_key: tuple[Any, str] | None = None
    last: dict[str, Any] | None = None
    for r in rows:
        formatted = _format_activity(r, session)
        key = (r.actor_id, r.event_type)
        if last is not None and key == last_key and last["count"] < 99:
            last["count"] += 1
            # Keep the freshest timestamp visible; the running count itself
            # tells the operator there's earlier history behind it.
            continue
        formatted["count"] = 1
        out.append(formatted)
        last = formatted
        last_key = key
        if len(out) >= limit:
            break
    return out


def _format_activity(a: Activity, session: Session) -> dict[str, Any]:
    actor_label = a.actor_type.value.title()
    if a.actor_id is not None:
        agent = session.get(AgentRole, a.actor_id)
        if agent is not None:
            actor_label = agent.title
    return {
        "actor_label": actor_label,
        "event_label": _humanize_event(a.event_type, a.payload),
        "time_label": _humanize_time(a.created_at),
    }


def _humanize_event(event_type: str, payload: dict[str, Any] | None) -> str:
    """Convert raw event_type strings ('blocker.submitted') into readable
    English. Falls back to the event_type if no nicer phrasing is known."""
    pretty = {
        "blocker.submitted": "raised a blocker",
        "blocker.duplicate": "deduped a blocker",
        "approval.created": "asked for approval",
        "approval.approved": "got approval",
        "approval.rejected": "approval denied",
        "ceo.responded": "responded to founder",
        "skill.invoked": "called a skill",
        "agent.hired": "was hired",
    }
    text = pretty.get(event_type, event_type.replace(".", " · "))
    if isinstance(payload, dict) and payload.get("title"):
        text += f" — {payload['title']}"
    return text


def _humanize_time(dt: datetime | None) -> str:
    if dt is None:
        return "never"
    aware = as_utc(dt) or dt
    delta = utcnow() - aware
    if delta < timedelta(seconds=45):
        return "just now"
    if delta < timedelta(minutes=2):
        return "a minute ago"
    if delta < timedelta(hours=1):
        return f"{int(delta.total_seconds() // 60)}m ago"
    if delta < timedelta(days=1):
        return f"{int(delta.total_seconds() // 3600)}h ago"
    if delta < timedelta(days=14):
        return f"{int(delta.days)}d ago"
    return aware.strftime("%b %d")


def _task_status_class(status: TaskStatus) -> str:
    return {
        TaskStatus.IN_PROGRESS: "running",
        TaskStatus.PENDING: "pending",
        TaskStatus.BLOCKED: "blocked",
        TaskStatus.DONE: "done",
        TaskStatus.CANCELLED: "done",
    }.get(status, "pending")


__all__ = ["build_dashboard_router"]
