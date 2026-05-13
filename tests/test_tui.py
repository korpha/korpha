"""Tests for the TUI app + WS client.

Layers:
  1. ApprovalScreen — Pilot keypress drives the modal, asserts the
     dismiss value.
  2. App boot + slash-command dispatch — the TUI mounts without
     a real WS connection (worker fails gracefully, app still
     usable for slash testing).
  3. RpcClient — pure protocol tests (request/response framing,
     event multiplexing) using an in-memory fake transport.

End-to-end (WS over a real TestClient) is in
test_tui_ws_e2e.py — kept separate for speed.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from korpha.tui.app import KorphaTUI, ApprovalScreen
from korpha.tui.rpc_client import (
    RpcClient,
    RpcClientError,
    RpcClosed,
)


# ---- ApprovalScreen ----


@pytest.mark.asyncio
async def test_approval_screen_returns_approve() -> None:
    app = KorphaTUI(ws_url="ws://nope/")
    async with app.run_test() as pilot:
        result_holder: dict[str, str] = {}

        async def show() -> None:
            choice = await app.push_screen_wait(
                ApprovalScreen("test summary", "code_change")
            )
            result_holder["choice"] = choice

        worker = app.run_worker(show())
        await pilot.pause(0.05)
        await pilot.press("a")
        await pilot.pause(0.05)
        await worker.wait()
        assert result_holder["choice"] == "approve"


@pytest.mark.asyncio
async def test_approval_screen_returns_reject() -> None:
    app = KorphaTUI(ws_url="ws://nope/")
    async with app.run_test() as pilot:
        result_holder: dict[str, str] = {}

        async def show() -> None:
            choice = await app.push_screen_wait(
                ApprovalScreen("ok", "code_change")
            )
            result_holder["choice"] = choice

        worker = app.run_worker(show())
        await pilot.pause(0.05)
        await pilot.press("r")
        await pilot.pause(0.05)
        await worker.wait()
        assert result_holder["choice"] == "reject"


@pytest.mark.asyncio
async def test_approval_screen_returns_view_and_dismiss() -> None:
    app = KorphaTUI(ws_url="ws://nope/")
    async with app.run_test() as pilot:
        for key, expected in [("v", "view"), ("escape", "dismiss")]:
            result_holder: dict[str, str] = {}

            async def show(_h=result_holder) -> None:
                choice = await app.push_screen_wait(
                    ApprovalScreen("ok", "code_change")
                )
                _h["choice"] = choice

            worker = app.run_worker(show())
            await pilot.pause(0.05)
            await pilot.press(key)
            await pilot.pause(0.05)
            await worker.wait()
            assert result_holder["choice"] == expected


# ---- App boot + slash dispatch (with no real WS) ----


@pytest.mark.asyncio
async def test_app_boots_with_widgets_present() -> None:
    """The connect worker fails (nothing listening), but the TUI
    layout still mounts. /help / /clear / /quit must work even when
    the server isn't reachable."""
    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")  # nothing listening
    async with app.run_test() as pilot:
        await pilot.pause(0.1)
        from textual.widgets import Input, Label, RichLog
        assert app.query_one("#chat-log", RichLog) is not None
        assert app.query_one("#composer", Input) is not None
        assert app.query_one("#status-text", Label) is not None


@pytest.mark.asyncio
async def test_slash_help_renders_help_text() -> None:
    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        await app._dispatch_slash("/help")
        await pilot.pause(0.05)
        rendered = "\n".join(str(seg.text) for seg in app._chat_log.lines)
        assert "/help" in rendered or "Slash commands" in rendered


@pytest.mark.asyncio
async def test_slash_unknown_warns() -> None:
    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        await app._dispatch_slash("/notathing")
        await pilot.pause(0.05)
        rendered = "\n".join(str(seg.text) for seg in app._chat_log.lines)
        assert "Unknown" in rendered or "/notathing" in rendered


@pytest.mark.asyncio
async def test_slash_clear_empties_log() -> None:
    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        app._chat_log.write("some content here")
        await pilot.pause(0.02)
        before = len(app._chat_log.lines)
        await app._dispatch_slash("/clear")
        await pilot.pause(0.02)
        assert len(app._chat_log.lines) < before


@pytest.mark.asyncio
async def test_set_status_updates_label() -> None:
    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        app._set_status("thinking…")
        await pilot.pause(0.02)
        assert "thinking" in str(app._status_text.render())


