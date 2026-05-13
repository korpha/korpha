"""Browser service + provider tests using the offline mock."""
from __future__ import annotations

import pytest

from korpha.browser import (
    BrowserError,
    BrowserResult,
    BrowserService,
    BrowserTask,
    MockBrowserProvider,
)


@pytest.mark.asyncio
async def test_mock_provider_returns_canned_default() -> None:
    p = MockBrowserProvider()
    r = await p.run(BrowserTask(instruction="hello", start_url="https://x"))
    assert r.success is True
    assert r.final_url == "https://x"
    assert r.extracted_text.startswith("(mock)")


@pytest.mark.asyncio
async def test_mock_provider_canned_overrides() -> None:
    canned = BrowserResult(
        success=True,
        final_url="https://example.com/pricing",
        extracted_text="Plans start at $29/mo",
        title="Pricing — Example",
    )
    p = MockBrowserProvider(canned=canned)
    r = await p.run(BrowserTask(instruction="x", start_url="https://anywhere"))
    assert r.title == "Pricing — Example"
    assert "29" in r.extracted_text


@pytest.mark.asyncio
async def test_mock_provider_records_calls() -> None:
    p = MockBrowserProvider()
    await p.run(BrowserTask(instruction="a", start_url="https://x"))
    await p.run(BrowserTask(instruction="b", start_url="https://y"))
    assert [c.instruction for c in p.calls] == ["a", "b"]


@pytest.mark.asyncio
async def test_mock_provider_can_raise() -> None:
    p = MockBrowserProvider(raise_for_url={"https://blocked": "captcha"})
    with pytest.raises(BrowserError) as exc:
        await p.run(BrowserTask(instruction="x", start_url="https://blocked"))
    assert "captcha" in str(exc.value)


@pytest.mark.asyncio
async def test_service_with_no_providers_errors() -> None:
    svc = BrowserService(providers=[])
    with pytest.raises(BrowserError):
        await svc.run(BrowserTask(instruction="x", start_url="https://y"))


@pytest.mark.asyncio
async def test_service_returns_first_success() -> None:
    p1 = MockBrowserProvider(name="p1")
    p2 = MockBrowserProvider(
        name="p2",
        canned=BrowserResult(success=True, extracted_text="from p2"),
    )
    svc = BrowserService(providers=[p1, p2])
    # 8.8.8.8 passes the SSRF gate without needing a DNS lookup;
    # the provider failover logic is what we're testing here.
    r = await svc.run(BrowserTask(instruction="x", start_url="https://8.8.8.8/"))
    # p1 wins because it succeeds first.
    assert r.extracted_text.startswith("(mock)")
    assert len(p1.calls) == 1
    assert len(p2.calls) == 0


@pytest.mark.asyncio
async def test_service_falls_through_on_error() -> None:
    p1 = MockBrowserProvider(
        name="p1", raise_for_url={"https://8.8.8.8/": "transient"}
    )
    p2 = MockBrowserProvider(
        name="p2",
        canned=BrowserResult(success=True, extracted_text="from p2"),
    )
    svc = BrowserService(providers=[p1, p2])
    r = await svc.run(BrowserTask(instruction="x", start_url="https://8.8.8.8/"))
    assert r.success is True
    assert r.extracted_text == "from p2"


@pytest.mark.asyncio
async def test_service_falls_through_on_failed_result() -> None:
    p1 = MockBrowserProvider(
        name="p1",
        canned=BrowserResult(success=False, error="rate limited"),
    )
    p2 = MockBrowserProvider(
        name="p2",
        canned=BrowserResult(success=True, extracted_text="ok"),
    )
    svc = BrowserService(providers=[p1, p2])
    r = await svc.run(BrowserTask(instruction="x", start_url="https://8.8.8.8/"))
    assert r.success is True


@pytest.mark.asyncio
async def test_service_aggregates_errors_when_all_fail() -> None:
    p1 = MockBrowserProvider(
        name="p1", raise_for_url={"https://8.8.8.8/": "blocked"}
    )
    p2 = MockBrowserProvider(
        name="p2",
        canned=BrowserResult(success=False, error="exhausted"),
    )
    svc = BrowserService(providers=[p1, p2])
    r = await svc.run(BrowserTask(instruction="x", start_url="https://8.8.8.8/"))
    assert r.success is False
    assert r.error is not None


@pytest.mark.asyncio
async def test_service_refuses_metadata_url_without_calling_providers() -> None:
    """SSRF gate fires before any provider runs. Verifies a malicious
    prompt that tries to fetch cloud metadata gets a refusal result
    without spending the cost of opening a browser."""
    p = MockBrowserProvider(name="p")
    svc = BrowserService(providers=[p])
    r = await svc.run(BrowserTask(
        instruction="exfil",
        start_url="http://169.254.169.254/latest/meta-data/",
    ))
    assert r.success is False
    assert "private/internal/metadata" in (r.error or "")
    # Provider was never invoked
    assert len(p.calls) == 0


@pytest.mark.asyncio
async def test_service_close_calls_each_provider() -> None:
    closed: list[str] = []

    class _Tracking(MockBrowserProvider):
        async def close(self) -> None:
            closed.append(self.name)

    svc = BrowserService(providers=[_Tracking(name="a"), _Tracking(name="b")])
    await svc.close()
    assert closed == ["a", "b"]
