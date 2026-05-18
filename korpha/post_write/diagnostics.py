"""Lightweight semantic diagnostics via language-specific CLIs.

We don't ship a full LSP JSON-RPC client (that's a half-day's work
per language to get right — server lifecycle, document open/close,
framing, race conditions). Instead, shell out to the CLI form of
each language's checker and parse the structured output. Covers the
80% case: catching obvious semantic errors the syntax-only
delta_lint misses.

Supported (auto-detected — skip silently when binary missing):

  * Python  → ``pyright --outputjson <file>``
  * TypeScript / TSX → ``tsc --noEmit --pretty false <file>``
  * YAML    → ``yamllint -f parsable <file>``
  * JSON    → already covered by json.loads in delta_lint; no extra
  * Shell   → ``shellcheck -f json <file>``

Plugins that want full LSP integration (rust-analyzer, gopls,
clangd) implement the LspDiagnosticsProvider contract and register
their backend — same plugin pattern as channel adapters + video
backends.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DiagnosticIssue:
    """One semantic issue from a checker."""

    file: str
    line: int | None
    col: int | None
    severity: str
    """One of 'error', 'warning', 'info'. Conservative — only
    'error'-level forces a fix loop; others surface as advisory."""

    message: str
    source: str
    """Which checker emitted this — 'pyright', 'tsc', 'shellcheck'."""

    def render(self) -> str:
        loc = ""
        if self.line is not None:
            loc = f":{self.line}"
            if self.col is not None:
                loc += f":{self.col}"
        return (
            f"  [{self.severity}] {self.file}{loc} ({self.source}): "
            f"{self.message}"
        )


@dataclass
class DiagnosticResult:
    """Aggregate result for one file."""

    file: str
    ok: bool
    issues: list[DiagnosticIssue] = field(default_factory=list)
    skipped_reason: str | None = None
    """Set when the checker couldn't run (missing binary, unsupported
    file type). Distinguishable from ok=True / issues=[] which means
    'ran cleanly'."""

    def render(self) -> str:
        if self.skipped_reason:
            return f"  [skip] {self.file}: {self.skipped_reason}"
        if self.ok:
            return f"  [ok] {self.file}"
        lines = [f"  [issues] {self.file}: {len(self.issues)}"]
        for issue in self.issues[:10]:
            lines.append(issue.render())
        if len(self.issues) > 10:
            lines.append(
                f"    … and {len(self.issues) - 10} more",
            )
        return "\n".join(lines)


async def run_lsp_diagnostics(
    file_path: str | Path,
    *,
    timeout_seconds: float = 15.0,
) -> DiagnosticResult:
    """Run the appropriate checker for ``file_path``'s extension.
    Returns a DiagnosticResult — never raises (checker failures
    degrade to skipped_reason). Async because checkers can be slow
    on large files."""
    path = Path(str(file_path))
    suffix = path.suffix.lower()
    file_str = str(path)

    if suffix in (".py",):
        return await _run_pyright(file_str, timeout_seconds=timeout_seconds)
    if suffix in (".ts", ".tsx"):
        return await _run_tsc(file_str, timeout_seconds=timeout_seconds)
    if suffix in (".yml", ".yaml"):
        return await _run_yamllint(file_str, timeout_seconds=timeout_seconds)
    if suffix in (".sh", ".bash"):
        return await _run_shellcheck(file_str, timeout_seconds=timeout_seconds)

    return DiagnosticResult(
        file=file_str,
        ok=True,
        skipped_reason=f"no semantic checker for {suffix or '(no extension)'}",
    )


async def _run_pyright(
    file: str, *, timeout_seconds: float,
) -> DiagnosticResult:
    if shutil.which("pyright") is None:
        return DiagnosticResult(
            file=file,
            ok=True,
            skipped_reason="pyright not installed",
        )
    proc = await asyncio.create_subprocess_exec(
        "pyright", "--outputjson", file,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        proc.kill()
        return DiagnosticResult(
            file=file, ok=True,
            skipped_reason=f"pyright timed out after {timeout_seconds}s",
        )
    try:
        data = json.loads(stdout.decode("utf-8", errors="replace") or "{}")
    except json.JSONDecodeError:
        return DiagnosticResult(
            file=file, ok=True,
            skipped_reason="pyright returned non-JSON",
        )

    issues: list[DiagnosticIssue] = []
    for diag in data.get("generalDiagnostics") or []:
        sev = (diag.get("severity") or "info").lower()
        loc = diag.get("range") or {}
        start = loc.get("start") or {}
        issues.append(DiagnosticIssue(
            file=diag.get("file") or file,
            line=int(start.get("line")) + 1 if "line" in start else None,
            col=int(start.get("character")) + 1 if "character" in start else None,
            severity=sev,
            message=str(diag.get("message") or "").strip(),
            source="pyright",
        ))
    return DiagnosticResult(
        file=file,
        ok=all(i.severity != "error" for i in issues),
        issues=issues,
    )


async def _run_tsc(
    file: str, *, timeout_seconds: float,
) -> DiagnosticResult:
    if shutil.which("tsc") is None:
        return DiagnosticResult(
            file=file, ok=True,
            skipped_reason="tsc (typescript) not installed",
        )
    proc = await asyncio.create_subprocess_exec(
        "tsc", "--noEmit", "--pretty", "false", file,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        proc.kill()
        return DiagnosticResult(
            file=file, ok=True,
            skipped_reason=f"tsc timed out after {timeout_seconds}s",
        )

    # tsc output format: ``file.ts(line,col): error TS2304: msg``
    issues: list[DiagnosticIssue] = []
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        if "error TS" not in line:
            continue
        # Best-effort parse — tsc lines occasionally lack the
        # location prefix on diagnostic continuation lines.
        try:
            file_part, _, err = line.partition(": error ")
            fpath, _, loc = file_part.rpartition("(")
            line_str, _, col_str = loc.rstrip(")").partition(",")
            issues.append(DiagnosticIssue(
                file=fpath or file,
                line=int(line_str) if line_str.isdigit() else None,
                col=int(col_str) if col_str.isdigit() else None,
                severity="error",
                message=err.strip(),
                source="tsc",
            ))
        except Exception:  # noqa: BLE001
            continue

    return DiagnosticResult(
        file=file,
        ok=not issues,
        issues=issues,
    )


async def _run_yamllint(
    file: str, *, timeout_seconds: float,
) -> DiagnosticResult:
    if shutil.which("yamllint") is None:
        return DiagnosticResult(
            file=file, ok=True,
            skipped_reason="yamllint not installed",
        )
    proc = await asyncio.create_subprocess_exec(
        "yamllint", "-f", "parsable", file,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        proc.kill()
        return DiagnosticResult(
            file=file, ok=True,
            skipped_reason=f"yamllint timed out after {timeout_seconds}s",
        )

    # yamllint parsable: ``file:line:col: [severity] msg (rule)``
    issues: list[DiagnosticIssue] = []
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        try:
            parts = line.split(":", 3)
            if len(parts) < 4:
                continue
            fpath, line_str, col_str, rest = parts
            rest = rest.strip()
            severity = "warning"
            if rest.startswith("[error]"):
                severity = "error"
                rest = rest[len("[error]"):].strip()
            elif rest.startswith("[warning]"):
                rest = rest[len("[warning]"):].strip()
            issues.append(DiagnosticIssue(
                file=fpath or file,
                line=int(line_str) if line_str.isdigit() else None,
                col=int(col_str) if col_str.isdigit() else None,
                severity=severity,
                message=rest,
                source="yamllint",
            ))
        except Exception:  # noqa: BLE001
            continue

    return DiagnosticResult(
        file=file,
        ok=all(i.severity != "error" for i in issues),
        issues=issues,
    )


async def _run_shellcheck(
    file: str, *, timeout_seconds: float,
) -> DiagnosticResult:
    if shutil.which("shellcheck") is None:
        return DiagnosticResult(
            file=file, ok=True,
            skipped_reason="shellcheck not installed",
        )
    proc = await asyncio.create_subprocess_exec(
        "shellcheck", "-f", "json", file,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        proc.kill()
        return DiagnosticResult(
            file=file, ok=True,
            skipped_reason=f"shellcheck timed out after {timeout_seconds}s",
        )

    try:
        raw = json.loads(stdout.decode("utf-8", errors="replace") or "[]")
    except json.JSONDecodeError:
        return DiagnosticResult(
            file=file, ok=True,
            skipped_reason="shellcheck returned non-JSON",
        )

    issues: list[DiagnosticIssue] = []
    for item in raw:
        sev = (item.get("level") or "info").lower()
        issues.append(DiagnosticIssue(
            file=item.get("file") or file,
            line=int(item.get("line", 0)) or None,
            col=int(item.get("column", 0)) or None,
            severity=sev,
            message=str(item.get("message") or "").strip(),
            source="shellcheck",
        ))

    return DiagnosticResult(
        file=file,
        ok=all(i.severity not in ("error",) for i in issues),
        issues=issues,
    )


__all__ = [
    "DiagnosticIssue",
    "DiagnosticResult",
    "run_lsp_diagnostics",
]
