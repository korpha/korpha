"""FastAPI app builder + endpoint handlers.

Each handler grabs a fresh SQLModel session, builds the services it needs
from the cofounder layer, runs the operation, and returns Pydantic
response models. Services are constructed per-request so the same code
path works whether the caller is the CLI, an external HTTP client, or
the dashboard renderer.

Note: this module deliberately does NOT use ``from __future__ import
annotations``. FastAPI relies on *runtime* type introspection to detect
``Depends()`` markers — when annotations are stringified by the future
import, the Annotated metadata isn't seen and dependency injection
silently breaks (parameters get treated as query/body fields).
"""
import os
import time
from collections.abc import AsyncIterator

# Captured at module import time so /healthz can report uptime.
# Subsequent reloads in dev mode reset it — that's the right
# semantics (each reload is a "restart" from monitoring's POV).
_PROCESS_START_MONOTONIC: float = time.monotonic()
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.engine import Engine
from sqlmodel import Session, create_engine, select

import korpha.db.registry  # noqa: F401  -- registers all models
from korpha.approvals.gate import ApprovalGate, Decision
from korpha.approvals.model import (
    Approval,
    ApprovalStatus,
)
from korpha.audit.model import Cost, InferenceTier
# Side-effect imports — register tables with SQLModel.metadata so
# create_all on a fresh DB picks them up.
from korpha.goals.model import Goal  # noqa: F401
from korpha.budgets.model import BudgetPolicy  # noqa: F401
from korpha.commerce.revenue import RevenueEvent  # noqa: F401
from korpha.kanban.artifacts import CardArtifact  # noqa: F401
from korpha.kanban.refs import KanbanCardRef  # noqa: F401
from korpha.kanban.model import KanbanCard, KanbanCardEvent  # noqa: F401
from korpha.memory.model import LongTermMemoryEntry  # noqa: F401
from korpha.memory.notes import FounderNote  # noqa: F401
from korpha.scriptcron.model import ScriptCron  # noqa: F401
from korpha.blockers.queue import BlockerQueue
from korpha.business.model import Business
from korpha.cofounder.ceo import CEO, Plan
from korpha.cofounder.chief_of_staff import ChiefOfStaff
from korpha.cofounder.hiring import HiringService
from korpha.cofounder.memory import MemoryService
from korpha.cofounder.model import ThreadPlatform
from korpha.cofounder.routing import ConversationRouter
from korpha.cofounder.workforce import DirectorFactory, Workforce
from korpha.identity.model import Founder
from korpha.inference import (
    InferencePool,
    ProviderAccount,
    ollama_cloud_provider,
    opencode_go_provider,
)
from korpha.inference.cost_tracker import CostTracker
from korpha.inference.registry import AuthType
from korpha.plugins.hooks import (
    HookKind,
    PreGatewayDispatchEvent,
    hook_registry,
)
from korpha.skills import SkillContext
from korpha.skills import default_registry as skills_registry
from korpha.skills.types import SkillNotFound

# ---------- request / response models ----------


class HealthResponse(BaseModel):
    status: str
    """``ok`` (everything works) or ``degraded`` (DB unreachable
    or no provider configured). Treat anything other than ``ok``
    as a paging signal in production."""

    has_provider: bool
    skills_loaded: int
    db_reachable: bool
    """True iff a ``SELECT 1`` round-trip succeeded against the
    configured DB. Lets uptime monitors detect 'app up but DB
    blew up' separately from 'app down'."""

    version: str
    """The package version. Useful for verifying a deploy rolled
    out before the load balancer starts forwarding traffic."""

    uptime_seconds: float
    """How long the process has been alive. Reset on restart —
    a sharp drop tells you a container just bounced."""


class MeResponse(BaseModel):
    founder_email: str
    founder_name: str | None
    business_name: str
    business_status: str
    total_spend_usd: float


class AskRequest(BaseModel):
    message: str


class AskResponse(BaseModel):
    content: str
    skills_used: list[str]
    reasoning_chars: int
    cost_usd: float
    clarify_question: str | None = None
    """Set when the cofounder is asking a structured clarifying
    question. Frontend renders ``clarify_choices`` as buttons that
    submit the chosen text as the next message."""
    clarify_choices: list[str] | None = None


class ProposeRequest(BaseModel):
    prompt: str


class PlanResponse(BaseModel):
    summary: str
    rationale: list[str]
    next_action: str
    tasks: list[str]
    estimated_hours: float | None
    expected_impact: str | None
    approval_id: str | None