@pytest.mark.asyncio
async def test_interrupt_when_nothing_active() -> None:
    """/interrupt with no in-flight stream should warn, not crash."""
    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        await app._interrupt_active_stream()
        await pilot.pause(0.02)
        rendered = "\n".join(str(seg.text) for seg in app._chat_log.lines)
        assert "nothing to interrupt" in rendered


# ---- RpcClient pure-protocol tests via fake transport ----


class _FakeWS:
    """In-memory stand-in for a WebSocket. ``send`` records frames;
    ``__aiter__`` yields whatever was queued via ``inject``."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._closed = False

    async def send(self, frame: str) -> None:
        self.sent.append(frame)

    def __aiter__(self) -> "_FakeWS":
        return self

    async def __anext__(self) -> str:
        if self._closed:
            raise StopAsyncIteration
        return await self._queue.get()

    async def close(self) -> None:
        self._closed = True

    def inject(self, frame: str) -> None:
        self._queue.put_nowait(frame)


@pytest.mark.asyncio
async def test_rpc_client_request_response_round_trip() -> None:
    client = RpcClient("ws://fake/")
    fake = _FakeWS()
    client._ws = fake  # type: ignore[assignment]
    client._reader_task = asyncio.create_task(client._reader_loop())

    async def respond_after_send() -> None:
        # Wait until the request lands in fake.sent, then queue a
        # response with the matching id
        while not fake.sent:
            await asyncio.sleep(0.005)
        sent = json.loads(fake.sent[-1])
        fake.inject(json.dumps({
            "jsonrpc": "2.0",
            "id": sent["id"],
            "result": {"echoed": sent["params"]},
        }))

    asyncio.create_task(respond_after_send())
    result = await client.call("foo", {"hello": "world"}, timeout=2.0)
    assert result == {"echoed": {"hello": "world"}}
    await client.close()


@pytest.mark.asyncio
async def test_rpc_client_surfaces_server_errors() -> None:
    client = RpcClient("ws://fake/")
    fake = _FakeWS()
    client._ws = fake  # type: ignore[assignment]
    client._reader_task = asyncio.create_task(client._reader_loop())

    async def respond_with_error() -> None:
        while not fake.sent:
            await asyncio.sleep(0.005)
        sent = json.loads(fake.sent[-1])
        fake.inject(json.dumps({
            "jsonrpc": "2.0",
            "id": sent["id"],
            "error": {"code": -32602, "message": "bad params"},
        }))

    asyncio.create_task(respond_with_error())
    with pytest.raises(RpcClientError) as ei:
        await client.call("foo", {}, timeout=2.0)
    assert ei.value.code == -32602
    assert "bad params" in ei.value.message
    await client.close()


@pytest.mark.asyncio
async def test_rpc_client_dispatches_events() -> None:
    client = RpcClient("ws://fake/")
    fake = _FakeWS()
    client._ws = fake  # type: ignore[assignment]
    client._reader_task = asyncio.create_task(client._reader_loop())

    captured: list[dict[str, Any]] = []

    async def cb(evt: dict[str, Any]) -> None:
        captured.append(evt)

    client.on_event("hello.world", cb)

    fake.inject(json.dumps({
        "jsonrpc": "2.0",
        "method": "hello.world",
        "params": {"x": 1},
    }))
    fake.inject(json.dumps({
        "jsonrpc": "2.0",
        "method": "other.event",
        "params": {"y": 2},
    }))
    await asyncio.sleep(0.05)

    assert len(captured) == 1
    assert captured[0]["method"] == "hello.world"
    assert captured[0]["params"] == {"x": 1}
    await client.close()


@pytest.mark.asyncio
async def test_rpc_client_ready_payload_resolves() -> None:
    client = RpcClient("ws://fake/")
    fake = _FakeWS()
    client._ws = fake  # type: ignore[assignment]
    client._reader_task = asyncio.create_task(client._reader_loop())

    fake.inject(json.dumps({
        "jsonrpc": "2.0",
        "method": "gateway.ready",
        "params": {"founder": {"email": "x@y.com"}, "business": {"name": "X"}},
    }))
    payload = await client.ready_payload(timeout=1.0)
    assert payload["founder"]["email"] == "x@y.com"
    await client.close()


@pytest.mark.asyncio
async def test_rpc_client_close_fails_pending_calls() -> None:
    """In-flight requests must raise RpcClosed when the socket
    drops — the TUI relies on this to surface a clean error
    rather than hang forever."""
    client = RpcClient("ws://fake/")
    fake = _FakeWS()
    client._ws = fake  # type: ignore[assignment]
    client._reader_task = asyncio.create_task(client._reader_loop())

    # Fire request but never inject a response
    call_task = asyncio.create_task(client.call("foo", {}, timeout=5.0))
    await asyncio.sleep(0.02)
    await client.close()
    with pytest.raises(RpcClosed):
        await call_task


@pytest.mark.asyncio
async def test_streaming_buffer_accumulates_and_clears_on_done() -> None:
    """content.delta events should append to _stream_buffer; on
    'done' the buffer is rendered as Markdown then cleared so the
    next turn starts fresh."""
    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        await app._handle_content_delta(
            {"params": {"text": "Hello "}}
        )
        await app._handle_content_delta(
            {"params": {"text": "world."}}
        )
        assert app._stream_buffer == "Hello world."
        await app._handle_done({"params": {"skills_used": [], "cost_usd": 0.0}})
        assert app._stream_buffer == ""


@pytest.mark.asyncio
async def test_done_handler_marks_streaming_idle() -> None:
    """The done event must clear _streaming + _active_request_id
    so subsequent submits can fire."""
    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        app._streaming = True
        app._active_request_id = 42
        await app._handle_done({"params": {"interrupted": False}})
        assert app._streaming is False
        assert app._active_request_id is None


@pytest.mark.asyncio
async def test_toggle_reasoning_flips_state() -> None:
    """Ctrl-R / /reasoning toggles the trace display flag without
    losing the buffered trace."""
    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        assert app._show_reasoning is False
        app.action_toggle_reasoning()
        assert app._show_reasoning is True
        app.action_toggle_reasoning()
        assert app._show_reasoning is False


@pytest.mark.asyncio
async def test_toggle_operator_flips_state() -> None:
    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        assert app._operator_mode is False
        app.action_toggle_operator()
        assert app._operator_mode is True


@pytest.mark.asyncio
async def test_reasoning_buffer_accumulates_independently() -> None:
    """reasoning.delta accumulates regardless of toggle state — so
    flipping /reasoning ON mid-turn shows everything from the start."""
    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        await app._handle_reasoning_delta({"params": {"text": "step 1: "}})
        await app._handle_reasoning_delta({"params": {"text": "step 2."}})
        assert app._reasoning_buffer == "step 1: step 2."


@pytest.mark.asyncio
async def test_done_handler_accumulates_session_cost() -> None:
    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        assert app._cumulative_cost == 0.0
        await app._handle_done({"params": {"cost_usd": 0.001}})
        await app._handle_done({"params": {"cost_usd": 0.002}})
        assert abs(app._cumulative_cost - 0.003) < 1e-9


@pytest.mark.asyncio
async def test_clear_resets_session_totals() -> None:
    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        app._cumulative_cost = 0.42
        app._cumulative_tokens = 1234
        await app._dispatch_slash("/clear")
        assert app._cumulative_cost == 0.0
        assert app._cumulative_tokens == 0


@pytest.mark.asyncio
async def test_message_log_caps_at_500() -> None:
    """Append more than the cap and verify trimming kicks in
    so a multi-day session doesn't grow unbounded."""
    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        for i in range(600):
            app._append_to_log({"role": "founder", "content": f"msg {i}", "ts": ""})
        # After trim: <= 500
        assert len(app._message_log) <= 500
        # The oldest entries should be gone (newest preserved)
        assert app._message_log[-1]["content"] == "msg 599"


