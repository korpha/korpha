"""``korpha doctor`` — config/DB/provider/skill probes for support.

Each ``Check`` is a ``(name, fn)`` pair where ``fn() -> CheckResult``.
Results are aggregated into a ``DoctorReport`` the CLI prints as a
checklist. Failures don't abort — every check runs so the operator
sees the full picture, not just the first wedge.

Adapted from Hermes' ``hermes_cli/doctor.py`` pattern. Stripped to
the Korpha surfaces (config, DB, providers, channels, skills,
security, output budget, MCP servers).
"""
from __future__ import annotations

import importlib
import logging
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CheckResult:
    """One probe's outcome.

    ``ok`` is the overall verdict. ``severity`` distinguishes hard
    errors (config missing) from warnings (no provider configured —
    runnable but useless) so the CLI can render different markers
    without the report needing two separate lists.
    """

    name: str
    ok: bool
    detail: str = ""
    severity: str = "info"
    """``info`` (passing), ``warn`` (passes with caveats), ``error``
    (real failure). When ok=False this should be ``error`` or
    ``warn``; when ok=True it's typically ``info``."""

    fix_hint: str = ""
    """One-line suggestion for how to fix this if it failed.
    Shown only when ok=False so we don't drown passing checks in
    advice."""


@dataclass
class Check:
    name: str
    fn: Callable[[], CheckResult]


@dataclass
class DoctorReport:
    """Aggregate of all CheckResults."""

    results: list[CheckResult] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return any(
            (not r.ok and r.severity == "error") for r in self.results
        )

    @property
    def has_warnings(self) -> bool:
        return any(r.severity == "warn" for r in self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.ok)

    def render(self, *, color: bool = True) -> str:
        """Render the report as a human-readable checklist string."""
        lines: list[str] = []
        for r in self.results:
            mark = _mark(r, color=color)
            lines.append(f"  {mark} {r.name}")
            if r.detail:
                lines.append(f"      {r.detail}")
            if not r.ok and r.fix_hint:
                lines.append(f"      → fix: {r.fix_hint}")
        if self.has_errors:
            summary = (
                f"\n{self.failed} failed, {self.passed} passed. "
                "Fix the errored items above before running the cofounder."
            )
        elif self.has_warnings:
            summary = (
                f"\nAll critical checks passed; {sum(1 for r in self.results if r.severity == 'warn')}"
                " warnings to review."
            )
        else:
            summary = f"\nAll {self.passed} checks passed."
        return "\n".join(lines) + summary


def _mark(r: CheckResult, *, color: bool) -> str:
    if r.ok:
        return _green("✓", color) if r.severity != "warn" else _yellow("⚠", color)
    if r.severity == "warn":
        return _yellow("⚠", color)
    return _red("✗", color)


def _green(s: str, color: bool) -> str:
    return f"\x1b[32m{s}\x1b[0m" if color else s


def _yellow(s: str, color: bool) -> str:
    return f"\x1b[33m{s}\x1b[0m" if color else s


def _red(s: str, color: bool) -> str:
    return f"\x1b[31m{s}\x1b[0m" if color else s


# ---- individual probes ----


def _check_python_version() -> CheckResult:
    info = sys.version_info
    if info < (3, 12):
        return CheckResult(
            name="Python 3.12+",
            ok=False,
            detail=f"running on {info.major}.{info.minor}.{info.micro}",
            severity="error",
            fix_hint="install Python 3.12 or 3.13 and re-create the venv",
        )
    return CheckResult(
        name="Python 3.12+",
        ok=True,
        detail=f"{info.major}.{info.minor}.{info.micro}",
    )


def _check_data_dir() -> CheckResult:
    base = Path(
        os.environ.get("KORPHA_DATA_DIR")
        or (Path.home() / ".korpha")
    )
    if not base.exists():
        return CheckResult(
            name="Data directory",
            ok=False,
            detail=f"{base} does not exist",
            severity="warn",  # Created lazily by first run; not fatal
            fix_hint=f"will be created on first run, or `mkdir -p {base}`",
        )
    if not os.access(base, os.W_OK):
        return CheckResult(
            name="Data directory",
            ok=False,
            detail=f"{base} not writable by this user",
            severity="error",
            fix_hint=f"chown the dir or set KORPHA_DATA_DIR to a writable path",
        )
    return CheckResult(name="Data directory", ok=True, detail=str(base))