class ApprovalResponse(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    id: str
    action_class: str
    platform: str | None
    proposal_summary: str
    status: str
    created_at: str


class DecisionResponse(BaseModel):
    status: str
    consecutive_approvals: int
    threshold: int
    promotion_offered: bool


class ExecuteResponse(BaseModel):
    headline: str
    shipped: int
    blocked: int
    errored: int
    total_blockers: int
    total_cost_usd: float
    results: list[dict[str, Any]]


class BlockerResponse(BaseModel):
    id: str
    title: str
    detail: str
    kind: str
    urgency: str
    status: str
    options: list[str]
    cos_recommendation: str | None


class BlockersListResponse(BaseModel):
    items: list[BlockerResponse]
    digest_text: str
    auto_resolved: int
    dropped: int


class SkillSpecResponse(BaseModel):
    name: str
    description: str
    parameters: dict[str, str]


class SkillRunRequest(BaseModel):
    args: dict[str, Any] = {}


class SkillRunResponse(BaseModel):
    skill_name: str
    summary: str
    payload: dict[str, Any]
    cost_usd: float


# ---------- dependency injection ----------


def _data_dir() -> str:
    return os.getenv("KORPHA_DATA_DIR") or os.path.expanduser("~/.korpha")


def _build_engine() -> Engine:
    db_path = os.path.join(_data_dir(), "korpha.db")
    if not os.path.exists(os.path.dirname(db_path)):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Korpha is not initialized. Run `korpha init` first "
                "to create the data directory and DB."
            ),
        )
    return create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )


def _ollama_account() -> ProviderAccount | None:
    api_key = os.getenv("OLLAMA_CLOUD_API_KEY")
    if not api_key:
        return None
    return ProviderAccount(
        provider_name="ollama-cloud",
        auth_type=AuthType.API_KEY,
        tier_models={
            InferenceTier.WORKHORSE: "deepseek-v4-flash:cloud",
            InferenceTier.PRO: "deepseek-v4-pro:cloud",
        },
        api_key=api_key,
        label="ollama-cloud",
    )


def _opencode_account() -> ProviderAccount | None:
    api_key = os.getenv("OPENCODE_API_KEY")
    if not api_key:
        return None
    return ProviderAccount(
        provider_name="opencode-go",
        auth_type=AuthType.API_KEY,
        tier_models={
            InferenceTier.WORKHORSE: "deepseek-v4-flash",
            InferenceTier.PRO: "deepseek-v4-pro",
        },
        api_key=api_key,
        label="opencode-go",
    )


def _build_pool_pieces() -> tuple[list[Any], list[ProviderAccount]]:
    """Resolve providers from ``~/.korpha/providers.yaml`` if
    present, else from env vars across the full preset matrix
    (every Hermes-inherited provider + first-class DeepSeek +
    Xiaomi MiMo). See ``korpha.inference.env_fallback`` for
    the supported env vars."""
    from korpha.inference.config import ProviderConfigError, load_from_yaml
    from korpha.inference.env_fallback import (
        detect_configured_providers,
    )

    try:
        loaded = load_from_yaml()
    except ProviderConfigError:
        loaded = None
    if loaded is not None and loaded.accounts:
        return list(loaded.providers), list(loaded.accounts)

    providers: list[Any] = []
    accounts: list[ProviderAccount] = []
    for provider, account in detect_configured_providers():
        providers.append(provider)
        accounts.append(account)
    return providers, accounts


