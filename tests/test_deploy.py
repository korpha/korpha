"""Tests for deploy adapters + the publish_landing skill."""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlmodel import Session

from korpha.business.model import Business
from korpha.deploy import (
    DeploymentTarget, LocalFileDeployer, deploy_registry,
)
from korpha.deploy.contract import (
    Deployer, DeploymentResult, slugify,
)
from korpha.identity.model import Founder
from korpha.skills import default_registry
from korpha.skills.types import SkillContext, SkillError


@pytest.fixture(autouse=True)
def reset_registry() -> None:
    deploy_registry.reset()
    yield
    deploy_registry.reset()


# ---- slugify ----


def test_slugify_basic() -> None:
    assert slugify("Pricing Page") == "pricing-page"


def test_slugify_collapses_runs() -> None:
    assert slugify("a   b___c") == "a-b-c"


def test_slugify_trims_dashes() -> None:
    assert slugify("---x---") == "x"


def test_slugify_blank_falls_back_to_site() -> None:
    assert slugify("   ") == "site"
    assert slugify("!@#$") == "site"


def test_slugify_caps_length() -> None:
    s = slugify("a" * 100)
    assert len(s) <= 60


# ---- DeploymentTarget.from_html ----


def test_target_from_html_includes_index(
    business: Business,
) -> None:
    t = DeploymentTarget.from_html(
        business_id=business.id,
        slug="ship the demo",
        html="<h1>hi</h1>",
        title="Demo",
    )
    assert t.slug == "ship-the-demo"
    assert "index.html" in t.files
    assert t.files["index.html"] == "<h1>hi</h1>"


def test_target_from_html_with_extras(
    business: Business,
) -> None:
    t = DeploymentTarget.from_html(
        business_id=business.id,
        slug="x", html="<h1>hi</h1>",
        extras={"style.css": "body{}"},
    )
    assert t.files["style.css"] == "body{}"


# ---- LocalFileDeployer ----


@pytest.fixture
def deploys_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Path:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    return tmp_path / "deploys"


@pytest.mark.asyncio
async def test_deploy_writes_files(
    business: Business, deploys_root: Path,
) -> None:
    deployer = LocalFileDeployer()
    result = await deployer.deploy(DeploymentTarget.from_html(
        business_id=business.id,
        slug="hi",
        html="<h1>hello world</h1>",
        title="Hi",
    ))
    biz_dir = deploys_root / str(business.id) / "hi"
    assert (biz_dir / "index.html").read_text() == (
        "<h1>hello world</h1>"
    )
    assert "/app/deploys/" in result.url
    assert str(business.id) in result.url
    assert result.url.endswith("/hi/")
    assert result.bytes_written == len("<h1>hello world</h1>")
    assert result.deployer_name == "local-file"


@pytest.mark.asyncio
async def test_deploy_overwrites_previous(
    business: Business, deploys_root: Path,
) -> None:
    deployer = LocalFileDeployer()
    await deployer.deploy(DeploymentTarget.from_html(
        business_id=business.id, slug="x", html="<h1>v1</h1>",
    ))
    # Same slug, fresh content + a removed file
    await deployer.deploy(DeploymentTarget(
        business_id=business.id, slug="x",
        files={"index.html": "<h1>v2</h1>"},
        title="x",
    ))
    biz_dir = deploys_root / str(business.id) / "x"
    assert (biz_dir / "index.html").read_text() == "<h1>v2</h1>"


@pytest.mark.asyncio
async def test_deploy_requires_index_html(
    business: Business, deploys_root: Path,
) -> None:
    deployer = LocalFileDeployer()
    with pytest.raises(ValueError, match="index.html"):
        await deployer.deploy(DeploymentTarget(
            business_id=business.id, slug="x",
            files={"about.html": "<h1>x</h1>"},
        ))


@pytest.mark.asyncio
async def test_deploy_refuses_path_traversal(
    business: Business, deploys_root: Path,
) -> None:
    deployer = LocalFileDeployer()
    with pytest.raises(ValueError, match="unsafe path"):
        await deployer.deploy(DeploymentTarget(
            business_id=business.id, slug="x",
            files={
                "index.html": "<h1>x</h1>",
                "../escape.txt": "bad",
            },
        ))


@pytest.mark.asyncio
async def test_teardown_removes(
    business: Business, deploys_root: Path,
) -> None:
    deployer = LocalFileDeployer()
    await deployer.deploy(DeploymentTarget.from_html(
        business_id=business.id, slug="x", html="<h1>x</h1>",
    ))
    assert await deployer.teardown(
        slug="x", business_id=business.id,
    ) is True
    biz_dir = deploys_root / str(business.id) / "x"
    assert not biz_dir.exists()


@pytest.mark.asyncio
async def test_teardown_unknown_returns_false(
    business: Business, deploys_root: Path,
) -> None:
    deployer = LocalFileDeployer()
    assert await deployer.teardown(
        slug="ghost", business_id=business.id,
    ) is False


# ---- registry ----