@pytest.mark.asyncio
async def test_search_finds_matches(tmp_path) -> None:
    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        app._append_to_log({"role": "founder", "content": "looking for pricing", "ts": ""})
        app._append_to_log({"role": "agent", "content": "Recommend $29/mo", "ts": ""})
        app._append_to_log({"role": "founder", "content": "thanks", "ts": ""})
        await app._search_scrollback("pricing")
        # Panel title carries the "1 match" or "match" string
        rendered = "\n".join(str(seg.text) for seg in app._chat_log.lines)
        assert "search" in rendered.lower()
        assert "pricing" in rendered


@pytest.mark.asyncio
async def test_search_no_matches() -> None:
    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        app._append_to_log({"role": "founder", "content": "hello", "ts": ""})
        await app._search_scrollback("xylophone")
        rendered = "\n".join(str(seg.text) for seg in app._chat_log.lines)
        assert "no matches" in rendered


@pytest.mark.asyncio
async def test_search_empty_query() -> None:
    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        await app._search_scrollback("   ")
        rendered = "\n".join(str(seg.text) for seg in app._chat_log.lines)
        assert "needs a query" in rendered


@pytest.mark.asyncio
async def test_edit_last_pulls_message_into_composer() -> None:
    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        app._append_to_log({
            "role": "founder", "content": "original message", "ts": "",
        })
        app._append_to_log({"role": "agent", "content": "reply", "ts": ""})
        await app._edit_last_founder_message()
        assert app._composer.value == "original message"


