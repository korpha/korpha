"""``korpha`` command-line interface.

Subcommands:

- ``korpha init``   — create persistent config + DB at ``~/.korpha``.
- ``korpha status`` — show founder, business, agents, recent activity.
- ``korpha ask``    — one-shot Q&A through the CEO (real LLM).
- ``korpha propose``— ask CEO for a structured plan + auto-create approval.
- ``korpha demo``   — run the end-to-end scripted demo (in-memory DB).
"""
from __future__ import annotations

import asyncio
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Annotated
from uuid import UUID  # noqa: F401 -- used in _bootstrap_database return type

import typer

if TYPE_CHECKING:
    from korpha.approvals.model import Approval as _ApprovalForType
from dotenv import load_dotenv
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine, select

import korpha.db.registry  # noqa: F401  -- registers all models
from korpha.approvals.gate import (
    ApprovalGate,
    ProposalAccepted,
    ProposalDenied,
    ProposalPending,
)
from korpha.audit.model import Activity, Cost, InferenceTier
from korpha.blockers.queue import BlockerQueue
from korpha.business.model import Business, BusinessStatus
from korpha.business_units.model import BusinessUnit, BusinessUnitKind
from korpha.cofounder.ceo import CEO, Plan
from korpha.cofounder.chief_of_staff import ChiefOfStaff
from korpha.cofounder.hiring import HiringService
from korpha.cofounder.memory import MemoryService
from korpha.cofounder.model import AgentRole, ThreadPlatform
from korpha.cofounder.routing import ConversationRouter
from korpha.cofounder.workforce import DirectorFactory, Workforce
from korpha.identity.model import Founder
from korpha.inference import (
    InferencePool,
    ProviderAccount,
    ollama_cloud_provider,
    opencode_go_provider,
)
from korpha.inference.config import ProviderConfigError, load_from_yaml
from korpha.inference.cost_tracker import CostTracker
from korpha.inference.registry import AuthType

app = typer.Typer(help="Your AI cofounder for the online business you keep saying you'll start.")


@app.callback(invoke_without_command=True)
def _root_callback(ctx: typer.Context) -> None:
    """Friendly first-run hint when ``korpha`` is run with no
    subcommand and the local DB doesn't exist yet. Mike sees a
    one-line nudge instead of just the help block."""
    if ctx.invoked_subcommand is not None:
        return
    # Show help. Then the hint, if applicable.
    typer.echo(ctx.get_help())
    try:
        from pathlib import Path as _P
        import os as _os

        base = _os.environ.get("KORPHA_DATA_DIR")
        db = (
            (_P(base) / "korpha.db") if base
            else (_P.home() / ".korpha" / "korpha.db")
        )
        if not db.exists():
            typer.echo(
                "\n👋 First time? Run `korpha init` to set up "
                "your founder + first business."
            )
    except Exception:  # noqa: BLE001
        pass


@app.command()
def backup(
    output: Annotated[Path | None, typer.Option(
        "--output", "-o",
        help="Destination tarball. Default: ./korpha-backup-<date>.tar.gz",
    )] = None,
) -> None:
    """Snapshot the entire Korpha data dir to a tarball.

    Captures the sqlite DB, agent-authored skills, cron scripts,
    plugin configs, audit archive, and checkpoint blobs. Restore
    with ``korpha restore <tarball>``.
    """
    _ensure_load_env()
    import tarfile
    import os as _os
    from datetime import datetime as _dt
    from pathlib import Path as _P

    base_str = _os.environ.get("KORPHA_DATA_DIR")
    base = _P(base_str) if base_str else (_P.home() / ".korpha")
    if not base.is_dir():
        typer.echo(_red(
            f"No Korpha data dir at {base}. Run `korpha init` "
            "first."
        ))
        raise typer.Exit(code=1)

    if output is None:
        stamp = _dt.now().strftime("%Y%m%d-%H%M%S")
        output = _P(f"./korpha-backup-{stamp}.tar.gz").resolve()
    else:
        output = output.expanduser().resolve()

    typer.echo(f"Backing up {base} → {output}")
    try:
        with tarfile.open(output, "w:gz") as tar:
            tar.add(base, arcname="korpha")
    except OSError as exc:
        typer.echo(_red(f"backup failed: {exc}"))
        raise typer.Exit(code=1) from exc

    size = output.stat().st_size
    typer.echo(_green(
        f"✓ Backup written ({_human_bytes(size)})."
    ))
    typer.echo(_dim(
        "  Restore with: korpha restore "
        + str(output)
    ))


@app.command()
def restore(
    tarball: Annotated[Path, typer.Argument(
        help="Path to a tarball produced by `korpha backup`.",
    )],
    force: Annotated[bool, typer.Option(
        "--force",
        help="Overwrite existing KORPHA_DATA_DIR contents without "
             "prompting.",
    )] = False,
) -> None:
    """Restore an Korpha data dir from a backup tarball.

    By default refuses to clobber an existing data dir — use
    --force after you've confirmed the existing contents are
    expendable.
    """
    _ensure_load_env()
    import shutil
    import tarfile
    import tempfile
    import os as _os
    from pathlib import Path as _P

    src = tarball.expanduser().resolve()
    if not src.is_file():
        typer.echo(_red(f"backup file not found: {src}"))
        raise typer.Exit(code=1)

    base_str = _os.environ.get("KORPHA_DATA_DIR")
    base = _P(base_str) if base_str else (_P.home() / ".korpha")

    if base.exists() and any(base.iterdir()) and not force:
        typer.echo(_red(
            f"{base} is not empty. Re-run with --force to overwrite, "
            "or back it up first with `korpha backup` and remove "
            "the directory."
        ))
        raise typer.Exit(code=1)

    typer.echo(f"Restoring {src} → {base}")
    with tempfile.TemporaryDirectory() as td:
        staging = _P(td)
        try:
            with tarfile.open(src, "r:gz") as tar:
                tar.extractall(staging, filter="data")
        except (OSError, tarfile.TarError) as exc:
            typer.echo(_red(f"extract failed: {exc}"))
            raise typer.Exit(code=1) from exc
        unpacked = staging / "korpha"
        if not unpacked.is_dir():
            typer.echo(_red(
                "tarball missing top-level 'korpha/' directory; "
                "this doesn't look like an korpha backup"
            ))
            raise typer.Exit(code=1)
        if base.exists() and force:
            shutil.rmtree(base)
        base.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(unpacked), str(base))
    typer.echo(_green(
        f"✓ Restored to {base}. Run `korpha doctor` to verify."
    ))


def _data_dir() -> Path:
    override = os.getenv("KORPHA_DATA_DIR")
    return Path(override) if override else Path.home() / ".korpha"


def _db_path() -> Path:
    return _data_dir() / "korpha.db"


def _engine() -> Engine:
    db = _db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(
        f"sqlite:///{db}", connect_args={"check_same_thread": False}
    )


def _ollama_cloud_account() -> ProviderAccount | None:
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


def _opencode_go_account() -> ProviderAccount | None:
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


def _stamp_alembic_head_if_possible(db_path: Path) -> None:
    """Create the alembic_version table and mark the latest migration as
    applied. Called after `init` does a create_all() so subsequent `migrate`
    calls don't fail trying to re-apply baseline migrations on top of an
    already-built schema. Best-effort — if alembic isn't installed we skip
    silently, keeping init resilient."""
    import subprocess

    repo_root = Path(__file__).resolve().parent.parent
    alembic_ini = repo_root / "alembic.ini"
    if not alembic_ini.exists():
        return
    env = os.environ.copy()
    env["KORPHA_DB_URL"] = f"sqlite:///{db_path}"
    import contextlib

    with contextlib.suppress(FileNotFoundError):
        subprocess.run(
            ["alembic", "-c", str(alembic_ini), "stamp", "head"],
            env=env,
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )


def _build_provider_pool() -> tuple[list[object], list[ProviderAccount]] | None:
    """Build the (providers, accounts) lists.

    Resolution order:
    1. ``~/.korpha/providers.yaml`` (or ``KORPHA_PROVIDERS_FILE``) — full
       multi-provider config with per-account tier mappings, spend caps, etc.
    2. Env-var fallback: ``OPENCODE_API_KEY`` (preferred) + ``OLLAMA_CLOUD_API_KEY``
       fallback. Mirrors the original DeepSeek-on-OpenCode setup so existing
       installs keep working without writing YAML.

    Returns None if no provider can be configured either way.
    """
    try:
        loaded = load_from_yaml()
    except ProviderConfigError as exc:
        typer.echo(_yellow(f"providers.yaml problem: {exc}"))
        loaded = None
    if loaded is not None and loaded.accounts:
        return list(loaded.providers), list(loaded.accounts)

    providers: list[object] = []
    accounts: list[ProviderAccount] = []
    oc = _opencode_go_account()
    if oc is not None:
        providers.append(opencode_go_provider())
        accounts.append(oc)
    olc = _ollama_cloud_account()
    if olc is not None:
        providers.append(ollama_cloud_provider())
        accounts.append(olc)
    if not accounts:
        return None
    return providers, accounts


def _bold(s: str) -> str:
    return f"\033[1m{s}\033[0m"


def _dim(s: str) -> str:
    return f"\033[2m{s}\033[0m"


def _green(s: str) -> str:
    return f"\033[32m{s}\033[0m"


def _yellow(s: str) -> str:
    return f"\033[33m{s}\033[0m"


def _red(s: str) -> str:
    return f"\033[31m{s}\033[0m"


def _ensure_load_env() -> None:
    """Load .env from the user's cwd (not from Korpha's source dir).

    ``load_dotenv()`` with default args walks up from the *caller's
    source file*, which would pick up ``korpha/.env`` shipped with
    the package — wrong for an installed CLI. We pin to ``usecwd=True``
    so the user's project ``.env`` is what gets loaded, and only when
    they ran ``korpha`` from there.
    """
    from dotenv import find_dotenv

    path = find_dotenv(usecwd=True)
    if path:
        load_dotenv(path)


def _build_ceo(session: Session) -> CEO | None:
    pool_setup = _build_provider_pool()
    if pool_setup is None:
        typer.echo(
            _yellow(
                "warning: no provider configured. Set OPENCODE_API_KEY (preferred, "
                "https://opencode.ai/zen/go) or OLLAMA_CLOUD_API_KEY in your "
                "environment or in a .env file at cwd."
            )
        )
        return None
    providers_list, accounts_list = pool_setup
    from korpha.skills import default_registry as skills_registry

    pool = InferencePool(providers=providers_list, accounts=accounts_list)  # type: ignore[arg-type]
    tracker = CostTracker(pool=pool)
    hiring = HiringService(session)
    gate = ApprovalGate(session)
    queue = BlockerQueue(session=session)
    cos = ChiefOfStaff(session=session, queue=queue, hiring=hiring, gate=gate)
    factory = DirectorFactory(
        session=session, cost_tracker=tracker, queue=queue, hiring=hiring
    )
    workforce = Workforce.with_default_directors(director_factory=factory)

    # Optional browser service. Lazy-imported so tests / installs without
    # playwright don't pay for it.
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


async def _execute_email_outreach(
    session: Session,
    approval: _ApprovalForType,
    payload: dict[str, object],
    business: Business,
) -> None:
    """CLI wrapper around the shared email-outreach dispatcher. Keeps
    typer echo + exit codes; pure dispatch logic lives in
    korpha.approvals.dispatch so HTTP /approve can share it."""
    from korpha.approvals.dispatch import dispatch_email_outreach

    result = await dispatch_email_outreach(session, approval, payload, business)
    if result.ok:
        typer.echo(_green(f"✓ {result.message}"))
    else:
        typer.echo(_yellow(result.message))
        raise typer.Exit(code=1)


async def _execute_commerce(
    session: Session,
    approval: _ApprovalForType,
    payload: dict[str, object],
    business: Business,
) -> None:
    """CLI wrapper around the shared commerce dispatcher. See
    korpha.approvals.dispatch.dispatch_commerce for the implementation
    shared with the HTTP /approve handler."""
    from korpha.approvals.dispatch import dispatch_commerce

    result = await dispatch_commerce(session, approval, payload, business)
    if result.ok:
        typer.echo(_green("✓ payment link created"))
        url = result.details.get("url")
        if url:
            typer.echo(f"  {url}")
    else:
        typer.echo(_yellow(result.message))
        raise typer.Exit(code=1)


def _ensure_founder_and_business(session: Session) -> tuple[Founder, Business]:
    """Resolve the Founder + their active Business.

    The active business comes from ``founder.active_business_id`` when set,
    otherwise the Founder's only business if they have exactly one. Multiple
    businesses with no active selection raises a clear error pointing at
    ``business-switch``.
    """
    from korpha.business.multi import BusinessResolutionError, active_business

    founder = session.exec(select(Founder)).first()
    if founder is None:
        raise typer.BadParameter("No founder configured. Run `korpha init` first.")
    try:
        business = active_business(session, founder)
    except BusinessResolutionError as exc:
        raise typer.BadParameter(str(exc)) from exc
    return founder, business


def _bootstrap_database(
    *,
    email: str,
    name: str,
    business: str,
    description: str,
) -> tuple[str, str, str, "UUID"]:
    """Create the DB + schema + Founder + Business + root BusinessUnit + CEO.

    Idempotent: re-running with the same identity is a no-op except for
    the schema/CEO bits, which are already idempotent. Returns the
    display labels used by both `init` (prints them) and `server`
    (auto-bootstrap path, doesn't print).

    Returns (founder_label, business_name, ceo_title, ceo_id).
    """
    db = _db_path()
    db.parent.mkdir(parents=True, exist_ok=True)
    engine = _engine()
    SQLModel.metadata.create_all(engine)
    _stamp_alembic_head_if_possible(db)
    from korpha.cofounder.fts import ensure_fts_index

    with Session(engine) as fts_session:
        ensure_fts_index(fts_session)
        fts_session.commit()

    with Session(engine) as session:
        existing_founder = session.exec(select(Founder)).first()
        if existing_founder is None:
            founder = Founder(email=email, display_name=name or None)
            session.add(founder)
            session.commit()
            session.refresh(founder)
        else:
            founder = existing_founder

        existing_business = session.exec(select(Business)).first()
        if existing_business is None:
            biz = Business(
                founder_id=founder.id,
                name=business,
                description=description or None,
                status=BusinessStatus.IDEA,
            )
            session.add(biz)
            session.commit()
            session.refresh(biz)
        else:
            biz = existing_business

        if founder.active_business_id != biz.id:
            founder.active_business_id = biz.id
            session.add(founder)
            session.commit()
            session.refresh(founder)

        existing_unit = session.exec(
            select(BusinessUnit).where(BusinessUnit.business_id == biz.id)
        ).first()
        if existing_unit is None:
            root_unit = BusinessUnit(
                business_id=biz.id,
                parent_id=None,
                kind=BusinessUnitKind.DEFAULT,
                name=biz.name,
                slug="default",
            )
            session.add(root_unit)
            session.commit()

        hiring = HiringService(session)
        ceo = hiring.ensure_ceo(biz.id)
        return (
            founder.display_name or founder.email,
            biz.name,
            ceo.title,
            ceo.id,
        )


# Sentinel value the dashboard recognizes as "founder identity not yet
# captured — show the /app/welcome step before the brief textarea."
_BOOTSTRAP_PLACEHOLDER_EMAIL = "founder@localhost.invalid"
_BOOTSTRAP_PLACEHOLDER_BUSINESS = "My Business"


def _system_username_or(fallback: str = "Founder") -> str:
    """Best-effort guess at the founder's display name from the OS."""
    import getpass
    try:
        return getpass.getuser() or fallback
    except Exception:  # noqa: BLE001
        return fallback


@app.command()
def init(
    email: Annotated[
        str | None, typer.Option(help="Founder email address.")
    ] = None,
    name: Annotated[str | None, typer.Option(help="Founder display name.")] = None,
    business: Annotated[
        str | None, typer.Option(help="Business name.")
    ] = None,
    description: Annotated[
        str | None, typer.Option(help="Business description.")
    ] = None,
) -> None:
    """Initialize the local Korpha config and database."""
    _ensure_load_env()
    db = _db_path()

    if email is None:
        email = typer.prompt("Founder email")
    if name is None:
        name = typer.prompt("Founder display name", default="")
    if business is None:
        business = typer.prompt("Business name")
    if description is None:
        description = typer.prompt("Business description", default="")

    founder_label, business_name, ceo_title, ceo_id = _bootstrap_database(
        email=email,
        name=name,
        business=business,
        description=description,
    )

    typer.echo(_bold("Korpha initialized."))
    typer.echo(f"  Data dir:  {_data_dir()}")
    typer.echo(f"  DB file:   {db}")
    typer.echo(f"  Founder:   {founder_label}")
    typer.echo(f"  Business:  {business_name}")
    typer.echo(f"  CEO:       {ceo_title} (id={ceo_id})")

    # Provider check: env var found OR providers.yaml has a usable entry.
    has_provider = _has_any_provider_configured()
    if has_provider:
        typer.echo(_green("  Provider:  configured ✓"))
    else:
        typer.echo(_yellow("  Provider:  none configured yet"))
        typer.echo()
        if typer.confirm(
            "Set up an LLM provider now? (Mike-friendly, ~30 seconds)",
            default=True,
        ):
            from korpha.cli_config import run_provider_wizard

            run_provider_wizard(
                on_done_message="You're done. Run `korpha server` to launch."
            )
        else:
            typer.echo(
                _dim(
                    "  No problem — run `korpha config` later when ready."
                )
            )

    # Coding-delegation status: separate concern from inference. The CTO
    # uses Claude Code / Codex CLI to actually write + ship code, both
    # auth via subscription (no API key). Mike doesn't know to install
    # them — print status so he can fix it now or later.
    _print_delegation_status()


def _print_delegation_status(*, with_header: bool = True) -> None:
    """Print whether Claude Code / Codex CLI are installed + authed.

    Optional for Korpha to function — only matters once the cofounder's
    CTO actually delegates code work. We surface it during init so Mike
    knows it's a gap, not a hidden requirement.

    Pass ``with_header=False`` when the caller has already printed its
    own grouping header (e.g. ``doctor`` does this so we don't double-
    label the section)."""
    from korpha.delegation.status import check_all

    statuses = check_all()
    if with_header:
        typer.echo()
        typer.echo(_bold("Coding delegation (optional)"))
        typer.echo(
            _dim(
                "  Lets the CTO ship code. Skip if you only want planning + "
                "drafting; add later via the hints below."
            )
        )
    for s in statuses:
        if s.authenticated:
            typer.echo(
                f"  {_green('✓')} {s.name}: ready  "
                + _dim(f"({s.binary} on PATH, auth file present)")
            )
        elif s.installed:
            typer.echo(
                f"  {_yellow('○')} {s.name}: installed but not signed in"
            )
            typer.echo(_dim(f"      → {s.login_hint}"))
        else:
            typer.echo(f"  {_dim('·')} {s.name}: not installed")
            typer.echo(_dim(f"      → install: {s.install_hint}"))
            typer.echo(_dim(f"      → then:    {s.login_hint}"))


def _has_any_provider_configured() -> bool:
    """True when there's any usable provider — env-var fallback
    across the full preset matrix OR a providers.yaml entry
    with a resolvable api key. Used by `init` to decide whether
    to launch the wizard."""
    from korpha.inference.env_fallback import (
        list_configured_provider_names,
    )

    if list_configured_provider_names():
        return True
    try:
        from korpha.inference.config import load_from_yaml

        loaded = load_from_yaml()
    except Exception:
        return False
    return loaded is not None and len(loaded.accounts) > 0


@app.command()
def eval(
    role: Annotated[
        str | None,
        typer.Option(help="Score one role only: ceo / cto / cmo / coo"),
    ] = None,
    tier: Annotated[
        str, typer.Option(help="Inference tier: pro | workhorse")
    ] = "pro",
    json_out: Annotated[
        bool, typer.Option("--json", help="Emit JSON instead of human text")
    ] = False,
    max_tokens: Annotated[
        int | None,
        typer.Option(
            "--max-tokens",
            help=(
                "Override max_tokens for this run. Default uses the "
                "agent_max_tokens() floor (16k). Useful for A/B sweeps: "
                "e.g. --max-tokens 8000 to reproduce the old budget, "
                "--max-tokens 32000 to give reasoning more headroom."
            ),
        ),
    ] = None,
    runs: Annotated[
        int,
        typer.Option(
            "--runs",
            min=1,
            max=10,
            help=(
                "Run the sweep N times and average — flattens reasoning-"
                "model nondeterminism on borderline assertions. Default 1."
            ),
        ),
    ] = 1,
) -> None:
    """Score role prompts against deterministic fixtures.

    Methodology borrowed from ClawEval — exact-expected-answer
    assertions, no LLM-as-judge. Recommended canonical baseline:
    DeepSeek V4 Pro (open weights, frontier).

    Configure that with ``korpha config`` first; pick "deepseek"
    and set ``tiers.pro: deepseek-v4-pro``.
    """
    _ensure_load_env()
    from korpha.audit.model import InferenceTier
    from korpha.evals import run_eval
    from korpha.evals.runner import (
        EvalReport,
        average_reports,
        render_report,
    )
    from korpha.inference.config import load_from_yaml

    try:
        loaded = load_from_yaml()
    except ProviderConfigError as exc:
        typer.echo(_yellow(f"providers.yaml problem: {exc}"))
        raise typer.Exit(code=1) from exc
    if loaded is None or not loaded.accounts:
        typer.echo(_yellow(
            "No provider configured. Run `korpha config` first."
        ))
        raise typer.Exit(code=1)

    # Pick the first account that has a model for the requested tier.
    try:
        tier_enum = InferenceTier(tier)
    except ValueError as exc:
        typer.echo(_yellow(f"unknown tier {tier!r} — use 'pro' or 'workhorse'"))
        raise typer.Exit(code=1) from exc

    account = next(
        (a for a in loaded.accounts if tier_enum in a.tier_models), None
    )
    if account is None:
        typer.echo(_yellow(
            f"No configured account has a model for tier {tier!r}. "
            "Add one with `korpha config`."
        ))
        raise typer.Exit(code=1)

    pool = InferencePool(
        providers=list(loaded.providers), accounts=list(loaded.accounts),
    )

    async def _go() -> None:
        if runs > 1:
            # Multi-run averaging: run the sweep N times, mark each
            # assertion passed if it passed in ≥ majority of runs.
            # Smooths out reasoning-model nondeterminism.
            reports: list[EvalReport] = []
            for i in range(runs):
                typer.echo(_dim(f"  run {i+1}/{runs}…"))
                r = await run_eval(
                    pool=pool,
                    account=account,
                    provider_label=(
                        f"{account.label or account.provider_name}"
                        f"/{account.tier_models[tier_enum]}"
                    ),
                    role=role,
                    tier=tier_enum,
                    max_tokens=max_tokens,
                )
                reports.append(r)
            report = average_reports(reports)
        else:
            report = await run_eval(
                pool=pool,
                account=account,
                provider_label=(
                    f"{account.label or account.provider_name}"
                    f"/{account.tier_models[tier_enum]}"
                ),
                role=role,
                tier=tier_enum,
                max_tokens=max_tokens,
            )
        if json_out:
            import json

            payload = {
                "provider_label": report.provider_label,
                "overall_pass_rate": report.overall_pass_rate,
                "total_cost_usd": report.total_cost_usd,
                "roles": [
                    {
                        "role": rs.role,
                        "pass_rate": rs.pass_rate,
                        "passed": rs.passed_assertions,
                        "total": rs.total_assertions,
                        "cost_usd": rs.total_cost_usd,
                        "tasks": [
                            {
                                "id": tr.task.id,
                                "pass_rate": tr.pass_rate,
                                "error": tr.error,
                                "failures": [
                                    {
                                        "kind": r.assertion.kind,
                                        "description": r.assertion.description,
                                        "detail": r.detail,
                                    }
                                    for r in tr.results if not r.passed
                                ],
                            }
                            for tr in rs.tasks
                        ],
                    }
                    for rs in report.roles
                ],
            }
            typer.echo(json.dumps(payload, indent=2))
        else:
            typer.echo(render_report(report))

    asyncio.run(_go())


@app.command()
def doctor() -> None:
    """Check what's configured and what's not (provider + delegation).

    Run anytime to see whether Mike's set up is complete enough for the
    BRIEF.md 5-minute demo to work end-to-end. Output is grouped:

      Required          — things Korpha literally cannot run without
      Optional          — features unlocked by adding more setup
      Coding delegation — separate because both are optional CLIs that
                          Korpha subprocesses, not API integrations

    Each line ends with a one-sentence plain-English explanation when
    the status is ``not configured`` so non-technical Founders see
    'why does this matter' alongside 'how do I fix it'.
    """
    _ensure_load_env()
    typer.echo(_bold("Korpha health check"))
    typer.echo()

    # ----------------------- Required (must exist for anything to work)
    typer.echo(_bold("Required"))
    typer.echo(_dim(
        "  Without these, Korpha can't talk to an LLM or do anything useful."
    ))
    from korpha.inference.env_fallback import (
        list_configured_provider_names,
        list_supported_env_vars,
    )

    configured = list_configured_provider_names()
    if configured:
        typer.echo(
            f"  {_green('✓')} Inference provider — {len(configured)} "
            "configured via env"
        )
        typer.echo(_dim(
            "    " + ", ".join(configured)
        ))
        typer.echo(_dim(
            "    Pool routes cheapest-first — set providers.yaml to override."
        ))
    elif _has_any_provider_configured():
        typer.echo(
            f"  {_green('✓')} Inference provider (via providers.yaml)"
        )
    else:
        typer.echo(f"  {_yellow('○')} Inference provider — not configured")
        typer.echo(_dim(
            "    Set ANY of these to enable:"
        ))
        # Show top-3 most useful env vars
        for name, env_var in list_supported_env_vars()[:5]:
            typer.echo(_dim(f"      {env_var}  → {name}"))
        typer.echo(_dim(
            "    Or run `korpha config` for the interactive wizard."
        ))
    typer.echo()

    # ----------------------- Optional integrations
    typer.echo(_bold("Optional integrations"))
    typer.echo(_dim(
        "  Each unlocks a specific feature. Skip the ones you don't need."
    ))

    # RankMyAnswer (GEO + SEO)
    from korpha.integrations.rank_my_answer import client_from_env_or_config

    rma = client_from_env_or_config()
    if rma is not None:
        typer.echo(f"  {_green('✓')} RankMyAnswer — GEO + SEO audits enabled")
        typer.echo(_dim(
            "    Your cofounder can audit landing pages for both Google and"
        ))
        typer.echo(_dim(
            "    LLM-citation signals (ChatGPT, Claude, Gemini answers)."
        ))
    else:
        typer.echo(f"  {_dim('·')} RankMyAnswer — not configured")
        typer.echo(_dim(
            "    Skip if SEO/GEO isn't a focus. Add later when you want"
        ))
        typer.echo(_dim(
            "    your cofounder to work on getting eyeballs to your product."
        ))
        typer.echo(_dim("    → fix:  korpha config-rankmyanswer-add"))

    # Resend (email outbound)
    if os.getenv("RESEND_API_KEY"):
        typer.echo(f"  {_green('✓')} Email outbound (Resend) — daily digests + cold-email send enabled")
        typer.echo(_dim(
            "    Your cofounder can email you the daily digest + send"
        ))
        typer.echo(_dim(
            "    approved cold emails."
        ))
    else:
        typer.echo(f"  {_dim('·')} Email outbound — not configured")
        typer.echo(_dim(
            "    Skip if you only want the dashboard. Add to get daily"
        ))
        typer.echo(_dim(
            "    digests in your inbox + send approved cold emails."
        ))
        typer.echo(_dim(
            "    → fix:  set RESEND_API_KEY in ~/.korpha/.env (see docs/CHANNELS.md)"
        ))

    # Stripe (payment links)
    if os.getenv("STRIPE_API_KEY"):
        typer.echo(f"  {_green('✓')} Stripe — payment-link creation enabled")
        typer.echo(_dim(
            "    Your cofounder can spin up checkout links from chat."
        ))
    else:
        typer.echo(f"  {_dim('·')} Stripe — not configured")
        typer.echo(_dim(
            "    Skip until you're charging. Add when ready to monetize."
        ))
        typer.echo(_dim(
            "    → fix:  set STRIPE_API_KEY in ~/.korpha/.env"
        ))

    typer.echo()

    # ----------------------- Coding delegation
    typer.echo(_bold("Coding delegation (optional)"))
    typer.echo(_dim(
        "  Lets your CTO actually ship code (not just plan). Skip if you"
    ))
    typer.echo(_dim(
        "  only want planning + drafting; Korpha works fully without these."
    ))
    _print_delegation_status(with_header=False)
    typer.echo()

    # ----------------------- Stack health (structural probes)
    typer.echo(_bold("Stack health"))
    typer.echo(_dim(
        "  Python version, DB reachability, security guards, optional"
    ))
    typer.echo(_dim(
        "  dependencies. If anything below is ✗, the cofounder won't run."
    ))
    from korpha.diagnostics import run_doctor

    structural = run_doctor()
    for line in structural.render(color=True).splitlines():
        typer.echo(line)
    typer.echo()

    # ----------------------- Footer
    typer.echo(_dim(
        "Full reference: https://github.com/korpha/korpha/blob/main/docs/TROUBLESHOOTING.md"
    ))


@app.command()
def config() -> None:
    """Walk through adding an LLM provider (interactive, Mike-friendly).

    Writes to ``~/.korpha/providers.yaml``. Run as many times as you
    want to add multiple keys / providers — earlier ones are tried first.
    """
    _ensure_load_env()
    from korpha.cli_config import run_provider_wizard

    run_provider_wizard()


@app.command(name="config-rankmyanswer-add")
def config_rankmyanswer_add() -> None:
    """Add a RankMyAnswer.com API key so Korpha can work on
    getting eyeballs to your product or service (GEO + SEO).

    GEO = getting cited by ChatGPT / Perplexity / Claude / Gemini answers.
    SEO = getting found on Google. Both ranking surfaces matter today.

    Optional integration. Sign up at https://rankmyanswer.com to get a key.
    """
    _ensure_load_env()
    from korpha.cli_config import run_rankmyanswer_wizard

    run_rankmyanswer_wizard()


@app.command(name="config-image-add")
def config_image_add() -> None:
    """Add an image-generation provider (Replicate / fal.ai / local SD /
    Codex CLI). Image gen is separate from inference — most users want
    one of these even if they're using a non-Codex inference provider.
    """
    _ensure_load_env()
    from korpha.cli_config import run_image_provider_wizard

    run_image_provider_wizard()


cofounder_app = typer.Typer(
    help=(
        "Cofounder Protocol — install / list / uninstall third-party "
        "Korpha-native partners. A partner ships a single "
        "``cofounder.yaml`` manifest declaring which skills it brings, "
        "how the user links their account, and what branding it owns."
    )
)
app.add_typer(cofounder_app, name="cofounder")


# ---------------------------------------------------------------------------
# `korpha setup` — interactive walkthrough for plugin-aware setup.
# Drives off provider_profile_registry + platform_registry so plugin-
# supplied entries auto-appear in the catalog. Mike-non-technical:
# every credential / opt-in goes through these prompts, no YAML
# editing.
# ---------------------------------------------------------------------------


setup_app = typer.Typer(
    help=(
        "Interactive setup for providers, channels, and plugins. "
        "Walks every credential prompt so you never edit YAML by hand."
    )
)
app.add_typer(setup_app, name="setup")


