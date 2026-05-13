"""Smoke test — validates package is importable and version is set."""
from __future__ import annotations

import korpha


def test_version() -> None:
    assert korpha.__version__ == "0.0.1"


def test_cli_app_constructs() -> None:
    """Verify the typer app builds without errors."""
    from korpha.cli import app

    assert app is not None