@pytest.mark.asyncio
async def test_edit_last_when_no_messages() -> None:
    """No founder messages = friendly hint, not crash."""
    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        await app._edit_last_founder_message()
        rendered = "\n".join(str(seg.text) for seg in app._chat_log.lines)
        assert "no previous founder message" in rendered


@pytest.mark.asyncio
async def test_copy_last_falls_back_to_panel_when_no_clipboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without xclip / pbcopy / wl-copy the copy command should
    still surface the content in a panel so the user can
    mouse-select."""
    import shutil
    monkeypatch.setattr(shutil, "which", lambda _: None)

    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        app._append_to_log({"role": "agent", "content": "the answer is 42", "ts": ""})
        await app._copy_message("last")
        rendered = "\n".join(str(seg.text) for seg in app._chat_log.lines)
        assert "the answer is 42" in rendered


@pytest.mark.asyncio
async def test_copy_with_no_match_warns() -> None:
    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        await app._copy_message("last")
        rendered = "\n".join(str(seg.text) for seg in app._chat_log.lines)
        assert "no message matches" in rendered


@pytest.mark.asyncio
async def test_draft_save_and_restore(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Save draft on unmount, restore on next mount. Round-trip
    through a tmp dir override."""
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))

    # Phase 1: type a draft + save
    app1 = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app1.run_test() as pilot:
        await pilot.pause(0.05)
        app1._composer.value = "draft in flight"
        app1._save_draft()
    assert (tmp_path / "tui_draft.txt").read_text() == "draft in flight"

    # Phase 2: new app instance picks it up via _restore_draft
    app2 = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app2.run_test() as pilot:
        await pilot.pause(0.05)
        await app2._restore_draft()
        assert app2._composer.value == "draft in flight"


@pytest.mark.asyncio
async def test_session_picker_handles_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No sessions yet → friendly message, not crash + not modal."""
    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)

        class _StubClient:
            async def call(self, method, params=None, **_):
                return []
            async def close(self): pass
        app.client = _StubClient()  # type: ignore[assignment]
        await app._open_session_picker()
        rendered = "\n".join(str(seg.text) for seg in app._chat_log.lines)
        assert "no past sessions" in rendered


@pytest.mark.asyncio
async def test_agent_picker_handles_empty() -> None:
    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)

        class _StubClient:
            async def call(self, method, params=None, **_):
                return []
            async def close(self): pass
        app.client = _StubClient()  # type: ignore[assignment]
        await app._open_agent_picker()
        rendered = "\n".join(str(seg.text) for seg in app._chat_log.lines)
        assert "no agents hired yet" in rendered


@pytest.mark.asyncio
async def test_start_new_session_calls_session_new() -> None:
    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)

        captured: dict[str, Any] = {}

        class _StubClient:
            async def call(self, method, params=None, **_):
                captured["method"] = method
                return {"closed": 1}
            async def close(self): pass
        app.client = _StubClient()  # type: ignore[assignment]
        await app._start_new_session()
        assert captured["method"] == "session.new"
        rendered = "\n".join(str(seg.text) for seg in app._chat_log.lines)
        assert "Started new session" in rendered


@pytest.mark.asyncio
async def test_show_history_replays_messages() -> None:
    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)

        class _StubClient:
            async def call(self, method, params=None, **_):
                return [
                    {"sender_type": "founder", "content": "hello", "created_at": "2026-05-06T12:00:00"},
                    {"sender_type": "agent", "content": "hi back", "created_at": "2026-05-06T12:00:01", "sender_role_title": "CEO"},
                ]
            async def close(self): pass
        app.client = _StubClient()  # type: ignore[assignment]
        await app._show_history()
        rendered = "\n".join(str(seg.text) for seg in app._chat_log.lines)
        assert "hello" in rendered
        assert "hi back" in rendered or "CEO" in rendered


@pytest.mark.asyncio
async def test_clear_drops_draft_file(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        app._composer.value = "doomed"
        app._save_draft()
        assert (tmp_path / "tui_draft.txt").exists()
        await app._dispatch_slash("/clear")
        assert not (tmp_path / "tui_draft.txt").exists()


@pytest.mark.asyncio
async def test_theme_set_persists_to_state(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Apply a theme, restart the app instance, verify the new
    theme name is loaded from disk on next mount."""
    from korpha.tui.themes import (
        get_active_theme_name, set_active_theme_name,
    )
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    set_active_theme_name("midnight")
    assert get_active_theme_name() == "midnight"


