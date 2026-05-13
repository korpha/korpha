"""Tests for the image-gen provider abstraction.

Covers the four backends (Codex CLI, Replicate, fal.ai, local SD) +
the service that picks among them + the loader that reads
providers.yaml's ``image_providers:`` section.

All HTTP / subprocess calls are mocked — no real keys, no real GPU.
"""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from korpha.cli import app
from korpha.imagery import ImageGenRequest
from korpha.imagery.providers.codex_cli_image import CodexCLIImageProvider
from korpha.imagery.providers.fal_image import FalImageProvider
from korpha.imagery.providers.local_sd import LocalSDProvider
from korpha.imagery.providers.replicate_image import ReplicateImageProvider
from korpha.imagery.service import (
    ImageConfigError,
    ImageGenService,
    build_provider,
    load_image_providers,
)

# ---------------------------------------------------------------------------
# Replicate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replicate_happy_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Replicate flow: create prediction → poll → succeeded → download."""
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        if request.method == "POST" and "predictions" in str(request.url):
            return httpx.Response(
                200,
                json={"id": "pred-1", "status": "starting"},
            )
        if request.method == "GET" and "/predictions/pred-1" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "id": "pred-1",
                    "status": "succeeded",
                    "output": ["https://example.com/output.png"],
                },
            )
        if request.method == "GET" and "output.png" in str(request.url):
            return httpx.Response(200, content=b"\x89PNG_FAKE_BYTES")
        return httpx.Response(404, text="not mocked")

    transport = httpx.MockTransport(handler)
    p = ReplicateImageProvider(api_token="test-token")
    p._client = httpx.AsyncClient(
        base_url="https://api.replicate.com/v1",
        headers={"Authorization": "Bearer test-token"},
        transport=transport,
    )
    p.poll_interval_seconds = 0.01

    result = await p.generate(
        ImageGenRequest(
            prompt="a red circle",
            save_to=tmp_path / "out.png",
        )
    )
    await p.close()

    assert result.success
    assert len(result.image_paths) == 1
    assert result.image_paths[0] == tmp_path / "out.png"
    assert result.image_paths[0].read_bytes().startswith(b"\x89PNG")
    assert "flux-1.1-pro" in (result.model_used or "")


@pytest.mark.asyncio
async def test_replicate_no_token_returns_clean_error() -> None:
    p = ReplicateImageProvider(api_token="")
    result = await p.generate(ImageGenRequest(prompt="x"))
    assert not result.success
    assert "API token" in (result.error or "")


@pytest.mark.asyncio
async def test_replicate_failed_status_surfaces_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "predictions" in str(request.url) and request.method == "POST":
            return httpx.Response(200, json={"id": "x", "status": "starting"})
        return httpx.Response(
            200,
            json={
                "id": "x",
                "status": "failed",
                "error": "content moderation triggered",
            },
        )

    p = ReplicateImageProvider(api_token="t")
    p._client = httpx.AsyncClient(
        base_url="https://api.replicate.com/v1",
        transport=httpx.MockTransport(handler),
    )
    p.poll_interval_seconds = 0.01
    result = await p.generate(ImageGenRequest(prompt="x"))
    await p.close()
    assert not result.success
    assert "content moderation" in (result.error or "")