@setup_app.command("providers")
def setup_providers(
    name: Annotated[
        str | None,
        typer.Argument(
            help=(
                "Provider profile name (e.g. 'deepseek', 'opencode-go'). "
                "Omit to list all available profiles + their status."
            )
        ),
    ] = None,
) -> None:
    """List provider profiles or configure one interactively."""
    from korpha import cli_setup

    if name is None:
        cli_setup.list_providers()
        return
    cli_setup.setup_provider(name)


@setup_app.command("channels")
def setup_channels(
    name: Annotated[
        str | None,
        typer.Argument(
            help=(
                "Channel adapter name (e.g. 'telegram', 'email'). "
                "Omit to list all registered adapters."
            )
        ),
    ] = None,
) -> None:
    """List channel adapters or configure one interactively."""
    from korpha import cli_setup

    if name is None:
        cli_setup.list_channels()
        return
    cli_setup.setup_channel(name)


@setup_app.command("plugins")
def setup_plugins(
    action: Annotated[
        str | None,
        typer.Argument(
            help=(
                "'enable <name>', 'disable <name>', or omit to list "
                "discovered plugins + current allow / deny lists."
            )
        ),
    ] = None,
    name: Annotated[
        str | None,
        typer.Argument(
            help="Plugin name (only required for enable / disable)."
        ),
    ] = None,
) -> None:
    """Enable/disable plugins. Default policy is opt-in: nothing
    loads unless explicitly enabled (or env var ``KORPHA_PLUGINS_ENABLED``
    is set)."""
    from korpha import cli_setup

    if action is None:
        cli_setup.list_plugins_status()
        return
    if action not in ("enable", "disable"):
        typer.echo(
            typer.style(
                f"Unknown action {action!r}. Use 'enable' or 'disable'.",
                fg=typer.colors.RED,
            )
        )
        raise typer.Exit(2)
    if not name:
        typer.echo(
            typer.style(
                f"`korpha setup plugins {action}` needs a plugin name.",
                fg=typer.colors.RED,
            )
        )
        raise typer.Exit(2)
    if action == "enable":
        cli_setup.enable_plugin(name)
    else:
        cli_setup.disable_plugin(name)


@cofounder_app.command("install")
def cofounder_install(
    source: Annotated[
        str,
        typer.Argument(
            help=(
                "Local path or http(s) URL to a ``cofounder.yaml`` manifest. "
                "URLs are fetched read-only; no partner code is executed."
            )
        ),
    ],
) -> None:
    """Install a Cofounder Protocol manifest from a path or URL."""
    _ensure_load_env()
    from pathlib import Path

    from korpha.protocol import ManifestError, install_manifest

    src = source
    tmp_path: Path | None = None
    try:
        if src.startswith(("http://", "https://")):
            import tempfile

            import httpx

            try:
                resp = httpx.get(src, timeout=30, follow_redirects=True)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                typer.echo(_yellow(f"Couldn't fetch manifest: {exc}"))
                raise typer.Exit(code=1) from exc
            tmp_dir = Path(tempfile.mkdtemp(prefix="korpha-cofounder-"))
            tmp_path = tmp_dir / "cofounder.yaml"
            tmp_path.write_text(resp.text, encoding="utf-8")
            src = str(tmp_path)
        try:
            installed = install_manifest(src)
        except ManifestError as exc:
            typer.echo(_yellow(f"Manifest error: {exc}"))
            raise typer.Exit(code=1) from exc
        typer.echo(_bold(f"✓ Installed cofounder partner: {installed.manifest.display_name}"))
        typer.echo(_dim(f"   {installed.manifest.description.splitlines()[0]}"))
        typer.echo(_dim(f"   Stored at: {installed.install_dir}"))
        if installed.manifest.auth and installed.manifest.auth.setup_command:
            typer.echo()
            typer.echo("Next: link your account by running")
            typer.echo(_green(f"   {installed.manifest.auth.setup_command}"))
        if installed.manifest.auth and installed.manifest.auth.signup_url:
            typer.echo(_dim(f"   No account yet? Sign up: {installed.manifest.auth.signup_url}"))
    finally:
        if tmp_path is not None:
            import contextlib
            import shutil

            with contextlib.suppress(OSError):
                shutil.rmtree(tmp_path.parent)


@cofounder_app.command("list")
def cofounder_list() -> None:
    """Show every installed Cofounder Protocol partner."""
    _ensure_load_env()
    from korpha.protocol import list_installed

    installed = list_installed()
    if not installed:
        typer.echo(_dim("No cofounder partners installed."))
        typer.echo(_dim("   Try: korpha cofounder install <url>"))
        return
    for entry in installed:
        m = entry.manifest
        typer.echo(_bold(f"• {m.name}  ({m.display_name})"))
        typer.echo(f"   {m.description.splitlines()[0]}")
        typer.echo(_dim(f"   homepage: {m.homepage}"))
        if m.auth and m.auth.setup_command:
            typer.echo(_dim(f"   setup:    {m.auth.setup_command}"))
        if m.provides.skills:
            typer.echo(_dim(f"   skills:   {', '.join(m.provides.skills)}"))
        typer.echo()


@cofounder_app.command("uninstall")
def cofounder_uninstall(
    name: Annotated[
        str,
        typer.Argument(help="Partner name (snake_case) to uninstall."),
    ],
) -> None:
    """Remove an installed Cofounder Protocol partner."""
    _ensure_load_env()
    from korpha.protocol import uninstall_manifest

    if uninstall_manifest(name):
        typer.echo(_green(f"✓ Uninstalled cofounder partner {name!r}"))
    else:
        typer.echo(_yellow(f"No cofounder partner named {name!r} is installed."))
        raise typer.Exit(code=1)


@app.command(name="config-remove")
def config_remove(
    label: Annotated[
        str, typer.Argument(help="Label of the provider entry to remove.")
    ],
) -> None:
    """Remove a provider entry by its label.

    Find the label via `korpha providers`. The action is irreversible
    but the file is human-readable so you can always re-add via
    `korpha config`.
    """
    _ensure_load_env()
    from korpha.inference.config_writer import remove_provider_entry

    if remove_provider_entry(label):
        typer.echo(_green(f"✓ Removed provider {label!r}"))
    else:
        typer.echo(_yellow(f"No provider with label {label!r} in your config."))
        raise typer.Exit(code=1)


@app.command()
def status() -> None:
    """Show founder, business, agents, recent activity, total spend."""
    _ensure_load_env()
    if not _db_path().exists():
        typer.echo(_yellow("Korpha not initialized. Run `korpha init`."))
        raise typer.Exit(code=1)

    engine = _engine()
    with Session(engine) as session:
        founder, business = _ensure_founder_and_business(session)
        agents = session.exec(
            select(AgentRole).where(AgentRole.business_id == business.id)
        ).all()
        recent = list(
            session.exec(
                select(Activity)
                .where(Activity.business_id == business.id)
                .order_by(Activity.created_at.desc())  # type: ignore[attr-defined]
            ).all()
        )[:10]
        costs = session.exec(
            select(Cost).where(Cost.business_id == business.id)
        ).all()
        total_cost = sum((c.cost_usd for c in costs), Decimal("0"))

    typer.echo(_bold(f"\n{business.name} — {business.status.value}"))
    if business.description:
        typer.echo(_dim(business.description))
    typer.echo(f"\n{_bold('Founder:')} {founder.display_name or founder.email}")

    typer.echo(f"\n{_bold('Org chart')} ({len([a for a in agents if a.is_active])} active):")
    for agent in agents:
        marker = "✓" if agent.is_active else "✗"
        typer.echo(f"  {marker} {agent.role_type.value:8} {agent.title}")

    typer.echo(f"\n{_bold('Recent activity:')}")
    if recent:
        for ev in recent:
            typer.echo(f"  - {ev.event_type}")
    else:
        typer.echo(_dim("  (none yet)"))

    typer.echo(f"\n{_bold('Total spend:')} ${total_cost} ({len(costs)} call(s))")
    typer.echo()


def _ask_async(prompt: str) -> None:
    _ensure_load_env()
    if not _db_path().exists():
        typer.echo(_yellow("Korpha not initialized. Run `korpha init`."))
        raise typer.Exit(code=1)
    engine = _engine()

    async def _run() -> None:
        with Session(engine) as session:
            founder, business = _ensure_founder_and_business(session)
            ceo = _build_ceo(session)
            if ceo is None:
                raise typer.Exit(code=1)

            # Persist Founder's message via the conversation router (creates
            # the web thread on first run, appends Message rows on subsequent
            # asks). This is what MemoryService reads on the next turn.
            hiring = HiringService(session)
            router = ConversationRouter(session=session, hiring=hiring)
            decision = router.route_inbound(
                business_id=business.id,
                founder_id=founder.id,
                platform=ThreadPlatform.WEB,
                content=prompt,
            )

            # Load prior conversation history so CEO has continuity.
            memory = MemoryService(session=session)
            history = memory.load_recent(
                business_id=business.id,
                founder_id=founder.id,
                platform=ThreadPlatform.WEB,
                limit=20,
            )
            # The just-inserted Founder turn is in history; CEO.handle adds it
            # as the user message itself. Strip the trailing turn to avoid
            # duplicating it.
            if history and history[-1].role.value == "user" and history[-1].content == prompt:
                history = history[:-1]

            typer.echo(_dim("→ CEO is thinking via deepseek-v4-pro:cloud..."))
            result = await ceo.handle(
                business=business,
                founder=founder,
                founder_message=prompt,
                history=history,
                thread_id=decision.thread_id,
            )

            # Persist CEO's response to the thread so the next ask sees it.
            router.route_outbound(
                business_id=business.id,
                founder_id=founder.id,
                platform=ThreadPlatform.WEB,
                content=result.content,
                requesting_agent_role_id=decision.delivering_agent_role_id,
            )

            typer.echo()
            if result.skills_used:
                names = ", ".join(s.skill_name for s in result.skills_used)
                typer.echo(_dim(f"  CEO used skill(s): {names}"))
                typer.echo()
            if history:
                typer.echo(_dim(f"  (continuing from {len(history)} prior turn(s))"))
                typer.echo()
            typer.echo(_bold("CEO:"))
            typer.echo(result.content or "(no content — see reasoning below)")
            if result.reasoning:
                typer.echo()
                typer.echo(_dim(f"reasoning ({len(result.reasoning)} chars, hidden by default)"))

    asyncio.run(_run())


@app.command()
def ask(
    prompt: Annotated[str, typer.Argument(help="What to ask the CEO.")],
) -> None:
    """One-shot Q&A: ask the CEO a question, get a response."""
    _ask_async(prompt)


def _propose_async(prompt: str) -> None:
    _ensure_load_env()
    if not _db_path().exists():
        typer.echo(_yellow("Korpha not initialized. Run `korpha init`."))
        raise typer.Exit(code=1)
    engine = _engine()

    async def _run() -> None:
        with Session(engine) as session:
            founder, business = _ensure_founder_and_business(session)
            ceo = _build_ceo(session)
            if ceo is None:
                raise typer.Exit(code=1)
            typer.echo(_dim("→ CEO is drafting a plan..."))
            plan, proposal = await ceo.propose(
                business=business, founder=founder, founder_input=prompt
            )

            typer.echo()
            typer.echo(_bold(_yellow("Plan")))
            typer.echo(f"  summary: {plan.summary}")
            for r in plan.rationale:
                typer.echo(f"  • {r}")
            typer.echo(f"  next:    {plan.next_action}")
            if plan.tasks:
                typer.echo(f"  parallel tasks ({len(plan.tasks)}):")
                for i, t in enumerate(plan.tasks, 1):
                    typer.echo(f"    {i}. {t}")
            if plan.estimated_hours is not None:
                typer.echo(f"  hours:   {plan.estimated_hours}")
            if plan.expected_impact:
                typer.echo(f"  impact:  {plan.expected_impact}")

            if isinstance(proposal, ProposalPending):
                typer.echo(
                    f"\n{_dim('Approval pending. To approve, decide via the gate.')}"
                )
                typer.echo(_dim(f"approval_id={proposal.approval_id}"))
            elif isinstance(proposal, ProposalAccepted):
                typer.echo(_green("\nAuto-executed (envelope mode = AUTO)"))
            elif isinstance(proposal, ProposalDenied):
                typer.echo(_yellow(f"\nDenied: {proposal.reason}"))

    asyncio.run(_run())


@app.command()
def propose(
    prompt: Annotated[str, typer.Argument(help="What to plan.")],
) -> None:
    """Ask the CEO for a structured plan; an Approval is queued."""
    _propose_async(prompt)


@app.command()
def approve(
    approval_id: Annotated[str, typer.Argument(help="UUID of the pending approval.")],
    note: Annotated[
        str | None,
        typer.Option(help="Optional modification note (treated as approve-with-edits)."),
    ] = None,
) -> None:
    """Approve a pending Approval. With --note, treated as approve-with-edits."""
    from uuid import UUID

    from korpha.approvals.gate import Decision

    _ensure_load_env()
    if not _db_path().exists():
        typer.echo(_yellow("Korpha not initialized. Run `korpha init`."))
        raise typer.Exit(code=1)

    engine = _engine()
    with Session(engine) as session:
        founder, _business = _ensure_founder_and_business(session)
        gate = ApprovalGate(session)
        decision_kind = (
            Decision.APPROVE_WITH_EDITS if note else Decision.APPROVE
        )
        result = gate.decide(
            approval_id=UUID(approval_id),
            decision=decision_kind,
            decided_by_founder_id=founder.id,
            modification_note=note,
        )
        # Capture display values before the session closes.
        status_value = result.approval.status.value
        action_class_value = result.approval.action_class.value
        counter = result.envelope.consecutive_approvals
        threshold = result.envelope.threshold
        promotion_offered = result.promotion_offered

    typer.echo(
        _green(
            f"✓ {status_value} | envelope counter: {counter}/{threshold}"
        )
    )
    if promotion_offered:
        typer.echo(
            _yellow(
                "  → Threshold reached. Run `korpha promote-to-auto "
                f"--action-class {action_class_value}` to auto-execute future similar actions."
            )
        )


@app.command()
def reject(
    approval_id: Annotated[str, typer.Argument(help="UUID of the pending approval.")],
) -> None:
    """Reject a pending Approval."""
    from uuid import UUID

    from korpha.approvals.gate import Decision

    _ensure_load_env()
    if not _db_path().exists():
        typer.echo(_yellow("Korpha not initialized. Run `korpha init`."))
        raise typer.Exit(code=1)

    engine = _engine()
    with Session(engine) as session:
        founder, _business = _ensure_founder_and_business(session)
        gate = ApprovalGate(session)
        result = gate.decide(
            approval_id=UUID(approval_id),
            decision=Decision.REJECT,
            decided_by_founder_id=founder.id,
        )
        counter = result.envelope.consecutive_approvals

    typer.echo(_yellow(f"✗ rejected | counter reset to {counter}"))


@app.command()
def pending() -> None:
    """List pending approvals."""
    _ensure_load_env()
    if not _db_path().exists():
        typer.echo(_yellow("Korpha not initialized. Run `korpha init`."))
        raise typer.Exit(code=1)

    from korpha.approvals.model import Approval, ApprovalStatus

    engine = _engine()
    with Session(engine) as session:
        _founder, business = _ensure_founder_and_business(session)
        rows = list(
            session.exec(
                select(Approval)
                .where(Approval.business_id == business.id)
                .where(Approval.status == ApprovalStatus.PENDING)
                .order_by(Approval.created_at.desc())  # type: ignore[attr-defined]
            ).all()
        )

        # Capture for display outside the session.
        rendered = [
            (str(a.id), a.action_class.value, a.platform, a.proposal_summary)
            for a in rows
        ]

    if not rendered:
        typer.echo(_dim("No pending approvals."))
        return
    typer.echo(_bold(f"{len(rendered)} pending approval(s):\n"))
    for approval_id_str, action_class, platform, summary in rendered:
        platform_str = f" [{platform}]" if platform else ""
        typer.echo(f"  {approval_id_str}  {action_class}{platform_str}")
        typer.echo(f"    {summary}")


@app.command()
def blockers(
    show_all: Annotated[
        bool, typer.Option("--all", help="Include resolved + dropped blockers.")
    ] = False,
) -> None:
    """Inspect the Chief of Staff blocker queue (power-user view).

    Founder normally sees only the CEO's consolidated digest. This command
    exposes the raw queue + CoS triage state for debugging or curiosity.
    """
    _ensure_load_env()
    if not _db_path().exists():
        typer.echo(_yellow("Korpha not initialized. Run `korpha init`."))
        raise typer.Exit(code=1)

    from korpha.blockers.model import BlockerStatus

    engine = _engine()
    with Session(engine) as session:
        _founder, business = _ensure_founder_and_business(session)
        queue = BlockerQueue(session=session)
        statuses = (
            tuple(BlockerStatus)
            if show_all
            else (
                BlockerStatus.OPEN,
                BlockerStatus.TRIAGED,
                BlockerStatus.AWAITING_FOUNDER,
                BlockerStatus.RESOLVED_BY_COS,
            )
        )
        rows = queue.list_open(business.id, statuses=statuses)
        # Build the digest so we also see what the next CEO message would surface.
        hiring = HiringService(session)
        gate = ApprovalGate(session)
        cos = ChiefOfStaff(session=session, queue=queue, hiring=hiring, gate=gate)
        digest = cos.digest_for_ceo(business.id)
        rendered = [
            (
                str(b.id),
                b.kind.value,
                b.urgency.value,
                b.status.value,
                b.title,
                b.cos_recommendation,
                b.topic_tag,
            )
            for b in rows
        ]

    typer.echo(_bold(f"\n{len(rendered)} blocker(s):\n"))
    for bid, kind, urgency, status, title, recommendation, tag in rendered:
        tag_str = f" [{tag}]" if tag else ""
        typer.echo(f"  {bid}  {urgency:6} {kind:14} {status:18}{tag_str}")
        typer.echo(f"    {title}")
        if recommendation:
            typer.echo(_dim(f"    → CoS: {recommendation}"))
    typer.echo()
    typer.echo(_bold("CEO digest preview:"))
    typer.echo(digest.render())
    typer.echo()
    typer.echo(
        _dim(
            f"  CoS auto-resolved: {digest.auto_resolved_count} | "
            f"dropped (dupes): {digest.dropped_count}"
        )
    )


@app.command()
def execute(
    approval_id: Annotated[
        str,
        typer.Argument(help="UUID of an approved Approval to dispatch."),
    ],
) -> None:
    """Dispatch an approved Plan to the Workforce. Directors will attempt
    to ship and submit blockers as needed; CoS triages, CEO surfaces the
    consolidated digest the next time you ask anything."""
    from uuid import UUID

    from korpha.approvals.model import Approval, ApprovalStatus

    _ensure_load_env()
    if not _db_path().exists():
        typer.echo(_yellow("Korpha not initialized. Run `korpha init`."))
        raise typer.Exit(code=1)
    engine = _engine()

    async def _run() -> None:
        with Session(engine) as session:
            founder, business = _ensure_founder_and_business(session)
            approval = session.get(Approval, UUID(approval_id))
            if approval is None:
                typer.echo(_yellow(f"Approval {approval_id} not found."))
                raise typer.Exit(code=1)
            if approval.status not in (
                ApprovalStatus.APPROVED,
                ApprovalStatus.AUTO_EXECUTED,
                ApprovalStatus.MODIFIED,
            ):
                typer.echo(
                    _yellow(
                        f"Approval is {approval.status.value!r}, not approved. "
                        f"Run `korpha approve {approval_id}` first."
                    )
                )
                raise typer.Exit(code=1)

            payload = approval.action_payload or {}

            # Branch by action class. EMAIL_OUTREACH approvals carry a
            # {to, subject, body} payload and execute by sending via the
            # configured Resend notifier — no Workforce dispatch needed.
            from korpha.approvals.model import ActionClass as _AC

            if approval.action_class == _AC.EMAIL_OUTREACH:
                await _execute_email_outreach(session, approval, payload, business)
                return
            if approval.action_class == _AC.COMMERCE:
                await _execute_commerce(session, approval, payload, business)
                return

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
            if ceo is None:
                raise typer.Exit(code=1)

            typer.echo(_dim(f"→ dispatching: {plan.next_action[:120]}"))
            summary = await ceo.execute_plan(
                business=business, founder=founder, plan=plan
            )

            typer.echo()
            typer.echo(_bold("Workforce dispatch:") + " " + summary.headline())
            for r in summary.results:
                marker = {"shipped": "✓", "blocked": "⚠", "error": "✗"}.get(
                    r.status, "?"
                )
                colorize = {"shipped": _green, "blocked": _yellow}.get(
                    r.status, lambda x: x
                )
                typer.echo(
                    colorize(f"  {marker} {r.title:6} {r.status:8} {r.summary[:80]}")
                )
                if r.detail and r.status != "shipped":
                    typer.echo(_dim(f"     {r.detail[:200]}"))
            typer.echo()
            typer.echo(_dim(f"  total cost: ${summary.total_cost_usd:.6f}"))

    asyncio.run(_run())


@app.command()
def chat() -> None:
    """Interactive REPL with the CEO. Maintains conversation history within the session."""
    _ensure_load_env()
    if not _db_path().exists():
        typer.echo(_yellow("Korpha not initialized. Run `korpha init`."))
        raise typer.Exit(code=1)
    engine = _engine()

    async def _run() -> None:
        from korpha.inference.types import Message, Role

        with Session(engine) as session:
            founder, business = _ensure_founder_and_business(session)
            ceo = _build_ceo(session)
            if ceo is None:
                raise typer.Exit(code=1)

            founder_label = founder.display_name or founder.email
            business_name = business.name

            typer.echo(_bold(f"\n{business_name} — chat with your cofounder"))
            typer.echo(_dim(f"  founder: {founder_label}  |  type 'exit' to quit, 'plan' for a structured plan\n"))

            history: list[Message] = []
            while True:
                try:
                    user_input = typer.prompt(_bold("you"), default="", show_default=False)
                except (KeyboardInterrupt, EOFError):
                    typer.echo()
                    break
                if user_input.strip().lower() in {"exit", "quit", ":q"}:
                    break
                if not user_input.strip():
                    continue

                if user_input.strip().lower().startswith("plan "):
                    plan_input = user_input[5:].strip()
                    typer.echo(_dim("→ drafting plan..."))
                    plan, proposal = await ceo.propose(
                        business=business, founder=founder, founder_input=plan_input
                    )
                    typer.echo(_yellow(f"plan: {plan.summary}"))
                    typer.echo(f"  next: {plan.next_action}")
                    if isinstance(proposal, ProposalPending):
                        typer.echo(_dim(f"  approval queued: {proposal.approval_id}"))
                    history.append(Message(role=Role.USER, content=plan_input))
                    history.append(Message(role=Role.ASSISTANT, content=plan.summary))
                    continue

                typer.echo(_dim("→ thinking..."))
                response = await ceo.respond(
                    business=business,
                    founder=founder,
                    founder_message=user_input,
                    history=history,
                )
                content = response.content or "(no content; reasoning available)"
                typer.echo(_bold("ceo:") + " " + content + "\n")
                history.append(Message(role=Role.USER, content=user_input))
                history.append(Message(role=Role.ASSISTANT, content=content))

        typer.echo(_dim("session ended."))

    asyncio.run(_run())


skill_app = typer.Typer(help="Run individual skills (niche pickers, drafters, etc.).")
app.add_typer(skill_app, name="skill")


@skill_app.command("list")
def skill_list() -> None:
    """List available skills."""
    from korpha.skills import default_registry

    typer.echo()
    for spec in sorted(default_registry.list_specs(), key=lambda s: s.name):
        typer.echo(_bold(spec.name))
        typer.echo(_dim(f"  {spec.description}"))
        if spec.parameters:
            for pname, pdesc in spec.parameters.items():
                typer.echo(f"    --arg {pname}=...   {_dim(pdesc)}")
        typer.echo()


@skill_app.command("run")
def skill_run(
    name: Annotated[str, typer.Argument(help="Skill name (e.g. niche.find_micro_niches)")],
    arg: Annotated[
        list[str] | None,
        typer.Option("--arg", help="key=value (repeatable)"),
    ] = None,
) -> None:
    """Run a skill against the configured business + LLM."""
    from json import dumps

    from korpha.skills import SkillContext, default_registry

    _ensure_load_env()
    if not _db_path().exists():
        typer.echo(_yellow("Korpha not initialized. Run `korpha init`."))
        raise typer.Exit(code=1)
    if not os.getenv("OLLAMA_CLOUD_API_KEY"):
        typer.echo(_yellow("OLLAMA_CLOUD_API_KEY not set."))
        raise typer.Exit(code=1)

    args: dict[str, str] = {}
    for raw in arg or []:
        if "=" not in raw:
            typer.echo(_yellow(f"--arg expects key=value, got {raw!r}"))
            raise typer.Exit(code=1)
        key, value = raw.split("=", 1)
        args[key.strip()] = value

    engine = _engine()

    async def _run() -> None:
        with Session(engine) as session:
            founder, business = _ensure_founder_and_business(session)
            account = _ollama_cloud_account()
            assert account is not None
            pool = InferencePool(
                providers=[ollama_cloud_provider()], accounts=[account]
            )
            tracker = CostTracker(pool=pool)
            ctx = SkillContext(
                business=business,
                founder=founder,
                session=session,
                cost_tracker=tracker,
            )
            typer.echo(_dim(f"→ running {name}..."))
            result = await default_registry.run(name, ctx=ctx, args=args)

            typer.echo()
            typer.echo(_bold(result.summary))
            typer.echo()
            typer.echo(dumps(result.payload, indent=2))
            typer.echo()
            typer.echo(_dim(f"  cost: ${result.cost_usd:.6f}"))
            if result.reasoning:
                typer.echo(_dim(f"  reasoning: {len(result.reasoning)} chars (hidden)"))

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Skills hub — install / search / list community skills from registries
# ---------------------------------------------------------------------------


@skill_app.command("hub-search")
def skill_hub_search(
    query: Annotated[
        str, typer.Argument(help="Search term (matches skill name + description)")
    ] = "",
    limit: Annotated[int, typer.Option(help="Max results")] = 25,
) -> None:
    """Search the Korpha skills hub for installable skills.

    Hits skills.korpha.com (or KORPHA_SKILLS_HUB_URL) and prints
    matching skill names with descriptions + trust level + verdict.
    """
    _ensure_load_env()
    from korpha.skills_hub.client import KorphaHubSource

    src = KorphaHubSource()
    hits = src.search(query, limit=limit)
    if not hits:
        typer.echo(_dim("No matches. Try a broader query — or `korpha skill list` for built-ins."))
        return
    for h in hits:
        verified_mark = " ✓verified" if h.extra.get("verified") else ""
        verdict = h.extra.get("scan_verdict") or "?"
        typer.echo(_bold(h.name) + _dim(f"  ({h.trust_level}, scan={verdict}{verified_mark})"))
        typer.echo(_dim(f"  {h.description}"))
        if h.tags:
            typer.echo(_dim(f"  tags: {', '.join(h.tags)}"))
        typer.echo()


@skill_app.command("hub-install")
def skill_hub_install(
    name: Annotated[
        str, typer.Argument(help="Skill name from the hub")
    ],
    force: Annotated[
        bool, typer.Option(help="Override scanner block decision")
    ] = False,
) -> None:
    """Install a skill from the Korpha hub.

    Flow: download → security scan (regex + invisible-unicode + threat
    pattern) → install policy decision → copy into ~/.korpha/skills/
    if allowed. Lock file records provenance for ``hub-list``.
    """
    _ensure_load_env()
    from korpha.skills_hub.client import KorphaHubSource, install_skill

    src = KorphaHubSource()
    typer.echo(_dim(f"→ fetching {name} from {src.base_url}..."))
    try:
        bundle = src.fetch(name)
    except Exception as exc:
        typer.echo(_yellow(f"download failed: {exc}"))
        raise typer.Exit(code=1) from exc

    typer.echo(_dim("→ scanning..."))
    result = install_skill(bundle, force=force)
    typer.echo()
    typer.echo(result.scan_report)
    typer.echo()
    if result.installed:
        typer.echo(_green(f"✓ Installed at {result.install_path}"))
    elif "NEEDS CONFIRMATION" in result.reason:
        typer.echo(_yellow(
            f"⚠ {result.reason}. Re-run with --force to install anyway."
        ))
        raise typer.Exit(code=2)
    else:
        typer.echo(_yellow(f"✗ {result.reason}"))
        typer.echo(_dim("  Pass --force to install anyway (your risk)."))
        raise typer.Exit(code=1)


@skill_app.command("install")
def skill_install_cmd(
    target: Annotated[
        str,
        typer.Argument(
            help=(
                "Hub skill name, GitHub URL "
                "(github.com/owner/repo or owner/repo/sub/path), "
                "local directory, or path to a .tar.gz bundle."
            ),
        ),
    ],
    force: Annotated[
        bool, typer.Option(help="Override scanner block decision")
    ] = False,
) -> None:
    """Install a skill from any source — hub / GitHub / local.

    Auto-dispatches based on the target shape:

      * ``./my-skill.tar.gz``   → LocalSource (tarball)
      * ``./my-skill/``         → LocalSource (directory)
      * ``github.com/foo/bar``  → GitHubSource
      * ``some-name``           → KorphaHubSource

    The fetch + scan + install flow is unchanged across all
    sources — same security scanner, same trust-level policy,
    same lock file recording provenance.
    """
    _ensure_load_env()
    from pathlib import Path as _P

    from korpha.skills_hub.client import (
        KorphaHubSource, GitHubSource, install_skill,
    )

    target_str = target.strip()
    src: object
    fetch_id = target_str

    # Path-shaped targets win — even when the string starts with
    # a hub-style name, "./name" wouldn't accidentally hit the hub.
    candidate = _P(target_str).expanduser()
    if candidate.exists():
        from korpha.skills_hub.local import LocalSource
        src = LocalSource()
        fetch_id = str(candidate.resolve())
        kind = "local"
    elif (
        target_str.startswith("github.com/")
        or target_str.startswith("https://github.com/")
        or target_str.startswith("http://github.com/")
    ):
        # Strip scheme and split owner/repo[/path...] for the
        # adapter. The adapter takes (repo, base_path, branch).
        path = target_str.split("github.com/", 1)[1].rstrip("/")
        parts = path.split("/")
        if len(parts) < 2:
            typer.echo(_red(
                f"github URL must include owner/repo: "
                f"{target_str!r}",
            ))
            raise typer.Exit(code=1)
        repo = f"{parts[0]}/{parts[1]}"
        base_path = "/".join(parts[2:])
        src = GitHubSource(repo=repo, base_path=base_path)
        # Identifier is the skill subdir name (last segment) when
        # base_path points at a specific skill, else the repo
        # root browse target.
        fetch_id = parts[-1] if len(parts) > 2 else parts[1]
        kind = "github"
    else:
        src = KorphaHubSource()
        kind = "hub"

    typer.echo(_dim(f"→ fetching from {kind}: {target_str}…"))
    try:
        bundle = src.fetch(fetch_id)
    except Exception as exc:
        typer.echo(_red(f"fetch failed: {exc}"))
        raise typer.Exit(code=1) from exc

    typer.echo(_dim("→ scanning..."))
    result = install_skill(bundle, force=force)
    typer.echo()
    typer.echo(result.scan_report)
    typer.echo()
    if result.installed:
        typer.echo(_green(f"✓ Installed at {result.install_path}"))
    elif "NEEDS CONFIRMATION" in result.reason:
        typer.echo(_yellow(
            f"⚠ {result.reason}. Re-run with --force to install anyway."
        ))
        raise typer.Exit(code=2)
    else:
        typer.echo(_yellow(f"✗ {result.reason}"))
        typer.echo(_dim(
            "  Pass --force to install anyway (your risk)."
        ))
        raise typer.Exit(code=1)


