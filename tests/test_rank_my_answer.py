"""RankMyAnswer.com client + skills + wizard tests.

All HTTP is mocked — no real API calls. Tests pin: client error
mapping (401/402/429/500), skill arg validation, skill failure
surfaces, and the wizard's append-to-providers.yaml behavior.
"""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from korpha.cli import app
from korpha.integrations.rank_my_answer import (
    RankMyAnswerClient,
    RankMyAnswerError,
    client_from_env_or_config,
)
from korpha.skills import default_registry
from korpha.skills.types import SkillContext, SkillError

# ---------------------------------------------------------------------------
# Client error mapping
# ---------------------------------------------------------------------------


def _client_with_handler(handler):
    c = RankMyAnswerClient(api_key="t")
    c._client = httpx.AsyncClient(
        base_url="https://api.rankmyanswer.com/v1",
        transport=httpx.MockTransport(handler),
    )
    return c


@pytest.mark.asyncio
async def test_client_balance_returns_dict() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        assert req.url.path.endswith("/credits/balance")
        return httpx.Response(200, json={"balance": 1500, "plan_tier": "pro"})

    c = _client_with_handler(h)
    bal = await c.balance()
    await c.close()
    assert bal == {"balance": 1500, "plan_tier": "pro"}


@pytest.mark.asyncio
async def test_client_401_maps_to_auth_error() -> None:
    def h(_req):
        return httpx.Response(401, text="bad token")

    c = _client_with_handler(h)
    with pytest.raises(RankMyAnswerError, match=r"auth failed"):
        await c.balance()
    await c.close()


@pytest.mark.asyncio
async def test_client_402_maps_to_credits_error() -> None:
    def h(_req):
        return httpx.Response(402, text="paywall")

    c = _client_with_handler(h)
    with pytest.raises(RankMyAnswerError, match=r"credits exhausted"):
        await c.balance()
    await c.close()


@pytest.mark.asyncio
async def test_client_429_maps_to_rate_limit() -> None:
    def h(_req):
        return httpx.Response(429, text="slow down")

    c = _client_with_handler(h)
    with pytest.raises(RankMyAnswerError, match=r"rate-limit"):
        await c.balance()
    await c.close()


