"""Tests for FileMutationTracker + diagnostics dispatcher."""
from __future__ import annotations

from pathlib import Path

import pytest

from korpha.post_write import (
    DiagnosticResult,
    FileMutation,
    FileMutationTracker,
    render_mutation_footer,
    run_lsp_diagnostics,
)


# ---- FileMutationTracker ------------------------------------------


def test_observe_create():
    with FileMutationTracker() as t:
        m = t.observe_write("/tmp/new.py", None, "x = 1\n")
    assert m.kind == "created"
    assert m.lines_before == 0
    assert m.lines_after == 2
    assert m.bytes_after == 6
    assert m.sha_before is None
    assert m.sha_after


def test_observe_modify():
    with FileMutationTracker() as t:
        m = t.observe_write("/tmp/x.py", "old", "new content")
    assert m.kind == "modified"
    assert m.changed is True
    assert m.lines_delta == 0


def test_observe_no_op_rewrite_detected():
    with FileMutationTracker() as t:
        m = t.observe_write("/tmp/x.py", "same", "same")
    assert m.changed is False


def test_observe_delete():
    with FileMutationTracker() as t:
        m = t.observe_write("/tmp/x.py", "content", None)
    assert m.kind == "deleted"
    assert m.lines_after == 0


def test_render_footer_empty_when_no_mutations():
    assert render_mutation_footer([]) == ""


def test_render_footer_lists_changed_files():
    mutations = [
        FileMutation(
            path="/a.py", kind="created",
            lines_before=0, lines_after=5,
            bytes_before=0, bytes_after=50,
            sha_before=None, sha_after="abc",
        ),
        FileMutation(
            path="/b.py", kind="modified",
            lines_before=10, lines_after=12,
            bytes_before=100, bytes_after=120,
            sha_before="aaa", sha_after="bbb",
        ),
    ]
    out = render_mutation_footer(mutations)
    assert "file-mutation: 2 file(s)" in out
    assert "+ /a.py (+5 lines)" in out
    assert "~ /b.py (+2 lines)" in out


def test_render_footer_warns_on_all_noop():
    """If every Write was a no-op (model claims changes but content
    is unchanged), the footer surfaces a warning."""
    mutations = [
        FileMutation(
            path="/x.py", kind="modified",
            lines_before=5, lines_after=5,
            bytes_before=50, bytes_after=50,
            sha_before="aaa", sha_after="aaa",
        ),
    ]
    out = render_mutation_footer(mutations)
    assert "⚠" in out
    assert "none changed" in out


# ---- diagnostics dispatcher --------------------------------------


@pytest.mark.anyio
async def test_unknown_extension_skipped_cleanly(tmp_path):
    f = tmp_path / "thing.xyz"
    f.write_text("anything")
    result = await run_lsp_diagnostics(f)
    assert result.ok is True
    assert "no semantic checker" in (result.skipped_reason or "")


@pytest.mark.anyio
async def test_missing_pyright_skips(tmp_path, monkeypatch):
    f = tmp_path / "x.py"
    f.write_text("import nonexistent_pkg_xyz\n")

    # Force shutil.which to return None for pyright.
    import korpha.post_write.diagnostics as diag_mod
    monkeypatch.setattr(
        diag_mod.shutil, "which",
        lambda n: None if n == "pyright" else "/usr/bin/" + n,
    )
    result = await run_lsp_diagnostics(f)
    assert result.ok is True
    assert "pyright not installed" in (result.skipped_reason or "")


@pytest.mark.anyio
async def test_pyright_on_valid_python(tmp_path):
    """If pyright is installed, valid Python returns ok=True with
    no issues. If not installed, test silently skips (no failure)."""
    import shutil
    if not shutil.which("pyright"):
        pytest.skip("pyright not installed in this env")
    f = tmp_path / "valid.py"
    f.write_text("x = 1\ny = x + 2\n")
    result = await run_lsp_diagnostics(f)
    assert result.ok is True


@pytest.fixture
def anyio_backend():
    return "asyncio"