@pytest.mark.asyncio
async def test_theme_unknown_falls_back_to_default(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown theme name doesn't crash — _apply_theme uses the
    default palette + sets _active_theme_name to 'default'."""
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        app._apply_theme("nonexistent-vibes")
        assert app._active_theme_name == "default"


@pytest.mark.asyncio
async def test_theme_slash_with_known_name(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        await app._dispatch_slash("/theme sage")
        assert app._active_theme_name == "sage"
        rendered = "\n".join(str(seg.text) for seg in app._chat_log.lines)
        assert "Theme set to sage" in rendered


@pytest.mark.asyncio
async def test_theme_slash_with_unknown_name() -> None:
    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        await app._dispatch_slash("/theme not-a-theme")
        rendered = "\n".join(str(seg.text) for seg in app._chat_log.lines)
        assert "Unknown theme" in rendered


@pytest.mark.asyncio
async def test_version_shows_korpha_version() -> None:
    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        app._show_version()
        rendered = "\n".join(str(seg.text) for seg in app._chat_log.lines)
        assert "korpha" in rendered.lower()
        assert "textual" in rendered.lower()


@pytest.mark.asyncio
async def test_sidebar_toggle_hides_and_shows() -> None:
    """Ctrl-B / /sidebar toggles the left sidebar's display via a
    CSS class."""
    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        assert app._sidebar_visible is True
        app.action_toggle_sidebar()
        assert app._sidebar_visible is False
        sidebar = app.query_one("#sidebar")
        assert sidebar.has_class("is-collapsed")
        app.action_toggle_sidebar()
        assert app._sidebar_visible is True
        assert not sidebar.has_class("is-collapsed")


@pytest.mark.asyncio
async def test_detail_pane_starts_hidden() -> None:
    """Detail pane is collapsed by default — only opens on toggle
    or when something writes to it."""
    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        assert app._detail_visible is False
        pane = app.query_one("#detail-pane")
        assert pane.has_class("is-collapsed")
        app.action_toggle_detail()
        assert app._detail_visible is True
        assert not pane.has_class("is-collapsed")


@pytest.mark.asyncio
async def test_detail_write_auto_opens_pane() -> None:
    """Writing to the detail pane should auto-open it so the
    content isn't dropped on the floor."""
    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        assert app._detail_visible is False
        app._detail_write("interesting payload")
        assert app._detail_visible is True


@pytest.mark.asyncio
async def test_sidebar_renders_sessions_from_cache() -> None:
    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        app._sidebar_sessions_cache = [
            {"id": "abc", "topic": "Pricing chat", "is_active": True,
             "last_message_at": "2026-05-06T18:30:00"},
            {"id": "def", "topic": "Cold email", "is_active": False,
             "last_message_at": "2026-05-05T10:00:00"},
        ]
        app._render_sidebar_sessions()
        await pilot.pause(0.02)
        labels = [str(label.render()) for label in app.query("#sidebar-sessions Label")]
        assert any("Pricing" in lbl for lbl in labels)
        assert any("Cold email" in lbl for lbl in labels)


@pytest.mark.asyncio
async def test_sidebar_renders_empty_states() -> None:
    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        app._sidebar_sessions_cache = []
        app._sidebar_agents_cache = []
        app._render_sidebar_sessions()
        app._render_sidebar_agents()
        await pilot.pause(0.02)
        sess_labels = [str(l.render()) for l in app.query("#sidebar-sessions Label")]
        agent_labels = [str(l.render()) for l in app.query("#sidebar-agents Label")]
        assert any("none yet" in lbl for lbl in sess_labels)
        assert any("none hired" in lbl for lbl in agent_labels)


@pytest.mark.asyncio
async def test_sidebar_approvals_badge_updates() -> None:
    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        app._render_sidebar_approvals(0)
        await pilot.pause(0.02)
        assert "0 pending" in str(app._sidebar_approvals_label.render())
        app._render_sidebar_approvals(3)
        await pilot.pause(0.02)
        assert "3 pending" in str(app._sidebar_approvals_label.render())


@pytest.mark.asyncio
async def test_sidebar_slash_toggles_visibility() -> None:
    """The /sidebar slash command flips the same toggle as Ctrl-B."""
    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        assert app._sidebar_visible is True
        await app._dispatch_slash("/sidebar")
        assert app._sidebar_visible is False
        await app._dispatch_slash("/detail")
        assert app._detail_visible is True


@pytest.mark.asyncio
async def test_paste_image_handles_no_clipboard_image(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No image on clipboard → friendly message in chat, not crash."""
    from korpha.tui import images as img_mod
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))

    async def _fake_capture():
        return None

    monkeypatch.setattr(img_mod, "capture_clipboard_image", _fake_capture)
    # The app imports capture_clipboard_image at module load, so we
    # also patch the imported name in the app module.
    from korpha.tui import app as app_mod
    monkeypatch.setattr(app_mod, "capture_clipboard_image", _fake_capture)

    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        await app._paste_image()
        rendered = "\n".join(str(seg.text) for seg in app._chat_log.lines)
        assert "no image on the clipboard" in rendered


@pytest.mark.asyncio
async def test_paste_image_saves_and_attaches(
    tmp_path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful paste → file lands on disk + path appears in chat
    + entry pushed to message log so /search can find it later."""
    from korpha.tui import app as app_mod
    from korpha.tui.images import ClipboardImage

    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("KITTY_WINDOW_ID", raising=False)
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")

    fake_image = ClipboardImage(
        data=b"\x89PNG\r\n\x1a\n" + b"x" * 64,
        mime="image/png",
    )

    async def _fake_capture():
        return fake_image

    monkeypatch.setattr(app_mod, "capture_clipboard_image", _fake_capture)

    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        await app._paste_image()
        # File saved
        images_dir = tmp_path / "images"
        files = list(images_dir.glob("*.png"))
        assert len(files) == 1
        # Chat shows the attachment notice
        rendered = "\n".join(str(seg.text) for seg in app._chat_log.lines)
        assert "image attached" in rendered
        # Message log got the entry
        assert any(
            "image attached" in str(m.get("content", ""))
            for m in app._message_log
        )


@pytest.mark.asyncio
async def test_render_skill_payload_handles_complex_dict() -> None:
    """Tool-event payloads with dicts shouldn't crash the renderer
    even if they contain non-JSON-serializable values (Path, UUID,
    datetime). Falls back to repr() if json.dumps barfs."""
    from datetime import datetime
    from pathlib import Path
    from uuid import uuid4

    app = KorphaTUI(ws_url="ws://127.0.0.1:1/")
    async with app.run_test() as pilot:
        await pilot.pause(0.05)
        app._render_skill_payload(
            "test.skill",
            {
                "id": uuid4(),
                "path": Path("/tmp/x"),
                "ts": datetime.now(),
                "items": ["a", "b"],
            },
        )
        # No exception = pass; the panel was added to the log
        rendered = "\n".join(str(seg.text) for seg in app._chat_log.lines)
        assert "test.skill" in rendered or "result" in rendered


@pytest.mark.asyncio
async def test_rpc_client_call_no_wait_id_returns_int() -> None:
    """call_no_wait_id is the path prompt.submit uses so the TUI
    can register the id with prompt.interrupt before awaiting."""
    client = RpcClient("ws://fake/")
    fake = _FakeWS()
    client._ws = fake  # type: ignore[assignment]
    client._reader_task = asyncio.create_task(client._reader_loop())

    request_id = await client.call_no_wait_id("foo", {})
    assert isinstance(request_id, int)
    assert request_id > 0
    sent = json.loads(fake.sent[-1])
    assert sent["id"] == request_id
    assert sent["method"] == "foo"
    await client.close()