@skill_app.command("publish")
def skill_publish_cmd(
    source: Annotated[
        Path,
        typer.Argument(
            help=(
                "Path to the skill directory to pack — usually "
                "~/.korpha/skills/<name>/ or a working dir."
            ),
        ),
    ],
    output: Annotated[
        Path | None,
        typer.Option(
            "--output", "-o",
            help="Where to write the tarball. Default: "
                 "./<skill-name>.tar.gz next to your cwd.",
        ),
    ] = None,
) -> None:
    """Pack a local skill into a sharable .tar.gz bundle.

    Mike runs this on a skill he wants to share — the agent's
    ``meta.author_python_skill`` output, a hand-written one,
    anything in ``~/.korpha/skills/``. The resulting tarball
    drops in cleanly when someone runs::

        korpha skill install ./<bundle>.tar.gz

    Excludes the usual junk (.git / __pycache__ / .venv / etc.)
    so a published skill stays small even when authored inside
    a development workspace.
    """
    _ensure_load_env()
    from korpha.skills_hub.local import pack_skill

    try:
        result = pack_skill(source, output=output)
    except (FileNotFoundError, ValueError) as exc:
        typer.echo(_red(str(exc)))
        raise typer.Exit(code=1) from exc

    typer.echo(_green(
        f"✓ Packed {result.skill_name!r} → "
        f"{result.output_path}"
    ))
    typer.echo(_dim(
        f"  {result.file_count} file(s), "
        f"{_human_bytes(result.size_bytes)}"
    ))
    typer.echo(_dim(
        "  Share the tarball; recipients install via "
        "`korpha skill install <path>`."
    ))


@skill_app.command("hub-list")
def skill_hub_list() -> None:
    """List skills installed from the hub (provenance + scan verdicts).

    Reads ~/.korpha/skills/.hub/lock.json — records every install +
    where it came from + scan verdict at install time.
    """
    _ensure_load_env()
    from korpha.skills_hub.client import list_installed

    entries = list_installed()
    if not entries:
        typer.echo(_dim("No hub-installed skills. Try `korpha skill hub-search`."))
        return
    for e in entries:
        verdict = e.get("scan_verdict", "?")
        typer.echo(_bold(e["name"]) + _dim(f"  (verdict={verdict})"))
        typer.echo(_dim(f"  source: {e['source']}"))
        typer.echo(_dim(f"  identifier: {e['identifier']}"))
        typer.echo(_dim(f"  installed: {e.get('installed_at', '?')}"))
        typer.echo()


@skill_app.command("hub-uninstall")
def skill_hub_uninstall(
    name: Annotated[
        str, typer.Argument(help="Installed skill name to remove")
    ],
) -> None:
    """Remove a hub-installed skill."""
    _ensure_load_env()
    from korpha.skills_hub.client import uninstall_skill

    if uninstall_skill(name):
        typer.echo(_green(f"✓ Uninstalled {name!r}"))
    else:
        typer.echo(_yellow(f"No hub-installed skill named {name!r}"))
        raise typer.Exit(code=1)


@skill_app.command("hub-scan")
def skill_hub_scan(
    path: Annotated[
        Path, typer.Argument(help="Local skill directory to scan")
    ],
    source: Annotated[
        str, typer.Option(help="Source identifier for trust resolution")
    ] = "community",
) -> None:
    """Run the security scanner on a LOCAL skill (without installing).

    Useful before publishing — author runs ``korpha skill hub-scan
    ./my_skill --source community`` to see exactly what the registry
    will flag at submission time.
    """
    from korpha.skills_hub.guard import format_scan_report, scan_skill

    if not path.exists():
        typer.echo(_yellow(f"path does not exist: {path}"))
        raise typer.Exit(code=1)
    result = scan_skill(path, source=source)
    typer.echo(format_scan_report(result))


@app.command()
def onboard(
    answer: Annotated[
        str | None,
        typer.Option(
            "--answer",
            help="Pre-supply the freeform answer; otherwise we prompt.",
        ),
    ] = None,
) -> None:
    """Day-0 intake: capture the Founder's goal and structure it.

    Equivalent to running ``founder.intake_brief`` but with the prompt the
    BRIEF.md 5-minute demo opens with — *"What do you actually want?"* —
    and a friendly summary readout instead of raw JSON.
    """
    from korpha.skills import SkillContext, default_registry

    _ensure_load_env()
    if not _db_path().exists():
        typer.echo(_yellow("Korpha not initialized. Run `korpha init`."))
        raise typer.Exit(code=1)
    if not os.getenv("OLLAMA_CLOUD_API_KEY"):
        typer.echo(
            _yellow(
                "OLLAMA_CLOUD_API_KEY not set — onboard needs an LLM to "
                "structure the brief."
            )
        )
        raise typer.Exit(code=1)

    if answer is None:
        typer.echo()
        typer.echo(_bold("What do you actually want?"))
        typer.echo(
            _dim(
                "Be concrete. Goal, timeline, hours/week, savings, what "
                "you're good at, what you've already tried."
            )
        )
        typer.echo()
        answer = typer.prompt("→")

    engine = _engine()

    async def _run() -> None:
        with Session(engine) as session:
            founder, business = _ensure_founder_and_business(session)
            account = _ollama_cloud_account()
            assert account is not None
            pool = InferencePool(
                providers=[ollama_cloud_provider()], accounts=[account]
            )
            tracker = CostTracker(pool=pool)
            ctx = SkillContext(
                business=business,
                founder=founder,
                session=session,
                cost_tracker=tracker,
            )
            typer.echo()
            typer.echo(_dim("→ structuring your brief..."))
            result = await default_registry.run(
                "founder.intake_brief", ctx=ctx, args={"answer": answer}
            )

            brief = result.payload
            typer.echo()
            typer.echo(_bold("Got it."))
            typer.echo()
            typer.echo(brief.get("summary") or "(no summary)")
            typer.echo()
            typer.echo(_dim("Captured:"))
            typer.echo(_dim(f"  goal: {brief.get('goal') or '(none)'}"))
            typer.echo(_dim(f"  timeline: {brief.get('timeline_months')} months"))
            typer.echo(_dim(f"  time/week: {brief.get('time_per_week_hours')} hours"))
            typer.echo(_dim(f"  savings: ${brief.get('savings_usd')}"))
            typer.echo(_dim(f"  skills: {brief.get('skills') or '(none)'}"))
            niches = brief.get("niches_considered") or []
            if niches:
                typer.echo(_dim(f"  niches you mentioned: {', '.join(niches)}"))
            constraints = brief.get("constraints") or []
            if constraints:
                typer.echo(_dim(f"  constraints: {', '.join(constraints)}"))
            typer.echo()
            typer.echo(
                _dim(
                    "Next: `korpha skill run niche.find_micro_niches` — "
                    "your brief is now the default for skill arguments."
                )
            )

    asyncio.run(_run())


@app.command()
def providers() -> None:
    """Show configured inference providers + which tiers each one serves.

    Reads from ``~/.korpha/providers.yaml`` first (or
    ``KORPHA_PROVIDERS_FILE``), then falls back to the env-var pair
    ``OPENCODE_API_KEY`` + ``OLLAMA_CLOUD_API_KEY``. Useful when a key is set
    but it's not clear which provider is actually being routed to."""
    _ensure_load_env()
    from korpha.inference.config import config_path, load_from_yaml

    try:
        loaded = load_from_yaml()
    except ProviderConfigError as exc:
        typer.echo(_yellow(f"providers.yaml problem: {exc}"))
        raise typer.Exit(code=1) from exc

    if loaded is not None and loaded.accounts:
        typer.echo(_bold(f"Source: {loaded.source}"))
        for acc in loaded.accounts:
            tiers = ", ".join(f"{t.value}={m}" for t, m in acc.tier_models.items())
            typer.echo(f"  • {acc.label}  [{acc.provider_name}]  {tiers}")
        return

    if loaded is not None:
        typer.echo(
            _yellow(
                f"providers.yaml at {loaded.source} loaded, but every entry "
                "is missing its API key (api_key_env unset?)."
            )
        )

    typer.echo(_dim(f"(no providers.yaml at {config_path()} — using env-var fallback)"))
    pool_setup = _build_provider_pool()
    if pool_setup is None:
        typer.echo(
            _yellow(
                "no provider configured. Set OPENCODE_API_KEY (preferred) or "
                "OLLAMA_CLOUD_API_KEY, or write a providers.yaml."
            )
        )
        return
    _, accounts = pool_setup
    for acc in accounts:
        tiers = ", ".join(f"{t.value}={m}" for t, m in acc.tier_models.items())
        typer.echo(f"  • {acc.label}  [{acc.provider_name}]  {tiers}")