def test_default_active_is_local_file() -> None:
    """Fresh registry → fall back to LocalFileDeployer."""
    deployer = deploy_registry.active()
    assert isinstance(deployer, LocalFileDeployer)


def test_set_active_replaces() -> None:
    class _Dummy(Deployer):
        name = "dummy"

        async def deploy(self, target):
            return DeploymentResult(
                url="https://x", slug="x",
                deployer_name=self.name, bytes_written=0,
            )

        async def teardown(self, *, slug, business_id):
            return True

    deploy_registry.set_active(_Dummy(), plugin_name="t")
    assert deploy_registry.active().name == "dummy"


# ---- deploy.publish_landing skill ----


def _ctx(session, business, founder):
    from korpha.inference.cost_tracker import CostTracker
    from korpha.inference.pool import InferencePool

    return SkillContext(
        business=business, founder=founder, session=session,
        cost_tracker=CostTracker(pool=InferencePool(
            providers=[], accounts=[],
        )),
    )


@pytest.mark.asyncio
async def test_publish_with_structured_inputs(
    session: Session, business: Business, founder: Founder,
    deploys_root: Path,
) -> None:
    skill = default_registry.skills["deploy.publish_landing"]
    result = await skill.run(
        ctx=_ctx(session, business, founder),
        args={
            "headline": "Stop fixing deploys at 2am",
            "subhead": "Your cofounder ships while you sleep",
            "cta_label": "Get early access",
            "cta_url": "https://stripe.com/buy/abc",
        },
    )
    assert "url" in result.payload
    biz_dir = deploys_root / str(business.id)
    deployed = list(biz_dir.iterdir())
    assert len(deployed) == 1
    html = (deployed[0] / "index.html").read_text()
    assert "Stop fixing deploys at 2am" in html
    assert "Get early access" in html
    assert "https://stripe.com/buy/abc" in html


@pytest.mark.asyncio
async def test_publish_with_pre_rendered_html(
    session: Session, business: Business, founder: Founder,
    deploys_root: Path,
) -> None:
    skill = default_registry.skills["deploy.publish_landing"]
    custom = "<!doctype html><html><body>HAND-WRITTEN</body></html>"
    result = await skill.run(
        ctx=_ctx(session, business, founder),
        args={"slug": "custom", "html": custom},
    )
    biz_dir = deploys_root / str(business.id) / "custom"
    assert (biz_dir / "index.html").read_text() == custom


@pytest.mark.asyncio
async def test_publish_html_escapes_lt_gt(
    session: Session, business: Business, founder: Founder,
    deploys_root: Path,
) -> None:
    """LLM-generated copy that contains < or > shouldn't break
    the HTML wrapper."""
    skill = default_registry.skills["deploy.publish_landing"]
    await skill.run(
        ctx=_ctx(session, business, founder),
        args={
            "headline": "<script>alert(1)</script>",
            "subhead": "x",
            "cta_label": "y",
            "cta_url": "z",
        },
    )
    biz_dir = deploys_root / str(business.id)
    deployed = list(biz_dir.iterdir())
    html = (deployed[0] / "index.html").read_text()
    assert "&lt;script&gt;" in html
    assert "<script>alert(1)" not in html


@pytest.mark.asyncio
async def test_publish_rejects_blank_inputs(
    session: Session, business: Business, founder: Founder,
) -> None:
    skill = default_registry.skills["deploy.publish_landing"]
    with pytest.raises(SkillError, match="html=|headline="):
        await skill.run(
            ctx=_ctx(session, business, founder), args={},
        )


@pytest.mark.asyncio
async def test_publish_rejects_partial_structured(
    session: Session, business: Business, founder: Founder,
) -> None:
    skill = default_registry.skills["deploy.publish_landing"]
    with pytest.raises(SkillError, match="non-empty"):
        await skill.run(
            ctx=_ctx(session, business, founder),
            args={"headline": "hi"},  # missing subhead/cta
        )


@pytest.mark.asyncio
async def test_publish_attaches_artifact_to_kanban_card(
    session: Session, business: Business, founder: Founder,
    deploys_root: Path,
) -> None:
    """When kanban_card_id is provided, the deploy URL lands as
    a typed DEPLOY artifact tagged primary."""
    from korpha.kanban import (
        ArtifactKind, ArtifactService, CreateCardInput,
        KanbanBoard,
    )

    board = KanbanBoard(session)
    card = board.create(CreateCardInput(
        business_id=business.id, title="ship landing",
    ))

    skill = default_registry.skills["deploy.publish_landing"]
    await skill.run(
        ctx=_ctx(session, business, founder),
        args={
            "headline": "Stop the bleed",
            "subhead": "x",
            "cta_label": "y",
            "cta_url": "https://example.com",
            "kanban_card_id": str(card.id),
        },
    )
    arts = ArtifactService(session).list_for_card(card.id)
    assert len(arts) == 1
    assert arts[0].kind == ArtifactKind.DEPLOY
    assert arts[0].is_primary is True
    assert "/app/deploys/" in arts[0].location