def _check_database_url() -> CheckResult:
    url = (
        os.environ.get("DATABASE_URL")
        or os.environ.get("KORPHA_DATABASE_URL")
    )
    if not url:
        return CheckResult(
            name="Database URL",
            ok=True,
            detail="not set — using SQLite default (dev mode)",
            severity="warn",
            fix_hint=(
                "set DATABASE_URL=postgresql://user:pass@host/db for "
                "production"
            ),
        )
    if url.startswith("sqlite"):
        return CheckResult(
            name="Database URL",
            ok=True,
            detail="SQLite (dev mode)",
            severity="warn",
            fix_hint="use Postgres in production",
        )
    return CheckResult(
        name="Database URL",
        ok=True,
        detail=f"{url.split('@')[-1] if '@' in url else url}",
    )


def _check_database_connect() -> CheckResult:
    try:
        from sqlmodel import Session, select

        from korpha.db._session import get_engine
        engine = get_engine()
        with Session(engine) as s:
            s.exec(select(1))
        return CheckResult(
            name="Database connect", ok=True, detail="reachable",
        )
    except Exception as exc:
        return CheckResult(
            name="Database connect",
            ok=False,
            detail=str(exc).splitlines()[0],
            severity="error",
            fix_hint=(
                "verify DATABASE_URL credentials and that the DB is "
                "running (run `korpha db migrate` if schema is "
                "missing)"
            ),
        )