async def _pre_gateway_dispatch(
    *,
    text: str,
    business_id: UUID,
    founder_id: UUID,
    channel: str,
    thread_id: UUID | None = None,
) -> str | None:
    """Run plugin pre_gateway_dispatch chain on inbound founder text.
    Returns the (possibly-mutated) text, or None to drop the message.
    No-op fast-path when no listeners are registered."""
    if not hook_registry.has(HookKind.PRE_GATEWAY_DISPATCH):
        return text
    return await hook_registry.dispatch_transform(
        HookKind.PRE_GATEWAY_DISPATCH,
        text=text,
        event_factory=lambda current: PreGatewayDispatchEvent(
            text=current,
            business_id=business_id,
            founder_id=founder_id,
            channel=channel,
            thread_id=thread_id,
        ),
    )


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Startup: ensure the FTS5 search index exists, hot-load any
    YAML / Python skills the agent authored in previous sessions, all
    idempotent."""
    import contextlib

    from korpha.cofounder.fts import ensure_fts_index
    from korpha.skills import (
        load_agent_created_python_skills,
        load_user_yaml_skills,
    )

    with contextlib.suppress(Exception):
        engine = _build_engine()
        with Session(engine) as session:
            ensure_fts_index(session)
            session.commit()

    # Best-effort additive auto-migration: ADD COLUMN for any nullable
    # column the model has but the DB doesn't. Stops every `git pull`
    # from breaking installs that pre-date a new column.
    with contextlib.suppress(Exception):
        from korpha.db.auto_schema import add_missing_columns
        engine = _build_engine()
        add_missing_columns(engine)

    # Survive-restart loading for agent-authored skills. Failures are
    # logged inside the loaders + don't break startup.
    with contextlib.suppress(Exception):
        load_user_yaml_skills()
    with contextlib.suppress(Exception):
        load_agent_created_python_skills()

    # Browser pool: restore the concurrency cap Mike picked previously.
    # Without this, a server restart silently drops back to 1 even
    # though the SharedResource row says 4.
    with contextlib.suppress(Exception):
        from korpha.browser.pool import hydrate_from_db
        hydrate_from_db()

    yield


def build_app() -> FastAPI:
    app = FastAPI(
        title="Korpha",
        description="Your AI cofounder for the online business you keep saying you'll start.",
        version="0.0.1",
        lifespan=_lifespan,
    )

    def _require_session() -> Session:
        engine = _build_engine()
        return Session(engine)

    def _build_ceo(session: Session) -> CEO:
        providers_list, accounts_list = _build_pool_pieces()
        if not accounts_list:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "No provider configured. Set OPENCODE_API_KEY (preferred) "
                    "or OLLAMA_CLOUD_API_KEY on the server."
                ),
            )
        pool = InferencePool(providers=providers_list, accounts=accounts_list)
        tracker = CostTracker(pool=pool)
        hiring = HiringService(session)
        gate = ApprovalGate(session)
        queue = BlockerQueue(session=session)
        cos = ChiefOfStaff(session=session, queue=queue, hiring=hiring, gate=gate)
        factory = DirectorFactory(
            session=session, cost_tracker=tracker, queue=queue, hiring=hiring
        )
        workforce = Workforce.with_default_directors(director_factory=factory)

        browser = None
        try:
            from korpha.browser import BrowserService, PlaywrightFetchProvider

            browser = BrowserService(providers=[PlaywrightFetchProvider()])
        except Exception:
            pass

        return CEO(
            session=session,
            cost_tracker=tracker,
            hiring=hiring,
            gate=gate,
            chief_of_staff=cos,
            workforce=workforce,
            skills=skills_registry,
            browser=browser,
        )

    def _founder_business(session: Session) -> tuple[Founder, Business]:
        from korpha.business.multi import BusinessResolutionError, active_business

        f = session.exec(select(Founder)).first()
        if f is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No founder configured. Run `korpha init`.",
            )
        try:
            b = active_business(session, f)
        except BusinessResolutionError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=str(exc),
            ) from exc
        return f, b

    # ---------- endpoints ----------

    static_dir = Path(__file__).parent / "static"
    index_path = static_dir / "index.html"

    # Mount /static for the dashboard's CSS / JS bundle.
    from fastapi.staticfiles import StaticFiles

    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Serve agent-deployed landing pages from
    # ``$KORPHA_DATA_DIR/deploys/`` so LocalFileDeployer's
    # returned URLs actually resolve. Only mount when the data
    # dir already exists — we don't want to silently auto-create
    # the parent and mask "missing data dir" as healthy in
    # /healthz. The deployer creates the deploys/ subtree on
    # first use; before that, /app/deploys/* simply 404s.
    try:
        data_root = Path(_data_dir())
        if data_root.is_dir():
            deploys_root = data_root / "deploys"
            deploys_root.mkdir(parents=True, exist_ok=True)
            app.mount(
                "/app/deploys",
                StaticFiles(
                    directory=str(deploys_root), html=True,
                ),
                name="deploys",
            )
            # Calendar invites (.ics) generated by
            # ``calendar.create_event``. Same gate posture as
            # /app/deploys — only mounted when data dir already
            # exists so /healthz stays honest about an unconfigured
            # install. The skill creates the calendar/ subtree on
            # first use; before that, /app/calendar/* simply 404s.
            calendar_root = data_root / "calendar"
            calendar_root.mkdir(parents=True, exist_ok=True)
            app.mount(
                "/app/calendar",
                StaticFiles(directory=str(calendar_root)),
                name="calendar",
            )
    except (OSError, PermissionError):
        pass

    # The legacy chat page stays at /chat so anyone bookmarked there keeps
    # working; / now redirects to /dashboard. The HTML dashboard router is
    # included AFTER all the JSON API routes below so /skills, /me, etc.
    # JSON endpoints win on collision.
    from fastapi.responses import RedirectResponse

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/app/dashboard", status_code=307)

    @app.get("/chat", include_in_schema=False)
    def chat_legacy() -> FileResponse:
        if not index_path.exists():
            raise HTTPException(404, "static/index.html not found")
        return FileResponse(index_path, media_type="text/html")

    @app.get("/healthz", response_model=HealthResponse)
    def healthz() -> HealthResponse:
        from sqlalchemy import text as _text

        from korpha.inference.env_fallback import (
            list_configured_provider_names,
        )

        # Match the init wizard's definition: an env-var preset OR a
        # providers.yaml entry counts. Without this, /healthz reports
        # has_provider=false even right after the wizard wrote a working
        # yaml-only provider, confusing both Mike and any external
        # uptime monitor.
        has_any = bool(list_configured_provider_names())
        if not has_any:
            try:
                from korpha.inference.config import load_from_yaml
                loaded = load_from_yaml()
                has_any = bool(loaded and loaded.accounts)
            except Exception:  # noqa: BLE001
                pass

        # DB ping. A 1-row SELECT through the engine catches every
        # common failure mode (connection refused, auth, missing
        # tables, etc.) without scanning real data.
        db_ok = False
        try:
            engine = _build_engine()
            with engine.connect() as conn:
                conn.execute(_text("SELECT 1"))
            db_ok = True
        except Exception:  # noqa: BLE001
            db_ok = False

        # Version — read from package metadata. Falls back to
        # 'dev' when the package isn't installed (running from
        # source).
        version = "dev"
        try:
            from importlib.metadata import version as _pkg_ver

            version = _pkg_ver("korpha")
        except Exception:  # noqa: BLE001
            pass

        import time
        uptime = time.monotonic() - _PROCESS_START_MONOTONIC

        status = "ok" if db_ok else "degraded"
        return HealthResponse(
            status=status,
            has_provider=has_any,
            skills_loaded=len(skills_registry.skills),
            db_reachable=db_ok,
            version=version,
            uptime_seconds=round(uptime, 2),
        )

    # ---------- TUI WebSocket transport ----------
    # JSON-RPC 2.0 over WS. Each connection resolves the active
    # founder + business once, then services prompt.submit /
    # session.* / approval.* methods. Streaming events
    # (content.delta, reasoning.delta, tool.event, done) flow
    # back on the same socket while a request is in flight.
    from fastapi import WebSocket as _WS  # local import — avoid top-level coupling

    from korpha.api.tui_ws import tui_websocket_handler

    @app.websocket("/api/tui/ws")
    async def _tui_ws(ws: _WS) -> None:
        engine = _build_engine()

        def session_factory() -> Session:
            return Session(engine)

        await tui_websocket_handler(
            ws, session_factory=session_factory, engine=engine,
        )

    @app.get("/me", response_model=MeResponse)
    def me(session: Annotated[Session, Depends(_require_session)]) -> MeResponse:
        try:
            founder, business = _founder_business(session)
            costs = session.exec(
                select(Cost).where(Cost.business_id == business.id)
            ).all()
            total = sum((c.cost_usd for c in costs), Decimal("0"))
            return MeResponse(
                founder_email=founder.email,
                founder_name=founder.display_name,
                business_name=business.name,
                business_status=business.status.value,
                total_spend_usd=float(total),
            )
        finally:
            session.close()

    @app.post("/ask", response_model=AskResponse)
    async def ask(
        body: AskRequest,
        session: Annotated[Session, Depends(_require_session)],
    ) -> AskResponse:
        try:
            founder, business = _founder_business(session)
            inbound = await _pre_gateway_dispatch(
                text=body.message,
                business_id=business.id,
                founder_id=founder.id,
                channel="web",
            )
            if inbound is None:
                # Plugin filtered the message — return a quiet ack so
                # the UI doesn't hang. We don't surface plugin internals.
                return AskResponse(
                    content="(message filtered)",
                    skills_used=[],
                    reasoning_chars=0,
                    cost_usd=0.0,
                )
            ceo = _build_ceo(session)
            hiring = HiringService(session)
            router = ConversationRouter(session=session, hiring=hiring)
            decision = router.route_inbound(
                business_id=business.id,
                founder_id=founder.id,
                platform=ThreadPlatform.WEB,
                content=inbound,
            )
            memory = MemoryService(session=session)
            history = memory.load_recent(
                business_id=business.id,
                founder_id=founder.id,
                platform=ThreadPlatform.WEB,
                limit=20,
            )
            if (
                history
                and history[-1].role.value == "user"
                and history[-1].content == inbound
            ):
                history = history[:-1]
            result = await ceo.handle(
                business=business,
                founder=founder,
                founder_message=inbound,
                history=history,
                thread_id=decision.thread_id,
            )
            outbound_attachments: dict | None = None
            if result.clarify is not None and result.clarify.choices:
                outbound_attachments = {
                    "clarify_question": result.clarify.question,
                    "clarify_choices": list(result.clarify.choices),
                }
            router.route_outbound(
                business_id=business.id,
                founder_id=founder.id,
                platform=ThreadPlatform.WEB,
                content=result.content,
                requesting_agent_role_id=decision.delivering_agent_role_id,
                attachments=outbound_attachments,
            )
            return AskResponse(
                content=result.content,
                skills_used=[s.skill_name for s in result.skills_used],
                reasoning_chars=len(result.reasoning) if result.reasoning else 0,
                cost_usd=result.cost_usd,
                clarify_question=(
                    result.clarify.question if result.clarify is not None else None
                ),
                clarify_choices=(
                    list(result.clarify.choices) if (
                        result.clarify is not None and result.clarify.choices
                    ) else None
                ),
            )
        finally:
            session.close()

    @app.post("/ask/stream")
    async def ask_stream(
        body: AskRequest,
        session: Annotated[Session, Depends(_require_session)],
    ) -> StreamingResponse:
        """SSE-streamed variant of /ask. Each ``data:`` frame is a JSON event:

        - ``{"type":"phase","phase":"router|skill|synth"}``
        - ``{"type":"content","text":"..."}``
        - ``{"type":"reasoning","text":"..."}``
        - ``{"type":"done","skills_used":[...],"content":"<full>"}``

        Front-end consumes via EventSource. The response Message is persisted
        only on the final ``done`` event so we don't write half-replies on
        client disconnect."""
        import asyncio
        import json as jsonlib

        founder, business = _founder_business(session)
        ceo = _build_ceo(session)

        hiring = HiringService(session)
        router = ConversationRouter(session=session, hiring=hiring)
        decision = router.route_inbound(
            business_id=business.id,
            founder_id=founder.id,
            platform=ThreadPlatform.WEB,
            content=body.message,
        )
        memory = MemoryService(session=session)
        history = memory.load_recent(
            business_id=business.id,
            founder_id=founder.id,
            platform=ThreadPlatform.WEB,
            limit=20,
        )
        if (
            history
            and history[-1].role.value == "user"
            and history[-1].content == body.message
        ):
            history = history[:-1]

        async def _events() -> AsyncIterator[str]:
            try:
                stream = await ceo.handle_stream(
                    business=business,
                    founder=founder,
                    founder_message=body.message,
                    history=history,
                    thread_id=decision.thread_id,
                )
                final_content = ""
                final_clarify_question: str | None = None
                final_clarify_choices: list[str] | None = None
                async for ev in stream:
                    if ev.get("type") == "done":
                        final_content = str(ev.get("content") or "")
                        if ev.get("clarify_choices"):
                            final_clarify_question = ev.get("clarify_question")
                            final_clarify_choices = list(ev.get("clarify_choices") or [])
                    yield f"data: {jsonlib.dumps(ev)}\n\n"
                # Persist the CEO reply once the stream completes.
                if final_content:
                    persist_attachments: dict | None = None
                    if final_clarify_choices:
                        persist_attachments = {
                            "clarify_question": final_clarify_question,
                            "clarify_choices": final_clarify_choices,
                        }
                    router.route_outbound(
                        business_id=business.id,
                        founder_id=founder.id,
                        platform=ThreadPlatform.WEB,
                        content=final_content,
                        requesting_agent_role_id=decision.delivering_agent_role_id,
                        attachments=persist_attachments,
                    )
            except asyncio.CancelledError:
                # Client disconnected — don't persist the partial reply.
                raise
            except Exception as exc:
                yield f"data: {jsonlib.dumps({'type': 'error', 'message': str(exc)})}\n\n"
            finally:
                session.close()

        return StreamingResponse(_events(), media_type="text/event-stream")

    @app.post("/propose", response_model=PlanResponse)
    async def propose(
        body: ProposeRequest,
        session: Annotated[Session, Depends(_require_session)],
    ) -> PlanResponse:
        try:
            founder, business = _founder_business(session)
            ceo = _build_ceo(session)
            plan, proposal = await ceo.propose(
                business=business, founder=founder, founder_input=body.prompt
            )
            approval_id = getattr(proposal, "approval_id", None)
            return PlanResponse(
                summary=plan.summary,
                rationale=plan.rationale,
                next_action=plan.next_action,
                tasks=plan.tasks,
                estimated_hours=plan.estimated_hours,
                expected_impact=plan.expected_impact,
                approval_id=str(approval_id) if approval_id else None,
            )
        finally:
            session.close()

    @app.get("/approvals/pending", response_model=list[ApprovalResponse])
    def pending(
        session: Annotated[Session, Depends(_require_session)],
    ) -> list[ApprovalResponse]:
        try:
            _, business = _founder_business(session)
            rows = session.exec(
                select(Approval)
                .where(Approval.business_id == business.id)
                .where(Approval.status == ApprovalStatus.PENDING)
                .order_by(Approval.created_at.desc())  # type: ignore[attr-defined]
            ).all()
            return [
                ApprovalResponse(
                    id=str(a.id),
                    action_class=a.action_class.value,
                    platform=a.platform,
                    proposal_summary=a.proposal_summary,
                    status=a.status.value,
                    created_at=a.created_at.isoformat() if a.created_at else "",
                )
                for a in rows
            ]
        finally:
            session.close()

    @app.post("/approvals/{approval_id}/approve", response_model=DecisionResponse)
    def approve(
        approval_id: str,
        session: Annotated[Session, Depends(_require_session)],
    ) -> DecisionResponse:
        try:
            founder, _business = _founder_business(session)
            gate = ApprovalGate(session)
            result = gate.decide(
                approval_id=UUID(approval_id),
                decision=Decision.APPROVE,
                decided_by_founder_id=founder.id,
            )
            # Post-approve dispatch: some payload kinds finish their work
            # at approve-time rather than going through /execute. Skill-
            # author flows write files + reload the registry inline so
            # the next user message can use the new skill without a
            # restart.
            payload = result.approval.action_payload or {}
            payload_kind = payload.get("kind")
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
            return DecisionResponse(
                status=result.approval.status.value,
                consecutive_approvals=result.envelope.consecutive_approvals,
                threshold=result.envelope.threshold,
                promotion_offered=result.promotion_offered,
            )
        finally:
            session.close()

    @app.post("/approvals/{approval_id}/reject", response_model=DecisionResponse)
    def reject(
        approval_id: str,
        session: Annotated[Session, Depends(_require_session)],
    ) -> DecisionResponse:
        try:
            founder, _business = _founder_business(session)
            gate = ApprovalGate(session)
            result = gate.decide(
                approval_id=UUID(approval_id),
                decision=Decision.REJECT,
                decided_by_founder_id=founder.id,
            )
            return DecisionResponse(
                status=result.approval.status.value,
                consecutive_approvals=result.envelope.consecutive_approvals,
                threshold=result.envelope.threshold,
                promotion_offered=False,
            )
        finally:
            session.close()

    @app.post("/approvals/{approval_id}/execute", response_model=ExecuteResponse)
    async def execute(
        approval_id: str,
        session: Annotated[Session, Depends(_require_session)],
    ) -> ExecuteResponse:
        try:
            founder, business = _founder_business(session)
            approval = session.get(Approval, UUID(approval_id))
            if approval is None:
                raise HTTPException(404, f"Approval {approval_id} not found")
            if approval.status not in (
                ApprovalStatus.APPROVED,
                ApprovalStatus.AUTO_EXECUTED,
                ApprovalStatus.MODIFIED,
            ):
                raise HTTPException(
                    400,
                    f"Approval is {approval.status.value!r}; approve it first",
                )

            payload = approval.action_payload or {}
            plan = Plan(
                summary=approval.proposal_summary,
                rationale=list(payload.get("rationale") or []),
                next_action=str(payload.get("next_action") or ""),
                tasks=[str(t) for t in (payload.get("tasks") or [])],
                estimated_hours=payload.get("estimated_hours"),
                expected_impact=payload.get("expected_impact"),
                requires_founder_approval=False,
                reasoning=None,
                raw_response="",
            )
            ceo = _build_ceo(session)
            summary_obj = await ceo.execute_plan(
                business=business, founder=founder, plan=plan
            )
            return ExecuteResponse(
                headline=summary_obj.headline(),
                shipped=summary_obj.shipped,
                blocked=summary_obj.blocked,
                errored=summary_obj.errored,
                total_blockers=summary_obj.total_blockers,
                total_cost_usd=summary_obj.total_cost_usd,
                results=[
                    {
                        "title": r.title,
                        "status": r.status,
                        "summary": r.summary,
                        "blocker_count": len(r.blocker_ids),
                    }
                    for r in summary_obj.results
                ],
            )
        finally:
            session.close()

    @app.get("/blockers", response_model=BlockersListResponse)
    def blockers(
        session: Annotated[Session, Depends(_require_session)],
    ) -> BlockersListResponse:
        try:
            _, business = _founder_business(session)
            queue = BlockerQueue(session=session)
            hiring = HiringService(session)
            gate = ApprovalGate(session)
            cos = ChiefOfStaff(session=session, queue=queue, hiring=hiring, gate=gate)
            digest = cos.digest_for_ceo(business.id)
            rows = queue.list_open(business.id)
            return BlockersListResponse(
                items=[
                    BlockerResponse(
                        id=str(b.id),
                        title=b.title,
                        detail=b.detail,
                        kind=b.kind.value,
                        urgency=b.urgency.value,
                        status=b.status.value,
                        options=list(b.options),
                        cos_recommendation=b.cos_recommendation,
                    )
                    for b in rows
                ],
                digest_text=digest.render(),
                auto_resolved=digest.auto_resolved_count,
                dropped=digest.dropped_count,
            )
        finally:
            session.close()

    @app.get("/skills", response_model=list[SkillSpecResponse])
    def skills() -> list[SkillSpecResponse]:
        return [
            SkillSpecResponse(
                name=s.name, description=s.description, parameters=dict(s.parameters)
            )
            for s in skills_registry.list_specs()
        ]

    @app.post("/skills/{name}/run", response_model=SkillRunResponse)
    async def skill_run(
        name: str,
        body: SkillRunRequest,
        session: Annotated[Session, Depends(_require_session)],
    ) -> SkillRunResponse:
        try:
            founder, business = _founder_business(session)
            providers_list, accounts_list = _build_pool_pieces()
            if not accounts_list:
                raise HTTPException(503, "No provider configured")
            pool = InferencePool(providers=providers_list, accounts=accounts_list)
            tracker = CostTracker(pool=pool)
            ctx = SkillContext(
                business=business,
                founder=founder,
                session=session,
                cost_tracker=tracker,
            )
            try:
                result = await skills_registry.run(name, ctx=ctx, args=body.args)
            except SkillNotFound as exc:
                raise HTTPException(404, f"Unknown skill {name!r}") from exc
            return SkillRunResponse(
                skill_name=result.skill_name,
                summary=result.summary,
                payload=result.payload,
                cost_usd=result.cost_usd,
            )
        finally:
            session.close()

    # ------------------------------------------------------------ themes API
    # Mirrors Hermes-agent's GET /api/dashboard/themes + PUT /api/dashboard/theme
    # (including the May-4 fix to ship full definition for user themes so the
    # picker can render real palette swatches). See docs/THEME_PROTOCOL.md.

    from pydantic import BaseModel

    from korpha.themes import (
        DashboardTheme,
        DashboardThemesError,
        get_active_theme_name,
        list_themes,
        set_active_theme_name,
    )

    def _theme_to_dict(theme: DashboardTheme) -> dict[str, Any]:
        """Wire-format a DashboardTheme. Only used for user themes —
        built-ins ship name/label/description only since the dashboard
        already knows their full definition."""
        from dataclasses import asdict

        return asdict(theme)

    @app.get("/api/dashboard/themes")
    def get_dashboard_themes() -> dict[str, Any]:
        """List built-in + user themes, plus the currently active name."""
        entries = list_themes()
        return {
            "active": get_active_theme_name(),
            "themes": [
                {
                    "name": e.name,
                    "label": e.label,
                    "description": e.description,
                    "is_builtin": e.is_builtin,
                    "definition": _theme_to_dict(e.definition) if e.definition else None,
                }
                for e in entries
            ],
        }

    class ThemeSetBody(BaseModel):
        name: str

    @app.put("/api/dashboard/theme")
    def set_dashboard_theme(body: ThemeSetBody) -> dict[str, Any]:
        """Persist the active theme name. Validates that the theme
        resolves (built-in OR a real YAML in ~/.korpha/dashboard-themes/)."""
        try:
            set_active_theme_name(body.name)
        except DashboardThemesError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"ok": True, "theme": body.name}

    # Linear-style HTML dashboard. Registered last so the JSON API routes
    # above (/skills, /approvals/pending, /me, ...) take precedence on any
    # path collision.
    from korpha.api.dashboard import build_dashboard_router

    def _build_cost_tracker() -> CostTracker:
        providers_list, accounts_list = _build_pool_pieces()
        if not accounts_list:
            raise HTTPException(503, "No provider configured")
        pool = InferencePool(providers=providers_list, accounts=accounts_list)
        return CostTracker(pool=pool)

    # Inbound email — Resend POSTs parsed reply payloads here. Routes
    # through the channel framework so a digest reply lands as a
    # CEO message in the same EMAIL thread the digest came from.
    @app.post("/api/stripe/webhook")
    async def stripe_webhook_endpoint(request: Request) -> dict[str, Any]:
        """Stripe webhook ingestion — verifies the
        ``Stripe-Signature`` header and persists revenue events
        to the ``revenue_event`` table.

        Returns 200 even for events we don't track (Stripe retries
        forever on 5xx). Returns 400 on signature failure or bad
        body. Returns 503 if STRIPE_WEBHOOK_SECRET isn't set —
        misconfiguration shouldn't masquerade as success."""
        from korpha.commerce.stripe_webhook import (
            StripeWebhookError, process_webhook,
        )
        body = await request.body()
        sig = request.headers.get("stripe-signature")
        engine = _build_engine()
        with Session(engine) as session:
            try:
                outcome = process_webhook(
                    session=session,
                    payload=body,
                    signature_header=sig,
                )
            except StripeWebhookError as exc:
                raise HTTPException(
                    status_code=exc.status_code,
                    detail=str(exc),
                ) from exc

        return {
            "ok": True,
            "event_kind": outcome.event_kind,
            "persisted": outcome.persisted,
            "note": outcome.note,
        }

    @app.post("/api/email/inbound")
    async def email_inbound(payload: dict[str, Any]) -> dict[str, Any]:
        from korpha.channels.email_inbound import (
            incoming_from_resend,
            parse_resend_inbound,
        )

        try:
            parsed = parse_resend_inbound(payload)
        except ValueError as exc:
            raise HTTPException(400, f"malformed inbound payload: {exc}") from exc

        if not parsed.text.strip():
            # Empty / quoted-only reply — acknowledge but don't dispatch.
            # Resend webhook expects 2xx so it doesn't retry endlessly.
            return {"ok": True, "dispatched": False, "reason": "empty body"}

        incoming = incoming_from_resend(parsed)

        # Persist the Founder message + dispatch to CEO. We do NOT send
        # the reply synchronously here — Resend webhooks need a fast
        # 2xx response, and the Founder gets the CEO's reply via the
        # next scheduled digest (or via Telegram/Discord if those are
        # also active). This keeps the webhook handler simple + fast.
        from korpha.cofounder.model import (
            Message as DbMessage,
        )
        from korpha.cofounder.model import (
            MessageSenderType,
        )

        with Session(_build_engine()) as session:
            try:
                founder, business = _founder_business(session)
            except HTTPException:
                return {"ok": True, "dispatched": False, "reason": "no founder"}
            _providers_list, accounts_list = _build_pool_pieces()
            if not accounts_list:
                return {"ok": True, "dispatched": False, "reason": "no provider"}

            # Find or create the email thread for this from-address.
            from korpha.cofounder.hiring import HiringService
            from korpha.cofounder.model import (
                Thread,
                ThreadPlatform,
                ThreadStatus,
            )

            stmt = (
                select(Thread)
                .where(Thread.business_id == business.id)
                .where(Thread.platform == ThreadPlatform.EMAIL)
                .where(Thread.platform_thread_id == incoming.channel_user_id)
                .where(Thread.status == ThreadStatus.ACTIVE)
            )
            thread = session.exec(stmt).first()
            if thread is None:
                # New email conversation — pin it to the CEO since email
                # is a Founder-direct channel
                ceo = HiringService(session).ensure_ceo(business.id)
                thread = Thread(
                    business_id=business.id,
                    founder_id=founder.id,
                    agent_role_id=ceo.id,
                    platform=ThreadPlatform.EMAIL,
                    platform_thread_id=incoming.channel_user_id,
                    status=ThreadStatus.ACTIVE,
                )
                session.add(thread)
                session.commit()
                session.refresh(thread)

            session.add(
                DbMessage(
                    thread_id=thread.id,
                    sender_type=MessageSenderType.FOUNDER,
                    content=incoming.text,
                )
            )
            session.commit()

        return {"ok": True, "dispatched": True}

    app.include_router(
        build_dashboard_router(
            require_session=_require_session,
            founder_business=_founder_business,
            cost_tracker_factory=_build_cost_tracker,
            engine_factory=_build_engine,
        ),
        prefix="/app",
    )

    return app
