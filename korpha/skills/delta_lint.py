"""Post-write delta lint — fast syntax check for authored content.

When the agent writes a file (Python skill, cron script, manifest), we
run an extension-driven syntax check before approval / staging. The
goal isn't safety — that's the scanner's job — it's *self-correction*:
a precise ``SyntaxError: unexpected EOF (line 17)`` lands back in the
LLM's next turn so it can fix the typo without burning a deploy +
silent-failure round-trip.

Hermes added this in v0.13 ("post-write delta lint") because
authored-Python skills frequently shipped with stray indent / bracket
errors that only surfaced when the cron tried to execute, hours later.

Each linter is best-effort + non-fatal — if a tool isn't installed
(e.g. ``bash`` missing on Windows CI), we skip rather than fail.
"""
from __future__ import annotations

import ast
import json
from dataclasses import dataclass


@dataclass(frozen=True)
class LintError:
    """One precise problem the LLM can fix."""

    file: str
    line: int | None
    col: int | None
    msg: str

    def render(self) -> str:
        loc = ""
        if self.line is not None:
            loc = f" (line {self.line}"
            if self.col is not None:
                loc += f", col {self.col}"
            loc += ")"
        return f"{self.file}: {self.msg}{loc}"


@dataclass(frozen=True)
class LintResult:
    """Aggregate result. ``ok`` is True iff no errors found."""

    ok: bool
    errors: list[LintError]

    def render(self) -> str:
        if self.ok:
            return "lint: clean"
        return "lint: " + "; ".join(e.render() for e in self.errors)


_PY_SUFFIXES = (".py",)
_JSON_SUFFIXES = (".json",)
_YAML_SUFFIXES = (".yaml", ".yml")
_TOML_SUFFIXES = (".toml",)
_BASH_SUFFIXES = (".sh", ".bash")


def lint_text(
    text: str,
    *,
    suffix: str,
    filename: str = "<authored>",
) -> LintResult:
    """Syntax-check ``text`` by file ``suffix`` (must include the dot,
    e.g. ``.py``). Unknown suffixes return clean — we never reject
    formats we don't know how to validate."""
    suffix = suffix.lower()
    if suffix in _PY_SUFFIXES:
        return _lint_python(text, filename=filename)
    if suffix in _JSON_SUFFIXES:
        return _lint_json(text, filename=filename)
    if suffix in _YAML_SUFFIXES:
        return _lint_yaml(text, filename=filename)
    if suffix in _TOML_SUFFIXES:
        return _lint_toml(text, filename=filename)
    if suffix in _BASH_SUFFIXES:
        return _lint_bash(text, filename=filename)
    return LintResult(ok=True, errors=[])


def _lint_python(text: str, *, filename: str) -> LintResult:
    try:
        ast.parse(text, filename=filename)
    except SyntaxError as exc:
        return LintResult(
            ok=False,
            errors=[LintError(
                file=filename,
                line=exc.lineno,
                col=exc.offset,
                msg=f"SyntaxError: {exc.msg}",
            )],
        )
    return LintResult(ok=True, errors=[])


def _lint_json(text: str, *, filename: str) -> LintResult:
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        return LintResult(
            ok=False,
            errors=[LintError(
                file=filename,
                line=exc.lineno,
                col=exc.colno,
                msg=f"JSONDecodeError: {exc.msg}",
            )],
        )
    return LintResult(ok=True, errors=[])


def _lint_yaml(text: str, *, filename: str) -> LintResult:
    try:
        import yaml
    except ImportError:
        return LintResult(ok=True, errors=[])  # PyYAML not installed
    try:
        yaml.safe_load(text)
    except yaml.YAMLError as exc:
        line: int | None = None
        col: int | None = None
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            line = mark.line + 1
            col = mark.column + 1
        return LintResult(
            ok=False,
            errors=[LintError(
                file=filename,
                line=line,
                col=col,
                msg=f"YAMLError: {getattr(exc, 'problem', None) or exc}",
            )],
        )
    return LintResult(ok=True, errors=[])


def _lint_toml(text: str, *, filename: str) -> LintResult:
    try:
        import tomllib
    except ImportError:
        return LintResult(ok=True, errors=[])  # py < 3.11
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return LintResult(
            ok=False,
            errors=[LintError(
                file=filename, line=None, col=None,
                msg=f"TOMLDecodeError: {exc}",
            )],
        )
    return LintResult(ok=True, errors=[])


def _lint_bash(text: str, *, filename: str) -> LintResult:
    """Run ``bash -n`` on the script. If bash is missing (Windows CI,
    minimal containers), return clean rather than fail-closed — the
    contract is best-effort syntax checking, not a hard gate."""
    import shutil
    import subprocess

    bash = shutil.which("bash")
    if bash is None:
        return LintResult(ok=True, errors=[])
    try:
        result = subprocess.run(
            [bash, "-n"],
            input=text,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return LintResult(
            ok=False,
            errors=[LintError(
                file=filename, line=None, col=None,
                msg=f"bash -n failed: {exc}",
            )],
        )
    if result.returncode == 0:
        return LintResult(ok=True, errors=[])
    stderr = (result.stderr or "").strip()
    line, msg = _parse_bash_error(stderr)
    return LintResult(
        ok=False,
        errors=[LintError(
            file=filename, line=line, col=None,
            msg=f"bash syntax: {msg or stderr or 'unknown error'}",
        )],
    )


def _parse_bash_error(stderr: str) -> tuple[int | None, str | None]:
    r"""Pull line + message out of bash's typical error format:
    ``bash: line 17: syntax error near unexpected token \`}\```. Return
    (None, None) if we can't parse."""
    if not stderr:
        return (None, None)
    # bash spits multiple lines; pick the first informative one.
    for line in stderr.splitlines():
        if ": line " in line:
            try:
                _, after = line.split(": line ", 1)
                num_part, msg = after.split(":", 1)
                return (int(num_part.strip()), msg.strip())
            except (ValueError, IndexError):
                continue
    return (None, stderr.splitlines()[0] if stderr else None)


__all__ = [
    "LintError",
    "LintResult",
    "lint_text",
]