def _check_provider_accounts() -> CheckResult:
    """At least one configured provider account, else the cofounder
    has no LLM to talk to. Reads ``~/.korpha/providers.yaml``
    directly — same source the CLI setup wizard writes to."""
    base = Path(
        os.environ.get("KORPHA_DATA_DIR")
        or (Path.home() / ".korpha")
    )
    providers_path = base / "providers.yaml"
    if not providers_path.exists():
        return CheckResult(
            name="Provider accounts",
            ok=False,
            detail="no providers.yaml — none configured",
            severity="error",
            fix_hint="run `korpha setup providers`",
        )
    try:
        import yaml as _yaml

        body = _yaml.safe_load(providers_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        return CheckResult(
            name="Provider accounts",
            ok=False,
            detail=f"providers.yaml unparseable: {exc}",
            severity="error",
            fix_hint="check YAML syntax or re-run `korpha setup providers`",
        )
    entries = body.get("providers") if isinstance(body, dict) else None
    if not isinstance(entries, list) or not entries:
        return CheckResult(
            name="Provider accounts",
            ok=False,
            detail="providers.yaml has no entries",
            severity="error",
            fix_hint="run `korpha setup providers`",
        )
    names = sorted({
        str(e.get("name") or e.get("provider") or "?")
        for e in entries
        if isinstance(e, dict)
    })
    return CheckResult(
        name="Provider accounts",
        ok=True,
        detail=f"{len(entries)} configured ({', '.join(names)})",
    )


def _check_skills() -> CheckResult:
    """Skill registry has at least the built-ins, and no platform-
    incompatible skills are exposed in the catalog."""
    try:
        from korpha.skills.registry import default_registry
    except Exception as exc:
        return CheckResult(
            name="Skills",
            ok=False,
            detail=f"registry import failed: {exc}",
            severity="error",
        )
    visible = default_registry.list_specs()
    all_specs = default_registry.list_specs(include_unsupported=True)
    hidden = len(all_specs) - len(visible)
    detail = f"{len(visible)} skills exposed"
    if hidden:
        detail += f" ({hidden} hidden by platform whitelist)"
    return CheckResult(name="Skills", ok=True, detail=detail)


def _check_security_module() -> CheckResult:
    try:
        from korpha.security import is_safe_url

        # Smoke: floor still works
        if is_safe_url("http://169.254.169.254/") is True:
            return CheckResult(
                name="SSRF guard",
                ok=False,
                detail="metadata IP is not blocked — guard disabled?",
                severity="error",
            )
        return CheckResult(
            name="SSRF guard",
            ok=True,
            detail="metadata floor armed",
        )
    except Exception as exc:
        return CheckResult(
            name="SSRF guard",
            ok=False,
            detail=str(exc),
            severity="error",
        )


def _check_output_budget_dir() -> CheckResult:
    """Persistent overflow dir writable. Fails closed only if the
    custom path is unwritable; missing default is fine — created on
    first spill."""
    from korpha.limits.output_budget import _default_storage_dir

    base = _default_storage_dir().parent
    if not base.exists():
        return CheckResult(
            name="Tool result spillover",
            ok=True,
            detail=f"{base} will be created on first spill",
            severity="info",
        )
    if not os.access(base, os.W_OK):
        return CheckResult(
            name="Tool result spillover",
            ok=False,
            detail=f"{base} not writable",
            severity="warn",
            fix_hint="ensure the Korpha user owns ~/.korpha/",
        )
    return CheckResult(
        name="Tool result spillover", ok=True, detail=f"writable at {base}",
    )


def _check_mcp_config() -> CheckResult:
    """Optional — only check loadability, not whether the binaries
    exist (those are the user's choice)."""
    try:
        from korpha.mcp.config import config_path, load_mcp_config
    except ImportError:
        return CheckResult(
            name="MCP servers",
            ok=True,
            detail="not configured",
            severity="info",
        )
    try:
        path = config_path()
        if not path.exists():
            return CheckResult(
                name="MCP servers",
                ok=True,
                detail="no config file (skip)",
            )
        configs = load_mcp_config()
        return CheckResult(
            name="MCP servers",
            ok=True,
            detail=f"{len(configs)} configured",
        )
    except Exception as exc:
        return CheckResult(
            name="MCP servers",
            ok=False,
            detail=str(exc).splitlines()[0],
            severity="warn",
            fix_hint="check the MCP config syntax",
        )


def _check_optional_deps() -> CheckResult:
    """Surface which optional features are available given the
    current install."""
    optional = {
        "playwright": "browser automation",
        "textual": "TUI",
        "websockets": "TUI ↔ server transport",
    }
    missing: list[str] = []
    for mod, label in optional.items():
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(f"{mod} ({label})")
    if not missing:
        return CheckResult(
            name="Optional features",
            ok=True,
            detail=f"all available ({', '.join(optional.keys())})",
        )
    return CheckResult(
        name="Optional features",
        ok=True,
        detail=f"missing: {', '.join(missing)}",
        severity="warn",
        fix_hint="pip install the relevant extra to enable",
    )


# ---- runner ----


def default_checks() -> list[Check]:
    """All checks, in display order. Order matters — earliest
    failures usually cause later ones, so we put foundational ones
    first."""
    return [
        Check("python", _check_python_version),
        Check("data_dir", _check_data_dir),
        Check("db_url", _check_database_url),
        Check("db_connect", _check_database_connect),
        Check("providers", _check_provider_accounts),
        Check("skills", _check_skills),
        Check("ssrf_guard", _check_security_module),
        Check("output_budget", _check_output_budget_dir),
        Check("mcp", _check_mcp_config),
        Check("optional_deps", _check_optional_deps),
    ]


def run_doctor(checks: list[Check] | None = None) -> DoctorReport:
    """Run every check, swallow exceptions per-check, return the
    aggregate report. A check that *throws* (rather than returning
    a result) is recorded as a failure with the exception message
    so a single broken probe never wedges the whole report."""
    if checks is None:
        checks = default_checks()
    report = DoctorReport()
    for check in checks:
        try:
            result = check.fn()
        except Exception as exc:  # noqa: BLE001
            result = CheckResult(
                name=check.name,
                ok=False,
                detail=f"check raised: {exc}",
                severity="error",
            )
        report.results.append(result)
    return report