@app.command()
def migrate(
    revision: Annotated[
        str, typer.Option(help="Target revision (default: head)")
    ] = "head",
) -> None:
    """Apply Alembic migrations to bring the DB schema to ``revision``.

    Reads the DB URL from ``KORPHA_DB_URL`` env var, falling back to
    the local SQLite file at ``$KORPHA_DATA_DIR/korpha.db``."""
    _ensure_load_env()
    import subprocess

    db_path = _db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_url = os.getenv("KORPHA_DB_URL") or f"sqlite:///{db_path}"
    env = os.environ.copy()
    env["KORPHA_DB_URL"] = db_url

    repo_root = Path(__file__).resolve().parent.parent
    alembic_ini = repo_root / "alembic.ini"
    if not alembic_ini.exists():
        typer.echo(_yellow(f"alembic.ini not found at {alembic_ini}"))
        raise typer.Exit(code=1)

    typer.echo(_dim(f"→ alembic upgrade {revision} (db: {db_url})"))
    proc = subprocess.run(
        ["alembic", "-c", str(alembic_ini), "upgrade", revision],
        env=env,
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        typer.echo(_yellow(proc.stderr or proc.stdout))
        raise typer.Exit(code=proc.returncode)
    typer.echo(_green("✓ migrations applied"))


@app.command(name="channel-run")
def channel_run(
    platform: Annotated[
        str,
        typer.Argument(help="Channel to run (telegram | discord | email | ...)"),
    ] = "telegram",
    allow_chat_id: Annotated[
        list[int] | None,
        typer.Option(
            "--allow-chat-id",
            help="Whitelist a Telegram chat_id. Repeat for multiple. "
            "Empty = no allowlist (DANGEROUS in production).",
        ),
    ] = None,
) -> None:
    """Run a channel adapter against the configured Founder + business.

    Telegram: needs ``TELEGRAM_BOT_TOKEN`` in the environment (or .env).
    The bot will long-poll for messages, route each to your CEO, and
    push the response back to the same chat. Ctrl-C to stop.
    """
    _ensure_load_env()
    from korpha.channels import TelegramAdapter
    from korpha.channels.router import ChannelRouter, platform_from_name

    plat = platform_from_name(platform)
    engine = _engine()

    with Session(engine) as bootstrap:
        founder, business = _ensure_founder_and_business(bootstrap)
        founder_id = founder.id
        business_id = business.id

    if plat.value == "telegram":
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        if not token:
            typer.echo(
                _yellow(
                    "TELEGRAM_BOT_TOKEN not set. Get one from @BotFather, then "
                    "add it to .env or your shell environment."
                )
            )
            raise typer.Exit(code=1)
        adapter = TelegramAdapter(
            token=token,
            allowed_chat_ids=set(allow_chat_id or []),
        )
    else:
        typer.echo(_yellow(f"channel {platform!r} not yet implemented"))
        raise typer.Exit(code=2)

    def factory(session: Session) -> CEO:
        ceo = _build_ceo(session)
        if ceo is None:
            raise RuntimeError(
                "No inference provider configured — see `korpha providers`"
            )
        return ceo

    router = ChannelRouter(
        engine=engine,
        adapter=adapter,
        ceo_factory=factory,
        business_id=business_id,
        founder_id=founder_id,
    )
    # Surface router errors to stdout so operators see them. Without this,
    # `logger.exception` calls in the router go nowhere by default.
    import logging as _logging

    _logging.basicConfig(
        level=_logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    typer.echo(_green(f"✓ {platform} channel running. Ctrl-C to stop."))
    if allow_chat_id:
        typer.echo(_dim(f"  allowlist: {', '.join(str(i) for i in allow_chat_id)}"))
    else:
        typer.echo(_yellow("  no allowlist set — anyone who finds the bot can DM"))

    async def _run() -> None:
        try:
            await router.run()
        finally:
            await adapter.close()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        typer.echo(_dim("\nstopped"))


@app.command(name="business-list")
def business_list() -> None:
    """List businesses owned by this Founder, marking the active one."""
    _ensure_load_env()
    from korpha.business.multi import list_businesses

    engine = _engine()
    with Session(engine) as session:
        founder = session.exec(select(Founder)).first()
        if founder is None:
            typer.echo(_yellow("No founder. Run `korpha init` first."))
            raise typer.Exit(code=1)
        rows = list_businesses(session, founder.id)
        if not rows:
            typer.echo(_dim("(no businesses — `korpha business-create <name>`)"))
            return
        for biz in rows:
            marker = "* " if biz.id == founder.active_business_id else "  "
            archived = " [archived]" if biz.archived_at else ""
            typer.echo(
                f"{marker}{biz.name}  ({biz.status.value}){archived}"
            )
            typer.echo(_dim(f"    id: {biz.id}"))


@app.command(name="business-create")
def business_create(
    name: Annotated[str, typer.Argument(help="Business name")],
    description: Annotated[
        str | None, typer.Option("--description", help="Short description")
    ] = None,
    activate: Annotated[
        bool,
        typer.Option(
            "--activate/--no-activate",
            help="Make this the active business after creating",
        ),
    ] = True,
) -> None:
    """Create a new business owned by the current Founder."""
    _ensure_load_env()
    from korpha.business.multi import create_business

    engine = _engine()
    with Session(engine) as session:
        founder = session.exec(select(Founder)).first()
        if founder is None:
            typer.echo(_yellow("No founder. Run `korpha init` first."))
            raise typer.Exit(code=1)
        biz = create_business(
            session,
            founder,
            name=name,
            description=description,
            set_active=activate,
        )
        # Ensure CEO + a web thread exist for the new business so chat works
        # immediately without needing another setup step.
        HiringService(session).ensure_ceo(biz.id)
        verb = "Created and activated" if activate else "Created"
        typer.echo(_green(f"✓ {verb} business: {biz.name} ({biz.id})"))


@app.command(name="business-export")
def business_export(
    output: Annotated[
        str, typer.Option("--to", help="Output JSON path")
    ],
    business_id: Annotated[
        str | None,
        typer.Option(
            "--id",
            help="Business UUID (defaults to active)",
        ),
    ] = None,
    no_messages: Annotated[
        bool,
        typer.Option(
            "--no-messages",
            help="Exclude threads + messages (template share, not full backup)",
        ),
    ] = False,
) -> None:
    """Export a business to a portable JSON file (secrets scrubbed)."""
    _ensure_load_env()
    from uuid import UUID as _UUID

    from korpha.business.portability import PortabilityError, export_to_file

    engine = _engine()
    with Session(engine) as session:
        founder, active = _ensure_founder_and_business(session)
        target_id = _UUID(business_id) if business_id else active.id
        try:
            result = export_to_file(
                session,
                business_id=target_id,
                path=output,
                include_messages=not no_messages,
            )
        except PortabilityError as exc:
            typer.echo(_yellow(str(exc)))
            raise typer.Exit(code=1) from exc
        _ = founder
    typer.echo(_green(f"✓ exported to {output}"))
    for table, count in result.table_counts.items():
        if isinstance(count, int) and count > 0:
            typer.echo(_dim(f"  {table}: {count}"))


@app.command(name="business-import")
def business_import(
    source: Annotated[str, typer.Argument(help="JSON file to import")],
    name: Annotated[
        str | None,
        typer.Option("--name", help="Override the imported business name"),
    ] = None,
    activate: Annotated[
        bool,
        typer.Option(
            "--activate/--no-activate",
            help="Make the imported business active after import",
        ),
    ] = False,
) -> None:
    """Import a previously-exported business JSON. UUIDs are regenerated
    so the same payload can be imported repeatedly."""
    _ensure_load_env()
    from korpha.business.portability import PortabilityError, import_from_file

    engine = _engine()
    with Session(engine) as session:
        founder = session.exec(select(Founder)).first()
        if founder is None:
            typer.echo(_yellow("No founder. Run `korpha init` first."))
            raise typer.Exit(code=1)
        try:
            result = import_from_file(
                session, path=source, founder=founder, new_name=name
            )
        except PortabilityError as exc:
            typer.echo(_yellow(str(exc)))
            raise typer.Exit(code=1) from exc
        if activate:
            founder.active_business_id = result.business.id
            session.add(founder)
            session.commit()
    typer.echo(_green(f"✓ imported business: {result.business.name} ({result.business.id})"))
    for table, count in result.table_counts.items():
        if count > 0:
            typer.echo(_dim(f"  {table}: {count}"))


@app.command(name="business-switch")
def business_switch(
    business_id: Annotated[str, typer.Argument(help="Business UUID to activate")],
) -> None:
    """Set the active business by UUID. Subsequent CLI / API calls target it."""
    _ensure_load_env()
    from uuid import UUID as _UUID

    from korpha.business.multi import BusinessResolutionError, switch_active

    try:
        biz_id = _UUID(business_id)
    except ValueError as exc:
        typer.echo(_yellow(f"not a valid UUID: {business_id!r}"))
        raise typer.Exit(code=1) from exc

    engine = _engine()
    with Session(engine) as session:
        founder = session.exec(select(Founder)).first()
        if founder is None:
            typer.echo(_yellow("No founder. Run `korpha init` first."))
            raise typer.Exit(code=1)
        try:
            biz = switch_active(session, founder, biz_id)
        except BusinessResolutionError as exc:
            typer.echo(_yellow(str(exc)))
            raise typer.Exit(code=1) from exc
        typer.echo(_green(f"✓ active business: {biz.name}"))


@app.command(name="plugins-list")
def plugins_list() -> None:
    """List installed plugins from ``~/.korpha/plugins/`` (or
    ``KORPHA_PLUGINS_DIR``). Shows declared permissions per plugin
    so you know what each one is allowed to do."""
    _ensure_load_env()
    from korpha.plugins import PluginLoadError, discover_plugins
    from korpha.plugins.loader import plugins_dir

    try:
        manifests = discover_plugins()
    except PluginLoadError as exc:
        typer.echo(_yellow(str(exc)))
        raise typer.Exit(code=1) from exc

    if not manifests:
        typer.echo(
            _dim(
                f"(no plugins under {plugins_dir()} — set KORPHA_PLUGINS_DIR "
                "or drop a plugin directory there)"
            )
        )
        return

    for m in manifests:
        perms = ", ".join(sorted(m.permissions)) or "(none)"
        typer.echo(_bold(f"• {m.name}  v{m.version}"))
        typer.echo(f"    by {m.author}")
        typer.echo(f"    permissions: {perms}")
        if m.description:
            first = m.description.strip().splitlines()[0]
            typer.echo(_dim(f"    {first[:120]}"))


@app.command(name="email-test")
def email_test(
    to: Annotated[str, typer.Option("--to", help="Recipient address")],
    subject: Annotated[
        str, typer.Option(help="Subject line")
    ] = "Korpha test email",
) -> None:
    """Send a test email via Resend to verify your config.

    Requires RESEND_API_KEY and RESEND_FROM in .env. Run this once after
    setting up the Resend account + verifying your sending domain.
    """
    _ensure_load_env()
    from korpha.notifications import (
        Notification,
        NotifierError,
        ResendEmailNotifier,
    )

    notifier = ResendEmailNotifier()

    async def _run() -> None:
        try:
            await notifier.send(
                Notification(
                    to=to,
                    subject=subject,
                    text_body=(
                        "This is a test email from your Korpha cofounder.\n\n"
                        "If you got this, Resend is wired up. Add a routine:\n\n"
                        "  korpha routine-add 'morning digest' "
                        "--kind email.daily_digest --every-seconds 86400\n"
                    ),
                    html_body=(
                        "<p>This is a test email from your "
                        "<b>Korpha</b> cofounder.</p>"
                        "<p>If you got this, Resend is wired up.</p>"
                    ),
                )
            )
        finally:
            await notifier.close()

    try:
        asyncio.run(_run())
    except NotifierError as exc:
        typer.echo(_yellow(str(exc)))
        raise typer.Exit(code=1) from exc
    typer.echo(_green(f"✓ test email sent to {to}"))


@app.command(name="email-digest")
def email_digest(
    to: Annotated[
        str | None,
        typer.Option("--to", help="Override recipient (defaults to founder email)"),
    ] = None,
) -> None:
    """Build the morning digest from current state and email it now.

    Useful for previewing the digest before scheduling it as a routine,
    or for one-shot delivery from cron.
    """
    _ensure_load_env()
    from korpha.notifications import (
        Notification,
        NotifierError,
        ResendEmailNotifier,
    )
    from korpha.notifications.digest import build_snapshot, render_digest

    engine = _engine()
    with Session(engine) as session:
        founder, business = _ensure_founder_and_business(session)
        snap = build_snapshot(session, business)
        notification = render_digest(snap, founder_name=founder.display_name)
        target = to or founder.email
        notification = Notification(
            to=target,
            subject=notification.subject,
            text_body=notification.text_body,
            html_body=notification.html_body,
        )

    notifier = ResendEmailNotifier()

    async def _send() -> None:
        try:
            await notifier.send(notification)
        finally:
            await notifier.close()

    try:
        asyncio.run(_send())
    except NotifierError as exc:
        typer.echo(_yellow(str(exc)))
        raise typer.Exit(code=1) from exc
    typer.echo(_green(f"✓ digest sent to {target}"))


@app.command(name="browser-do")
def browser_do(
    instruction: Annotated[
        str, typer.Argument(help="Natural-language goal for the agent")
    ],
    url: Annotated[
        str | None,
        typer.Option("--url", help="Optional starting URL"),
    ] = None,
    headless: Annotated[
        bool,
        typer.Option(
            "--headless/--headed",
            help="Run hidden (default) or visible — use --headed to watch",
        ),
    ] = True,
    max_steps: Annotated[
        int, typer.Option(help="Cap on action-loop iterations")
    ] = 10,
) -> None:
    """Run an LLM-driven browser action loop. Each step the agent looks at
    the page, picks ONE action (click / type / navigate / scroll / done),
    executes it, and repeats until the goal is met.

    Costs LLM tokens per step — use --headed when you want to watch what
    it's doing, --headless for unattended runs.

    Requires playwright + at least one inference provider configured.
    Install browsers once: ``playwright install chromium``
    """
    _ensure_load_env()
    pool_setup = _build_provider_pool()
    if pool_setup is None:
        typer.echo(
            _yellow(
                "no inference provider configured. Set OPENCODE_API_KEY or "
                "OLLAMA_CLOUD_API_KEY in .env."
            )
        )
        raise typer.Exit(code=1)
    providers_list, accounts_list = pool_setup

    from korpha.browser import (
        BrowserService,
        BrowserTask,
        PlaywrightActionProvider,
    )

    pool = InferencePool(providers=providers_list, accounts=accounts_list)  # type: ignore[arg-type]

    engine = _engine()
    with Session(engine) as session:
        _, business = _ensure_founder_and_business(session)
        biz_id = business.id

    provider = PlaywrightActionProvider(
        pool=pool, business_id=biz_id, max_steps=max_steps
    )
    service = BrowserService(providers=[provider])

    async def _run() -> None:
        task = BrowserTask(
            instruction=instruction,
            start_url=url,
            headless=headless,
            timeout_seconds=60.0,
            extract_text=False,  # we'll rely on the action loop snapshot
        )
        try:
            result = await service.run(task)
        finally:
            await service.close()
        steps = result.raw.get("steps") or []
        cost = result.raw.get("cost_usd") or 0.0
        typer.echo(
            _dim(f"  steps={len(steps)}  cost=${cost:.4f}  url={result.final_url}")
        )
        for i, s in enumerate(steps, 1):
            typer.echo(_dim(f"    [{i}] {s}"))
        if not result.success:
            typer.echo(_yellow(f"failed: {result.error}"))
            raise typer.Exit(code=1)
        typer.echo(_green(f"✓ done — {result.extracted_text or '(no result text)'}"))

    asyncio.run(_run())


@app.command(name="browser-test")
def browser_test(
    url: Annotated[str, typer.Argument(help="URL to fetch (https://…)")],
    headless: Annotated[
        bool,
        typer.Option("--headless/--headed", help="Run hidden (default) or visible"),
    ] = True,
    chars: Annotated[
        int, typer.Option(help="Truncate extracted text to N chars")
    ] = 800,
) -> None:
    """Fetch a URL via the local Playwright browser and print the rendered text.

    Quick sanity check that the browser stack is wired up. No LLM call is
    made — just navigate, extract, print. Install Chromium first:

        playwright install chromium
    """
    _ensure_load_env()
    from korpha.browser import (
        BrowserService,
        BrowserTask,
        PlaywrightFetchProvider,
    )

    service = BrowserService(providers=[PlaywrightFetchProvider()])

    async def _run() -> None:
        task = BrowserTask(
            instruction="cli sanity check", start_url=url, headless=headless
        )
        try:
            result = await service.run(task)
        finally:
            await service.close()
        if not result.success:
            typer.echo(_yellow(f"failed: {result.error}"))
            raise typer.Exit(code=1)
        typer.echo(_green(f"✓ {result.title or '(untitled)'}"))
        typer.echo(_dim(f"  {result.final_url}"))
        text = (result.extracted_text or "").strip()
        if len(text) > chars:
            text = text[:chars] + "…"
        typer.echo("\n" + text)

    asyncio.run(_run())


@app.command(name="mcp-list")
def mcp_list() -> None:
    """Show MCP servers configured in ``~/.korpha/mcp.yaml`` + their tools.

    Connects to each enabled server, calls tools/list, prints the result.
    Disabled servers are noted but not connected to."""
    _ensure_load_env()
    from korpha.mcp import (
        McpClientError,
        McpConfigError,
        StdioMcpClient,
        load_mcp_config,
    )

    try:
        configs = load_mcp_config()
    except McpConfigError as exc:
        typer.echo(_yellow(f"mcp.yaml problem: {exc}"))
        raise typer.Exit(code=1) from exc

    if not configs:
        typer.echo(
            _dim(
                "(no mcp.yaml found — write one to ~/.korpha/mcp.yaml "
                "to declare MCP servers)"
            )
        )
        return

    async def _inspect() -> None:
        for cfg in configs:
            if not cfg.enabled:
                typer.echo(_dim(f"  • {cfg.name}  [disabled]"))
                continue
            typer.echo(_bold(f"• {cfg.name}  ({' '.join(cfg.command)})"))
            client = StdioMcpClient(
                command=cfg.command,
                env=cfg.env,
                cwd=cfg.cwd,
                request_timeout_seconds=cfg.request_timeout_seconds,
            )
            try:
                async with client:
                    tools = await client.list_tools()
            except McpClientError as exc:
                typer.echo(_yellow(f"    error: {exc}"))
                continue
            if not tools:
                typer.echo(_dim("    (no tools)"))
                continue
            for t in tools:
                typer.echo(f"    - {t.name}: {t.description.strip().splitlines()[0][:80]}")

    asyncio.run(_inspect())


@app.command()
def tick(
    watch: Annotated[bool, typer.Option(
        "--watch", "-w",
        help="Loop forever, ticking every --interval seconds. Ctrl-C to exit.",
    )] = False,
    interval: Annotated[int, typer.Option(
        "--interval",
        help="Seconds between ticks in --watch mode (default 60).",
    )] = 60,
) -> None:
    """Run one heartbeat cycle: evaluate routines, fire due wakeups,
    run due agentless cron scripts.

    Designed to be called from cron (e.g. ``* * * * * korpha tick``)
    or a long-running sidecar that loops. With ``--watch`` the command
    runs forever, ticking every ``--interval`` seconds — single-process
    deploys can use this instead of a system cron.
    """
    _ensure_load_env()
    from korpha.heartbeats.dispatcher import HeartbeatService
    from korpha.heartbeats.handlers import register_builtins

    register_builtins()

    engine = _engine()

    def _one_tick() -> None:
        with Session(engine) as session:
            svc = HeartbeatService(session=session)
            result = asyncio.run(svc.tick())
        typer.echo(
            f"tick: fired={result.fired} failed={result.failed} "
            f"skipped={result.skipped_no_handler} "
            f"routines_enqueued={result.routines_enqueued} "
            f"recovered={result.recovered} "
            f"script_cron_ran={result.script_cron_ran}"
        )

    if not watch:
        _one_tick()
        return

    typer.echo(_dim(
        f"Watching: tick every {interval}s. Ctrl-C to stop."
    ))
    import time as _time
    try:
        while True:
            try:
                _one_tick()
            except Exception as exc:  # noqa: BLE001
                # Tick errored — log + keep looping. Don't crash the
                # daemon on a transient DB hiccup.
                typer.echo(_red(f"tick errored: {exc}"))
            _time.sleep(max(1, interval))
    except KeyboardInterrupt:
        typer.echo(_dim("\nstopped."))
        return


@app.command()
def routine_add(
    name: Annotated[str, typer.Argument(help="Display name for this routine")],
    kind: Annotated[
        str, typer.Option(help="Wakeup kind to fire (e.g. ceo.daily_digest)")
    ],
    every_seconds: Annotated[
        int, typer.Option(help="Fire interval in seconds")
    ] = 86400,
) -> None:
    """Register a recurring routine. Fires every ``--every-seconds`` (default daily)."""
    _ensure_load_env()
    from korpha.heartbeats.model import Routine, RoutineSchedule

    engine = _engine()
    with Session(engine) as session:
        _, business = _ensure_founder_and_business(session)
        routine = Routine(
            business_id=business.id,
            name=name,
            kind=kind,
            schedule_kind=RoutineSchedule.EVERY_SECONDS,
            schedule_value=every_seconds,
        )
        session.add(routine)
        session.commit()
        session.refresh(routine)
        typer.echo(_green(f"✓ routine added: {routine.name} ({routine.kind})"))


@app.command()
def routine_list() -> None:
    """Show registered routines."""
    _ensure_load_env()
    from korpha.heartbeats.model import Routine

    engine = _engine()
    with Session(engine) as session:
        routines = session.exec(select(Routine)).all()
        if not routines:
            typer.echo(_dim("(no routines registered — try `korpha routine-add`)"))
            return
        for r in routines:
            tag = "" if r.enabled else _dim(" [disabled]")
            last = r.last_fired_at.strftime("%Y-%m-%d %H:%M") if r.last_fired_at else "never"
            typer.echo(
                f"  • {r.name}  [{r.kind}]  every {r.schedule_value}s  "
                f"last={last}{tag}"
            )


@app.command()
def server(
    host: Annotated[str, typer.Option(help="Bind host (default localhost only)")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port")] = 8765,
    reload: Annotated[bool, typer.Option(help="Auto-reload on code changes")] = False,
) -> None:
    """Start the FastAPI server. Defaults to localhost:8765 (no auth)."""
    import uvicorn

    _ensure_load_env()
    if not _db_path().exists():
        # Auto-bootstrap with placeholder identity. The dashboard's
        # /app/welcome route detects the placeholder + redirects new
        # users into a Mike-friendly identity-capture form, so a fresh
        # `pip install korpha && korpha server` Just Works — no
        # separate `korpha init` step required.
        typer.echo(_yellow(
            "No database found — bootstrapping a fresh install."
        ))
        typer.echo(_dim(
            "  Open the dashboard to finish setup in your browser."
        ))
        _bootstrap_database(
            email=_BOOTSTRAP_PLACEHOLDER_EMAIL,
            name=_system_username_or("Founder"),
            business=_BOOTSTRAP_PLACEHOLDER_BUSINESS,
            description="",
        )
    # Install the structured-log file handler so `korpha logs`
    # has something to tail. Stderr stays on too — uvicorn forwards
    # its own access logs there.
    from korpha.diagnostics.logs import install_file_handler
    log_path = install_file_handler()
    typer.echo(_bold(f"Starting Korpha server on http://{host}:{port}"))
    typer.echo(_dim("  Swagger docs: /docs"))
    typer.echo(_dim("  Endpoints: /healthz /me /ask /propose /approvals/* /skills /blockers"))
    typer.echo(_dim(f"  Logs: {log_path} (tail with `korpha logs -f`)"))
    uvicorn.run(
        "korpha.api.server:build_app",
        host=host,
        port=port,
        reload=reload,
        factory=True,
    )


@app.command()
def tui(
    ws_url: Annotated[
        str | None,
        typer.Option(
            "--ws",
            help=(
                "WebSocket URL of the Korpha server. Default: "
                "ws://localhost:8765/api/tui/ws (or KORPHA_TUI_WS_URL "
                "env var). Use this to point at a remote server over "
                "an SSH tunnel."
            ),
        ),
    ] = None,
) -> None:
    """Open the Korpha TUI — full-screen terminal chat with your
    cofounder. Connects to a running ``korpha server`` over
    WebSocket so chat history + approvals stay in sync with the
    web dashboard.

    First time?  In one terminal: ``korpha server``. In another:
    ``korpha tui``. Same machine or via SSH tunnel for VPS use.

    Quit with Ctrl-C or type ``/quit``. ``/help`` lists all slashes.
    """
    _ensure_load_env()
    from korpha.tui import run_tui
    run_tui(ws_url=ws_url)


@app.command()
def demo() -> None:
    """Run the in-memory end-to-end demo (no persistent DB)."""
    _ensure_load_env()
    script = Path(__file__).resolve().parent.parent / "scripts" / "demo.py"
    if not script.exists():
        typer.echo(_yellow(f"demo script not found at {script}"))
        raise typer.Exit(code=1)
    os.execvp(sys.executable, [sys.executable, str(script)])


@app.command()
def logs(
    follow: Annotated[bool, typer.Option(
        "-f", "--follow", help="Tail the log file, printing new lines as they arrive.",
    )] = False,
    level: Annotated[str | None, typer.Option(
        "--level",
        help="Minimum level to show (DEBUG / INFO / WARNING / ERROR / CRITICAL).",
    )] = None,
    since: Annotated[str | None, typer.Option(
        "--since",
        help="Only show records from this point onward. Accepts ISO ('2026-05-07T12:00') or relative ('1h', '15m', '7d').",
    )] = None,
    limit: Annotated[int, typer.Option(
        "--limit", help="Cap the initial backlog count (default 200).",
    )] = 200,
    path: Annotated[Path | None, typer.Option(
        "--path", help="Override log file location.",
    )] = None,
) -> None:
    """Tail / filter the Korpha structured log file.

    Logs land in ``~/.korpha/logs/korpha.log`` once the server
    has installed the file handler (``korpha server`` does this
    on startup). One JSONL record per line — easy to grep, easy to
    pipe into other tools.
    """
    _ensure_load_env()
    from datetime import datetime, timedelta, timezone

    from korpha.diagnostics.logs import DEFAULT_LOG_PATH, tail_log

    target = path if path is not None else DEFAULT_LOG_PATH
    if not target.exists() and not follow:
        typer.echo(_yellow(
            f"No logs found at {target}. Start the server with "
            f"`korpha server` to generate logs."
        ))
        raise typer.Exit(code=0)

    since_dt: datetime | None = None
    if since:
        since_dt = _parse_since(since)
        if since_dt is None:
            typer.echo(_red(
                f"could not parse --since={since!r}. Use ISO timestamp "
                "or relative like '1h', '15m', '7d'."
            ))
            raise typer.Exit(code=1)

    try:
        for record in tail_log(
            target,
            min_level=level,
            since=since_dt,
            limit=limit,
            follow=follow,
        ):
            ts = record.get("ts", "")
            lvl = record.get("level", "?")
            logger_name = record.get("logger", "?")
            msg = record.get("msg", "")
            typer.echo(f"{ts} {lvl:<8} {logger_name}  {msg}")
    except KeyboardInterrupt:
        # Clean exit on Ctrl-C while following — no traceback noise.
        raise typer.Exit(code=0)


@app.command()
def insights(
    days: Annotated[int, typer.Option(
        "--days", help="Window size in days (default 7).",
    )] = 7,
    no_color: Annotated[bool, typer.Option(
        "--no-color", help="Suppress ANSI color codes.",
    )] = False,
) -> None:
    """Aggregate cofounder activity into a cost / tokens / skills /
    hours-saved report. Reuses the existing audit + cost trail —
    no new data collection."""
    _ensure_load_env()
    if not _db_path().exists():
        typer.echo(_yellow(
            "Korpha not initialized — no data to report on yet. "
            "Run `korpha init` first."
        ))
        raise typer.Exit(code=0)

    from sqlmodel import Session, select

    from korpha.business.model import Business
    from korpha.db._session import get_engine
    from korpha.insights import compute_insights, render_insights_terminal

    engine = get_engine()
    with Session(engine) as session:
        business = session.exec(select(Business)).first()
        if business is None:
            typer.echo(_yellow(
                "No business found — onboard one first via the dashboard."
            ))
            raise typer.Exit(code=0)
        report = compute_insights(
            session, business_id=business.id, window_days=days,
        )
        typer.echo(render_insights_terminal(report, color=not no_color))


goal_app = typer.Typer(
    name="goal",
    help=(
        "Persistent goal — the Ralph loop. Founder sets a goal on "
        "their active chat thread; the agent works toward it across "
        "turns until the judge says done, the turn budget hits, the "
        "founder pauses, or a real new message preempts."
    ),
)
app.add_typer(goal_app)


def _resolve_active_thread(session, business_id):
    """Find the most-recent ACTIVE web thread for the business.
    Used by the CLI when the founder doesn't pass an explicit
    thread id."""
    from korpha.cofounder.model import (
        Thread, ThreadPlatform, ThreadStatus,
    )
    from sqlmodel import select as _select
    return session.exec(
        _select(Thread)
        .where(Thread.business_id == business_id)
        .where(Thread.platform == ThreadPlatform.WEB)
        .where(Thread.status == ThreadStatus.ACTIVE)
        .order_by(Thread.last_message_at.desc())  # type: ignore[attr-defined]
        .limit(1)
    ).first()


@goal_app.command("set")
def goal_set(
    text: Annotated[str, typer.Argument(
        help="The goal statement (e.g. 'get me 10 customers').",
    )],
    max_turns: Annotated[int, typer.Option(
        "--max-turns",
        help="Cap on judge-driven continuations (default 20).",
    )] = 20,
) -> None:
    """Set / replace the active goal on the founder's most-recent
    web chat thread. Existing active goal moves to CLEARED."""
    _ensure_load_env()
    from sqlmodel import Session, select as _select
    from korpha.business.model import Business
    from korpha.db._session import get_engine
    from korpha.goals import GoalManager

    engine = get_engine()
    with Session(engine) as session:
        business = session.exec(_select(Business)).first()
        if business is None:
            typer.echo(_red("No business — onboard one first."))
            raise typer.Exit(code=1)
        thread = _resolve_active_thread(session, business.id)
        if thread is None:
            typer.echo(_red(
                "No active web thread. Open the chat first to "
                "create one (visit /app/chat)."
            ))
            raise typer.Exit(code=1)
        mgr = GoalManager(
            session=session, thread_id=thread.id,
            business_id=business.id, cost_tracker=None,
        )
        try:
            goal = mgr.set(text, max_turns=max_turns)
        except ValueError as exc:
            typer.echo(_red(str(exc)))
            raise typer.Exit(code=1) from exc
        typer.echo(_green(f"✓ Goal set: {goal.text!r}"))
        typer.echo(_dim(
            f"  Thread: {thread.id} • Max turns: {max_turns}\n"
            f"  Resume the chat (web/TUI) to start the loop."
        ))


@goal_app.command("status")
def goal_status() -> None:
    """Show the current goal on the active thread."""
    _ensure_load_env()
    from sqlmodel import Session, select as _select
    from korpha.business.model import Business
    from korpha.db._session import get_engine
    from korpha.goals import GoalManager

    engine = get_engine()
    with Session(engine) as session:
        business = session.exec(_select(Business)).first()
        if business is None:
            typer.echo(_red("No business."))
            raise typer.Exit(code=1)
        thread = _resolve_active_thread(session, business.id)
        if thread is None:
            typer.echo(_dim("No active thread."))
            return
        mgr = GoalManager(
            session=session, thread_id=thread.id,
            business_id=business.id, cost_tracker=None,
        )
        goal = mgr.latest()
        if goal is None:
            typer.echo(_dim("No goal set on this thread."))
            return
        typer.echo(_bold(f"Goal ({goal.status.value}):"))
        typer.echo(f"  {goal.text}")
        typer.echo(_dim(
            f"  Turns: {goal.turns_used}/{goal.max_turns}  "
            f"Verdict: {goal.last_verdict or '—'}"
        ))
        if goal.last_reason:
            typer.echo(_dim(f"  Reason: {goal.last_reason}"))
        if goal.paused_reason:
            typer.echo(_yellow(f"  Paused: {goal.paused_reason}"))


@goal_app.command("pause")
def goal_pause() -> None:
    """Pause the active goal."""
    _ensure_load_env()
    from sqlmodel import Session, select as _select
    from korpha.business.model import Business
    from korpha.db._session import get_engine
    from korpha.goals import GoalManager

    engine = get_engine()
    with Session(engine) as session:
        business = session.exec(_select(Business)).first()
        if business is None:
            typer.echo(_red("No business."))
            raise typer.Exit(code=1)
        thread = _resolve_active_thread(session, business.id)
        if thread is None:
            typer.echo(_dim("No active thread."))
            return
        mgr = GoalManager(
            session=session, thread_id=thread.id,
            business_id=business.id, cost_tracker=None,
        )
        out = mgr.pause()
        if out is None:
            typer.echo(_dim("No active goal to pause."))
        else:
            typer.echo(_green(f"✓ Paused: {out.text!r}"))


@goal_app.command("resume")
def goal_resume(
    keep_budget: Annotated[bool, typer.Option(
        "--keep-budget",
        help="Don't reset turn budget on resume.",
    )] = False,
) -> None:
    """Resume the most-recent paused goal (resets turn budget by default)."""
    _ensure_load_env()
    from sqlmodel import Session, select as _select
    from korpha.business.model import Business
    from korpha.db._session import get_engine
    from korpha.goals import GoalManager

    engine = get_engine()
    with Session(engine) as session:
        business = session.exec(_select(Business)).first()
        if business is None:
            typer.echo(_red("No business."))
            raise typer.Exit(code=1)
        thread = _resolve_active_thread(session, business.id)
        if thread is None:
            typer.echo(_dim("No active thread."))
            return
        mgr = GoalManager(
            session=session, thread_id=thread.id,
            business_id=business.id, cost_tracker=None,
        )
        out = mgr.resume(reset_budget=not keep_budget)
        if out is None:
            typer.echo(_dim("No paused goal to resume."))
        else:
            typer.echo(_green(f"✓ Resumed: {out.text!r}"))


@goal_app.command("clear")
def goal_clear() -> None:
    """Drop the active goal entirely."""
    _ensure_load_env()
    from sqlmodel import Session, select as _select
    from korpha.business.model import Business
    from korpha.db._session import get_engine
    from korpha.goals import GoalManager

    engine = get_engine()
    with Session(engine) as session:
        business = session.exec(_select(Business)).first()
        if business is None:
            typer.echo(_red("No business."))
            raise typer.Exit(code=1)
        thread = _resolve_active_thread(session, business.id)
        if thread is None:
            typer.echo(_dim("No active thread."))
            return
        mgr = GoalManager(
            session=session, thread_id=thread.id,
            business_id=business.id, cost_tracker=None,
        )
        out = mgr.clear()
        if out is None:
            typer.echo(_dim("No active goal."))
        else:
            typer.echo(_green(f"✓ Cleared: {out.text!r}"))


scriptcron_app = typer.Typer(
    name="cron",
    help=(
        "Agentless script cron — schedule scripts that ping a "
        "channel with their stdout. No LLM in the loop, $0 cost "
        "per tick. Empty output = silent (watchdog pattern). "
        "Failure = error alert."
    ),
)
app.add_typer(scriptcron_app)


@scriptcron_app.command("add")
def cron_add(
    name: Annotated[str, typer.Argument(
        help="Short slug, e.g. 'memory-watchdog' or 'rss-pull'.",
    )],
    script: Annotated[Path, typer.Argument(
        help="Path to the script (.sh / .py / executable).",
    )],
    cadence: Annotated[str, typer.Option(
        "--every", help="How often: 'every 5m', 'every 12h', 'every 7d'.",
    )] = "every 1h",
    deliver: Annotated[str | None, typer.Option(
        "--deliver",
        help="Channel to push stdout to ('email' or 'telegram'). "
             "Skip to log-only.",
    )] = None,
    recipient: Annotated[str | None, typer.Option(
        "--to",
        help="Email address or telegram chat_id. Required when --deliver is set.",
    )] = None,
) -> None:
    """Register a new agentless cron job."""
    _ensure_load_env()
    from sqlmodel import Session

    from korpha.business.model import Business
    from korpha.db._session import get_engine
    from korpha.scriptcron import ScriptCron, parse_cadence
    from sqlmodel import select as _select

    # Validate cadence eagerly so the founder gets feedback at create
    try:
        parse_cadence(cadence)
    except ValueError as exc:
        typer.echo(_red(str(exc)))
        raise typer.Exit(code=1) from exc

    if deliver and not recipient:
        typer.echo(_red(
            f"--deliver={deliver} requires --to <recipient>."
        ))
        raise typer.Exit(code=1)
    if deliver and deliver.lower() not in ("email", "telegram"):
        typer.echo(_red(
            f"--deliver {deliver!r} not supported. Use 'email' or 'telegram'."
        ))
        raise typer.Exit(code=1)

    script_path = script.expanduser().resolve()
    if not script_path.exists():
        typer.echo(_yellow(
            f"Script not found at {script_path} — adding anyway. "
            "Make sure to create it before the next tick."
        ))

    engine = get_engine()
    with Session(engine) as session:
        business = session.exec(_select(Business)).first()
        if business is None:
            typer.echo(_red("No business — onboard one first."))
            raise typer.Exit(code=1)
        job = ScriptCron(
            business_id=business.id,
            name=name,
            script_path=str(script_path),
            cadence=cadence,
            deliver_platform=deliver.lower() if deliver else None,
            deliver_recipient=recipient,
        )
        session.add(job); session.commit(); session.refresh(job)
        typer.echo(_green(f"✓ Cron {job.id} added: {name} ({cadence})"))
        if deliver:
            typer.echo(_dim(
                f"  Delivers to {deliver}/{recipient}"
            ))
        else:
            typer.echo(_dim("  Log-only (no channel push)."))


@scriptcron_app.command("list")
def cron_list() -> None:
    """List all script cron jobs for the active business."""
    _ensure_load_env()
    from sqlmodel import Session, select

    from korpha.business.model import Business
    from korpha.db._session import get_engine
    from korpha.scriptcron import ScriptCron

    engine = get_engine()
    with Session(engine) as session:
        business = session.exec(select(Business)).first()
        if business is None:
            typer.echo(_dim("No business yet."))
            return
        jobs = list(session.exec(
            select(ScriptCron).where(
                ScriptCron.business_id == business.id,
            )
        ).all())
        if not jobs:
            typer.echo(_dim(
                "No cron jobs. Add one with `korpha cron add`."
            ))
            return
        for j in jobs:
            status_color = {
                "ok": _green, "silent": _dim,
                "failed": _red, "never_run": _dim,
            }.get(j.last_status.value, lambda s: s)
            label = "" if j.enabled else _dim(" [disabled]")
            last = (
                j.last_run_at.strftime("%Y-%m-%d %H:%M")
                if j.last_run_at else "never"
            )
            typer.echo(
                f"  {j.name:<24} {status_color(j.last_status.value):<10} "
                f"{j.cadence:<12} last={last}{label}"
            )


@scriptcron_app.command("run")
def cron_run(
    name: Annotated[str, typer.Argument(
        help="Job slug (the name you used in `cron add`).",
    )],
) -> None:
    """Run a job immediately, ignoring its cadence. Useful for
    testing a new script before relying on the schedule."""
    _ensure_load_env()
    import asyncio as _aio

    from sqlmodel import Session, select

    from korpha.business.model import Business
    from korpha.db._session import get_engine
    from korpha.scriptcron import ScriptCron, run_job

    engine = get_engine()
    with Session(engine) as session:
        business = session.exec(select(Business)).first()
        if business is None:
            typer.echo(_red("No business."))
            raise typer.Exit(code=1)
        job = session.exec(
            select(ScriptCron)
            .where(ScriptCron.business_id == business.id)
            .where(ScriptCron.name == name)
        ).first()
        if job is None:
            typer.echo(_red(f"No cron named {name!r}."))
            raise typer.Exit(code=1)
        outcome = _aio.run(run_job(session, job))
        typer.echo(
            f"  status: {outcome.status.value}"
        )
        if outcome.exit_code is not None:
            typer.echo(f"  exit:   {outcome.exit_code}")
        if outcome.stdout:
            typer.echo(f"  stdout: {outcome.stdout[:400]}")
        if outcome.stderr:
            typer.echo(f"  stderr: {outcome.stderr[:400]}")
        if outcome.error:
            typer.echo(_red(f"  error:  {outcome.error}"))
        if outcome.delivered:
            typer.echo(_green("  ✓ delivered to channel"))


@scriptcron_app.command("add-digest")
def cron_add_digest(
    every: Annotated[str, typer.Option(
        "--every", help="Cadence: 'every 24h' / 'every 1d' / 'every 12h'.",
    )] = "every 24h",
    deliver: Annotated[str | None, typer.Option(
        "--deliver",
        help="Channel for the digest ('email' or 'telegram').",
    )] = None,
    recipient: Annotated[str | None, typer.Option(
        "--to",
        help="Email or chat_id. Required when --deliver is set.",
    )] = None,
    days: Annotated[int, typer.Option(
        "--days", help="Window the digest covers (default 1d).",
    )] = 1,
    name: Annotated[str, typer.Option(
        "--name",
        help="Cron job name (must be unique). Default: 'daily-digest'.",
    )] = "daily-digest",
) -> None:
    """Preset cron that emails/telegrams the cofounder ROI digest.

    Runs ``korpha insights --no-color --days N`` on the configured
    cadence and pushes stdout to ``--deliver``. Same backstop as the
    raw ``cron add``: empty stdout (no activity → no message) means
    silent tick. Saves the founder from writing the wrapper script
    themselves."""
    _ensure_load_env()
    from pathlib import Path as _P
    import os as _os
    from sqlmodel import Session, select as _select

    from korpha.business.model import Business
    from korpha.db._session import get_engine
    from korpha.scriptcron import ScriptCron, parse_cadence
    from korpha.skills.cron_author import (
        _CRON_SCRIPTS_DIR_NAME, _SAFE_NAME_RE,
    )

    if not _SAFE_NAME_RE.match(name):
        typer.echo(_red(
            f"--name {name!r} invalid. Use letters/digits/._-, "
            "1-60 chars, must start with alphanumeric."
        ))
        raise typer.Exit(code=1)
    try:
        parse_cadence(every)
    except ValueError as exc:
        typer.echo(_red(str(exc)))
        raise typer.Exit(code=1) from exc
    if deliver and deliver.lower() not in ("email", "telegram"):
        typer.echo(_red(
            f"--deliver {deliver!r} not supported. Use 'email' or 'telegram'."
        ))
        raise typer.Exit(code=1)
    if deliver and not recipient:
        typer.echo(_red(
            f"--deliver {deliver} requires --to <recipient>."
        ))
        raise typer.Exit(code=1)
    if days < 1:
        typer.echo(_red(f"--days must be ≥ 1, got {days}."))
        raise typer.Exit(code=1)

    # Generate the wrapper script. Uses `korpha` from PATH
    # (assumes the cron is running on the same host where the CLI
    # is installed — the common solo-founder case).
    base = _os.environ.get("KORPHA_DATA_DIR")
    scripts_dir = (
        (_P(base) / _CRON_SCRIPTS_DIR_NAME)
        if base
        else (_P.home() / ".korpha" / _CRON_SCRIPTS_DIR_NAME)
    )
    scripts_dir.mkdir(parents=True, exist_ok=True)
    script_path = scripts_dir / f"{name}.sh"
    script_body = (
        "#!/bin/bash\n"
        "# Auto-generated by `korpha cron add-digest`.\n"
        f"korpha insights --no-color --days {days}\n"
    )
    script_path.write_text(script_body, encoding="utf-8")
    script_path.chmod(0o755)

    engine = get_engine()
    with Session(engine) as session:
        business = session.exec(_select(Business)).first()
        if business is None:
            typer.echo(_red("No business — onboard one first."))
            raise typer.Exit(code=1)
        existing = session.exec(
            _select(ScriptCron)
            .where(ScriptCron.business_id == business.id)
            .where(ScriptCron.name == name)
        ).first()
        if existing is not None:
            typer.echo(_red(
                f"Cron {name!r} already exists. Pick a different "
                "--name or remove the existing one with "
                f"`korpha cron remove {name}`."
            ))
            raise typer.Exit(code=1)
        job = ScriptCron(
            business_id=business.id,
            name=name,
            script_path=str(script_path),
            cadence=every,
            deliver_platform=deliver.lower() if deliver else None,
            deliver_recipient=recipient,
        )
        session.add(job); session.commit(); session.refresh(job)
        typer.echo(_green(f"✓ Daily digest cron added: {name} ({every})"))
        typer.echo(_dim(
            f"  Script: {script_path}\n"
            f"  Window: last {days} day{'s' if days != 1 else ''}\n"
            f"  Delivery: {(deliver + ' → ' + recipient) if deliver else 'log-only'}"
        ))


@scriptcron_app.command("add-healthcheck")
def cron_add_healthcheck(
    url: Annotated[str, typer.Argument(
        help=(
            "URL to ping. Should resolve to a public address — the "
            "SSRF guard refuses metadata / private IPs."
        ),
    )],
    every: Annotated[str, typer.Option(
        "--every", help="Cadence: 'every 5m' / 'every 1h' / 'every 12h'.",
    )] = "every 5m",
    deliver: Annotated[str | None, typer.Option(
        "--deliver",
        help="Channel for failure alerts ('email' or 'telegram').",
    )] = None,
    recipient: Annotated[str | None, typer.Option(
        "--to",
        help="Email or chat_id. Required when --deliver is set.",
    )] = None,
    timeout: Annotated[int, typer.Option(
        "--timeout", help="HTTP timeout in seconds (default 10).",
    )] = 10,
    name: Annotated[str | None, typer.Option(
        "--name",
        help="Cron job name. Default: derived from URL hostname.",
    )] = None,
) -> None:
    """Preset cron that pings a URL, alerts on non-200 / unreachable.

    Generates a wrapper script that uses curl to hit ``url``,
    captures the HTTP status, and prints a failure line ONLY when
    something's wrong. Healthy → silent (no message). Non-2xx /
    timeout / DNS failure → ❌ alert on the configured channel.
    """
    _ensure_load_env()
    from pathlib import Path as _P
    import os as _os
    from urllib.parse import urlparse as _urlparse

    from sqlmodel import Session, select as _select

    from korpha.business.model import Business
    from korpha.db._session import get_engine
    from korpha.scriptcron import ScriptCron, parse_cadence
    from korpha.security import is_safe_url
    from korpha.skills.cron_author import (
        _CRON_SCRIPTS_DIR_NAME, _SAFE_NAME_RE,
    )

    # SSRF gate — don't let the founder accidentally point the
    # watchdog at metadata / RFC1918 / loopback. is_safe_url has
    # the KORPHA_ALLOW_PRIVATE_URLS=1 escape hatch for lab use.
    if not is_safe_url(url):
        typer.echo(_red(
            f"URL {url!r} resolves to a private / metadata address. "
            "Use a public URL, or set KORPHA_ALLOW_PRIVATE_URLS=1 "
            "for a local test."
        ))
        raise typer.Exit(code=1)

    try:
        parse_cadence(every)
    except ValueError as exc:
        typer.echo(_red(str(exc)))
        raise typer.Exit(code=1) from exc

    if deliver and deliver.lower() not in ("email", "telegram"):
        typer.echo(_red(
            f"--deliver {deliver!r} not supported. Use 'email' or 'telegram'."
        ))
        raise typer.Exit(code=1)
    if deliver and not recipient:
        typer.echo(_red(
            f"--deliver {deliver} requires --to <recipient>."
        ))
        raise typer.Exit(code=1)
    if timeout < 1:
        typer.echo(_red(f"--timeout must be ≥ 1, got {timeout}."))
        raise typer.Exit(code=1)

    # Derive a default name from the URL hostname if the founder
    # didn't supply one. Strips dots so it's a clean slug.
    if not name:
        host = _urlparse(url).hostname or "site"
        derived = host.replace(".", "-")[:50]
        name = f"healthcheck-{derived}"
    if not _SAFE_NAME_RE.match(name):
        typer.echo(_red(
            f"--name {name!r} invalid. Use letters/digits/._-, "
            "1-60 chars, must start with alphanumeric."
        ))
        raise typer.Exit(code=1)

    base = _os.environ.get("KORPHA_DATA_DIR")
    scripts_dir = (
        (_P(base) / _CRON_SCRIPTS_DIR_NAME)
        if base
        else (_P.home() / ".korpha" / _CRON_SCRIPTS_DIR_NAME)
    )
    scripts_dir.mkdir(parents=True, exist_ok=True)
    script_path = scripts_dir / f"{name}.sh"
    # Bash script: curl with --fail (non-2xx → exit 22) + capture
    # status line. Empty stdout on success, "DOWN: ..." on failure.
    # Single-quote the URL to neutralize shell metacharacters; we
    # already validated it's a parseable URL.
    safe_url = url.replace("'", "'\\''")  # escape single quotes
    script_body = f"""#!/bin/bash
# Auto-generated by `korpha cron add-healthcheck`.
# Hits the URL; prints to stdout ONLY on failure.
URL='{safe_url}'
TIMEOUT={timeout}
RESPONSE=$(curl -fsS -o /dev/null -w '%{{http_code}}' \\
    --max-time "$TIMEOUT" "$URL" 2>&1)
EXIT=$?
if [ "$EXIT" -ne 0 ]; then
    # 0 = success, 22 = non-2xx HTTP, 28 = timeout, 6 = DNS, ...
    echo "DOWN: $URL (curl exit $EXIT)"
    [ -n "$RESPONSE" ] && echo "$RESPONSE"
    exit 1
fi
"""
    script_path.write_text(script_body, encoding="utf-8")
    script_path.chmod(0o755)

    engine = get_engine()
    with Session(engine) as session:
        business = session.exec(_select(Business)).first()
        if business is None:
            typer.echo(_red("No business — onboard one first."))
            raise typer.Exit(code=1)
        existing = session.exec(
            _select(ScriptCron)
            .where(ScriptCron.business_id == business.id)
            .where(ScriptCron.name == name)
        ).first()
        if existing is not None:
            typer.echo(_red(
                f"Cron {name!r} already exists. Pick a different "
                "--name or remove the existing one with "
                f"`korpha cron remove {name}`."
            ))
            raise typer.Exit(code=1)
        job = ScriptCron(
            business_id=business.id,
            name=name,
            script_path=str(script_path),
            cadence=every,
            deliver_platform=deliver.lower() if deliver else None,
            deliver_recipient=recipient,
        )
        session.add(job); session.commit(); session.refresh(job)
        typer.echo(_green(
            f"✓ Healthcheck cron added: {name} ({every}) → {url}"
        ))
        typer.echo(_dim(
            f"  Script: {script_path}\n"
            f"  Timeout: {timeout}s\n"
            f"  Delivery: "
            f"{(deliver + ' → ' + recipient) if deliver else 'log-only'}"
        ))


@scriptcron_app.command("add-disk-watch")
def cron_add_disk_watch(
    every: Annotated[str, typer.Option(
        "--every", help="Cadence: 'every 15m' / 'every 1h' / 'every 6h'.",
    )] = "every 15m",
    threshold: Annotated[int, typer.Option(
        "--threshold",
        help="Alert when % used >= this (1-99, default 90).",
    )] = 90,
    mount: Annotated[str, typer.Option(
        "--mount", help="Filesystem mount to check (default '/').",
    )] = "/",
    deliver: Annotated[str | None, typer.Option(
        "--deliver",
        help="Channel for the alert ('email' or 'telegram').",
    )] = None,
    recipient: Annotated[str | None, typer.Option(
        "--to", help="Email or chat_id. Required when --deliver is set.",
    )] = None,
    name: Annotated[str, typer.Option(
        "--name", help="Cron job name (must be unique).",
    )] = "disk-watch",
) -> None:
    """Preset cron that alerts when a filesystem fills up.

    Generates a bash script that runs ``df`` against ``--mount``,
    parses the % used, and prints to stdout (alert) ONLY when it
    crosses ``--threshold``. Healthy → silent. POSIX-only — Termux
    + Linux + macOS work; Windows doesn't have df.
    """
    _ensure_load_env()
    from pathlib import Path as _P
    import os as _os

    from sqlmodel import Session, select as _select

    from korpha.business.model import Business
    from korpha.db._session import get_engine
    from korpha.scriptcron import ScriptCron, parse_cadence
    from korpha.skills.cron_author import (
        _CRON_SCRIPTS_DIR_NAME, _SAFE_NAME_RE,
    )

    if not _SAFE_NAME_RE.match(name):
        typer.echo(_red(
            f"--name {name!r} invalid. Use letters/digits/._-, "
            "1-60 chars, must start with alphanumeric."
        ))
        raise typer.Exit(code=1)
    try:
        parse_cadence(every)
    except ValueError as exc:
        typer.echo(_red(str(exc)))
        raise typer.Exit(code=1) from exc
    if not (1 <= threshold <= 99):
        typer.echo(_red(
            f"--threshold must be 1-99, got {threshold}."
        ))
        raise typer.Exit(code=1)
    if deliver and deliver.lower() not in ("email", "telegram"):
        typer.echo(_red(
            f"--deliver {deliver!r} not supported. Use 'email' or 'telegram'."
        ))
        raise typer.Exit(code=1)
    if deliver and not recipient:
        typer.echo(_red(
            f"--deliver {deliver} requires --to <recipient>."
        ))
        raise typer.Exit(code=1)
    # Mount must look like a path; reject obvious shell injection.
    if not mount.startswith("/") or any(
        c in mount for c in (";", "|", "&", "`", "$", "\n")
    ):
        typer.echo(_red(
            f"--mount {mount!r} must start with / and contain no "
            "shell metacharacters."
        ))
        raise typer.Exit(code=1)

    base = _os.environ.get("KORPHA_DATA_DIR")
    scripts_dir = (
        (_P(base) / _CRON_SCRIPTS_DIR_NAME)
        if base
        else (_P.home() / ".korpha" / _CRON_SCRIPTS_DIR_NAME)
    )
    scripts_dir.mkdir(parents=True, exist_ok=True)
    script_path = scripts_dir / f"{name}.sh"
    safe_mount = mount.replace("'", "'\\''")
    script_body = f"""#!/bin/bash
# Auto-generated by `korpha cron add-disk-watch`.
# Alerts ONLY when disk usage crosses the threshold.
MOUNT='{safe_mount}'
THRESHOLD={threshold}
USED=$(df -P "$MOUNT" | awk 'NR==2 {{ gsub("%", "", $5); print $5 }}')
if [ -z "$USED" ]; then
    echo "disk-watch: could not read df output for $MOUNT"
    exit 1
fi
if [ "$USED" -ge "$THRESHOLD" ]; then
    echo "❌ Disk $MOUNT at ${{USED}}% (threshold $THRESHOLD%)"
    df -h "$MOUNT" | sed 's/^/    /'
fi
"""
    script_path.write_text(script_body, encoding="utf-8")
    script_path.chmod(0o755)

    engine = get_engine()
    with Session(engine) as session:
        business = session.exec(_select(Business)).first()
        if business is None:
            typer.echo(_red("No business — onboard one first."))
            raise typer.Exit(code=1)
        existing = session.exec(
            _select(ScriptCron)
            .where(ScriptCron.business_id == business.id)
            .where(ScriptCron.name == name)
        ).first()
        if existing is not None:
            typer.echo(_red(
                f"Cron {name!r} already exists. Pick a different "
                "--name or remove the existing one."
            ))
            raise typer.Exit(code=1)
        job = ScriptCron(
            business_id=business.id,
            name=name,
            script_path=str(script_path),
            cadence=every,
            deliver_platform=deliver.lower() if deliver else None,
            deliver_recipient=recipient,
        )
        session.add(job); session.commit(); session.refresh(job)
        typer.echo(_green(
            f"✓ Disk-watch cron added: {name} ({every}) "
            f"on {mount} @ {threshold}%"
        ))


@scriptcron_app.command("add-rss")
def cron_add_rss(
    feed_url: Annotated[str, typer.Argument(
        help="RSS / Atom feed URL. Must resolve to a public address.",
    )],
    every: Annotated[str, typer.Option(
        "--every", help="Cadence: 'every 1h' / 'every 6h' / 'every 1d'.",
    )] = "every 1h",
    deliver: Annotated[str | None, typer.Option(
        "--deliver", help="Channel for new-entry pings.",
    )] = None,
    recipient: Annotated[str | None, typer.Option(
        "--to", help="Email or chat_id. Required when --deliver is set.",
    )] = None,
    max_entries: Annotated[int, typer.Option(
        "--max", help="Max new entries to ship per tick (default 5).",
    )] = 5,
    name: Annotated[str | None, typer.Option(
        "--name", help="Cron job name. Default: derived from URL hostname.",
    )] = None,
) -> None:
    """Preset cron that pulls an RSS / Atom feed + alerts on new
    entries. State-tracked: previously-seen entry GUIDs are stored
    in a sidecar JSON next to the script so re-runs only ship NEW
    items. First-tick behavior: silent (we record the baseline).
    """
    _ensure_load_env()
    from pathlib import Path as _P
    import os as _os
    from urllib.parse import urlparse as _urlparse

    from sqlmodel import Session, select as _select

    from korpha.business.model import Business
    from korpha.db._session import get_engine
    from korpha.scriptcron import ScriptCron, parse_cadence
    from korpha.security import is_safe_url
    from korpha.skills.cron_author import (
        _CRON_SCRIPTS_DIR_NAME, _SAFE_NAME_RE,
    )

    if not is_safe_url(feed_url):
        typer.echo(_red(
            f"Feed URL {feed_url!r} resolves to a private / metadata "
            "address. Use a public URL."
        ))
        raise typer.Exit(code=1)
    try:
        parse_cadence(every)
    except ValueError as exc:
        typer.echo(_red(str(exc)))
        raise typer.Exit(code=1) from exc
    if deliver and deliver.lower() not in ("email", "telegram"):
        typer.echo(_red(
            f"--deliver {deliver!r} not supported. Use 'email' or 'telegram'."
        ))
        raise typer.Exit(code=1)
    if deliver and not recipient:
        typer.echo(_red(
            f"--deliver {deliver} requires --to <recipient>."
        ))
        raise typer.Exit(code=1)
    if max_entries < 1:
        typer.echo(_red(f"--max must be ≥ 1, got {max_entries}."))
        raise typer.Exit(code=1)

    if not name:
        host = _urlparse(feed_url).hostname or "feed"
        derived = host.replace(".", "-")[:50]
        name = f"rss-{derived}"
    if not _SAFE_NAME_RE.match(name):
        typer.echo(_red(
            f"--name {name!r} invalid. Use letters/digits/._-, "
            "1-60 chars, must start with alphanumeric."
        ))
        raise typer.Exit(code=1)

    base = _os.environ.get("KORPHA_DATA_DIR")
    scripts_dir = (
        (_P(base) / _CRON_SCRIPTS_DIR_NAME)
        if base
        else (_P.home() / ".korpha" / _CRON_SCRIPTS_DIR_NAME)
    )
    scripts_dir.mkdir(parents=True, exist_ok=True)
    script_path = scripts_dir / f"{name}.py"
    safe_url = feed_url.replace("'", "\\'")
    script_body = f"""#!/usr/bin/env python3
# Auto-generated by `korpha cron add-rss`.
# Pulls {feed_url}, ships new entries (deduped via sidecar state).
import json
import os
import re
import sys
import urllib.error
import urllib.request
from html import unescape

FEED_URL = '{safe_url}'
MAX_NEW = {max_entries}
STATE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    '{name}.state.json',
)

# Load previously-seen guids. baseline_mode = True on first ever
# run (no state file yet) — we record the initial entry list
# without alerting so the founder doesn't get a flood the moment
# they wire up an active feed.
baseline_mode = not os.path.exists(STATE_PATH)
try:
    with open(STATE_PATH, encoding='utf-8') as f:
        state = json.load(f)
    seen = set(state.get('seen') or [])
except (OSError, json.JSONDecodeError):
    state = {{}}
    seen = set()

# Fetch the feed (no external lib — stdlib urllib + regex parse)
try:
    req = urllib.request.Request(
        FEED_URL,
        headers={{'User-Agent': 'korpha-cron-rss/1.0'}},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read().decode('utf-8', errors='replace')
except (urllib.error.URLError, OSError, TimeoutError) as exc:
    print(f'❌ rss {{FEED_URL}} fetch failed: {{exc}}')
    sys.exit(1)

# Coarse extraction — looks for <item> + <entry> blocks. Pulls
# title, link, guid (or fallback to link). Good enough for most
# feeds; specialty feedparser libs are an upgrade path.
def _grab(block, tag):
    m = re.search(rf'<{{tag}}[^>]*>(.*?)</{{tag}}>', block, re.DOTALL)
    if not m:
        return ''
    text = re.sub(r'<!\\[CDATA\\[(.*?)\\]\\]>', r'\\1', m.group(1), flags=re.DOTALL)
    return unescape(re.sub(r'<[^>]+>', '', text)).strip()


def _grab_link(block):
    # Atom: <link href="..."/> ; RSS: <link>...</link>
    m = re.search(r'<link[^>]+href=["\\\']([^"\\\']+)', block)
    if m:
        return m.group(1)
    m = re.search(r'<link>(.*?)</link>', block, re.DOTALL)
    return m.group(1).strip() if m else ''


entries = re.findall(
    r'(?:<item>.*?</item>|<entry>.*?</entry>)', body, re.DOTALL,
)
new_items = []
for entry_block in entries:
    guid = (
        _grab(entry_block, 'guid')
        or _grab(entry_block, 'id')
        or _grab_link(entry_block)
        or _grab(entry_block, 'title')
    )
    if not guid or guid in seen:
        continue
    title = _grab(entry_block, 'title') or '(untitled)'
    link = _grab_link(entry_block)
    new_items.append({{'guid': guid, 'title': title, 'link': link}})
    seen.add(guid)

# First-tick behavior: silent baseline. State file didn't exist
# before, so this is the first run — record the entries we found
# but don't alert (would be a flood for an active feed).
if baseline_mode:
    pass  # silent — fall through to save the seen set
elif new_items:
    for item in new_items[:MAX_NEW]:
        link_part = f'\\n  {{item["link"]}}' if item["link"] else ''
        print(f'• {{item["title"]}}{{link_part}}')
    if len(new_items) > MAX_NEW:
        print(f'\\n…and {{len(new_items) - MAX_NEW}} more new item(s).')

# Persist state. Cap at 500 guids to keep the sidecar bounded.
trimmed = list(seen)[-500:]
try:
    with open(STATE_PATH, 'w', encoding='utf-8') as f:
        json.dump({{'seen': trimmed}}, f)
except OSError as exc:
    print(f'⚠ rss state write failed: {{exc}}', file=sys.stderr)
"""
    script_path.write_text(script_body, encoding="utf-8")
    script_path.chmod(0o755)

    engine = get_engine()
    with Session(engine) as session:
        business = session.exec(_select(Business)).first()
        if business is None:
            typer.echo(_red("No business — onboard one first."))
            raise typer.Exit(code=1)
        existing = session.exec(
            _select(ScriptCron)
            .where(ScriptCron.business_id == business.id)
            .where(ScriptCron.name == name)
        ).first()
        if existing is not None:
            typer.echo(_red(
                f"Cron {name!r} already exists. Pick a different --name."
            ))
            raise typer.Exit(code=1)
        job = ScriptCron(
            business_id=business.id,
            name=name,
            script_path=str(script_path),
            cadence=every,
            deliver_platform=deliver.lower() if deliver else None,
            deliver_recipient=recipient,
        )
        session.add(job); session.commit(); session.refresh(job)
        typer.echo(_green(
            f"✓ RSS cron added: {name} ({every}) → {feed_url}"
        ))
        typer.echo(_dim(
            f"  Script: {script_path}\n"
            f"  First tick is a silent baseline; alerts start "
            "from the second tick."
        ))


@scriptcron_app.command("add-vacuum")
def cron_add_vacuum(
    every: Annotated[str, typer.Option(
        "--every",
        help="Cadence: 'every 7d' / 'every 24h' / 'every 12h'.",
    )] = "every 7d",
    skip_db: Annotated[bool, typer.Option(
        "--skip-db",
        help="Skip the sqlite VACUUM step (faster on big DBs).",
    )] = False,
    name: Annotated[str, typer.Option(
        "--name", help="Cron job name (must be unique).",
    )] = "disk-vacuum",
) -> None:
    """Preset cron that runs ``korpha disk vacuum`` on a schedule.

    Reclaims orphan checkpoint blobs (and optionally compacts the
    sqlite DB). Output stays empty when there's nothing to clean —
    Mike only hears about it when something actually got reclaimed.
    Watchdog pattern: silent = healthy.
    """
    _ensure_load_env()
    import os as _os
    import shutil
    from pathlib import Path as _P

    from sqlmodel import Session, select as _select

    from korpha.business.model import Business
    from korpha.db._session import get_engine
    from korpha.scriptcron import ScriptCron, parse_cadence
    from korpha.skills.cron_author import (
        _CRON_SCRIPTS_DIR_NAME, _SAFE_NAME_RE,
    )

    if not _SAFE_NAME_RE.match(name):
        typer.echo(_red(
            f"--name {name!r} invalid. Use letters/digits/._-, "
            "1-60 chars, must start with alphanumeric."
        ))
        raise typer.Exit(code=1)
    try:
        parse_cadence(every)
    except ValueError as exc:
        typer.echo(_red(str(exc)))
        raise typer.Exit(code=1) from exc

    aig_bin = shutil.which("korpha") or "korpha"
    skip_flag = " --skip-db" if skip_db else ""

    base = _os.environ.get("KORPHA_DATA_DIR")
    scripts_dir = (
        (_P(base) / _CRON_SCRIPTS_DIR_NAME)
        if base
        else (_P.home() / ".korpha" / _CRON_SCRIPTS_DIR_NAME)
    )
    scripts_dir.mkdir(parents=True, exist_ok=True)
    script_path = scripts_dir / f"{name}.sh"
    # Capture vacuum output, then check whether anything was
    # actually reclaimed. If totally clean, stay silent (watchdog
    # pattern). Reclaimed bytes ≠ 0 → emit a one-line summary so
    # the founder sees the gain.
    safe_bin = aig_bin.replace("'", "'\\''")
    script_body = f"""#!/bin/bash
# Auto-generated by `korpha cron add-vacuum`.
# Reclaims orphan checkpoint blobs + optionally compacts the DB.
set -u
OUT=$('{safe_bin}' disk vacuum{skip_flag} 2>&1) || true
# Stay silent unless something reclaimed > 0 bytes.
if echo "$OUT" | grep -qE '\\b[1-9][0-9]* (orphan|tmp|KB|MB|GB)\\b'; then
    echo "🧹 disk-vacuum reclaimed:"
    echo "$OUT" | sed 's/^/    /'
fi
"""
    script_path.write_text(script_body, encoding="utf-8")
    script_path.chmod(0o755)

    engine = get_engine()
    with Session(engine) as session:
        business = session.exec(_select(Business)).first()
        if business is None:
            typer.echo(_red("No business — onboard one first."))
            raise typer.Exit(code=1)
        existing = session.exec(
            _select(ScriptCron)
            .where(ScriptCron.business_id == business.id)
            .where(ScriptCron.name == name)
        ).first()
        if existing is not None:
            typer.echo(_red(
                f"Cron {name!r} already exists. Pick a different "
                "--name or remove the existing one."
            ))
            raise typer.Exit(code=1)
        job = ScriptCron(
            business_id=business.id,
            name=name,
            script_path=str(script_path),
            cadence=every,
            deliver_platform=None,  # log-only by default
            deliver_recipient=None,
        )
        session.add(job); session.commit(); session.refresh(job)
        typer.echo(_green(
            f"✓ Disk-vacuum cron added: {name} ({every})"
        ))
        typer.echo(_dim(
            f"  Script: {script_path}\n"
            f"  Silent when nothing to reclaim."
        ))


@scriptcron_app.command("add-monthly-review")
def cron_add_monthly_review(
    every: Annotated[str, typer.Option(
        "--every",
        help="Cadence — 'every 30d' fires roughly monthly.",
    )] = "every 30d",
    deliver: Annotated[str | None, typer.Option(
        "--deliver",
        help="Channel for the report — 'email' / 'telegram'. "
             "Skip for log-only.",
    )] = None,
    recipient: Annotated[str | None, typer.Option(
        "--to", help="Recipient when --deliver is set.",
    )] = None,
    name: Annotated[str, typer.Option(
        "--name", help="Cron job name.",
    )] = "monthly-review",
) -> None:
    """Schedule the finance.monthly_review skill to fire monthly.

    Generates a watchdog script that runs ``korpha review`` and
    pipes the report to the configured channel. ``every 30d`` is
    a rolling 30-day window — not the calendar 1st-of-month.
    """
    _ensure_load_env()
    import os as _os
    import shutil
    from pathlib import Path as _P

    from sqlmodel import Session, select as _select

    from korpha.business.model import Business
    from korpha.db._session import get_engine
    from korpha.scriptcron import ScriptCron, parse_cadence
    from korpha.skills.cron_author import (
        _CRON_SCRIPTS_DIR_NAME, _SAFE_NAME_RE,
    )

    if not _SAFE_NAME_RE.match(name):
        typer.echo(_red(f"--name {name!r} invalid."))
        raise typer.Exit(code=1)
    try:
        parse_cadence(every)
    except ValueError as exc:
        typer.echo(_red(str(exc)))
        raise typer.Exit(code=1) from exc
    if deliver and deliver.lower() not in ("email", "telegram"):
        typer.echo(_red(
            f"--deliver must be 'email' or 'telegram'; got "
            f"{deliver!r}"
        ))
        raise typer.Exit(code=1)
    if deliver and not recipient:
        typer.echo(_red(
            f"--deliver={deliver} requires --to <recipient>."
        ))
        raise typer.Exit(code=1)

    aig_bin = shutil.which("korpha") or "korpha"
    base = _os.environ.get("KORPHA_DATA_DIR")
    scripts_dir = (
        (_P(base) / _CRON_SCRIPTS_DIR_NAME) if base
        else (_P.home() / ".korpha" / _CRON_SCRIPTS_DIR_NAME)
    )
    scripts_dir.mkdir(parents=True, exist_ok=True)
    script_path = scripts_dir / f"{name}.sh"
    safe_bin = aig_bin.replace("'", "'\\''")
    script_body = f"""#!/bin/bash
# Auto-generated by `korpha cron add-monthly-review`.
# Runs the finance.monthly_review skill and prints its report.
set -u
'{safe_bin}' review 2>&1
"""
    script_path.write_text(script_body, encoding="utf-8")
    script_path.chmod(0o755)

    engine = get_engine()
    with Session(engine) as session:
        business = session.exec(_select(Business)).first()
        if business is None:
            typer.echo(_red("No business — onboard one first."))
            raise typer.Exit(code=1)
        existing = session.exec(
            _select(ScriptCron)
            .where(ScriptCron.business_id == business.id)
            .where(ScriptCron.name == name)
        ).first()
        if existing is not None:
            typer.echo(_red(
                f"Cron {name!r} already exists. Pick a different "
                "--name or remove the existing one."
            ))
            raise typer.Exit(code=1)
        job = ScriptCron(
            business_id=business.id,
            name=name,
            script_path=str(script_path),
            cadence=every,
            deliver_platform=deliver.lower() if deliver else None,
            deliver_recipient=recipient,
        )
        session.add(job); session.commit(); session.refresh(job)
        typer.echo(_green(
            f"✓ Monthly review cron added: {name} ({every})"
        ))
        typer.echo(_dim(
            f"  Script: {script_path}\n"
            f"  First fire happens after {every} from now."
        ))


@scriptcron_app.command("add-card-dispatcher")
def cron_add_card_dispatcher(
    every: Annotated[str, typer.Option(
        "--every",
        help="Cadence: 'every 5m' / 'every 15m'. Trade frequency "
             "vs cost — each tick fires LLM calls for any newly-"
             "claimed cards.",
    )] = "every 5m",
    stale_after: Annotated[int, typer.Option(
        "--stale-after",
        help="Skip cards last auto-dispatched within this many minutes.",
    )] = 30,
    max_cards: Annotated[int, typer.Option(
        "--max",
        help="Cap cards processed per tick.",
    )] = 12,
    name: Annotated[str, typer.Option(
        "--name",
        help="Cron job name (must be unique).",
    )] = "card-dispatcher",
) -> None:
    """Schedule a recurring scan that runs IN_PROGRESS kanban cards
    through Workforce.dispatch.

    Use this as Path 2 of the workforce auto-dispatch triggers
    (set KORPHA_WORKFORCE_AUTO_DISPATCH_MODE=cron in env) when you
    want cards to execute on a heartbeat instead of synchronously
    in chat. Catches cards that landed in IN_PROGRESS via routes
    other than ``kanban.fire_sprint`` — manual moves, future
    skills, etc."""
    _ensure_load_env()
    from pathlib import Path as _P
    import os as _os
    from sqlmodel import Session, select as _select

    from korpha.business.model import Business
    from korpha.db._session import get_engine
    from korpha.scriptcron import ScriptCron, parse_cadence
    from korpha.skills.cron_author import (
        _CRON_SCRIPTS_DIR_NAME, _SAFE_NAME_RE,
    )

    if not _SAFE_NAME_RE.match(name):
        typer.echo(_red(
            f"--name {name!r} invalid. Use letters/digits/._-, "
            "1-60 chars, must start with alphanumeric."
        ))
        raise typer.Exit(code=1)
    try:
        parse_cadence(every)
    except ValueError as exc:
        typer.echo(_red(str(exc)))
        raise typer.Exit(code=1) from exc

    base = _os.environ.get("KORPHA_DATA_DIR")
    scripts_dir = (
        (_P(base) / _CRON_SCRIPTS_DIR_NAME)
        if base
        else (_P.home() / ".korpha" / _CRON_SCRIPTS_DIR_NAME)
    )
    scripts_dir.mkdir(parents=True, exist_ok=True)
    script_path = scripts_dir / f"{name}.sh"
    script_body = (
        "#!/bin/bash\n"
        "# Auto-generated by `aigenteur cron add-card-dispatcher`.\n"
        "# Scans IN_PROGRESS kanban cards + runs them through\n"
        "# Workforce.dispatch. Idempotent — cards already auto-\n"
        "# dispatched in the last --stale-after minutes are skipped.\n"
        f"aigenteur kanban dispatch-pending "
        f"--stale-after {stale_after} --max {max_cards}\n"
    )
    script_path.write_text(script_body, encoding="utf-8")
    script_path.chmod(0o755)

    engine = get_engine()
    with Session(engine) as session:
        business = session.exec(_select(Business)).first()
        if business is None:
            typer.echo(_red("No business — onboard one first."))
            raise typer.Exit(code=1)
        existing = session.exec(
            _select(ScriptCron)
            .where(ScriptCron.business_id == business.id)
            .where(ScriptCron.name == name)
        ).first()
        if existing is not None:
            typer.echo(_red(
                f"Cron {name!r} already exists. Pick a different "
                "--name or `aigenteur cron remove "
                f"{name}` first."
            ))
            raise typer.Exit(code=1)
        job = ScriptCron(
            business_id=business.id, name=name,
            script_path=str(script_path), cadence=every,
        )
        session.add(job); session.commit(); session.refresh(job)
        typer.echo(_green(
            f"✓ Card dispatcher cron added: {name} ({every})"
        ))
        typer.echo(_dim(
            f"  Script: {script_path}\n"
            f"  Stale-after: {stale_after} min\n"
            f"  Max cards per tick: {max_cards}\n"
            f"  NOTE: set KORPHA_WORKFORCE_AUTO_DISPATCH_MODE=cron "
            f"to disable the inline trigger."
        ))


@scriptcron_app.command("add-backup")
def cron_add_backup(
    every: Annotated[str, typer.Option(
        "--every",
        help="Cadence — 'every 7d' / 'every 24h'.",
    )] = "every 7d",
    output_dir: Annotated[Path | None, typer.Option(
        "--to", help="Directory to write backup tarballs into. "
                     "Default: ~/.korpha/backups/",
    )] = None,
    keep_last: Annotated[int, typer.Option(
        "--keep-last",
        help="Number of backup tarballs to retain (oldest pruned).",
    )] = 4,
    name: Annotated[str, typer.Option(
        "--name", help="Cron job name.",
    )] = "auto-backup",
) -> None:
    """Schedule a recurring `korpha backup` to a local directory.

    Pair with rclone / restic / S3 sync for off-machine durability —
    this preset only writes locally. After each run, prunes the
    oldest tarballs beyond --keep-last so the directory doesn't
    grow unbounded.
    """
    _ensure_load_env()
    import os as _os
    import shutil
    from pathlib import Path as _P

    from sqlmodel import Session, select as _select

    from korpha.business.model import Business
    from korpha.db._session import get_engine
    from korpha.scriptcron import ScriptCron, parse_cadence
    from korpha.skills.cron_author import (
        _CRON_SCRIPTS_DIR_NAME, _SAFE_NAME_RE,
    )

    if not _SAFE_NAME_RE.match(name):
        typer.echo(_red(f"--name {name!r} invalid."))
        raise typer.Exit(code=1)
    try:
        parse_cadence(every)
    except ValueError as exc:
        typer.echo(_red(str(exc)))
        raise typer.Exit(code=1) from exc
    if keep_last < 1:
        typer.echo(_red("--keep-last must be >= 1"))
        raise typer.Exit(code=1)

    base = _os.environ.get("KORPHA_DATA_DIR")
    out_dir = (
        output_dir.expanduser().resolve()
        if output_dir is not None
        else (
            (_P(base) / "backups") if base
            else (_P.home() / ".korpha" / "backups")
        )
    )
    aig_bin = shutil.which("korpha") or "korpha"
    scripts_dir = (
        (_P(base) / _CRON_SCRIPTS_DIR_NAME) if base
        else (_P.home() / ".korpha" / _CRON_SCRIPTS_DIR_NAME)
    )
    scripts_dir.mkdir(parents=True, exist_ok=True)
    script_path = scripts_dir / f"{name}.sh"
    safe_bin = aig_bin.replace("'", "'\\''")
    safe_dir = str(out_dir).replace("'", "'\\''")
    script_body = f"""#!/bin/bash
# Auto-generated by `korpha cron add-backup`.
set -u
DEST='{safe_dir}'
mkdir -p "$DEST"
TS=$(date +%Y%m%d-%H%M%S)
OUT="$DEST/korpha-backup-$TS.tar.gz"
'{safe_bin}' backup --output "$OUT" >/dev/null
echo "✓ backup: $(basename "$OUT") ($(stat -c%s "$OUT" 2>/dev/null || stat -f%z "$OUT") bytes)"
# Prune oldest beyond --keep-last
ls -1t "$DEST"/korpha-backup-*.tar.gz 2>/dev/null | tail -n +{keep_last + 1} | xargs -r rm -f
"""
    script_path.write_text(script_body, encoding="utf-8")
    script_path.chmod(0o755)

    engine = get_engine()
    with Session(engine) as session:
        business = session.exec(_select(Business)).first()
        if business is None:
            typer.echo(_red("No business — onboard one first."))
            raise typer.Exit(code=1)
        existing = session.exec(
            _select(ScriptCron)
            .where(ScriptCron.business_id == business.id)
            .where(ScriptCron.name == name)
        ).first()
        if existing is not None:
            typer.echo(_red(
                f"Cron {name!r} already exists. Remove the existing "
                "one or pick a new --name."
            ))
            raise typer.Exit(code=1)
        job = ScriptCron(
            business_id=business.id,
            name=name,
            script_path=str(script_path),
            cadence=every,
            deliver_platform=None,
            deliver_recipient=None,
        )
        session.add(job); session.commit(); session.refresh(job)
        typer.echo(_green(
            f"✓ Auto-backup cron added: {name} ({every})"
        ))
        typer.echo(_dim(
            f"  Backups land at {out_dir}\n"
            f"  Keeps last {keep_last} tarballs."
        ))


@scriptcron_app.command("remove")
def cron_remove(
    name: Annotated[str, typer.Argument(help="Job name to remove.")],
) -> None:
    """Delete a cron job."""
    _ensure_load_env()
    from sqlmodel import Session, select

    from korpha.business.model import Business
    from korpha.db._session import get_engine
    from korpha.scriptcron import ScriptCron

    engine = get_engine()
    with Session(engine) as session:
        business = session.exec(select(Business)).first()
        if business is None:
            typer.echo(_red("No business."))
            raise typer.Exit(code=1)
        job = session.exec(
            select(ScriptCron)
            .where(ScriptCron.business_id == business.id)
            .where(ScriptCron.name == name)
        ).first()
        if job is None:
            typer.echo(_yellow(f"No cron named {name!r}."))
            raise typer.Exit(code=0)
        session.delete(job); session.commit()
        typer.echo(_green(f"✓ Removed cron {name!r}."))


kanban_app = typer.Typer(
    name="kanban",
    help=(
        "Kanban board — durable C-suite work queue. Cards flow "
        "BACKLOG → SPECIFY → READY → IN_PROGRESS → REVIEW → DONE. "
        "Mirrors the dashboard at /app/kanban + the TUI /kanban "
        "slash, exposed here for scripting + ad-hoc queries."
    ),
)
app.add_typer(kanban_app)


def _kanban_active_business(session) -> "Business":  # type: ignore[name-defined]
    """Resolve the founder's active business or exit."""
    from korpha.business.model import Business
    from sqlmodel import select as _select

    business = session.exec(_select(Business)).first()
    if business is None:
        typer.echo(_red("No business — onboard one first."))
        raise typer.Exit(code=1)
    return business


@kanban_app.command("list")
def kanban_list_cmd(
    column: Annotated[str | None, typer.Option(
        "--column", "-c",
        help="Filter to one column (backlog/specify/ready/in_progress/"
             "review/done/blocked). Default: all non-archived.",
    )] = None,
) -> None:
    """Show the kanban board (or one column)."""
    _ensure_load_env()
    from sqlmodel import Session

    from korpha.db._session import get_engine
    from korpha.kanban import KanbanBoard
    from korpha.kanban.model import KanbanColumn

    engine = get_engine()
    with Session(engine) as session:
        business = _kanban_active_business(session)
        board = KanbanBoard(session)
        if column:
            try:
                col = KanbanColumn(column.strip().lower())
            except ValueError:
                typer.echo(_red(
                    f"Unknown column {column!r}. Expected: "
                    + ", ".join(c.value for c in KanbanColumn)
                ))
                raise typer.Exit(code=1)
            cards = board.list_column(business.id, col)
            if not cards:
                typer.echo(_dim(f"({col.value}: empty)"))
                return
            typer.echo(f"{col.value.upper()} ({len(cards)})")
            for c in cards:
                _print_kanban_card_line(c)
            return

        snapshot = board.board_snapshot(business.id)
        any_printed = False
        for col, cards in snapshot.items():
            if not cards:
                continue
            any_printed = True
            typer.echo(_bold(f"\n{col.value.upper()} ({len(cards)})"))
            for c in cards:
                _print_kanban_card_line(c)
        if not any_printed:
            typer.echo(_dim(
                "Board is empty. Add a card with "
                "`korpha kanban add <title>`."
            ))


def _print_kanban_card_line(card) -> None:
    parts = [f"  {str(card.id)[:8]}"]
    if card.priority.value == "high":
        parts.append("[HI]")
    elif card.priority.value == "low":
        parts.append("[lo]")
    if card.owner_role:
        parts.append(f"[{card.owner_role.upper()}]")
    if card.acceptance_criteria:
        parts.append(f"[{len(card.acceptance_criteria)}c]")
    if card.review_evidence:
        parts.append("[ev✓]")
    if card.claimed_by_agent_role_id is not None:
        parts.append("[claimed]")
    parts.append(card.title)
    typer.echo(" ".join(parts))


@kanban_app.command("add")
def kanban_add_cmd(
    title: Annotated[str, typer.Argument(
        help="Short imperative — 'launch landing page'.",
    )],
    body: Annotated[str | None, typer.Option(
        "--body", "-b",
        help="Optional longer description / context.",
    )] = None,
    priority: Annotated[str, typer.Option(
        "--priority", "-p",
        help="high / normal / low. Default: normal.",
    )] = "normal",
    owner: Annotated[str | None, typer.Option(
        "--owner", "-o",
        help="Owner role: cto / cmo / coo. Optional at create time.",
    )] = None,
) -> None:
    """Add a card to the BACKLOG."""
    _ensure_load_env()
    from sqlmodel import Session

    from korpha.db._session import get_engine
    from korpha.kanban import (
        CreateCardInput, KanbanBoard, KanbanError,
    )
    from korpha.kanban.model import CardPriority

    title_clean = title.strip()
    if not title_clean:
        typer.echo(_red("Title required."))
        raise typer.Exit(code=1)
    if len(title_clean) > 200:
        typer.echo(_red("Title too long (>200 chars)."))
        raise typer.Exit(code=1)

    try:
        priority_val = CardPriority(priority.strip().lower())
    except ValueError:
        typer.echo(_red("Priority must be high/normal/low."))
        raise typer.Exit(code=1)

    owner_clean = owner.strip().lower() if owner else None
    if owner_clean and owner_clean not in ("cto", "cmo", "coo"):
        typer.echo(_red("Owner must be cto/cmo/coo."))
        raise typer.Exit(code=1)

    engine = get_engine()
    with Session(engine) as session:
        business = _kanban_active_business(session)
        board = KanbanBoard(session)
        try:
            card = board.create(CreateCardInput(
                business_id=business.id,
                title=title_clean,
                body=body or "",
                priority=priority_val,
                owner_role=owner_clean,
            ))
        except KanbanError as exc:
            typer.echo(_red(str(exc)))
            raise typer.Exit(code=1) from exc
        typer.echo(_green(
            f"✓ Added to BACKLOG: {card.title} ({str(card.id)[:8]})"
        ))


def _resolve_card_prefix(session, business_id, prefix: str):
    """Match a UUID prefix against this business's cards. Exits on
    no-match or multi-match."""
    from sqlmodel import select as _select

    from korpha.kanban.model import KanbanCard

    cards = list(session.exec(
        _select(KanbanCard).where(
            KanbanCard.business_id == business_id,
        )
    ).all())
    matches = [c for c in cards if str(c.id).startswith(prefix)]
    if not matches:
        typer.echo(_red(f"No card matches prefix {prefix!r}."))
        raise typer.Exit(code=1)
    if len(matches) > 1:
        typer.echo(_red(
            f"Prefix {prefix!r} matches {len(matches)} cards. "
            "Be more specific."
        ))
        raise typer.Exit(code=1)
    return matches[0]


@kanban_app.command("move")
def kanban_move_cmd(
    card_id: Annotated[str, typer.Argument(
        help="Card UUID (or unique prefix).",
    )],
    to_column: Annotated[str, typer.Argument(
        help="Target column: backlog/specify/ready/in_progress/"
             "review/done/blocked/archived.",
    )],
    note: Annotated[str | None, typer.Option(
        "--note", "-n",
        help="Optional rationale recorded in the audit log.",
    )] = None,
) -> None:
    """Move a card to a new column."""
    _ensure_load_env()
    from sqlmodel import Session

    from korpha.db._session import get_engine
    from korpha.kanban import KanbanBoard, KanbanError
    from korpha.kanban.model import KanbanColumn

    try:
        col = KanbanColumn(to_column.strip().lower())
    except ValueError:
        typer.echo(_red(
            f"Unknown column {to_column!r}. Expected: "
            + ", ".join(c.value for c in KanbanColumn)
        ))
        raise typer.Exit(code=1)

    engine = get_engine()
    with Session(engine) as session:
        business = _kanban_active_business(session)
        card = _resolve_card_prefix(session, business.id, card_id)
        board = KanbanBoard(session)
        try:
            moved = board.move(card.id, col, note=note)
        except KanbanError as exc:
            typer.echo(_red(str(exc)))
            raise typer.Exit(code=1) from exc
        typer.echo(_green(
            f"✓ {str(moved.id)[:8]} → {moved.column.value}"
        ))


@kanban_app.command("specify")
def kanban_specify_cmd(
    card_id: Annotated[str, typer.Argument(
        help="Card UUID (or unique prefix).",
    )],
    criteria: Annotated[list[str], typer.Option(
        "--criterion", "-c",
        help="Acceptance criterion (repeat the flag for multiple).",
    )],
    owner: Annotated[str | None, typer.Option(
        "--owner", "-o",
        help="Owner role: cto / cmo / coo. Required before READY.",
    )] = None,
) -> None:
    """Attach acceptance criteria + owner to a card."""
    _ensure_load_env()
    from sqlmodel import Session

    from korpha.db._session import get_engine
    from korpha.kanban import KanbanBoard, KanbanError

    if not criteria:
        typer.echo(_red(
            "At least one --criterion is required."
        ))
        raise typer.Exit(code=1)

    owner_clean = owner.strip().lower() if owner else None
    if owner_clean and owner_clean not in ("cto", "cmo", "coo"):
        typer.echo(_red("Owner must be cto/cmo/coo."))
        raise typer.Exit(code=1)

    engine = get_engine()
    with Session(engine) as session:
        business = _kanban_active_business(session)
        card = _resolve_card_prefix(session, business.id, card_id)
        board = KanbanBoard(session)
        try:
            spec = board.specify(
                card.id,
                acceptance_criteria=criteria,
                owner_role=owner_clean,
            )
        except KanbanError as exc:
            typer.echo(_red(str(exc)))
            raise typer.Exit(code=1) from exc
        typer.echo(_green(
            f"✓ Specified {str(spec.id)[:8]}: "
            f"{len(spec.acceptance_criteria)} criteria, "
            f"owner={spec.owner_role or 'unset'}, "
            f"column={spec.column.value}"
        ))


@kanban_app.command("archive")
def kanban_archive_cmd(
    card_id: Annotated[str, typer.Argument(
        help="Card UUID (or unique prefix) to archive.",
    )],
) -> None:
    """Archive a card (soft-delete; reversible via `kanban move <id> backlog`)."""
    _ensure_load_env()
    from sqlmodel import Session

    from korpha.db._session import get_engine
    from korpha.kanban import KanbanBoard, KanbanError
    from korpha.kanban.model import KanbanColumn

    engine = get_engine()
    with Session(engine) as session:
        business = _kanban_active_business(session)
        card = _resolve_card_prefix(session, business.id, card_id)
        board = KanbanBoard(session)
        try:
            board.move(card.id, KanbanColumn.ARCHIVED)
        except KanbanError as exc:
            typer.echo(_red(str(exc)))
            raise typer.Exit(code=1) from exc
        typer.echo(_red(f"✗ Archived {str(card.id)[:8]} ({card.title})"))


@kanban_app.command("dispatch-pending")
def kanban_dispatch_pending_cmd(
    stale_after_minutes: Annotated[int, typer.Option(
        "--stale-after",
        help="Skip cards with an auto_dispatch stamp newer than this.",
    )] = 30,
    max_cards: Annotated[int, typer.Option(
        "--max", help="Hard cap on cards dispatched this run.",
    )] = 12,
    force: Annotated[bool, typer.Option(
        "--force",
        help="Ignore stale-after — re-dispatch every IN_PROGRESS card.",
    )] = False,
) -> None:
    """Run IN_PROGRESS kanban cards through Workforce.dispatch.

    Used by the ``add-card-dispatcher`` cron preset (path 2 of the
    workforce auto-dispatch triggers). Also useful as a manual
    'kick the system' command when cards are stuck."""
    _ensure_load_env()
    import asyncio
    from sqlmodel import Session, select as _select

    from korpha.business.model import Business
    from korpha.cofounder.auto_dispatch import dispatch_pending_cards
    from korpha.db._session import get_engine
    from korpha.identity.model import Founder
    from korpha.inference.cost_tracker import CostTracker
    from korpha.inference import InferencePool
    from korpha.api.server import _build_pool_pieces

    engine = get_engine()
    providers_list, accounts_list = _build_pool_pieces()
    if not accounts_list:
        typer.echo(_red(
            "No inference provider configured. Run "
            "`aigenteur setup providers` first."
        ))
        raise typer.Exit(code=1)
    pool = InferencePool(providers=providers_list, accounts=accounts_list)
    tracker = CostTracker(pool=pool)

    with Session(engine) as session:
        business = session.exec(_select(Business)).first()
        if business is None:
            typer.echo(_red("No business found — onboard one first."))
            raise typer.Exit(code=1)
        founder = session.exec(_select(Founder)).first()
        if founder is None:
            typer.echo(_red("No founder found — run `aigenteur init`."))
            raise typer.Exit(code=1)

        summary = asyncio.run(dispatch_pending_cards(
            business=business, founder=founder,
            session=session, cost_tracker=tracker,
            stale_after_minutes=stale_after_minutes,
            max_cards=max_cards, force=force,
        ))

    n = summary.get("dispatched_count", 0)
    s = summary.get("skipped_count", 0)
    typer.echo(_green(
        f"✓ Dispatched {n} card(s); skipped {s}"
    ))
    if summary.get("error"):
        typer.echo(_red(f"  error: {summary['error']}"))
    for t in summary.get("tasks", [])[:5]:
        typer.echo(_dim(f"  • {t}"))


secret_app = typer.Typer(
    name="secret",
    help=(
        "Encrypted local secrets vault. Mike pastes API keys "
        "(Stripe / HeyGen / Replicate / etc.) once and skills "
        "reference them via ${secret:name} at call time. Lives "
        "at ~/.korpha/secrets/vault.json.enc, master key in "
        "~/.korpha/secrets/master.key (chmod 0600)."
    ),
)
app.add_typer(secret_app)


@secret_app.command("set")
def secret_set_cmd(
    name: Annotated[str, typer.Argument(
        help="Short name — 'stripe' / 'heygen' / 'replicate'.",
    )],
    value: Annotated[str | None, typer.Option(
        "--value",
        help="Secret value. If omitted, prompted (hidden).",
    )] = None,
    description: Annotated[str, typer.Option(
        "--desc", help="Optional description.",
    )] = "",
) -> None:
    """Store or overwrite a secret."""
    _ensure_load_env()
    from korpha.secrets import SecretStore

    if value is None:
        value = typer.prompt(
            f"Value for {name!r}",
            hide_input=True, confirmation_prompt=False,
        )
    if not value:
        typer.echo(_red("Value cannot be empty."))
        raise typer.Exit(code=1)

    try:
        SecretStore().set(name, value, description=description)
    except ValueError as exc:
        typer.echo(_red(str(exc)))
        raise typer.Exit(code=1) from exc
    typer.echo(_green(f"✓ Stored secret {name!r} ({len(value)} chars)"))


@secret_app.command("list")
def secret_list_cmd() -> None:
    """List secret names + lengths (never values)."""
    _ensure_load_env()
    from korpha.secrets import SecretStore

    rows = SecretStore().list()
    if not rows:
        typer.echo(_dim("(no secrets yet)"))
        return
    typer.echo(_bold(f"\n{len(rows)} secret(s)"))
    for r in rows:
        desc = (
            f" — {r['description']}" if r['description'] else ""
        )
        typer.echo(
            f"  {r['name']:<20}  {r['length']:>4} chars"
            + (f"  [{r['updated_at'][:10]}]" if r['updated_at'] else "")
            + desc
        )


@secret_app.command("delete")
def secret_delete_cmd(
    name: Annotated[str, typer.Argument(help="Secret name to remove.")],
) -> None:
    """Remove a secret permanently."""
    _ensure_load_env()
    from korpha.secrets import SecretStore

    if SecretStore().delete(name):
        typer.echo(_red(f"✗ Deleted {name!r}"))
    else:
        typer.echo(_yellow(f"No secret named {name!r}."))


budget_app = typer.Typer(
    name="budget",
    help=(
        "Spend caps with hard stops. Set a USD limit per business / "
        "agent / tier; when tripped, the next LLM call fails fast "
        "instead of running up your card. Resume manually when the "
        "founder is ready."
    ),
)
app.add_typer(budget_app)


def _budget_business(session) -> "Business":  # type: ignore[name-defined]
    from korpha.business.model import Business
    from sqlmodel import select as _select

    business = session.exec(_select(Business)).first()
    if business is None:
        typer.echo(_red("No business — onboard one first."))
        raise typer.Exit(code=1)
    return business


@budget_app.command("list")
def budget_list_cmd() -> None:
    """Show every policy + current usage + paused state."""
    _ensure_load_env()
    from sqlmodel import Session

    from korpha.budgets import BudgetService
    from korpha.db._session import get_engine

    engine = get_engine()
    with Session(engine) as session:
        business = _budget_business(session)
        rows = BudgetService(session).status(business.id)

    if not rows:
        typer.echo(_dim(
            "No budget policies. Add one with: "
            "korpha budget set --scope business --limit 5"
        ))
        return

    typer.echo(_bold(f"\n{len(rows)} budget policy(ies)"))
    for s in rows:
        p = s.policy
        scope_label = p.scope.value
        if p.scope.value == "agent_role" and p.agent_role_id:
            scope_label += f"({str(p.agent_role_id)[:8]})"
        elif p.scope.value == "tier" and p.tier:
            scope_label += f"({p.tier})"
        state = (
            _red(f" [paused: {p.paused_reason}]")
            if s.is_paused else ""
        )
        typer.echo(
            f"  {str(p.id)[:8]}  "
            f"{scope_label:<24}  "
            f"${float(s.spent_usd):>7.4f} / "
            f"${float(p.limit_usd):>7.4f} per {p.window.value}  "
            f"({s.pct_used * 100:.0f}%){state}"
        )
        if p.label:
            typer.echo(_dim(f"      {p.label}"))


@budget_app.command("set")
def budget_set_cmd(
    limit: Annotated[float, typer.Option(
        "--limit",
        help=(
            "Spend cap. Default unit is your display_currency. "
            "Pass --usd to enter in USD instead. Stored as USD."
        ),
    )],
    scope: Annotated[str, typer.Option(
        "--scope",
        help="business / business_unit / agent_role / tier",
    )] = "business",
    window: Annotated[str, typer.Option(
        "--window",
        help="hour / day / week / month",
    )] = "day",
    agent_role_id: Annotated[str | None, typer.Option(
        "--agent",
        help="When --scope=agent_role: target role UUID.",
    )] = None,
    unit: Annotated[str | None, typer.Option(
        "--unit",
        help=(
            "When --scope=business_unit: target Line name or UUID. "
            "Matches BusinessUnit.name (e.g. 'POD', 'KDP')."
        ),
    )] = None,
    tier: Annotated[str | None, typer.Option(
        "--tier",
        help="When --scope=tier: workhorse / pro / consultant / vision.",
    )] = None,
    label: Annotated[str, typer.Option(
        "--label", help="Friendly label for the dashboard.",
    )] = "",
    usd: Annotated[bool, typer.Option(
        "--usd",
        help="Treat --limit as raw USD instead of display currency.",
    )] = False,
) -> None:
    """Create a new budget policy. Default scope is 'business'."""
    _ensure_load_env()
    from decimal import Decimal as _D
    from sqlmodel import Session
    from uuid import UUID as _UUID

    from korpha.budgets import (
        BudgetScope, BudgetService, BudgetWindow,
    )
    from korpha.budgets.currency import display_to_usd
    from korpha.db._session import get_engine

    try:
        scope_val = BudgetScope(scope.strip().lower())
    except ValueError:
        typer.echo(_red(
            f"--scope must be business/business_unit/agent_role/tier; got {scope!r}"
        ))
        raise typer.Exit(code=1)
    try:
        window_val = BudgetWindow(window.strip().lower())
    except ValueError:
        typer.echo(_red(
            f"--window must be hour/day/week/month; got {window!r}"
        ))
        raise typer.Exit(code=1)
    if limit <= 0:
        typer.echo(_red("--limit must be > 0"))
        raise typer.Exit(code=1)

    limit_usd = _D(str(limit)) if usd else display_to_usd(_D(str(limit)))

    role_uuid: _UUID | None = None
    if agent_role_id:
        try:
            role_uuid = _UUID(agent_role_id)
        except ValueError:
            typer.echo(_red(f"bad --agent UUID: {agent_role_id}"))
            raise typer.Exit(code=1)

    engine = get_engine()
    with Session(engine) as session:
        business = _budget_business(session)
        unit_uuid: _UUID | None = None
        if unit:
            from korpha.business_units.context import resolve_unit_id
            unit_uuid = resolve_unit_id(session, business.id, unit)
            if unit_uuid is None:
                typer.echo(_red(f"no BusinessUnit matches {unit!r}"))
                raise typer.Exit(code=1)
        try:
            policy = BudgetService(session).create(
                business_id=business.id,
                scope=scope_val,
                window=window_val,
                limit_usd=limit_usd,
                agent_role_id=role_uuid,
                business_unit_id=unit_uuid,
                tier=tier,
                label=label,
            )
        except ValueError as exc:
            typer.echo(_red(str(exc)))
            raise typer.Exit(code=1) from exc
    typer.echo(_green(
        f"✓ Policy {str(policy.id)[:8]} created: "
        f"{scope_val.value} ${float(limit_usd):.4f} / {window_val.value}"
    ))


@budget_app.command("pause")
def budget_pause_cmd(
    policy_id: Annotated[str, typer.Argument(
        help="Policy UUID (or unique prefix from `budget list`).",
    )],
) -> None:
    """Pause a budget policy manually."""
    _ensure_load_env()
    _budget_apply_action(policy_id, action="pause")


@budget_app.command("resume")
def budget_resume_cmd(
    policy_id: Annotated[str, typer.Argument(
        help="Policy UUID (or prefix).",
    )],
) -> None:
    """Reactivate a paused policy with a fresh window."""
    _ensure_load_env()
    _budget_apply_action(policy_id, action="resume")


@budget_app.command("delete")
def budget_delete_cmd(
    policy_id: Annotated[str, typer.Argument(
        help="Policy UUID (or prefix) to remove permanently.",
    )],
) -> None:
    """Remove a policy."""
    _ensure_load_env()
    _budget_apply_action(policy_id, action="delete")


def _budget_apply_action(policy_id: str, *, action: str) -> None:
    from sqlmodel import Session
    from uuid import UUID as _UUID

    from korpha.budgets import BudgetService
    from korpha.budgets.model import BudgetPolicy
    from korpha.db._session import get_engine
    from sqlmodel import select as _select

    engine = get_engine()
    with Session(engine) as session:
        business = _budget_business(session)
        # Resolve full UUID from a prefix
        try:
            full_id = _UUID(policy_id)
        except ValueError:
            policies = list(session.exec(
                _select(BudgetPolicy).where(
                    BudgetPolicy.business_id == business.id,
                )
            ).all())
            matches = [
                p for p in policies
                if str(p.id).startswith(policy_id)
            ]
            if not matches:
                typer.echo(_red(
                    f"No policy matches prefix {policy_id!r}."
                ))
                raise typer.Exit(code=1)
            if len(matches) > 1:
                typer.echo(_red(
                    f"Prefix {policy_id!r} matches "
                    f"{len(matches)} policies; be more specific."
                ))
                raise typer.Exit(code=1)
            full_id = matches[0].id

        svc = BudgetService(session)
        try:
            if action == "pause":
                svc.pause(full_id)
                typer.echo(_yellow(
                    f"⏸ Paused policy {str(full_id)[:8]}"
                ))
            elif action == "resume":
                svc.resume(full_id)
                typer.echo(_green(
                    f"▶ Resumed policy {str(full_id)[:8]} "
                    "(window reset)"
                ))
            elif action == "delete":
                svc.delete(full_id)
                typer.echo(_red(
                    f"✗ Deleted policy {str(full_id)[:8]}"
                ))
        except KeyError as exc:
            typer.echo(_red(str(exc)))
            raise typer.Exit(code=1) from exc


team_app = typer.Typer(
    name="team",
    help=(
        "Manage your AI team — list, hire, fire workers. "
        "C-suite (CEO/CTO/CMO/COO) is auto-hired on demand; "
        "specialty workers (copywriter, designer, support) get "
        "hired explicitly when there's a recurring need."
    ),
)
app.add_typer(team_app)


def _team_active_business(session) -> "Business":  # type: ignore[name-defined]
    from korpha.business.model import Business
    from sqlmodel import select as _select

    business = session.exec(_select(Business)).first()
    if business is None:
        typer.echo(_red("No business — onboard one first."))
        raise typer.Exit(code=1)
    return business


@team_app.command("list")
def team_list_cmd(
    include_inactive: Annotated[bool, typer.Option(
        "--inactive",
        help="Include fired roles in the listing.",
    )] = False,
) -> None:
    """Show the current team (org chart)."""
    _ensure_load_env()
    from sqlmodel import Session, select as _select

    from korpha.cofounder.model import AgentRole
    from korpha.db._session import get_engine

    engine = get_engine()
    with Session(engine) as session:
        business = _team_active_business(session)
        stmt = (
            _select(AgentRole)
            .where(AgentRole.business_id == business.id)
        )
        if not include_inactive:
            stmt = stmt.where(AgentRole.is_active)
        rows = list(session.exec(stmt).all())
        if not rows:
            typer.echo(_dim("(team is empty)"))
            return

        # Group by role_type
        c_suite = [r for r in rows if r.role_type.value in (
            "ceo", "cto", "cmo", "coo", "chief_of_staff",
        )]
        workers = [r for r in rows if r.role_type.value == "worker"]

        if c_suite:
            typer.echo(_bold(f"\nC-suite ({len(c_suite)})"))
            for r in c_suite:
                state = "" if r.is_active else _red(" [fired]")
                typer.echo(
                    f"  {r.role_type.value.upper():>16}  "
                    f"{r.title}{state}"
                )

        if workers:
            typer.echo(_bold(f"\nWorkers ({len(workers)})"))
            for r in workers:
                state = "" if r.is_active else _red(" [fired]")
                spec = f" — {r.specialty}" if r.specialty else ""
                typer.echo(
                    f"  {str(r.id)[:8]}  {r.title}{spec}{state}"
                )


@team_app.command("hire")
def team_hire_cmd(
    specialty: Annotated[str, typer.Argument(
        help="Worker specialty — copywriter / designer / support / "
             "ads-manager / etc. Free-form, lowercase + hyphens.",
    )],
    title: Annotated[str | None, typer.Option(
        "--title", "-t",
        help="Friendly title. Defaults to title-cased specialty.",
    )] = None,
    reason: Annotated[str | None, typer.Option(
        "--reason", "-r",
        help="Why this hire — recorded in the audit log.",
    )] = None,
) -> None:
    """Hire a specialty worker."""
    _ensure_load_env()
    from sqlmodel import Session

    from korpha.cofounder.hiring import HiringService
    from korpha.cofounder.model import RoleType
    from korpha.db._session import get_engine

    spec = specialty.strip().lower()
    if not spec or " " in spec:
        typer.echo(_red(
            "Specialty must be one token (lowercase, hyphens). "
            f"Got {specialty!r}"
        ))
        raise typer.Exit(code=1)

    engine = get_engine()
    with Session(engine) as session:
        business = _team_active_business(session)
        hiring = HiringService(session)
        role = hiring.hire(
            business.id, RoleType.WORKER,
            title=title or spec.replace("-", " ").title(),
            specialty=spec,
            source=f"cli:hire:{reason[:80] if reason else 'manual'}",
        )
        typer.echo(_green(
            f"✓ Hired {role.title} ({spec}) — {str(role.id)[:8]}"
        ))


@team_app.command("fire")
def team_fire_cmd(
    role_id: Annotated[str, typer.Argument(
        help="Worker role UUID (or unique prefix from `team list`).",
    )],
    reason: Annotated[str | None, typer.Option(
        "--reason", "-r",
        help="Why this worker is being let go.",
    )] = None,
) -> None:
    """Fire a worker (refuses to fire C-suite — those need an
    explicit operator action)."""
    _ensure_load_env()
    from sqlmodel import Session, select as _select
    from uuid import UUID

    from korpha.cofounder.hiring import HiringService
    from korpha.cofounder.model import AgentRole, RoleType
    from korpha.db._session import get_engine

    engine = get_engine()
    with Session(engine) as session:
        business = _team_active_business(session)
        # Resolve prefix
        try:
            full_id = UUID(role_id)
            role = session.get(AgentRole, full_id)
        except ValueError:
            workers = list(session.exec(
                _select(AgentRole)
                .where(AgentRole.business_id == business.id)
                .where(AgentRole.role_type == RoleType.WORKER)
            ).all())
            matches = [w for w in workers if str(w.id).startswith(role_id)]
            if not matches:
                typer.echo(_red(f"No worker matches prefix {role_id!r}."))
                raise typer.Exit(code=1)
            if len(matches) > 1:
                typer.echo(_red(
                    f"Prefix {role_id!r} matches {len(matches)}; "
                    "be more specific."
                ))
                raise typer.Exit(code=1)
            role = matches[0]

        if role is None or role.business_id != business.id:
            typer.echo(_red("Role not found in this business."))
            raise typer.Exit(code=1)
        if role.role_type != RoleType.WORKER:
            typer.echo(_red(
                f"Refuses to fire role_type={role.role_type.value} "
                "via this command. Use `korpha fire <id>` for "
                "explicit C-suite termination."
            ))
            raise typer.Exit(code=1)

        hiring = HiringService(session)
        fired = hiring.fire(role.id, reason=reason)
        typer.echo(_red(
            f"✗ Fired {fired.title} ({fired.specialty})"
        ))


@app.command()
def liveness() -> None:
    """Report stuck kanban work — IDLE / REVIEW_OVERDUE / REWORK_LOOP.

    Read-only; doesn't move cards or notify channels. Use to find
    out what's wedged before opening /app/kanban for the cleanup."""
    _ensure_load_env()
    from sqlmodel import Session

    from korpha.business.model import Business
    from korpha.db._session import get_engine
    from korpha.liveness import classify_kanban_signals
    from sqlmodel import select as _select

    engine = get_engine()
    with Session(engine) as session:
        business = session.exec(_select(Business)).first()
        if business is None:
            typer.echo(_red("No business — onboard one first."))
            raise typer.Exit(code=1)
        signals = classify_kanban_signals(session, business.id)

    if not signals:
        typer.echo(_green(
            "✓ No stuck cards. Board is healthy."
        ))
        return

    crit = sum(1 for s in signals if s.severity == "critical")
    typer.echo(_bold(
        f"\n{len(signals)} stuck card(s) "
        + (f"({crit} critical)" if crit else "")
    ))
    for s in signals:
        marker = (
            _red("●") if s.severity == "critical" else _yellow("●")
        )
        kind = s.kind.value.replace("_", " ")
        typer.echo(
            f"  {marker} [{kind}] {s.title} "
            f"({s.age_hours:.1f}h)"
        )
        typer.echo(_dim(f"      {s.summary}"))


@app.command()
def review() -> None:
    """Run the monthly P&L + strategy review skill against your data
    and print the report. Pulls last 30 days of cost + revenue +
    kanban activity from the live DB; uses your configured Pro
    provider for the synthesis."""
    _ensure_load_env()
    from sqlmodel import Session

    from korpha.api.server import _build_pool_pieces
    from korpha.business.model import Business
    from korpha.cofounder.hiring import HiringService
    from korpha.db._session import get_engine
    from korpha.identity.model import Founder
    from korpha.inference import InferencePool
    from korpha.inference.cost_tracker import CostTracker
    from korpha.skills import default_registry
    from korpha.skills.types import SkillContext
    from sqlmodel import select as _select

    engine = get_engine()
    with Session(engine) as session:
        business = session.exec(_select(Business)).first()
        if business is None:
            typer.echo(_red("No business — onboard one first."))
            raise typer.Exit(code=1)
        founder = session.exec(_select(Founder)).first()
        if founder is None:
            typer.echo(_red("No founder — run `korpha init` first."))
            raise typer.Exit(code=1)

        providers, accounts = _build_pool_pieces()
        if not providers or not accounts:
            typer.echo(_red(
                "No provider configured. Run `korpha config` to "
                "set up an LLM provider before running review."
            ))
            raise typer.Exit(code=1)
        pool = InferencePool(providers=providers, accounts=accounts)
        tracker = CostTracker(pool=pool)
        ceo_role = HiringService(session).ensure_ceo(business.id)
        ctx = SkillContext(
            business=business, founder=founder, session=session,
            cost_tracker=tracker,
            invoking_agent_role_id=ceo_role.id,
        )
        skill = default_registry.skills["finance.monthly_review"]
        import asyncio as _aio
        result = _aio.run(skill.run(ctx=ctx, args={}))

    typer.echo(_bold(
        f"\n{result.payload['period_label']}: "
        f"{result.payload['headline']}"
    ))
    typer.echo(_dim(f"  Trend: {result.payload['trend']}"))
    metrics = result.payload.get("month_metrics") or {}
    if metrics:
        typer.echo(_dim(
            f"  Revenue ${metrics.get('revenue_usd', 0):.2f}, "
            f"Spend ${metrics.get('spend_usd', 0):.2f}, "
            f"Net ${metrics.get('net_usd', 0):.2f}, "
            f"Shipped {metrics.get('shipped_cards', 0)}"
        ))

    if result.payload.get("wins"):
        typer.echo(_bold("\nWins"))
        for w in result.payload["wins"]:
            typer.echo(f"  • {w}")
    if result.payload.get("concerns"):
        typer.echo(_bold("\nConcerns"))
        for c in result.payload["concerns"]:
            typer.echo(f"  • {c}")

    proposal = result.payload.get("strategy_proposal") or {}
    if proposal:
        typer.echo(_bold("\nStrategy proposal"))
        typer.echo(f"  Focus:  {proposal.get('next_month_focus', '')}")
        typer.echo(f"  KPI:    {proposal.get('kpi_target', '')}")
        for t in proposal.get("tasks") or []:
            typer.echo(f"  →       {t}")


pair_app = typer.Typer(
    name="pair",
    help=(
        "DM pairing — code-based authorization for new chat users. "
        "Used when a coworker / VA wants Telegram access without "
        "you editing YAML."
    ),
)
app.add_typer(pair_app)


@pair_app.command("approve")
def pair_approve(
    code: Annotated[str, typer.Argument(
        help="The 8-char code shared by the unknown user.",
    )],
) -> None:
    """Burn a pairing code, whitelist the user it was issued to."""
    _ensure_load_env()
    from korpha.identity.pairing import PairingStore

    store = PairingStore.load()
    ok, message = store.approve(code)
    if ok:
        typer.echo(_green(f"✓ {message}"))
    else:
        typer.echo(_red(message))
        raise typer.Exit(code=1)


@pair_app.command("pending")
def pair_pending() -> None:
    """List unburned codes (newest first). Useful for confirming
    'yes that code is real' when an unknown user shares one."""
    _ensure_load_env()
    from korpha.identity.pairing import PairingStore

    store = PairingStore.load()
    rows = store.list_pending()
    if not rows:
        typer.echo(_dim("No pending codes."))
        return
    import time as _time
    for p in rows:
        age = int(_time.time() - p.created_at)
        who = p.display_name or p.user_id
        typer.echo(
            f"  {_yellow(p.code)}  {p.platform}/{who}  "
            f"({age // 60}m old)"
        )


@pair_app.command("authorized")
def pair_authorized() -> None:
    """List who's currently authorized."""
    _ensure_load_env()
    from korpha.identity.pairing import PairingStore

    store = PairingStore.load()
    rows = store.list_authorized()
    if not rows:
        typer.echo(_dim("No authorized users yet."))
        return
    for platform, user_id in rows:
        typer.echo(f"  {platform:<10}  {user_id}")


@pair_app.command("revoke")
def pair_revoke(
    platform: Annotated[str, typer.Argument(
        help="Platform name (telegram, email, etc.).",
    )],
    user_id: Annotated[str, typer.Argument(
        help="The user identifier on that platform.",
    )],
) -> None:
    """Drop a previously-authorized user."""
    _ensure_load_env()
    from korpha.identity.pairing import PairingStore

    store = PairingStore.load()
    if store.revoke(platform, user_id):
        typer.echo(_green(f"✓ Revoked {platform}/{user_id}."))
    else:
        typer.echo(_yellow(f"{platform}/{user_id} was not authorized."))


curator_app = typer.Typer(
    name="curator",
    help=(
        "Manage agent-authored skills. List stale candidates, "
        "archive them, pin favorites, restore from archive."
    ),
)
app.add_typer(curator_app)


@curator_app.command("scan")
def curator_scan(
    stale_after_days: Annotated[int, typer.Option(
        "--stale-after-days",
        help="Skills unused this long become archive candidates.",
    )] = 30,
    min_uses: Annotated[int, typer.Option(
        "--min-uses",
        help="Below this lifetime use count → archive candidate.",
    )] = 3,
) -> None:
    """Dry-run: show which agent-authored skills would be archived."""
    _ensure_load_env()
    from korpha.skills.curator import find_stale

    cands = find_stale(
        stale_after_days=stale_after_days, min_uses=min_uses,
    )
    if not cands:
        typer.echo(_green(
            "No stale agent-authored skills. Curator has nothing to do."
        ))
        return
    typer.echo(_bold(f"{len(cands)} stale candidate(s):"))
    for c in cands:
        days = (
            f"{c.days_since_use:.0f}d"
            if c.days_since_use != float("inf") else "never"
        )
        typer.echo(
            f"  {_yellow(c.skill_name):<40}  {c.use_count} uses, "
            f"last {days} ago"
        )
    typer.echo(_dim(
        "\nRun `korpha curator archive <skill_name>` to archive "
        "individual skills, or `--apply` next iteration to bulk-archive."
    ))


@curator_app.command("archive")
def curator_archive(
    skill_name: Annotated[str, typer.Argument(
        help="Skill name (e.g. 'channel.teams_broadcast').",
    )],
) -> None:
    """Manually archive an agent-authored skill. Tar-gz's its source
    + drops it from the registry."""
    _ensure_load_env()
    from korpha.skills.curator import archive_skill

    path = archive_skill(skill_name)
    if path is None:
        typer.echo(_red(
            f"Could not archive {skill_name!r}. See logs for detail "
            "(not agent-authored? source dir missing? not registered?)."
        ))
        raise typer.Exit(code=1)
    typer.echo(_green(f"✓ Archived to {path}"))


@curator_app.command("archived")
def curator_archived() -> None:
    """List archive tarballs (newest first)."""
    _ensure_load_env()
    from korpha.skills.curator import list_archived

    rows = list_archived()
    if not rows:
        typer.echo(_dim("No archived skills."))
        return
    for path in rows:
        typer.echo(f"  {path.name}")


@curator_app.command("restore")
def curator_restore(
    archive_name: Annotated[str, typer.Argument(
        help="Archive filename (or stem). Use `archived` to list.",
    )],
) -> None:
    """Restore an archived skill back to agent_created/. Run
    `korpha server` (or re-import) to register it again."""
    _ensure_load_env()
    from korpha.skills.curator import restore_archived

    target = restore_archived(archive_name)
    if target is None:
        typer.echo(_red(f"Could not restore {archive_name!r}."))
        raise typer.Exit(code=1)
    typer.echo(_green(
        f"✓ Restored under {target}. Restart the server / re-import "
        "to re-register."
    ))


@curator_app.command("pin")
def curator_pin(
    skill_name: Annotated[str, typer.Argument(help="Skill name.")],
) -> None:
    """Pin a skill so the curator never archives it."""
    _ensure_load_env()
    from korpha.skills.curator import pin_skill
    pin_skill(skill_name)
    typer.echo(_green(f"✓ Pinned {skill_name}."))


@curator_app.command("unpin")
def curator_unpin(
    skill_name: Annotated[str, typer.Argument(help="Skill name.")],
) -> None:
    """Remove the pin so the curator can consider the skill again."""
    _ensure_load_env()
    from korpha.skills.curator import unpin_skill
    if unpin_skill(skill_name):
        typer.echo(_green(f"✓ Unpinned {skill_name}."))
    else:
        typer.echo(_yellow(
            f"{skill_name} has no usage record yet; nothing to unpin."
        ))


jobs_app = typer.Typer(
    name="jobs",
    help=(
        "Inspect background jobs (long-running Codex runs, etc.). "
        "Started via skills like `code.ship_via_codex` with wait=False."
    ),
)
app.add_typer(jobs_app)


@jobs_app.command("list")
def jobs_list(
    business: Annotated[str | None, typer.Option(
        "--business", help="Filter by business id (uuid).",
    )] = None,
) -> None:
    """List background jobs in the current process. The registry is
    in-memory — restart drops everything."""
    _ensure_load_env()
    from korpha.jobs import job_registry

    rows = job_registry.list(business_id=business)
    if not rows:
        typer.echo(_dim(
            "No jobs in flight or recently completed. "
            "(Registry is in-memory; restarts clear it.)"
        ))
        return
    for j in rows:
        dur = j.duration_seconds()
        dur_str = f"{dur:.1f}s" if dur is not None else "—"
        status_color = {
            "running": _yellow,
            "completed": _green,
            "failed": _red,
            "cancelled": _yellow,
            "pending": _dim,
        }.get(j.status.value, lambda s: s)
        typer.echo(
            f"  {status_color(j.status.value):<10} "
            f"{j.id} {dur_str:>8}  {j.label}"
        )


@jobs_app.command("status")
def jobs_status(
    job_id: Annotated[str, typer.Argument(help="Job id to inspect.")],
) -> None:
    """Show full detail for one job."""
    _ensure_load_env()
    from korpha.jobs import job_registry

    j = job_registry.get(job_id)
    if j is None:
        typer.echo(_yellow(f"job {job_id!r} not found"))
        raise typer.Exit(code=1)
    dur = j.duration_seconds()
    typer.echo(_bold(f"Job {j.id}"))
    typer.echo(f"  Label:    {j.label}")
    typer.echo(f"  Status:   {j.status.value}")
    if dur is not None:
        typer.echo(f"  Duration: {dur:.1f}s")
    if j.error:
        typer.echo(f"  Error:    {j.error}")
    if j.business_id:
        typer.echo(f"  Business: {j.business_id}")
    if j.extra:
        typer.echo("  Extra:")
        for k, v in j.extra.items():
            typer.echo(f"    {k}: {v}")


@jobs_app.command("cancel")
def jobs_cancel(
    job_id: Annotated[str, typer.Argument(
        help="Job id to cancel.",
    )],
) -> None:
    """Cooperatively cancel a running job."""
    _ensure_load_env()
    from korpha.jobs import job_registry

    cancelled = job_registry.cancel(job_id)
    if cancelled:
        typer.echo(_green(f"✓ Cancellation requested for {job_id}."))
    else:
        typer.echo(_yellow(
            f"Job {job_id!r} not running (unknown, already terminal, "
            "or no associated task)."
        ))


checkpoints_app = typer.Typer(
    name="checkpoints",
    help=(
        "Workspace snapshots taken before destructive Codex runs. "
        "Restore to undo a bad refactor without losing your seat."
    ),
)
app.add_typer(checkpoints_app)


@checkpoints_app.command("list")
def checkpoints_list(
    workspace: Annotated[Path, typer.Argument(
        help="Workspace directory whose checkpoints to list.",
    )],
) -> None:
    """List checkpoints for a workspace, newest first."""
    _ensure_load_env()
    from korpha.checkpoints import list_checkpoints

    cps = list_checkpoints(workspace)
    if not cps:
        typer.echo(_yellow(
            f"No checkpoints for {workspace}. They're created "
            "automatically before each Codex run."
        ))
        return
    for cp in cps:
        size_kb = cp.size_bytes / 1024
        size_str = (
            f"{size_kb / 1024:.1f}MB" if size_kb >= 1024
            else f"{size_kb:.1f}KB"
        )
        label = cp.label or "(no label)"
        typer.echo(
            f"  {_green(cp.id)}  {cp.created_at}  "
            f"{cp.file_count} files / {size_str}  — {label}"
        )


@checkpoints_app.command("restore")
def checkpoints_restore(
    snapshot_id: Annotated[str, typer.Argument(
        help="Checkpoint id to restore. Get it from `checkpoints list`.",
    )],
    workspace: Annotated[Path, typer.Argument(
        help="Workspace directory to restore into.",
    )],
    skip_pre_snapshot: Annotated[bool, typer.Option(
        "--skip-pre-snapshot",
        help="Don't auto-snapshot the current state before restoring.",
    )] = False,
) -> None:
    """Extract a snapshot's tarball over the workspace, replacing
    the current state. By default takes a "pre-restore" snapshot
    first so you can redo the restore if needed."""
    _ensure_load_env()
    from korpha.checkpoints import CheckpointError, restore

    try:
        pre = restore(
            workspace,
            snapshot_id,
            auto_pre_snapshot=not skip_pre_snapshot,
        )
    except CheckpointError as exc:
        typer.echo(_red(str(exc)))
        raise typer.Exit(code=1) from exc
    typer.echo(_green(
        f"✓ Restored {snapshot_id} into {workspace}."
    ))
    if not skip_pre_snapshot:
        typer.echo(_dim(
            f"  To undo this restore: "
            f"korpha checkpoints restore {pre.id} {workspace}"
        ))


audit_app = typer.Typer(
    name="audit",
    help=(
        "Audit log retention. Old Activity + Cost rows accumulate "
        "fast; this lets you archive them to compressed JSONL on "
        "disk and delete them from the live DB so insights queries "
        "stay fast."
    ),
)
app.add_typer(audit_app)


@audit_app.command("archive")
def audit_archive(
    days_keep: Annotated[int, typer.Option(
        "--days-keep",
        help="Rows newer than this stay in the live DB. Default: 180.",
    )] = 180,
    dry_run: Annotated[bool, typer.Option(
        "--dry-run",
        help="Show what would be archived without writing or deleting.",
    )] = False,
) -> None:
    """Archive Activity + Cost rows older than --days-keep.

    Archive files land at ``~/.korpha/archive/<table>-YYYY-MM.jsonl.gz``,
    one per month, gzipped + append-only. The DB rows are deleted
    after successful archive (use --dry-run to preview)."""
    _ensure_load_env()
    from sqlmodel import Session

    from korpha.audit.retention import (
        archive_activity, archive_cost,
    )
    from korpha.db._session import get_engine

    if days_keep < 1:
        typer.echo(_red("--days-keep must be >= 1"))
        raise typer.Exit(code=1)

    engine = get_engine()
    with Session(engine) as session:
        stats_a = archive_activity(
            session, days_keep=days_keep, delete_after=not dry_run,
        )
        stats_c = archive_cost(
            session, days_keep=days_keep, delete_after=not dry_run,
        )
    typer.echo(_bold("Audit archive"))
    typer.echo(
        f"  Activity: {stats_a.rows_archived:>6} rows  "
        f"({_human_bytes(stats_a.bytes_written)} on disk, "
        f"{len(stats_a.months_touched)} month files)"
    )
    typer.echo(
        f"  Cost:     {stats_c.rows_archived:>6} rows  "
        f"({_human_bytes(stats_c.bytes_written)} on disk, "
        f"{len(stats_c.months_touched)} month files)"
    )
    if dry_run:
        typer.echo(_dim(
            "  --dry-run: no rows deleted from the live DB."
        ))
    else:
        if stats_a.rows_archived or stats_c.rows_archived:
            typer.echo(_green(
                "  ✓ Live DB rows deleted. Run "
                "`korpha disk vacuum` to reclaim the freed space."
            ))
        else:
            typer.echo(_dim("  Nothing older than the cutoff."))


disk_app = typer.Typer(
    name="disk",
    help=(
        "Disk usage report + vacuum. Tells you where Korpha "
        "data is sitting (DB, checkpoint blobs, agent-authored "
        "skills, cron scripts, job outputs) and reclaims space "
        "from orphan checkpoint blobs + sqlite slack."
    ),
)
app.add_typer(disk_app)


def _human_bytes(n: int) -> str:
    """Format ``n`` bytes as a short human string ('1.2 GB')."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            if unit == "B":
                return f"{n} {unit}"
            return f"{n / 1024**(['B','KB','MB','GB','TB'].index(unit)):.1f} {unit}"
    return f"{n} B"


def _dir_size(path: Path) -> int:
    """Recursive byte-sum. Best-effort — broken symlinks etc.
    are silently ignored; the report shows what we *can* read."""
    total = 0
    if not path.is_dir():
        return 0
    for sub in path.rglob("*"):
        try:
            if sub.is_file():
                total += sub.stat().st_size
        except OSError:
            continue
    return total


def _data_root() -> Path:
    base = os.environ.get("KORPHA_DATA_DIR")
    return Path(base) if base else (Path.home() / ".korpha")


def _collect_disk_stats() -> list[tuple[str, int, str]]:
    """Snapshot of every Korpha-owned directory + the DB file.
    Returns a list of (label, bytes, location) tuples ordered for
    display."""
    rows: list[tuple[str, int, str]] = []
    root = _data_root()

    # Main DB
    try:
        from korpha.config import get_settings

        db_url = get_settings().db_url
        if db_url.startswith("sqlite:///"):
            db_path = Path(db_url[len("sqlite:///"):])
            if db_path.is_file():
                rows.append((
                    "Main DB (sqlite)",
                    db_path.stat().st_size,
                    str(db_path),
                ))
    except Exception:  # noqa: BLE001
        pass

    # Checkpoints — separate v2 blob store from per-workspace dirs
    from korpha.checkpoints.v2 import disk_breakdown
    try:
        bd = disk_breakdown()
        rows.append((
            f"Checkpoint blobs ({bd['blob_count']} files)",
            bd["blob_bytes"],
            str(root / "checkpoints" / "blobs"),
        ))
        for w in bd["workspaces"]:
            rows.append((
                f"Workspace '{w['slug']}' "
                f"(v1: {w['v1_count']}, v2: {w['v2_count']})",
                w["v1_bytes"] + w["manifest_bytes"],
                str(root / "checkpoints" / w["slug"]),
            ))
    except Exception:  # noqa: BLE001
        pass

    # Agent-authored skills
    skills_dir = root / "skills"
    if skills_dir.is_dir():
        rows.append((
            "Agent-authored skills",
            _dir_size(skills_dir),
            str(skills_dir),
        ))

    # Cron scripts
    cron_dir = root / "cron-scripts"
    if cron_dir.is_dir():
        rows.append((
            "Cron scripts",
            _dir_size(cron_dir),
            str(cron_dir),
        ))

    # Job logs
    jobs_dir = root / "jobs"
    if jobs_dir.is_dir():
        rows.append((
            "Background job logs",
            _dir_size(jobs_dir),
            str(jobs_dir),
        ))

    # Audit archive
    archive_dir = root / "archive"
    if archive_dir.is_dir():
        try:
            from korpha.audit.retention import archive_size_breakdown

            ab = archive_size_breakdown()
            if ab["total_bytes"] > 0:
                rows.append((
                    f"Audit archive ({len(ab['files'])} files)",
                    ab["total_bytes"],
                    str(archive_dir),
                ))
        except Exception:  # noqa: BLE001
            pass

    return rows


@disk_app.command("show", help="Show disk usage breakdown (default).")
@disk_app.callback(invoke_without_command=True)
def disk_show(ctx: typer.Context) -> None:
    """Print a per-area breakdown of Korpha-owned disk usage."""
    if ctx.invoked_subcommand is not None:
        return
    _ensure_load_env()
    rows = _collect_disk_stats()
    if not rows:
        typer.echo(_dim("(no Korpha data on disk yet)"))
        return
    total = sum(r[1] for r in rows)
    typer.echo(_bold(f"Korpha disk usage: {_human_bytes(total)}"))
    typer.echo("")
    label_width = max(len(r[0]) for r in rows) + 2
    for label, n, loc in rows:
        typer.echo(
            f"  {label.ljust(label_width)} {_human_bytes(n).rjust(10)}"
        )
        typer.echo(_dim(f"    {loc}"))
    typer.echo("")
    typer.echo(_dim(
        "Run `korpha disk vacuum` to reclaim space from orphan "
        "checkpoint blobs + sqlite slack."
    ))


@disk_app.command("vacuum")
def disk_vacuum(
    skip_db: Annotated[bool, typer.Option(
        "--skip-db", help="Skip the sqlite VACUUM (saves time on big DBs)",
    )] = False,
) -> None:
    """Reclaim disk: GC orphan checkpoint blobs + sqlite VACUUM."""
    _ensure_load_env()
    from korpha.checkpoints.v2 import vacuum as v2_vacuum

    typer.echo("Vacuuming checkpoint blob store…")
    stats = v2_vacuum()
    typer.echo(_green(
        f"  ✓ {stats['blobs_deleted']} orphan blobs removed, "
        f"{stats['tmp_swept']} tmp files swept, "
        f"{_human_bytes(stats['bytes_reclaimed'])} reclaimed"
    ))
    typer.echo(_dim(
        f"  ({stats['blobs_kept']} blobs kept — referenced by manifests)"
    ))

    if skip_db:
        typer.echo(_dim("Skipping sqlite VACUUM (--skip-db)."))
        return

    try:
        from korpha.config import get_settings

        db_url = get_settings().db_url
        if not db_url.startswith("sqlite:///"):
            typer.echo(_dim(
                f"VACUUM only runs on sqlite (got {db_url[:60]}…). "
                "Postgres reclaims via autovacuum."
            ))
            return
        db_path = Path(db_url[len("sqlite:///"):])
        before = db_path.stat().st_size if db_path.is_file() else 0
        typer.echo(f"Running sqlite VACUUM on {db_path.name}…")
        import sqlite3
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("VACUUM")
            conn.commit()
        finally:
            conn.close()
        after = db_path.stat().st_size if db_path.is_file() else 0
        reclaimed = max(0, before - after)
        typer.echo(_green(
            f"  ✓ DB shrank by {_human_bytes(reclaimed)} "
            f"({_human_bytes(before)} → {_human_bytes(after)})"
        ))
    except Exception as exc:  # noqa: BLE001
        typer.echo(_yellow(f"  ! sqlite VACUUM failed: {exc}"))


@checkpoints_app.command("migrate")
def checkpoints_migrate(
    dry_run: Annotated[bool, typer.Option(
        "--dry-run",
        help="Re-pack v1 → v2 but keep the originals so you can verify "
             "before committing the disk reclaim.",
    )] = False,
) -> None:
    """Re-pack legacy v1 tar.gz checkpoints into v2 dedup blobs.

    Each v1 ``<id>.tar.gz`` becomes a v2 ``<id>.v2.json`` manifest
    pointing at the shared blob store. Identical files across
    snapshots collapse to one blob. Drops the v1 originals once
    the v2 manifest lands (unless --dry-run).
    """
    _ensure_load_env()
    from korpha.checkpoints.v2 import migrate_v1_to_v2

    typer.echo("Migrating v1 tar.gz checkpoints to v2 dedup blobs…")
    stats = migrate_v1_to_v2(delete_originals=not dry_run)
    if stats["migrated"] == 0 and stats["skipped"] == 0:
        typer.echo(_dim(
            "Nothing to migrate (no v1 snapshots found)."
        ))
        return
    typer.echo(_green(
        f"  ✓ Migrated {stats['migrated']} snapshot(s) "
        f"({stats['skipped']} already v2, {stats['failed']} failed)"
    ))
    if dry_run:
        typer.echo(_dim(
            "  --dry-run: original tar.gz files kept. "
            "Re-run without --dry-run to reclaim disk."
        ))
    else:
        typer.echo(_dim(
            f"  Reclaimed {_human_bytes(stats['bytes_freed'])} "
            "of v1 archives."
        ))


@checkpoints_app.command("prune")
def checkpoints_prune(
    workspace: Annotated[Path, typer.Argument(
        help="Workspace directory whose checkpoints to prune.",
    )],
    keep_last: Annotated[int, typer.Option(
        "--keep-last",
        help="How many most-recent checkpoints to retain.",
    )] = 20,
) -> None:
    """Remove the oldest checkpoints beyond ``--keep-last``."""
    _ensure_load_env()
    from korpha.checkpoints import prune

    removed = prune(workspace, keep_last=keep_last)
    typer.echo(
        _green(f"✓ Removed {removed} old checkpoint(s).")
        if removed
        else _dim(f"Nothing to prune (under cap of {keep_last}).")
    )


def _parse_since(raw: str) -> "datetime | None":
    """Accept ``1h`` / ``15m`` / ``7d`` / ISO 8601. Returns timezone-
    aware UTC datetime or None if unparseable."""
    from datetime import datetime, timedelta, timezone

    raw = raw.strip()
    # Relative: <number><suffix>
    if raw and raw[-1].lower() in ("s", "m", "h", "d") and raw[:-1].isdigit():
        n = int(raw[:-1])
        unit = raw[-1].lower()
        delta = {
            "s": timedelta(seconds=n),
            "m": timedelta(minutes=n),
            "h": timedelta(hours=n),
            "d": timedelta(days=n),
        }[unit]
        return datetime.now(tz=timezone.utc) - delta
    # ISO 8601
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Business unit ops — PR12 surface bundle
# ---------------------------------------------------------------------------


unit_app = typer.Typer(
    name="unit",
    help="Manage BusinessUnits (Lines / Types / Audiences / Product VPs).",
)
app.add_typer(unit_app, name="unit")


@unit_app.command("list")
def unit_list_cmd() -> None:
    """List BusinessUnits for the active business."""
    from sqlmodel import Session, select
    from korpha.business_units.model import BusinessUnit
    engine = _get_business_unit_engine()
    with Session(engine) as session:
        units = list(session.exec(select(BusinessUnit)).all())
        if not units:
            typer.echo(
                "No units. Run `korpha unit start-line <kind>` to begin."
            )
            return
        typer.echo(f"{'KIND':<12} {'NAME':<30} {'STATUS':<10} ID")
        for u in units:
            typer.echo(
                f"{u.kind.value:<12} {u.name[:30]:<30} {u.status:<10} {u.id}"
            )


@unit_app.command("show")
def unit_show_cmd(unit_id: str) -> None:
    """Show a unit's details."""
    from sqlmodel import Session
    from korpha.business_units.model import BusinessUnit
    from uuid import UUID as _U
    engine = _get_business_unit_engine()
    with Session(engine) as session:
        unit = session.get(BusinessUnit, _U(unit_id))
        if unit is None:
            typer.echo(f"unit {unit_id} not found", err=True)
            raise typer.Exit(1)
        typer.echo(f"Name:           {unit.name}")
        typer.echo(f"Kind:           {unit.kind.value}")
        typer.echo(f"Slug:           {unit.slug}")
        typer.echo(f"Status:         {unit.status}")
        typer.echo(f"Parent:         {unit.parent_id or '(root)'}")
        typer.echo(f"Namespace:      {unit.memory_namespace_id}")
        typer.echo(f"Playbook:       {unit.playbook_skill_pack or '(none)'}")
        if unit.niche_profile:
            typer.echo(f"Niche profile:  {unit.niche_profile}")


@unit_app.command("pause")
def unit_pause_cmd(unit_id: str, reason: str = "") -> None:
    """Pause a unit. Blocks new card claims."""
    from sqlmodel import Session
    from korpha.business_units.board import (
        BusinessUnitBoard, BusinessUnitError,
    )
    from uuid import UUID as _U
    engine = _get_business_unit_engine()
    with Session(engine) as session:
        try:
            unit = BusinessUnitBoard(session).pause(
                _U(unit_id), reason=reason or None,
            )
        except BusinessUnitError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
        typer.echo(f"Paused {unit.name}")


@unit_app.command("resume")
def unit_resume_cmd(unit_id: str) -> None:
    """Resume a paused unit."""
    from sqlmodel import Session
    from korpha.business_units.board import (
        BusinessUnitBoard, BusinessUnitError,
    )
    from uuid import UUID as _U
    engine = _get_business_unit_engine()
    with Session(engine) as session:
        try:
            unit = BusinessUnitBoard(session).resume(_U(unit_id))
        except BusinessUnitError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
        typer.echo(f"Resumed {unit.name}")


@unit_app.command("archive")
def unit_archive_cmd(
    unit_id: str,
    cascade: bool = typer.Option(
        False, "--cascade",
        help="Archive descendants too (cascade).",
    ),
) -> None:
    """Archive a unit. Refuses with live children unless --cascade."""
    from sqlmodel import Session
    from korpha.business_units.board import (
        BusinessUnitBoard, BusinessUnitError,
    )
    from uuid import UUID as _U
    engine = _get_business_unit_engine()
    with Session(engine) as session:
        board = BusinessUnitBoard(session)
        try:
            if cascade:
                archived = board.archive_subtree(_U(unit_id))
                typer.echo(f"Archived {len(archived)} units (cascade).")
            else:
                unit = board.archive(_U(unit_id))
                typer.echo(f"Archived {unit.name}")
        except BusinessUnitError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc


@unit_app.command("backup")
def unit_backup_cmd(unit_id: str) -> None:
    """Back up a unit's filesystem subtree + DB rows to a tar.gz."""
    from sqlmodel import Session
    from korpha.business_units.filesystem import backup_unit
    from uuid import UUID as _U
    engine = _get_business_unit_engine()
    with Session(engine) as session:
        try:
            out = backup_unit(session, _U(unit_id))
        except ValueError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(1) from exc
    typer.echo(f"Wrote backup: {out}")


def _get_business_unit_engine():
    """Get a configured DB engine for the CLI unit commands.

    Reads KORPHA_DB_URL or falls back to the local sqlite at
    ~/.korpha/korpha.db. Ensures all tables exist via create_all
    so the unit commands work in fresh installs that haven't run
    alembic yet."""
    import os as _os
    from sqlmodel import create_engine, SQLModel
    import korpha.db.registry  # noqa: F401
    db_url = _os.environ.get(
        "KORPHA_DB_URL",
        f"sqlite:///{_os.path.expanduser('~/.korpha/korpha.db')}",
    )
    engine = create_engine(
        db_url, connect_args={"check_same_thread": False}
        if db_url.startswith("sqlite") else {},
    )
    SQLModel.metadata.create_all(engine)
    return engine


# ============================================================================
# korpha backup  — local snapshots + restore + retention
# ============================================================================


backups_app = typer.Typer(
    name="backups",
    help=(
        "Local rotating backups. Hourly DB snapshots + daily full "
        "bundles + GFS retention. Layer 1 protection (accidental "
        "delete, bad migration). For disk-death protection add an "
        "off-disk push: `korpha backups setup-litestream` (S3) or "
        "`korpha backups setup-rclone` (Dropbox/GDrive). The "
        "single-shot legacy `korpha backup` command still works."
    ),
)
app.add_typer(backups_app)


@backups_app.command("snapshot")
def backup_snapshot(
    full: Annotated[bool, typer.Option(
        "--full", help="Take a full tar.gz bundle (default: db only).",
    )] = False,
) -> None:
    """Take one snapshot now. Idempotent — safe to run by hand
    between cron firings."""
    from korpha.backup import take_db_snapshot, take_full_backup

    if full:
        info = take_full_backup()
        kind = "full bundle"
    else:
        info = take_db_snapshot()
        kind = "db snapshot"
    typer.echo(_green("✓") + f" {kind}: {info.path}")
    typer.echo(_dim(f"  size: {info.size_bytes:,} bytes"))


@backups_app.command("list")
def backup_list() -> None:
    """List every snapshot + bundle on disk, newest first."""
    from korpha.backup import BackupKind, list_backups

    items = list_backups()
    if not items:
        typer.echo(_dim("(no backups yet — run `korpha backup snapshot`)"))
        return
    by_kind: dict[str, list] = {}
    for b in items:
        by_kind.setdefault(b.kind.value, []).append(b)
    for k, lst in by_kind.items():
        typer.echo(_bold(f"{k} ({len(lst)})"))
        for b in lst:
            age_h = int(b.age.total_seconds() / 3600)
            typer.echo(
                f"  {b.filename}  "
                + _dim(f"{b.size_bytes:>10,} bytes  · {age_h}h ago")
            )


@backups_app.command("restore")
def backup_restore(
    snapshot: Annotated[str, typer.Argument(
        help="Snapshot filename or timestamp (e.g. 20260512T180000Z).",
    )],
    yes: Annotated[bool, typer.Option(
        "--yes", "-y",
        help="Skip the 'are you sure' prompt. STOP the server first.",
    )] = False,
) -> None:
    """Replace the live DB with the named snapshot.

    Stop the server BEFORE running this. A safety copy of the
    current DB is auto-saved as ``korpha.db.before-restore.<ts>``.
    """
    from korpha.backup import restore_db_snapshot

    if not yes:
        confirm = typer.confirm(
            f"Replace live DB with snapshot {snapshot!r}?"
        )
        if not confirm:
            typer.echo("aborted.")
            raise typer.Exit(code=1)
    target = restore_db_snapshot(snapshot)
    typer.echo(_green("✓") + f" restored: {target}")
    typer.echo(_dim("  start the server to resume."))


@backups_app.command("prune")
def backup_prune(
    hourly: Annotated[int, typer.Option(
        "--hourly", help="Keep last N hourly snapshots.",
    )] = 24,
    daily: Annotated[int, typer.Option(
        "--daily", help="Keep last N daily snapshots.",
    )] = 7,
    weekly: Annotated[int, typer.Option(
        "--weekly", help="Keep last N weekly snapshots.",
    )] = 4,
    monthly: Annotated[int, typer.Option(
        "--monthly", help="Keep last N monthly snapshots.",
    )] = 12,
) -> None:
    """Apply GFS retention. Safe to run any time — only deletes
    what falls outside every bucket."""
    from korpha.backup import apply_retention
    from korpha.backup.snapshot import RetentionPolicy

    result = apply_retention(policy=RetentionPolicy(
        hourly=hourly, daily=daily, weekly=weekly, monthly=monthly,
    ))
    typer.echo(
        _green("✓")
        + f" retained {result['kept']}, deleted {result['deleted']}"
    )


@backups_app.command("install-litestream")
def backup_install_litestream() -> None:
    """Download the pinned litestream binary into ~/.local/bin.

    Idempotent. Verifies SHA-256 on supported platforms. Mike-friendly
    alternative to the manual curl-and-tar dance in the litestream
    docs. The dashboard's /app/backups page invokes the same logic
    via POST /app/backups/install-litestream.
    """
    from korpha.backup.install import install_litestream

    typer.echo(_dim("downloading litestream release..."))
    result = install_litestream()
    if result.ok:
        typer.echo(_green(f"✓ {result.message}"))
        if result.path is not None:
            typer.echo(_dim(
                f"  add this to your shell rc if not already there:\n"
                f"    export PATH=\"$HOME/.local/bin:$PATH\""
            ))
    else:
        typer.echo(_red(f"✗ {result.message}"))
        raise typer.Exit(code=1)


@backups_app.command("setup-litestream")
def backup_setup_litestream(
    bucket: Annotated[str, typer.Option(
        "--bucket", help="S3 bucket name (or R2/B2/MinIO bucket).",
    )] = "",
    endpoint: Annotated[str, typer.Option(
        "--endpoint",
        help=(
            "S3 endpoint URL. Leave blank for AWS S3. Examples: "
            "'https://<account>.r2.cloudflarestorage.com' for R2, "
            "'https://s3.<region>.backblazeb2.com' for B2."
        ),
    )] = "",
    region: Annotated[str, typer.Option(
        "--region", help="S3 region. Default: us-east-1.",
    )] = "us-east-1",
    access_key_id: Annotated[str, typer.Option(
        "--access-key-id", prompt=True, hide_input=True,
        help="S3 access key id (will be stored in the encrypted vault).",
    )] = "",
    secret_access_key: Annotated[str, typer.Option(
        "--secret-access-key", prompt=True, hide_input=True,
        help="S3 secret (will be stored in the encrypted vault).",
    )] = "",
) -> None:
    """Wire continuous SQLite WAL replication to S3/R2/B2/MinIO via
    Litestream. Writes ``litestream.yml`` + a runner script in the
    data dir; you run the runner under systemd / supervisord / a
    plain background shell.

    Requires the ``litestream`` binary on PATH:
        curl -fsSL https://github.com/benbjohnson/litestream/releases/latest/download/litestream-linux-amd64.tar.gz | sudo tar -C /usr/local/bin -xzf -
    """
    import shutil as _shutil
    import json as _json
    from korpha.secrets.crypto import encrypt_bytes, load_master_key

    if _shutil.which("litestream") is None:
        typer.echo(_red("litestream not on $PATH"))
        typer.echo(_dim(
            "  install: curl -fsSL https://github.com/benbjohnson/"
            "litestream/releases/latest/download/litestream-linux-amd64.tar.gz "
            "| sudo tar -C /usr/local/bin -xzf -"
        ))
        raise typer.Exit(code=1)
    if not bucket:
        typer.echo(_red("--bucket required"))
        raise typer.Exit(code=1)

    data_dir = _data_dir()
    key_path = data_dir / "secrets" / "master.key"
    master = load_master_key(key_path)
    creds_blob = encrypt_bytes(
        _json.dumps({
            "access_key_id": access_key_id,
            "secret_access_key": secret_access_key,
        }, separators=(",", ":")).encode("utf-8"),
        master,
    )
    creds_file = data_dir / "secrets" / "litestream-s3.creds.enc"
    creds_file.write_bytes(creds_blob)
    creds_file.chmod(0o600)

    config_path = data_dir / "litestream.yml"
    db_path = data_dir / "korpha.db"
    endpoint_line = f"      endpoint: {endpoint}\n" if endpoint else ""
    config_path.write_text(
        "dbs:\n"
        f"  - path: {db_path}\n"
        "    replicas:\n"
        f"      - url: s3://{bucket}/korpha.db\n"
        f"        region: {region}\n"
        + endpoint_line +
        "        access-key-id: $LITESTREAM_ACCESS_KEY_ID\n"
        "        secret-access-key: $LITESTREAM_SECRET_ACCESS_KEY\n"
    )
    config_path.chmod(0o600)

    runner_path = data_dir / "litestream-run.sh"
    runner_path.write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        f"# Decrypt S3 creds, export, exec litestream.\n"
        f"export LITESTREAM_ACCESS_KEY_ID=\"$(korpha secrets dump litestream-s3 access_key_id 2>/dev/null)\"\n"
        f"export LITESTREAM_SECRET_ACCESS_KEY=\"$(korpha secrets dump litestream-s3 secret_access_key 2>/dev/null)\"\n"
        f"exec litestream replicate -config {config_path}\n"
    )
    runner_path.chmod(0o755)

    typer.echo(_green("✓") + " litestream configured")
    typer.echo(_dim(f"  config:  {config_path}"))
    typer.echo(_dim(f"  runner:  {runner_path}"))
    typer.echo(_dim(f"  bucket:  s3://{bucket}/korpha.db"))
    typer.echo()
    typer.echo(
        "Start the replicator in a separate shell (or under systemd):\n"
        f"  {runner_path}\n\n"
        "To restore from S3 on a new machine:\n"
        f"  litestream restore -o ~/.korpha/korpha.db s3://{bucket}/korpha.db"
    )


@backups_app.command("setup-rclone")
def backup_setup_rclone(
    remote: Annotated[str, typer.Option(
        "--remote",
        help=(
            "rclone remote name + path, e.g. 'dropbox:korpha-backup' "
            "or 'gdrive:Backups/Korpha'. Run `rclone config` first to "
            "register the remote."
        ),
    )] = "",
    every: Annotated[str, typer.Option(
        "--every", help="Push cadence: 'every 1h' / 'every 6h'.",
    )] = "every 1h",
) -> None:
    """Push every fresh backup to a configured rclone remote
    (Dropbox / Google Drive / OneDrive / etc.). Reuses storage you
    already pay for. Requires ``rclone`` + a configured remote."""
    import shutil as _shutil
    from korpha.scriptcron import parse_cadence
    from korpha.scriptcron.model import ScriptCron
    from sqlmodel import Session, select

    if _shutil.which("rclone") is None:
        typer.echo(_red("rclone not on $PATH"))
        typer.echo(_dim("  install: sudo apt install rclone"))
        raise typer.Exit(code=1)
    if not remote or ":" not in remote:
        typer.echo(_red("--remote required (e.g. 'dropbox:korpha-backup')"))
        raise typer.Exit(code=1)

    data_dir = _data_dir()
    cron_dir = data_dir / "cron-scripts"
    cron_dir.mkdir(parents=True, exist_ok=True)
    script_path = cron_dir / "backup-rclone-push.sh"
    script_path.write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        f"# Push the latest snapshots + bundles to {remote}\n"
        f"BACKUPS_DIR={data_dir}/backups\n"
        f"rclone sync \"$BACKUPS_DIR\" {remote} --exclude '*.tmp' --quiet\n"
    )
    script_path.chmod(0o755)

    try:
        parse_cadence(every)
    except ValueError as exc:
        typer.echo(_red(str(exc))); raise typer.Exit(code=1) from exc
    engine = _engine()
    with Session(engine) as s:
        from korpha.business.model import Business
        biz = s.exec(select(Business)).first()
        if biz is None:
            typer.echo(_red("no Business; run `korpha init` first"))
            raise typer.Exit(code=1)
        s.add(ScriptCron(
            business_id=biz.id,
            name="backup-rclone-push",
            script_path=str(script_path),
            cadence=every,
            enabled=True,
        ))
        s.commit()
    typer.echo(_green("✓") + " rclone push cron installed")
    typer.echo(_dim(f"  remote: {remote}"))
    typer.echo(_dim(f"  cadence: {every}"))


@scriptcron_app.command("add-backup-snapshot")
def cron_add_backup_snapshot(
    every: Annotated[str, typer.Option(
        "--every",
        help=(
            "Cadence: 'every 1h' / 'every 6h' / 'every 1d'. "
            "Default hourly is recommended for active businesses."
        ),
    )] = "every 1h",
    full_daily: Annotated[bool, typer.Option(
        "--full-daily/--no-full-daily",
        help=(
            "Also schedule a daily full bundle (tar.gz of the whole "
            "data dir). Recommended on."
        ),
    )] = True,
    name: Annotated[str, typer.Option(
        "--name", help="Cron job name (must be unique).",
    )] = "backup-snapshot",
) -> None:
    """Install the rotating-backup cron — hourly DB snapshots plus
    optional daily full-bundle plus auto-pruning. One command, Mike
    never thinks about it again. Separate from the older
    ``add-backup`` (per-unit filesystem backup)."""
    from pathlib import Path as _P
    import os as _os
    from korpha.scriptcron import parse_cadence
    from korpha.scriptcron.model import ScriptCron
    from sqlmodel import Session, select

    cron_dir = _data_dir() / "cron-scripts"
    cron_dir.mkdir(parents=True, exist_ok=True)
    script_path = cron_dir / f"{name}.sh"
    body = (
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        f"# Auto-generated by `korpha cron add-backup-snapshot`.\n"
        f"korpha backups snapshot\n"
    )
    if full_daily:
        body += (
            'if [ "$(date +%H)" = "03" ]; then\n'
            "  korpha backups snapshot --full\n"
            "fi\n"
        )
    body += "korpha backups prune\n"
    script_path.write_text(body)
    script_path.chmod(0o755)

    try:
        parse_cadence(every)
    except ValueError as exc:
        typer.echo(_red(str(exc))); raise typer.Exit(code=1) from exc
    engine = _engine()
    with Session(engine) as s:
        from korpha.business.model import Business
        biz = s.exec(select(Business)).first()
        if biz is None:
            typer.echo(_red("no Business; run `korpha init` first"))
            raise typer.Exit(code=1)
        s.add(ScriptCron(
            business_id=biz.id,
            name=name,
            script_path=str(script_path),
            cadence=every,
            enabled=True,
        ))
        s.commit()
    typer.echo(_green("✓") + f" backup cron installed: {name}")
    typer.echo(_dim(f"  script: {script_path}"))
    typer.echo(_dim(f"  cadence: {every}"))
    typer.echo(_dim(
        "  off-disk push? `korpha backups setup-litestream` (S3) "
        "or `korpha backups setup-rclone` (Dropbox/GDrive)."
    ))


# ============================================================================
# korpha credentials — per-unit external service accounts (UI/CLI parity)
# ============================================================================


credentials_app = typer.Typer(
    name="credentials",
    help=(
        "Manage per-unit external service credentials (Stripe, "
        "Resend, OpenAI, HeyGen, etc.). Agencies use this to set "
        "up a client's stack without touching the dashboard."
    ),
)
app.add_typer(credentials_app)


@credentials_app.command("set")
def credentials_set(
    service: Annotated[str, typer.Option(
        "--service", help="Service kind (stripe, resend, openai, ...).",
    )],
    label: Annotated[str, typer.Option(
        "--label", help="Human-readable label.",
    )],
    api_key: Annotated[str, typer.Option(
        "--api-key", prompt=True, hide_input=True,
        help="API key (encrypted via the local vault before storage).",
    )],
    unit: Annotated[str, typer.Option(
        "--unit", help=(
            "Optional BusinessUnit name OR UUID to scope this to. "
            "Omit for company-wide default."
        ),
    )] = "",
    cap: Annotated[float, typer.Option(
        "--cap", help="Monthly spending cap in USD (optional).",
    )] = 0.0,
) -> None:
    """Add an ExternalServiceAccount row, encrypting the api key."""
    import json as _json
    from decimal import Decimal
    from korpha.business.model import Business
    from korpha.business_units.context import resolve_unit_id
    from korpha.credentials.model import (
        ExternalServiceAccount, ExternalServiceKind,
    )
    from korpha.secrets.crypto import encrypt_bytes, load_master_key
    from sqlmodel import Session, select

    try:
        service_kind = ExternalServiceKind(service.lower())
    except ValueError:
        typer.echo(_red(f"unknown service kind: {service}"))
        raise typer.Exit(code=1)

    engine = _engine()
    with Session(engine) as s:
        biz = s.exec(select(Business)).first()
        if biz is None:
            typer.echo(_red("no Business; run `korpha init` first"))
            raise typer.Exit(code=1)
        unit_id = None
        if unit.strip():
            try:
                unit_id = resolve_unit_id(s, biz.id, unit.strip())
            except ValueError as exc:
                typer.echo(_red(f"unit: {exc}"))
                raise typer.Exit(code=1)

        data_dir = _data_dir()
        master = load_master_key(data_dir / "secrets" / "master.key")
        plaintext = _json.dumps(
            {"api_key": api_key}, separators=(",", ":"),
        ).encode("utf-8")
        encrypted = encrypt_bytes(plaintext, master)

        s.add(ExternalServiceAccount(
            business_id=biz.id,
            business_unit_id=unit_id,
            service=service_kind,
            label=label,
            credentials_encrypted=encrypted,
            spending_cap_usd_per_month=Decimal(str(cap)) if cap > 0 else None,
            is_active=True,
        ))
        s.commit()
    typer.echo(_green("✓") + f" {service} credential saved")
    typer.echo(_dim(f"  label: {label}"))
    typer.echo(_dim(
        f"  scope: {('unit ' + unit) if unit else 'company-wide default'}"
    ))


@credentials_app.command("list")
def credentials_list() -> None:
    """List every credential row + scope."""
    from korpha.business.model import Business
    from korpha.business_units.model import BusinessUnit
    from korpha.credentials.model import ExternalServiceAccount
    from sqlmodel import Session, select

    engine = _engine()
    with Session(engine) as s:
        biz = s.exec(select(Business)).first()
        if biz is None:
            typer.echo(_dim("(no business)"))
            return
        unit_names = {
            u.id: u.name for u in s.exec(
                select(BusinessUnit).where(BusinessUnit.business_id == biz.id)
            ).all()
        }
        rows = list(s.exec(
            select(ExternalServiceAccount).where(
                ExternalServiceAccount.business_id == biz.id
            )
        ).all())
        if not rows:
            typer.echo(_dim("(no credentials set)"))
            return
        for r in rows:
            scope = (
                unit_names.get(r.business_unit_id, str(r.business_unit_id))
                if r.business_unit_id else "company-wide"
            )
            cap = (
                f" cap=${r.spending_cap_usd_per_month}/mo"
                if r.spending_cap_usd_per_month else ""
            )
            active = _green("on") if r.is_active else _red("off")
            typer.echo(
                f"  [{active}] {r.service.value:10s} "
                f"{r.label[:40]:42s} → {scope}{cap}"
            )


@credentials_app.command("remove")
def credentials_remove(
    account_id: Annotated[str, typer.Argument(
        help="ExternalServiceAccount UUID (from `credentials list`).",
    )],
) -> None:
    """Delete a credential row."""
    from uuid import UUID as _U
    from korpha.credentials.model import ExternalServiceAccount
    from sqlmodel import Session

    engine = _engine()
    with Session(engine) as s:
        row = s.get(ExternalServiceAccount, _U(account_id))
        if row is None:
            typer.echo(_red(f"no credential with id {account_id}"))
            raise typer.Exit(code=1)
        s.delete(row); s.commit()
    typer.echo(_green("✓") + " removed")


# ============================================================================
# korpha units — manage BusinessUnits from CLI (UI/CLI parity for /app/units)
# ============================================================================


units_app = typer.Typer(
    name="units",
    help=(
        "Manage BusinessUnits (Lines, Types, Audiences). Lets an "
        "agency spawn / pause / list a client's business lines "
        "without using the chat or dashboard."
    ),
)
app.add_typer(units_app)


@units_app.command("list")
def units_list() -> None:
    """Show the org tree."""
    from korpha.business.model import Business
    from korpha.business_units.board import BusinessUnitBoard
    from sqlmodel import Session, select

    engine = _engine()
    with Session(engine) as s:
        biz = s.exec(select(Business)).first()
        if biz is None:
            typer.echo(_dim("(no business)"))
            return
        units = BusinessUnitBoard(s).list_for_business(biz.id)
        if not units:
            typer.echo(_dim("(no units)"))
            return
        parents = {u.id: u.name for u in units}
        for u in units:
            parent = (
                parents.get(u.parent_id, "—")
                if u.parent_id else "root"
            )
            owner = (
                str(u.owner_agent_role_id)[:8]
                if u.owner_agent_role_id else "—"
            )
            typer.echo(
                f"  [{u.status:8s}] {u.kind.value:10s} "
                f"{u.name[:30]:30s} parent={parent[:20]:20s} "
                f"owner_role={owner}"
            )


@units_app.command("start")
def units_start(
    kind: Annotated[str, typer.Option(
        "--kind",
        help="Line kind: pod | kdp | info | saas | affiliate | agency.",
    )],
    name: Annotated[str, typer.Option(
        "--name", help="Display name (e.g. 'Romance KDP').",
    )] = "",
    parent_unit: Annotated[str, typer.Option(
        "--parent",
        help=(
            "Optional parent unit name or UUID. Defaults to the "
            "business's DEFAULT root unit."
        ),
    )] = "",
) -> None:
    """Spawn a new business Line + auto-hire its VP.

    Mirrors what the /app/units form does. Useful for agency
    bootstrap scripts: ``korpha units start --kind kdp --name 'Client X — Romance KDP'``.
    """
    import asyncio as _asyncio
    from korpha.business.model import Business
    from korpha.business_units.context import resolve_unit_id
    from korpha.identity.model import Founder
    from korpha.inference.cost_tracker import CostTracker
    from korpha.inference.pool import InferencePool
    from korpha.skills import default_registry
    from korpha.skills.types import SkillContext
    from sqlmodel import Session, select

    engine = _engine()
    with Session(engine) as s:
        biz = s.exec(select(Business)).first()
        founder = s.exec(select(Founder)).first()
        if biz is None or founder is None:
            typer.echo(_red("run `korpha init` first"))
            raise typer.Exit(code=1)
        ctx = SkillContext(
            business=biz, founder=founder, session=s,
            cost_tracker=CostTracker(pool=InferencePool(
                providers=[], accounts=[],
            )),
        )
        args: dict[str, str] = {"kind": kind}
        if name:
            args["name"] = name
        if parent_unit:
            try:
                pid = resolve_unit_id(s, biz.id, parent_unit)
            except ValueError as exc:
                typer.echo(_red(f"parent: {exc}"))
                raise typer.Exit(code=1)
            args["parent_unit_id"] = str(pid)
        skill = default_registry.skills.get("hr.start_business_line")
        if skill is None:
            typer.echo(_red("hr.start_business_line skill not registered"))
            raise typer.Exit(code=1)
        try:
            result = _asyncio.run(skill.run(ctx=ctx, args=args))
        except Exception as exc:  # noqa: BLE001
            typer.echo(_red(f"failed: {exc}"))
            raise typer.Exit(code=1)
    typer.echo(_green("✓") + f" {result.summary}")
    typer.echo(_dim(f"  unit_id: {result.payload.get('unit_id')}"))
    typer.echo(_dim(f"  owner:   {result.payload.get('owner_title')}"))


@units_app.command("pause")
def units_pause(
    unit: Annotated[str, typer.Argument(
        help="Unit name or UUID.",
    )],
    reason: Annotated[str, typer.Option(
        "--reason", help="Why pause (recorded in activity log).",
    )] = "",
) -> None:
    """Pause a unit (its VP stops handling new work)."""
    from korpha.business.model import Business
    from korpha.business_units.board import BusinessUnitBoard
    from korpha.business_units.context import resolve_unit_id
    from sqlmodel import Session, select

    engine = _engine()
    with Session(engine) as s:
        biz = s.exec(select(Business)).first()
        try:
            uid = resolve_unit_id(s, biz.id, unit)
        except ValueError as exc:
            typer.echo(_red(str(exc))); raise typer.Exit(code=1)
        BusinessUnitBoard(s).pause(uid, reason=reason or None)
    typer.echo(_green("✓") + f" paused {unit}")


@units_app.command("resume")
def units_resume(
    unit: Annotated[str, typer.Argument(help="Unit name or UUID.")],
) -> None:
    """Resume a paused unit."""
    from korpha.business.model import Business
    from korpha.business_units.board import BusinessUnitBoard
    from korpha.business_units.context import resolve_unit_id
    from sqlmodel import Session, select

    engine = _engine()
    with Session(engine) as s:
        biz = s.exec(select(Business)).first()
        try:
            uid = resolve_unit_id(s, biz.id, unit)
        except ValueError as exc:
            typer.echo(_red(str(exc))); raise typer.Exit(code=1)
        BusinessUnitBoard(s).resume(uid)
    typer.echo(_green("✓") + f" resumed {unit}")


@units_app.command("archive")
def units_archive(
    unit: Annotated[str, typer.Argument(help="Unit name or UUID.")],
    subtree: Annotated[bool, typer.Option(
        "--subtree", help="Also archive descendant units.",
    )] = False,
) -> None:
    """Archive a unit (history kept; no new work)."""
    from korpha.business.model import Business
    from korpha.business_units.board import BusinessUnitBoard
    from korpha.business_units.context import resolve_unit_id
    from sqlmodel import Session, select

    engine = _engine()
    with Session(engine) as s:
        biz = s.exec(select(Business)).first()
        try:
            uid = resolve_unit_id(s, biz.id, unit)
        except ValueError as exc:
            typer.echo(_red(str(exc))); raise typer.Exit(code=1)
        board = BusinessUnitBoard(s)
        if subtree:
            archived = board.archive_subtree(uid)
            typer.echo(_green("✓") + f" archived {len(archived)} units")
        else:
            board.archive(uid)
            typer.echo(_green("✓") + f" archived {unit}")


# ============================================================================
# Backups daemon controls + cron toggle (UI/CLI parity backfill)
# ============================================================================


@backups_app.command("disconnect")
def backups_disconnect() -> None:
    """Tear down the off-disk push config (stops the replicator,
    removes the litestream.yml + runner). Local snapshots keep
    running. Mirrors the dashboard's "Disconnect" button."""
    from korpha.backup.offdisk import (
        _config_status_path, stop_replicator,
    )
    data_dir = _data_dir()
    stop_replicator(data_dir)
    for p in [
        _config_status_path(data_dir),
        data_dir / "litestream.yml",
        data_dir / "litestream-run.sh",
        data_dir / "litestream.pid",
    ]:
        p.unlink(missing_ok=True)
    typer.echo(_green("✓") + " off-disk disconnected — local snapshots continue")


@backups_app.command("start")
def backups_start() -> None:
    """Start the replicator daemon (if off-disk is configured)."""
    from korpha.backup.offdisk import (
        OffDiskConfig, current_status, start_replicator,
    )
    data_dir = _data_dir()
    status_dict = current_status(data_dir)
    if status_dict is None:
        typer.echo(_red(
            "off-disk not configured — run `korpha backups "
            "setup-litestream` or use the /app/backups wizard"
        ))
        raise typer.Exit(code=1)
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
    if ok:
        typer.echo(_green("✓") + f" {msg}")
    else:
        typer.echo(_red(msg)); raise typer.Exit(code=1)


@backups_app.command("stop")
def backups_stop() -> None:
    """Stop the replicator daemon (config kept; restart with
    `korpha backups start`)."""
    from korpha.backup.offdisk import stop_replicator
    ok, msg = stop_replicator(_data_dir())
    typer.echo((_green("✓") if ok else _red("✗")) + f" {msg}")


@backups_app.command("status")
def backups_status() -> None:
    """Show current off-disk config + replicator status."""
    from korpha.backup.offdisk import current_status, replicator_status
    data_dir = _data_dir()
    cfg = current_status(data_dir)
    rep = replicator_status(data_dir)
    if cfg is None:
        typer.echo(_dim("off-disk: not configured"))
        return
    typer.echo(_bold("off-disk:"))
    typer.echo(f"  provider: {cfg.get('provider_label', cfg['provider'])}")
    typer.echo(f"  bucket:   {cfg['bucket']}")
    typer.echo(f"  region:   {cfg.get('region', '')}")
    typer.echo(f"  endpoint: {cfg.get('endpoint', '')}")
    typer.echo(_bold("replicator:"))
    if rep["running"]:
        typer.echo(_green("  running") + f" (pid {rep['pid']})")
    else:
        typer.echo(_dim("  not running"))


@scriptcron_app.command("toggle")
def cron_toggle(
    name_or_id: Annotated[str, typer.Argument(
        help="Cron name or UUID to flip enabled/disabled.",
    )],
) -> None:
    """Enable / disable a cron job (mirrors the dashboard toggle)."""
    from uuid import UUID as _U
    from korpha.scriptcron.model import ScriptCron
    from sqlmodel import Session, select

    engine = _engine()
    with Session(engine) as s:
        row = None
        try:
            row = s.get(ScriptCron, _U(name_or_id))
        except (ValueError, AttributeError):
            row = s.exec(
                select(ScriptCron).where(ScriptCron.name == name_or_id)
            ).first()
        if row is None:
            typer.echo(_red(f"no cron job {name_or_id!r}"))
            raise typer.Exit(code=1)
        row.enabled = not row.enabled
        s.add(row); s.commit()
    typer.echo(_green("✓") + f" {name_or_id}: enabled={row.enabled}")


browser_app = typer.Typer(
    name="browser",
    help=(
        "Manage the headless browser pool. Concurrency-gated so "
        "two agents scraping in parallel don't both spin a 500MB "
        "Chromium on the same laptop. Default: 1. Bump on bigger "
        "boxes."
    ),
)
app.add_typer(browser_app)


@browser_app.command("status")
def browser_status() -> None:
    """Show current concurrency cap, in-use slots, lifetime usage."""
    from korpha.browser.pool import get_status, hydrate_from_db

    hydrate_from_db()
    st = get_status()
    typer.echo(f"{_bold('max concurrent'):20} {st.max_concurrent}")
    typer.echo(f"{_bold('in use right now'):20} {st.in_use}")
    typer.echo(f"{_bold('total acquisitions'):20} {st.total_acquisitions}")
    if st.last_acquired_at:
        typer.echo(f"{_bold('last acquired'):20} {st.last_acquired_at.isoformat()}")


@browser_app.command("set-concurrency")
def browser_set_concurrency(
    n: Annotated[int, typer.Argument(help="Max concurrent browser sessions. >=1.")],
) -> None:
    """Persist a new concurrency cap. Takes effect immediately for
    future acquisitions; in-flight sessions keep their slot."""
    import asyncio
    from korpha.browser.pool import persist_concurrency

    if n < 1:
        typer.echo(_red("✗") + " concurrency must be >= 1")
        raise typer.Exit(1)
    asyncio.run(persist_concurrency(n))
    typer.echo(_green("✓") + f" browser concurrency set to {n}")


inference_app = typer.Typer(
    name="inference",
    help=(
        "Tune the inference cascade. Set per-provider priority (lower "
        "= tried first), retries-before-swap, free-tier 429 semantics, "
        "and probe daily quotas. Mirrors what's available on "
        "/app/providers."
    ),
)
app.add_typer(inference_app)


def _load_pool_for_inspect() -> tuple[list, list, str]:
    """Build a transient pool snapshot for read-only commands."""
    from korpha.inference.config import load_from_yaml
    from korpha.inference.env_fallback import detect_configured_providers

    cfg = load_from_yaml()
    if cfg is not None:
        providers = list(cfg.providers)
        accounts = list(cfg.accounts)
        source = "providers.yaml"
    else:
        pairs = detect_configured_providers()
        providers = [p for p, _ in pairs]
        accounts = [a for _, a in pairs]
        source = "env vars (no providers.yaml)"
    return providers, accounts, source


@inference_app.command("list")
def inference_list() -> None:
    """Show every configured account with priority, retries, status."""
    providers, accounts, source = _load_pool_for_inspect()
    if not accounts:
        typer.echo(_dim("No inference accounts configured."))
        typer.echo(_dim("Run `korpha config` to add one."))
        return
    typer.echo(_dim(f"Source: {source}"))
    typer.echo(
        f"{_bold('label'):28} {_bold('provider'):20} "
        f"{_bold('prio'):>5} {_bold('retries'):>8} "
        f"{_bold('free-tier'):>10} {_bold('tiers')}"
    )
    sorted_accounts = sorted(
        accounts, key=lambda a: (a.priority, a.label or a.provider_name),
    )
    for a in sorted_accounts:
        free = "yes" if a.free_tier_quota else "—"
        tiers = ",".join(t.value for t in a.tier_models)
        typer.echo(
            f"{(a.label or a.provider_name):28} "
            f"{a.provider_name:20} "
            f"{a.priority:>5} "
            f"{a.retries_before_swap:>8} "
            f"{free:>10} "
            f"{tiers}"
        )


def _update_provider_field(label: str, field: str, value: object) -> None:
    from korpha.inference.config_writer import update_provider_entry
    ok = update_provider_entry(label, {field: value})
    if not ok:
        typer.echo(_red("✗") + f" no provider with label {label!r} in providers.yaml")
        raise typer.Exit(1)
    typer.echo(_green("✓") + f" {label}: {field} = {value}")


@inference_app.command("set-priority")
def inference_set_priority(
    label: Annotated[str, typer.Argument(help="ProviderAccount label.")],
    priority: Annotated[int, typer.Argument(help="Lower = tried first.")],
) -> None:
    """Set cascade priority for one account (lower = tried first)."""
    _update_provider_field(label, "priority", priority)


@inference_app.command("set-retries")
def inference_set_retries(
    label: Annotated[str, typer.Argument(help="ProviderAccount label.")],
    retries: Annotated[int, typer.Argument(
        help="Same-account retries on transient error before swap. 0 = no retry.",
    )],
) -> None:
    """Set retries-before-swap for one account."""
    if retries < 0:
        typer.echo(_red("✗") + " retries must be >= 0")
        raise typer.Exit(1)
    _update_provider_field(label, "retries_before_swap", retries)


@inference_app.command("set-free-tier")
def inference_set_free_tier(
    label: Annotated[str, typer.Argument(help="ProviderAccount label.")],
    window_kind: Annotated[str, typer.Argument(
        help="daily | hourly | monthly",
    )] = "daily",
    reset_utc: Annotated[str, typer.Option(
        "--reset-utc", help="Time-of-day for daily reset (UTC), HH:MM",
    )] = "00:00",
) -> None:
    """Mark an account as free-tier-quota-limited. 429 then means
    'daily cap consumed, jump to next priority until reset' instead
    of 'slow down for retry_after seconds'."""
    if window_kind not in ("daily", "hourly", "monthly"):
        typer.echo(_red("✗") + " window_kind must be daily/hourly/monthly")
        raise typer.Exit(1)
    _update_provider_field(
        label, "free_tier_quota",
        {"window_kind": window_kind, "reset_utc": reset_utc},
    )


@inference_app.command("clear-free-tier")
def inference_clear_free_tier(
    label: Annotated[str, typer.Argument(help="ProviderAccount label.")],
) -> None:
    """Remove free-tier-quota config; treat 429 as standard retry_after."""
    _update_provider_field(label, "free_tier_quota", None)


@inference_app.command("probe")
def inference_probe(
    label: Annotated[str | None, typer.Argument(
        help="Probe just this account (default: every account).",
    )] = None,
) -> None:
    """Send a 1-token request to each account and report whether
    the daily quota is reachable. Useful for free-tier keys —
    shows which ones are exhausted right now and the reset time."""
    import asyncio
    from korpha.inference.probe import probe_accounts

    providers, accounts, _ = _load_pool_for_inspect()
    if label:
        accounts = [a for a in accounts if (a.label or a.provider_name) == label]
        if not accounts:
            typer.echo(_red("✗") + f" no account with label {label!r}")
            raise typer.Exit(1)

    results = asyncio.run(probe_accounts(providers, accounts))
    for r in results:
        status = _green("OK") if r.ok else _red("FAIL")
        extra = f" reset={r.reset_utc}" if r.reset_utc else ""
        msg = f" — {r.message}" if r.message else ""
        typer.echo(f"{status} {r.label:28} {r.provider:20}{extra}{msg}")


@app.command()
def debrief(
    output: Annotated[str, typer.Option(
        "--output", "-o",
        help="Where to write the founder profile JSON. Defaults to "
             "<data_dir>/founder_profile.json so Korpha picks it up "
             "automatically.",
    )] = "",
) -> None:
    """Run the Debriefeur deep-dive interview (~20 min).

    Debriefeur is a separate tool — Korpha just orchestrates the
    handoff. It writes a founder profile JSON that the CEO and
    every Director / VP picks up on the next message: decision
    style, risk tolerance, blindspots, operating rhythm, etc.

    Same tool Hermes and OpenClaw users run for their own agents.

    Skippable. Run anytime later; profile gets picked up on the
    next message without restart.
    """
    import shutil
    import subprocess

    profile_path = Path(output) if output else _data_dir() / "founder_profile.json"
    profile_path.parent.mkdir(parents=True, exist_ok=True)

    binary = shutil.which("debriefeur")
    if binary is None:
        typer.echo(_yellow(
            "Debriefeur isn't installed yet. One-line install:"
        ))
        typer.echo("")
        typer.echo(_bold("  pip install debriefeur"))
        typer.echo("")
        typer.echo(_dim(
            "Then run `korpha debrief` again. Or run `debriefeur "
            "interview` directly and point Korpha at the output "
            f"with `--output {profile_path}`."
        ))
        typer.echo(_dim(
            "Source: https://github.com/AIgenteur/debriefeur"
        ))
        raise typer.Exit(code=1)

    typer.echo(_dim(
        f"Starting Debriefeur. Profile will be saved to "
        f"{profile_path} when complete."
    ))
    typer.echo("")
    try:
        result = subprocess.run(
            [binary, "interview", "--output", str(profile_path)],
            check=False,
        )
    except KeyboardInterrupt:
        typer.echo(_yellow(
            "\nInterview interrupted. Profile not saved. Run "
            "`korpha debrief` again when ready."
        ))
        raise typer.Exit(code=1)

    if result.returncode != 0:
        typer.echo(_red("✗") + " Debriefeur exited with an error.")
        raise typer.Exit(code=result.returncode)

    if not profile_path.exists():
        typer.echo(_yellow(
            "Debriefeur finished but didn't write a profile to "
            f"{profile_path}. Check Debriefeur's output."
        ))
        raise typer.Exit(code=1)

    typer.echo(_green("✓") + f" Founder profile saved: {profile_path}")
    typer.echo(_dim(
        "Korpha agents pick this up automatically on the next "
        "message. You can rerun anytime to refresh."
    ))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