# ---------------------------------------------------------------------------
# fal.ai
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fal_happy_path(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "fal-ai/flux/dev" in str(request.url) and request.method == "POST":
            return httpx.Response(
                200,
                json={"images": [{"url": "https://cdn.fal.ai/abc.png"}]},
            )
        if "abc.png" in str(request.url):
            return httpx.Response(200, content=b"\x89PNG_FAL_BYTES")
        return httpx.Response(404)

    p = FalImageProvider(api_key="test")
    p._client = httpx.AsyncClient(
        base_url="https://fal.run", transport=httpx.MockTransport(handler),
    )
    result = await p.generate(
        ImageGenRequest(
            prompt="a square",
            save_to=tmp_path / "fal_out.png",
        )
    )
    await p.close()
    assert result.success
    assert result.image_paths[0] == tmp_path / "fal_out.png"
    assert "fal-ai/flux/dev" in (result.model_used or "")


@pytest.mark.asyncio
async def test_fal_no_key_returns_error() -> None:
    p = FalImageProvider(api_key="")
    result = await p.generate(ImageGenRequest(prompt="x"))
    assert not result.success
    assert "API key" in (result.error or "")


# ---------------------------------------------------------------------------
# Local SD WebUI
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_sd_happy_path(tmp_path: Path) -> None:
    """A1111 returns base64-encoded PNGs in `images` array."""
    fake_png_b64 = base64.b64encode(b"\x89PNG_LOCAL_BYTES").decode("ascii")

    def handler(request: httpx.Request) -> httpx.Response:
        assert "/sdapi/v1/txt2img" in str(request.url)
        return httpx.Response(
            200,
            json={
                "images": [fake_png_b64],
                "info": json.dumps({"sd_model_name": "test-checkpoint"}),
            },
        )

    p = LocalSDProvider(base_url="http://test:7860")
    p._client = httpx.AsyncClient(
        base_url="http://test:7860", transport=httpx.MockTransport(handler),
    )
    result = await p.generate(
        ImageGenRequest(prompt="x", save_to=tmp_path / "local.png")
    )
    await p.close()
    assert result.success
    assert result.image_paths[0].read_bytes() == b"\x89PNG_LOCAL_BYTES"
    assert "test-checkpoint" in (result.model_used or "")
    assert float(result.cost_usd) == 0.0


@pytest.mark.asyncio
async def test_local_sd_connection_refused_returns_friendly_error() -> None:
    """Common case: WebUI isn't running. Surface a clear hint."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    p = LocalSDProvider(base_url="http://test:7860")
    p._client = httpx.AsyncClient(
        base_url="http://test:7860", transport=httpx.MockTransport(handler),
    )
    result = await p.generate(ImageGenRequest(prompt="x"))
    await p.close()
    assert not result.success
    assert "running" in (result.error or "").lower()
    assert "--api" in (result.error or "")


# ---------------------------------------------------------------------------
# Codex CLI image provider
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, stdout: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self.returncode = returncode

    async def communicate(self, input: bytes | None = None):
        del input
        return self._stdout, b""

    def kill(self) -> None:
        pass


@pytest.mark.asyncio
async def test_codex_cli_happy_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "shutil.which",
        lambda n: "/usr/local/bin/codex" if n == "codex" else None,
    )
    fake_thread = "thr-test"
    fake_dir = tmp_path / fake_thread
    fake_dir.mkdir()
    src = fake_dir / "ig_xyz.png"
    src.write_bytes(b"PNG_BYTES")
    monkeypatch.setattr(
        "korpha.imagery.providers.codex_cli_image._GENERATED_IMAGES_DIR",
        tmp_path,
    )

    body = (
        f'{{"type":"thread.started","thread_id":"{fake_thread}"}}\n'
        '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n'
    ).encode()

    async def fake_exec(*_args: str, **_kwargs: Any):
        return _FakeProc(stdout=body)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    p = CodexCLIImageProvider()
    result = await p.generate(ImageGenRequest(prompt="x"))
    assert result.success
    assert result.image_paths == [src]
    assert "gpt-image-2" in (result.model_used or "")


@pytest.mark.asyncio
async def test_codex_cli_missing_binary_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda _n: None)
    p = CodexCLIImageProvider()
    result = await p.generate(ImageGenRequest(prompt="x"))
    assert not result.success
    assert "not on PATH" in (result.error or "")


# ---------------------------------------------------------------------------
# Service + loader
# ---------------------------------------------------------------------------


def test_build_provider_replicate_requires_key() -> None:
    with pytest.raises(ImageConfigError, match="api_key"):
        build_provider({"preset": "replicate"})


def test_build_provider_local_sd_no_key_required() -> None:
    p = build_provider({"preset": "local-sd", "base_url": "http://x:7860"})
    assert isinstance(p, LocalSDProvider)
    assert p.base_url == "http://x:7860"


def test_build_provider_unknown_preset_errors() -> None:
    with pytest.raises(ImageConfigError, match="unknown image preset"):
        build_provider({"preset": "midjourney"})


def test_load_image_providers_reads_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "providers.yaml"
    cfg.write_text(
        yaml.safe_dump({
            "providers": [],  # inference section, irrelevant here
            "image_providers": [
                {"preset": "fal", "api_key": "fal-key"},
                {"preset": "local-sd", "base_url": "http://gpu:7860"},
            ],
        })
    )
    monkeypatch.setenv("KORPHA_PROVIDERS_FILE", str(cfg))
    providers = load_image_providers()
    assert len(providers) == 2
    assert isinstance(providers[0], FalImageProvider)
    assert isinstance(providers[1], LocalSDProvider)


def test_load_image_providers_missing_file_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KORPHA_PROVIDERS_FILE", str(tmp_path / "nope.yaml"))
    assert load_image_providers() == []


@pytest.mark.asyncio
async def test_service_falls_through_on_first_failure() -> None:
    """First provider returns success=False; second succeeds. The
    service must surface the second's result, not error out."""

    class _AlwaysFail:
        name = "fail"

        async def generate(self, request):
            return type(
                "R", (),
                {"success": False, "image_paths": [], "error": "nope",
                 "model_used": None, "cost_usd": 0.0, "raw": {}},
            )()

        async def close(self) -> None:
            pass

    class _AlwaysWin:
        name = "win"

        async def generate(self, request):
            return type(
                "R", (),
                {"success": True, "image_paths": [Path("/tmp/x.png")],
                 "error": None, "model_used": "winner", "cost_usd": 0.0,
                 "raw": {}},
            )()

        async def close(self) -> None:
            pass

    svc = ImageGenService(providers=[_AlwaysFail(), _AlwaysWin()])
    result = await svc.generate(ImageGenRequest(prompt="x"))
    assert result.success
    assert result.model_used == "winner"


@pytest.mark.asyncio
async def test_service_no_providers_returns_actionable_error() -> None:
    svc = ImageGenService(providers=[])
    result = await svc.generate(ImageGenRequest(prompt="x"))
    assert not result.success
    assert "config image-add" in (result.error or "")


# ---------------------------------------------------------------------------
# Wizard CLI test
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    target = tmp_path / "providers.yaml"
    monkeypatch.setenv("KORPHA_PROVIDERS_FILE", str(target))
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    return target


def test_config_image_add_replicate(isolated_config: Path) -> None:
    """Walk through the wizard, picking Replicate. Output: a new entry
    under ``image_providers:`` in providers.yaml."""
    runner = CliRunner()
    answers = "\n".join([
        "1",                       # replicate
        "rep-key-test",            # API key
        "",                        # default model — accept default
    ]) + "\n"
    result = runner.invoke(app, ["config-image-add"], input=answers)
    assert result.exit_code == 0, result.stdout
    assert "Wrote to" in result.stdout

    body = yaml.safe_load(isolated_config.read_text())
    assert "image_providers" in body
    assert len(body["image_providers"]) == 1
    entry = body["image_providers"][0]
    assert entry["preset"] == "replicate"
    assert entry["api_key"] == "rep-key-test"
    assert "flux-1.1-pro" in entry["default_model"]


def test_config_image_add_local_sd(isolated_config: Path) -> None:
    runner = CliRunner()
    answers = "\n".join([
        "3",                       # local-sd
        "http://localhost:7860",   # base URL
        "",                        # default checkpoint — skip
    ]) + "\n"
    result = runner.invoke(app, ["config-image-add"], input=answers)
    assert result.exit_code == 0
    body = yaml.safe_load(isolated_config.read_text())
    entry = body["image_providers"][0]
    assert entry["preset"] == "local-sd"
    assert entry["base_url"] == "http://localhost:7860"
    assert "default_model" not in entry  # was empty


def test_config_image_add_appends_to_existing(
    isolated_config: Path,
) -> None:
    """A second wizard pass appends to image_providers, not overwrites."""
    isolated_config.write_text(
        yaml.safe_dump({
            "providers": [],
            "image_providers": [
                {"preset": "fal", "api_key": "old"},
            ],
        })
    )
    runner = CliRunner()
    answers = "\n".join(["3", "http://localhost:7860", ""]) + "\n"
    result = runner.invoke(app, ["config-image-add"], input=answers)
    assert result.exit_code == 0
    body = yaml.safe_load(isolated_config.read_text())
    assert len(body["image_providers"]) == 2
    presets = [e["preset"] for e in body["image_providers"]]
    assert presets == ["fal", "local-sd"]
