"""AgentBrowserCliProvider tests.

The provider shells out to the ``agent-browser`` npm CLI. We monkeypatch
``asyncio.create_subprocess_exec`` to return canned JSON instead of
spawning a real process — both because the CI box doesn't have Node and
because we want to exercise the JSON-parsing + error paths deterministically.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from korpha.browser import BrowserError, BrowserTask
from korpha.browser.providers.agent_browser_cli import (
    AgentBrowserCliProvider,
    _resolve_agent_browser,
)


class _FakeProc:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def kill(self) -> None:
        pass


def _make_subprocess_factory(responses: dict[str, dict[str, Any]]):
    """Build a fake create_subprocess_exec that picks a canned response
    based on the *command* (``open``, ``ariaSnapshot``, etc) found in argv."""

    calls: list[list[str]] = []

    async def fake_exec(*args: str, **_kwargs: Any) -> _FakeProc:
        calls.append(list(args))
        # argv is [bin, --session, NAME, --json, COMMAND, ...rest]
        try:
            idx = args.index("--json")
            command = args[idx + 1]
        except (ValueError, IndexError):
            command = "<unknown>"
        payload = responses.get(command, {"success": True})
        return _FakeProc(stdout=json.dumps(payload).encode("utf-8"))

    fake_exec.calls = calls  # type: ignore[attr-defined]
    return fake_exec


@pytest.mark.asyncio
async def test_provider_raises_when_cli_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "korpha.browser.providers.agent_browser_cli._resolve_agent_browser",
        lambda: None,
    )
    p = AgentBrowserCliProvider()
    with pytest.raises(BrowserError, match="agent-browser CLI not found"):
        await p.run(BrowserTask(instruction="x", start_url="https://example.com"))


@pytest.mark.asyncio
async def test_provider_requires_start_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "korpha.browser.providers.agent_browser_cli._resolve_agent_browser",
        lambda: ["agent-browser"],
    )
    p = AgentBrowserCliProvider()
    with pytest.raises(BrowserError, match=r"requires task\.start_url"):
        await p.run(BrowserTask(instruction="x"))


@pytest.mark.asyncio
async def test_provider_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "korpha.browser.providers.agent_browser_cli._resolve_agent_browser",
        lambda: ["/fake/agent-browser"],
    )
    fake = _make_subprocess_factory(
        {
            "open": {"success": True},
            "ariaSnapshot": {
                "success": True,
                "snapshot": "- heading 'Welcome' [ref=h1]\n- button 'Sign in' [ref=b2]",
            },
            "eval": {
                "success": True,
                "result": {"title": "Example", "url": "https://example.com/landing"},
            },
        }
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

    p = AgentBrowserCliProvider()
    try:
        r = await p.run(BrowserTask(instruction="x", start_url="https://example.com"))
        assert r.success is True
        assert r.title == "Example"
        assert r.final_url == "https://example.com/landing"
        assert "Welcome" in r.extracted_text
        # Verify open / ariaSnapshot / eval all got dispatched
        commands = [
            c[c.index("--json") + 1] for c in fake.calls if "--json" in c
        ]
        assert commands == ["open", "ariaSnapshot", "eval"]
    finally:
        await p.close()


@pytest.mark.asyncio
async def test_provider_open_failure_returns_failed_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "korpha.browser.providers.agent_browser_cli._resolve_agent_browser",
        lambda: ["/fake/agent-browser"],
    )
    fake = _make_subprocess_factory(
        {"open": {"success": False, "error": "DNS lookup failed"}}
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)

    p = AgentBrowserCliProvider()
    try:
        r = await p.run(BrowserTask(instruction="x", start_url="https://nope.invalid"))
        assert r.success is False
        assert "DNS lookup failed" in (r.error or "")
        # ariaSnapshot must NOT have run after open failed
        commands = [
            c[c.index("--json") + 1] for c in fake.calls if "--json" in c
        ]
        assert commands == ["open"]
    finally:
        await p.close()


@pytest.mark.asyncio
async def test_provider_handles_non_json_output(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "korpha.browser.providers.agent_browser_cli._resolve_agent_browser",
        lambda: ["/fake/agent-browser"],
    )

    async def fake_exec(*args: str, **_kwargs: Any) -> _FakeProc:
        idx = args.index("--json")
        command = args[idx + 1]
        if command == "open":
            # Banner before the JSON line is the one real-world failure
            # mode the parser must tolerate.
            return _FakeProc(
                stdout=b"loading daemon...\n{\"success\": true}\n"
            )
        if command == "ariaSnapshot":
            return _FakeProc(stdout=b"not json at all")
        return _FakeProc(stdout=b'{"success": true, "result": {}}')

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    p = AgentBrowserCliProvider()
    try:
        # ariaSnapshot returning garbage shouldn't crash the run — open
        # already succeeded, so we still get a successful BrowserResult
        # with empty text.
        r = await p.run(BrowserTask(instruction="x", start_url="https://example.com"))
        assert r.success is True
        assert r.extracted_text == ""
    finally:
        await p.close()


@pytest.mark.asyncio
async def test_provider_text_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "korpha.browser.providers.agent_browser_cli._resolve_agent_browser",
        lambda: ["/fake/agent-browser"],
    )
    huge = "x" * 80_000
    fake = _make_subprocess_factory(
        {
            "open": {"success": True},
            "ariaSnapshot": {"success": True, "snapshot": huge},
            "eval": {"success": True, "result": {"title": "T", "url": "u"}},
        }
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    p = AgentBrowserCliProvider(text_char_limit=5_000)
    try:
        r = await p.run(BrowserTask(instruction="x", start_url="https://example.com"))
        assert r.success is True
        assert len(r.extracted_text) <= 5_000 + 50  # +epsilon for the marker
        assert r.extracted_text.endswith("[truncated]")
    finally:
        await p.close()


@pytest.mark.asyncio
async def test_provider_timeout_surfaces_as_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "korpha.browser.providers.agent_browser_cli._resolve_agent_browser",
        lambda: ["/fake/agent-browser"],
    )

    class _HangingProc:
        returncode = None

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.sleep(10)
            return b"", b""

        def kill(self) -> None:
            pass

    async def fake_exec(*_args: str, **_kwargs: Any) -> _HangingProc:
        return _HangingProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    p = AgentBrowserCliProvider()
    try:
        r = await p.run(
            BrowserTask(
                instruction="x",
                start_url="https://example.com",
                timeout_seconds=0.1,
            )
        )
        # open will time out → returns a failed BrowserResult, not raises
        assert r.success is False
        assert "timed out" in (r.error or "")
    finally:
        await p.close()


def test_resolve_prefers_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/local/bin/agent-browser" if name == "agent-browser" else None,
    )
    assert _resolve_agent_browser() == ["/usr/local/bin/agent-browser"]


def test_resolve_falls_back_to_npx(monkeypatch: pytest.MonkeyPatch) -> None:
    def which(name: str) -> str | None:
        return "/usr/local/bin/npx" if name == "npx" else None

    monkeypatch.setattr("shutil.which", which)
    monkeypatch.setattr(
        "pathlib.Path.exists", lambda self: False
    )
    assert _resolve_agent_browser() == ["/usr/local/bin/npx", "agent-browser"]


def test_resolve_returns_none_when_nothing_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: None)
    monkeypatch.setattr(
        "pathlib.Path.exists", lambda self: False
    )
    assert _resolve_agent_browser() is None