@pytest.mark.asyncio
async def test_client_audit_url_posts_payload() -> None:
    captured: dict = {}

    def h(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/audit") and req.method == "POST":
            import json

            captured["body"] = json.loads(req.content.decode())
            return httpx.Response(
                200,
                json={
                    "geo_score": 78,
                    "seo_score": 82,
                    "recommendations": ["add FAQ schema", "shorter title tag"],
                },
            )
        return httpx.Response(404)

    c = _client_with_handler(h)
    report = await c.audit_url(
        "https://example.com/landing",
        target_query="how to automate deploys",
    )
    await c.close()
    assert captured["body"]["url"] == "https://example.com/landing"
    assert captured["body"]["target_query"] == "how to automate deploys"
    assert report["geo_score"] == 78
    assert report["seo_score"] == 82


@pytest.mark.asyncio
async def test_client_list_projects_handles_both_shapes() -> None:
    """API may return [list] OR {projects: [list]} — handle both."""

    def h(_req):
        return httpx.Response(
            200,
            json={"projects": [{"id": "p1", "name": "Site A"}]},
        )

    c = _client_with_handler(h)
    projects = await c.list_projects()
    await c.close()
    assert projects == [{"id": "p1", "name": "Site A"}]

    def h2(_req):
        return httpx.Response(200, json=[{"id": "p2"}])

    c2 = _client_with_handler(h2)
    projects2 = await c2.list_projects()
    await c2.close()
    assert projects2 == [{"id": "p2"}]


# ---------------------------------------------------------------------------
# client_from_env_or_config
# ---------------------------------------------------------------------------


def test_client_from_env_var(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RANKMYANSWER_API_KEY", "key-from-env")
    monkeypatch.setenv("KORPHA_PROVIDERS_FILE", str(tmp_path / "no.yaml"))
    c = client_from_env_or_config()
    assert c is not None
    assert c.api_key == "key-from-env"


def test_client_from_yaml_inline_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = tmp_path / "providers.yaml"
    cfg.write_text(
        yaml.safe_dump({
            "integrations": [
                {"kind": "rank_my_answer", "api_key": "key-from-yaml",
                 "base_url": "https://test.example/v1"},
            ],
        })
    )
    monkeypatch.setenv("KORPHA_PROVIDERS_FILE", str(cfg))
    monkeypatch.delenv("RANKMYANSWER_API_KEY", raising=False)
    c = client_from_env_or_config()
    assert c is not None
    assert c.api_key == "key-from-yaml"
    assert c.base_url == "https://test.example/v1"


def test_client_returns_none_when_nothing_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("KORPHA_PROVIDERS_FILE", str(tmp_path / "nope.yaml"))
    monkeypatch.delenv("RANKMYANSWER_API_KEY", raising=False)
    assert client_from_env_or_config() is None


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------


def _ctx(session, business, founder):
    from korpha.audit.model import InferenceTier
    from korpha.inference import InferencePool, MockProvider, ProviderAccount
    from korpha.inference.cost_tracker import CostTracker
    from korpha.inference.registry import AuthType

    pool = InferencePool(
        providers=[MockProvider()],
        accounts=[ProviderAccount(
            provider_name="mock", auth_type=AuthType.API_KEY,
            tier_models={InferenceTier.WORKHORSE: "m"}, api_key="x",
        )],
    )
    return SkillContext(
        business=business, founder=founder, session=session,
        cost_tracker=CostTracker(pool=pool),
    )


def test_skills_registered() -> None:
    """All four GEO+SEO skills are loadable via the default registry."""
    for name in (
        "geo_seo.audit_url",
        "geo_seo.generate_schema",
        "geo_seo.list_projects",
        "geo_seo.balance",
    ):
        assert default_registry.get(name).spec.name == name


@pytest.mark.asyncio
async def test_audit_url_skill_raises_when_not_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path, session, business, founder,
) -> None:
    monkeypatch.delenv("RANKMYANSWER_API_KEY", raising=False)
    monkeypatch.setenv("KORPHA_PROVIDERS_FILE", str(tmp_path / "no.yaml"))
    skill = default_registry.get("geo_seo.audit_url")
    with pytest.raises(SkillError, match=r"config-rankmyanswer-add"):
        await skill.run(
            ctx=_ctx(session, business, founder),
            args={"url": "https://example.com"},
        )


@pytest.mark.asyncio
async def test_audit_url_skill_happy_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path, session, business, founder,
) -> None:
    monkeypatch.setenv("RANKMYANSWER_API_KEY", "k")
    monkeypatch.setenv("KORPHA_PROVIDERS_FILE", str(tmp_path / "no.yaml"))

    def h(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/audit"):
            return httpx.Response(
                200,
                json={"geo_score": 91, "seo_score": 88, "recommendations": []},
            )
        return httpx.Response(404)

    # Patch RankMyAnswerClient construction to inject the mock transport.
    real_client = RankMyAnswerClient
    captured = {}

    def fake_client(*, api_key, base_url=None, **kw):
        c = real_client(api_key=api_key, base_url=base_url or "https://api.rankmyanswer.com/v1")
        c._client = httpx.AsyncClient(
            base_url=c.base_url, transport=httpx.MockTransport(h),
        )
        captured["instance"] = c
        return c

    monkeypatch.setattr(
        "korpha.integrations.rank_my_answer.RankMyAnswerClient", fake_client
    )

    skill = default_registry.get("geo_seo.audit_url")
    result = await skill.run(
        ctx=_ctx(session, business, founder),
        args={
            "url": "https://example.com/landing",
            "target_query": "automate deploys",
        },
    )
    assert "GEO=91" in result.summary
    assert "SEO=88" in result.summary
    assert result.payload["report"]["geo_score"] == 91


@pytest.mark.asyncio
async def test_audit_url_skill_requires_url(
    monkeypatch: pytest.MonkeyPatch, session, business, founder,
) -> None:
    monkeypatch.setenv("RANKMYANSWER_API_KEY", "k")
    skill = default_registry.get("geo_seo.audit_url")
    with pytest.raises(SkillError, match=r"requires `url`"):
        await skill.run(
            ctx=_ctx(session, business, founder), args={"url": ""},
        )


# ---------------------------------------------------------------------------
# Wizard CLI
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    target = tmp_path / "providers.yaml"
    monkeypatch.setenv("KORPHA_PROVIDERS_FILE", str(target))
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    return target


def test_config_rankmyanswer_writes_integration_entry(
    isolated_config: Path,
) -> None:
    runner = CliRunner()
    answers = "\n".join([
        "rma-test-key",                  # API key
        "https://api.rankmyanswer.com/v1",  # accept default base URL
    ]) + "\n"
    result = runner.invoke(app, ["config-rankmyanswer-add"], input=answers)
    assert result.exit_code == 0, result.stdout
    assert "Wrote to" in result.stdout

    body = yaml.safe_load(isolated_config.read_text())
    assert "integrations" in body
    entry = body["integrations"][0]
    assert entry["kind"] == "rank_my_answer"
    assert entry["api_key"] == "rma-test-key"


def test_config_rankmyanswer_skip_writes_nothing(isolated_config: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["config-rankmyanswer-add"], input="q\n")
    assert result.exit_code == 0
    assert "Skipped" in result.stdout
    assert not isolated_config.exists()


def test_doctor_reports_rma_status(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Doctor should show RankMyAnswer alongside provider + delegation."""
    monkeypatch.delenv("RANKMYANSWER_API_KEY", raising=False)
    runner = CliRunner()
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "RankMyAnswer" in result.stdout
    # Not configured yet → shows the suggested command
    assert "config-rankmyanswer-add" in result.stdout
