"""Korpha TUI app — Textual single-pane chat over WebSocket.

Connects to the Korpha server's ``/api/tui/ws`` route. The TUI
owns no agent state; everything (DB, CEO, registry, gate) lives in
the server process. Mike runs ``korpha server`` once on a VPS;
``korpha tui`` (locally or via SSH) gets a chat surface against
that running process. Web dashboard sees identical state.

Layout (top to bottom):

  ┌────────────────────────────────────────────┐
  │ Korpha · WidgetCo · idle                │  ← status bar
  ├────────────────────────────────────────────┤
  │  [chat history scrollable]                 │
  │   You: …                                   │
  │   Cofounder: …                             │
  ├────────────────────────────────────────────┤
  │ ► [composer]                               │  ← input
  └────────────────────────────────────────────┘

Approval modal pops above the composer when one's pending — same
pattern Hermes uses for ApprovalPrompt.

Connection model: the TUI auto-launches a local server if it can't
connect (``localhost:8765`` is the default), or surfaces a clear
error pointing at the right command.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, Label, ListItem, ListView, RichLog, Static

from rich.console import Group as RichGroup
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from korpha.tui.images import (
    capture_clipboard_image,
    inline_render_escape,
    inline_render_supported,
    save_image,
)
from korpha.tui.rpc_client import (
    RpcClient,
    RpcClientError,
    RpcClosed,
)
from korpha.tui.themes import (
    BUILTIN_THEMES,
    all_themes,
    get_active_theme_name,
    set_active_theme_name,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Approval modal
# ---------------------------------------------------------------------------


class _PickerScreen(ModalScreen[dict[str, Any]]):
    """Generic modal picker — list of rows, arrow keys to navigate,
    Enter to choose, Esc to cancel.

    Returns the selected row's dict, or ``{}`` on cancel. Used for
    session picker + agent picker.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("enter", "select", "Select"),
    ]

    def __init__(self, title: str, rows: list[dict[str, Any]]) -> None:
        super().__init__()
        self._title = title
        self._rows = rows

    def compose(self) -> ComposeResult:
        with Container(id="picker-box"):
            yield Label(self._title, id="picker-title")
            items = []
            for row in self._rows:
                items.append(ListItem(
                    Static(row.get("__display") or row.get("name") or "?"),
                ))
            self._list_view = ListView(*items, id="picker-list")
            yield self._list_view
            yield Label(
                "  [↑↓] navigate   [enter] select   [esc] cancel",
                id="picker-actions",
            )

    def on_mount(self) -> None:
        if self._rows:
            self._list_view.index = 0
            self._list_view.focus()

    def action_select(self) -> None:
        idx = self._list_view.index
        if idx is None or idx < 0 or idx >= len(self._rows):
            self.dismiss({})
            return
        self.dismiss(self._rows[idx])

    def action_cancel(self) -> None:
        self.dismiss({})


class ApprovalScreen(ModalScreen[str]):
    """Block until the founder approves / rejects / views.

    Returns one of: ``"approve"``, ``"reject"``, ``"view"``,
    ``"dismiss"``.
    """

    BINDINGS = [
        Binding("a", "decide('approve')", "Approve"),
        Binding("r", "decide('reject')", "Reject"),
        Binding("v", "decide('view')", "View detail"),
        Binding("escape", "decide('dismiss')", "Dismiss"),
    ]

    def __init__(self, summary: str, action_class: str) -> None:
        super().__init__()
        self._summary = summary
        self._action_class = action_class

    def compose(self) -> ComposeResult:
        with Container(id="approval-box"):
            yield Label("⏵ Approval required", id="approval-title")
            yield Label(
                f"Action class: {self._action_class}", id="approval-class",
            )
            yield Static(self._summary, id="approval-summary")
            yield Label(
                "  [a]pprove   [r]eject   [v]iew detail   [esc] dismiss",
                id="approval-actions",
            )

    def action_decide(self, choice: str) -> None:
        self.dismiss(choice)


# ---------------------------------------------------------------------------
# The TUI app
# ---------------------------------------------------------------------------


class KorphaTUI(App[None]):
    """Single-pane chat TUI talking to /api/tui/ws."""

    CSS = """
    Screen {
        background: $surface;
    }
    #status-bar {
        dock: top;
        height: 1;
        background: $boost;
        color: $text;
        padding: 0 1;
    }
    #status-text {
        color: $text-muted;
    }
    /* Main horizontal split — sidebar | chat | optional detail */
    #main-row {
        height: 1fr;
        layout: horizontal;
    }
    #sidebar {
        width: 26;
        background: $boost;
        border-right: solid $primary-background-lighten-1;
        padding: 1 1;
        scrollbar-gutter: stable;
    }
    #sidebar.is-collapsed {
        display: none;
    }
    #sidebar-section-title {
        color: $accent;
        text-style: bold;
        margin-bottom: 1;
    }
    .sidebar-block {
        margin-bottom: 1;
        padding: 0 0 1 0;
        border-bottom: dashed $primary-background-lighten-1;
    }
    .sidebar-block-title {
        color: $text-muted;
        text-style: italic;
        margin-bottom: 1;
    }
    .sidebar-row {
        padding: 0 1;
    }
    .sidebar-row.is-active {
        background: $primary 30%;
        color: $text;
        text-style: bold;
    }
    .sidebar-row:hover {
        background: $boost;
    }
    .sidebar-badge {
        color: $warning;
        text-style: bold;
    }
    /* Detail pane on the right — toggled with /detail (Ctrl-D) */
    #detail-pane {
        width: 50;
        background: $surface;
        border-left: solid $primary-background-lighten-1;
        padding: 1 1;
    }
    #detail-pane.is-collapsed {
        display: none;
    }
    #detail-title {
        color: $accent;
        text-style: bold;
        margin-bottom: 1;
    }
    /* Chat — fills remaining space between sidebar + detail */
    #chat-log {
        width: 1fr;
        height: 1fr;
        background: $surface;
        scrollbar-gutter: stable;
        padding: 0 1;
    }
    #composer {
        dock: bottom;
        height: 3;
        margin: 0 0;
        background: $boost;
        border: solid $primary;
    }
    ApprovalScreen {
        align: center middle;
    }
    #approval-box {
        width: 70;
        max-width: 90%;
        height: auto;
        background: $boost;
        border: thick $warning;
        padding: 1 2;
    }
    #approval-title {
        color: $warning;
        text-style: bold;
        margin-bottom: 1;
    }
    #approval-class {
        color: $text-muted;
        margin-bottom: 1;
    }
    #approval-summary {
        background: $surface;
        padding: 1;
        margin-bottom: 1;
    }
    #approval-actions {
        color: $accent;
        margin-top: 1;
    }
    _PickerScreen, PickerScreen {
        align: center middle;
    }
    #picker-box {
        width: 80;
        max-width: 95%;
        max-height: 70%;
        background: $boost;
        border: thick $primary;
        padding: 1 2;
    }
    #picker-title {
        color: $primary;
        text-style: bold;
        margin-bottom: 1;
    }
    #picker-list {
        height: auto;
        max-height: 16;
        background: $surface;
        margin-bottom: 1;
    }
    #picker-actions {
        color: $accent;
        margin-top: 1;
    }
    """

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit", priority=True),
        Binding("ctrl+l", "clear_chat", "Clear chat"),
        Binding("ctrl+a", "review_approvals", "Approvals"),
        Binding("ctrl+x", "interrupt_stream", "Interrupt"),
        Binding("ctrl+r", "toggle_reasoning", "Reasoning"),
        Binding("ctrl+o", "toggle_operator", "Operator"),
        Binding("ctrl+f", "open_search", "Search"),
        Binding("ctrl+e", "edit_last", "Edit last"),
        Binding("ctrl+s", "open_sessions", "Sessions"),
        Binding("ctrl+t", "open_agents", "Agents"),
        Binding("ctrl+n", "start_new_session", "New session"),
        Binding("ctrl+b", "toggle_sidebar", "Sidebar"),
        Binding("ctrl+d", "toggle_detail", "Detail pane"),
        Binding("ctrl+v", "paste_image", "Paste image"),
        Binding("ctrl+z", "undo_last", "Undo"),
    ]

    def __init__(self, ws_url: str) -> None:
        super().__init__()
        self.ws_url = ws_url
        self.client: RpcClient | None = None
        self._chat_log: RichLog | None = None
        self._status_text: Label | None = None
        self._status_brand: Label | None = None
        self._composer: Input | None = None
        self._streaming: bool = False
        """Whether a prompt.submit is in flight. Used by /quit + the
        cancel handler to tell whether Ctrl-C should interrupt the
        stream or exit the app."""

        self._active_request_id: int | None = None
        """The RPC id of the currently-streaming prompt.submit. Set
        by _send_to_ceo + cleared on done. prompt.interrupt uses this."""

        self._last_approval_shown: str | None = None
        """Dedup approval modal pops — don't re-pop the same id."""

        self._business_name: str = "Korpha"

        self._stream_buffer: str = ""
        """Accumulates content.delta chunks during a streaming
        response so we can re-render the whole buffer as Markdown
        once 'done' fires."""

        self._reasoning_buffer: str = ""
        """Accumulates reasoning.delta chunks. Hidden by default;
        the founder presses Ctrl-R or types /reasoning to toggle
        whether the trace renders in chat."""

        self._show_reasoning: bool = False
        """Toggle for reasoning-trace visibility. Off by default
        (most reasoning traces are noisy + waste tokens for the
        reader). On = render the chain-of-thought as a dim panel
        before the final response."""

        self._operator_mode: bool = False
        """Operator mode = show raw payloads (tool events, RPC
        ids, internal state). Mirrors the dashboard's operator
        toggle. Off by default for Mike-non-technical UX."""

        self._cumulative_cost: float = 0.0
        """Running total of cost_usd across the session. Shown in
        the status bar. Resets on /clear or app restart."""

        self._cumulative_tokens: int = 0
        """Running total of tokens (input + output) across the
        session. Approximated from cost_usd at $0.50/Mtok if the
        server doesn't surface a token count directly."""

        self._message_log: list[dict[str, Any]] = []
        """Parallel store of every founder + agent message rendered
        in the chat. Lets us:
          - Search scrollback (Ctrl-F) without parsing the RichLog
          - Edit + resend the last founder message (/edit)
          - Copy any past message to the clipboard (/copy)

        Each entry: {role: 'founder'|'agent'|'system',
                     content: str, ts: ISO datetime}
        Bounded — we keep up to 500 turns (roughly a day's chat)
        before evicting the oldest. Persisted via session.history
        on the server side so the cap is just for in-memory ops."""

        self._active_theme_name: str = "default"
        """Set by ``_apply_theme``. The /theme slash + Ctrl-T-style
        picker reads this to mark the current row."""

        self._sidebar_visible: bool = True
        """Toggleable left sidebar showing session list + agents
        + pending-approval count. Hide via /sidebar (Ctrl-B) when
        Mike wants the chat full-width."""

        self._detail_visible: bool = False
        """Toggleable right detail pane. Default off — only opens
        when Mike toggles via /detail (Ctrl-D) or when something
        worth detailing arrives (long skill output, big reasoning
        trace). Renders into #detail-log."""

        self._sidebar_sessions_cache: list[dict[str, Any]] = []
        self._sidebar_agents_cache: list[dict[str, Any]] = []
        """Cached most-recent ``session.list`` + ``agents.list``
        results. Sidebar renders from these so a flaky network
        doesn't blank the panel mid-operation."""

        self._sidebar_status_badge: Label | None = None
        self._sidebar_approvals_label: Label | None = None
        self._detail_log: RichLog | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="status-bar"):
            yield Label("Korpha · connecting…", id="status-brand")
            yield Static(" · ", id="status-sep")
            yield Label("idle", id="status-text")
            yield Static(" · ", id="status-sep-2")
            yield Label("0 pending", id="status-badge-approvals")
        with Horizontal(id="main-row"):
            with Vertical(id="sidebar"):
                yield Label("Sessions", id="sidebar-section-title")
                with Vertical(id="sidebar-sessions", classes="sidebar-block"):
                    yield Label("(loading…)", id="sidebar-sessions-loading")
                yield Label("Agents", classes="sidebar-block-title")
                with Vertical(id="sidebar-agents", classes="sidebar-block"):
                    yield Label("(loading…)", id="sidebar-agents-loading")
                yield Label("Approvals", classes="sidebar-block-title")
                with Vertical(id="sidebar-approvals", classes="sidebar-block"):
                    yield Label("0 pending", id="sidebar-approvals-count")
            yield RichLog(highlight=True, markup=True, id="chat-log")
            with Vertical(id="detail-pane", classes="is-collapsed"):
                yield Label("Detail", id="detail-title")
                yield RichLog(highlight=True, markup=True, id="detail-log")
        yield Input(placeholder="Ask your cofounder…", id="composer")
        yield Footer()

    def on_mount(self) -> None:
        self._chat_log = self.query_one("#chat-log", RichLog)
        self._status_text = self.query_one("#status-text", Label)
        self._status_brand = self.query_one("#status-brand", Label)
        self._composer = self.query_one("#composer", Input)
        self._sidebar_status_badge = self.query_one(
            "#status-badge-approvals", Label,
        )
        self._sidebar_approvals_label = self.query_one(
            "#sidebar-approvals-count", Label,
        )
        self._detail_log = self.query_one("#detail-log", RichLog)
        self._composer.focus()
        # Apply persisted theme. Best-effort — bad theme name falls
        # back to default rather than crashing the boot path.
        self._apply_theme(get_active_theme_name())
        self._chat_log.write(
            "[bold cyan]Korpha TUI[/]  v1 — chat with your cofounder.\n"
            "[dim]Try /help for the full command list. "
            "Sidebar: Ctrl-B  ·  Detail: Ctrl-D[/]\n"
        )
        # Kick off WS connection in the background — UI stays
        # responsive even if the server isn't up yet.
        self.run_worker(self._connect_and_run(), exclusive=True)

    def _apply_theme(self, name: str) -> None:
        """Push a theme's hex tokens into Textual's CSS variables.
        Falls back to ``default`` for unknown names + emits a hint
        so Mike sees what went wrong."""
        themes = all_themes()
        theme = themes.get(name) or BUILTIN_THEMES["default"]
        for token, value in theme.as_textual_variables().items():
            try:
                # Textual exposes design-token mutation via App.theme
                # in newer versions; for stability we set CSS vars
                # the same way --color-X is set in stylesheets. The
                # design token names match.
                self.stylesheet.parse(
                    f":root {{ ${token}: {value}; }}", path=str(__file__),
                )
            except Exception:
                # Silent — Textual's CSS API is moving target across
                # versions; the per-app CSS already has good defaults.
                pass
        # Always remember the actual applied name (for /theme display)
        self._active_theme_name = theme.name

    # ---- WS connection lifecycle ----

    async def _connect_and_run(self) -> None:
        """Establish the WS, register event handlers, kick off
        approval polling. Errors during connect render in the chat
        with a clear hint at how to fix."""
        client = RpcClient(self.ws_url, timeout_seconds=600.0)
        try:
            await client.connect()
        except OSError as exc:
            self._chat_log.write(
                f"\n[red]Couldn't reach the server at {self.ws_url}.[/]\n"
                f"[dim]Run `korpha server` in another terminal "
                f"first, then `korpha tui` again.[/]\n"
                f"[dim]({exc})[/]\n"
            )
            return
        self.client = client
        try:
            ready = await client.ready_payload(timeout=10.0)
        except (RpcClosed, asyncio.TimeoutError) as exc:
            self._chat_log.write(
                f"\n[red]Server didn't send gateway.ready: {exc}[/]\n"
            )
            return

        biz = ready.get("business") or {}
        founder = ready.get("founder") or {}
        self._business_name = biz.get("name") or "Korpha"
        if self._status_brand is not None:
            self._status_brand.update(
                f"Korpha · {self._business_name} · "
                f"{founder.get('display_name') or founder.get('email') or 'founder'}"
            )

        # Register streaming event handlers
        client.on_event("phase", self._handle_phase)
        client.on_event("content.delta", self._handle_content_delta)
        client.on_event("reasoning.delta", self._handle_reasoning_delta)
        client.on_event("tool.event", self._handle_tool_event)
        client.on_event("done", self._handle_done)
        client.on_event("approval.decided", self._handle_approval_decided)

        # Replay history so the TUI mirrors the web /app/chat view.
        try:
            history = await client.call("session.history", {"limit": 30})
        except (RpcClientError, RpcClosed):
            history = []
        for msg in history:
            sender = msg.get("sender_type") or "system"
            content = msg.get("content") or ""
            ts = msg.get("created_at") or ""
            self._message_log.append({
                "role": sender, "content": content, "ts": ts,
            })
            if sender == "founder":
                self._chat_log.write(f"\n[bold green]You[/]")
                self._chat_log.write(content)
            elif sender == "agent":
                title = msg.get("sender_role_title") or "Cofounder"
                self._chat_log.write(f"\n[bold magenta]{title}[/]")
                self._chat_log.write(Markdown(content))
            else:
                self._chat_log.write(f"\n[dim]{content}[/]")
        if history:
            self._chat_log.write("\n[dim]— end of recent history —[/]\n")
        # Restore any draft input the user had in flight at last quit.
        await self._restore_draft()

        # Start the approval poll — server pushes `approval.decided`
        # but doesn't push approval.created, so we poll every 5s for
        # approvals staged elsewhere (web, agent skills).
        self.set_interval(5.0, self._poll_approvals)

    # ---- Streaming event handlers ----

    async def _handle_phase(self, evt: dict[str, Any]) -> None:
        params = evt.get("params") or {}
        self._set_status(
            {
                "router": "routing…",
                "skill": "running skill…",
                "synth": "drafting reply…",
            }.get(str(params.get("phase", "")), "working…")
        )

    async def _handle_content_delta(self, evt: dict[str, Any]) -> None:
        text = (evt.get("params") or {}).get("text") or ""
        if not text:
            return
        # Live append: show raw text while the LLM streams.
        # On 'done' we strip everything below the anchor + re-render
        # the buffer as Markdown so code blocks / lists / headers
        # land properly. Most users will never notice the swap —
        # it happens in one frame.
        self._stream_buffer += text
        self._chat_log.write(text)

    async def _handle_reasoning_delta(self, evt: dict[str, Any]) -> None:
        # Always accumulate — toggle controls *display*, not capture.
        # That way Mike can flip /reasoning on mid-turn and see the
        # full trace immediately rather than waiting for the next
        # response.
        text = (evt.get("params") or {}).get("text") or ""
        if text:
            self._reasoning_buffer += text

    async def _handle_tool_event(self, evt: dict[str, Any]) -> None:
        params = evt.get("params") or {}
        skill_name = params.get("skill_name") or params.get("name")
        if skill_name:
            self._chat_log.write(f"\n[dim](running skill: {skill_name})[/]")
        # Pretty-print structured payloads if present (skills often
        # return JSON-shaped results we want to surface readably).
        payload = params.get("payload") or params.get("result")
        if isinstance(payload, dict):
            self._render_skill_payload(skill_name or "(skill)", payload)

    async def _handle_done(self, evt: dict[str, Any]) -> None:
        params = evt.get("params") or {}
        skills = params.get("skills_used") or []
        cost = float(params.get("cost_usd") or 0.0)
        tokens = int(params.get("tokens") or 0)
        interrupted = bool(params.get("interrupted"))

        # Show the reasoning trace BEFORE the final markdown render
        # if the toggle is on. Trace appears as a dim panel so it's
        # visually distinct from the user-facing reply.
        if self._show_reasoning and self._reasoning_buffer.strip():
            self._render_reasoning_trace(self._reasoning_buffer)

        if self._stream_buffer.strip():
            self._render_assistant_markdown(self._stream_buffer)
            self._append_to_log({
                "role": "agent",
                "content": self._stream_buffer,
                "ts": _now_iso(),
            })
        self._stream_buffer = ""
        self._reasoning_buffer = ""

        if interrupted:
            self._chat_log.write("[yellow](interrupted)[/]\n")

        # Per-turn cost + token line. Always show if cost > 0;
        # operator mode also shows zero-cost turns for transparency.
        self._cumulative_cost += cost
        if tokens:
            self._cumulative_tokens += tokens
        meta_parts: list[str] = []
        if skills:
            meta_parts.append(f"skills: {', '.join(skills)}")
        if cost:
            meta_parts.append(f"this turn: ${cost:.4f}")
        if tokens:
            meta_parts.append(f"{tokens:,} tok")
        meta_parts.append(f"session: ${self._cumulative_cost:.4f}")
        if self._operator_mode and self._active_request_id is not None:
            meta_parts.append(f"req={self._active_request_id}")
        self._chat_log.write(f"[dim]({' · '.join(meta_parts)})[/]\n")

        self._set_status("idle")
        self._streaming = False
        self._active_request_id = None

    def _render_reasoning_trace(self, content: str) -> None:
        """Surface the raw chain-of-thought as a dim panel above the
        final response. Reasoning models burn output tokens on this;
        Mike's choice whether to read it. Default off."""
        if len(content) > 6_000:
            content = content[:6_000] + "\n\n…(truncated)"
        panel = Panel(
            Text(content, style="dim italic"),
            title="[dim]reasoning trace[/]",
            border_style="dim",
        )
        self._chat_log.write(panel)

    # ---- Rendering helpers ----

    def _render_assistant_markdown(self, content: str) -> None:
        """Post-stream: replace the trailing raw-text region with a
        Markdown-rendered version. RichLog doesn't have an in-place
        replace API, so we just write the Markdown after the raw —
        gives the best of both: 'I saw tokens land' + 'final looks
        polished'. Cheaper than tracking line indices.
        """
        # Render Markdown — handles ``` fences, lists, **bold**,
        # backticks, headings.
        md = Markdown(content)
        self._chat_log.write("\n[dim]── final ──[/]")
        self._chat_log.write(md)

    def _render_skill_payload(
        self, skill_name: str, payload: dict[str, Any],
    ) -> None:
        """Pretty-print a skill's result payload as a panel with
        syntax-highlighted JSON. Most skills return small dicts;
        showing them inline avoids forcing the user to dig in
        the activity log.
        """
        try:
            body = json.dumps(payload, indent=2, default=str)
        except (TypeError, ValueError):
            body = repr(payload)
        if len(body) > 4_000:
            # Truncate huge payloads — full data still in DB
            body = body[:4_000] + "\n  …(truncated; see /app/activity)"
        syntax = Syntax(
            body, "json", theme="monokai", word_wrap=True, line_numbers=False,
        )
        panel = Panel(
            syntax,
            title=f"[cyan]{skill_name}[/] result",
            border_style="dim",
        )
        self._chat_log.write(panel)

    async def _handle_approval_decided(self, evt: dict[str, Any]) -> None:
        params = evt.get("params") or {}
        approval_id = params.get("approval_id")
        decision = params.get("decision")
        if approval_id and decision:
            self._chat_log.write(
                f"\n[dim](approval {decision} via {approval_id[:8]})[/]"
            )

    # ---- Composer submit ----

    @on(Input.Submitted, "#composer")
    async def on_composer_submit(self, event: Input.Submitted) -> None:
        text = (event.value or "").strip()
        if not text:
            return
        self._composer.value = ""
        # Successful submit = drop the persisted draft so the next
        # launch starts clean.
        self._clear_draft_file()

        if text.startswith("/"):
            await self._dispatch_slash(text)
            return

        await self._send_to_ceo(text)

    # ---- Slash commands ----

    async def _dispatch_slash(self, raw: str) -> None:
        cmd = raw.lstrip("/").split()[0].lower()
        if cmd == "help":
            self._chat_log.write(
                "\n[bold]Slash commands[/]\n"
                "  [bold cyan]Chat[/]\n"
                "  [cyan]/help[/]       — this list\n"
                "  [cyan]/clear[/]      — clear the view + reset session totals (keep DB history)\n"
                "  [cyan]/quit[/]       — exit the TUI\n"
                "  [cyan]/interrupt[/]  — cancel the active stream (Ctrl-X)\n\n"
                "  [bold cyan]Approvals[/]\n"
                "  [cyan]/approvals[/]  — review pending approvals (Ctrl-A)\n\n"
                "  [bold cyan]Sessions[/]\n"
                "  [cyan]/sessions[/]   — pick a past session to resume (Ctrl-S)\n"
                "  [cyan]/new[/]        — archive active session, start fresh (Ctrl-N)\n"
                "  [cyan]/history[/]    — replay current session's recent messages\n\n"
                "  [bold cyan]Routing[/]\n"
                "  [cyan]/agents[/]     — pick an agent to address directly (Ctrl-T)\n\n"
                "  [bold cyan]Messages[/]\n"
                "  [cyan]/search Q[/]   — find messages containing Q (Ctrl-F)\n"
                "  [cyan]/copy[/]       — copy last reply to clipboard ('me'|'<n>')\n"
                "  [cyan]/edit[/]       — pull last message back into composer (Ctrl-E)\n"
                "  [cyan]/paste[/]      — paste image from clipboard (Ctrl-V)\n\n"
                "  [bold cyan]Display[/]\n"
                "  [cyan]/reasoning[/]  — toggle reasoning-trace display (Ctrl-R)\n"
                "  [cyan]/operator[/]   — toggle operator mode (Ctrl-O)\n"
                "  [cyan]/sidebar[/]    — toggle the left sidebar (Ctrl-B)\n"
                "  [cyan]/detail[/]     — toggle the right detail pane (Ctrl-D)\n"
                "  [cyan]/cost[/]       — show session cost summary\n"
                "  [cyan]/theme[/]      — pick a TUI theme (or /theme NAME to set directly)\n\n"
                "  [bold cyan]Memory + watchdogs[/]\n"
                "  [cyan]/recall Q[/]   — search the long-term memory store\n"
                "  [cyan]/note[/]       — list/add/remove bounded MEMORY/USER notes\n"
                "  [cyan]/cron[/]       — list/run/toggle/delete agentless cron jobs\n"
                "  [cyan]/kanban[/]     — list/add/move/archive C-suite board cards\n"
                "  [cyan]/team[/]       — list/hire/fire org chart members\n"
                "  [cyan]/goal[/]       — set/status/pause/resume/clear standing goal\n\n"
                "  [bold cyan]Diagnostics[/]\n"
                "  [cyan]/skills[/]     — list installed skills\n"
                "  [cyan]/me[/]         — show founder + business identity\n"
                "  [cyan]/methods[/]    — list server RPC methods (debug)\n"
                "  [cyan]/version[/]    — TUI + library versions\n"
                "  [cyan]/save[/]       — manually persist composer draft\n"
            )
        elif cmd == "clear":
            self._chat_log.clear()
            self._cumulative_cost = 0.0
            self._cumulative_tokens = 0
            self._message_log.clear()
            self._clear_draft_file()
            self._chat_log.write(
                "[dim](history cleared on screen + session totals reset)[/]\n"
            )
        elif cmd in ("quit", "exit", "q"):
            self.exit()
        elif cmd in ("approvals", "approval"):
            await self._review_approvals()
        elif cmd == "interrupt":
            await self._interrupt_active_stream()
        elif cmd in ("reasoning", "trace"):
            self.action_toggle_reasoning()
        elif cmd in ("operator", "ops"):
            self.action_toggle_operator()
        elif cmd == "cost":
            self._chat_log.write(
                f"\n[bold]Session totals[/]\n"
                f"  cost:   ${self._cumulative_cost:.4f}\n"
                f"  tokens: {self._cumulative_tokens:,}\n"
            )
        elif cmd in ("search", "find", "grep"):
            query = raw[len(cmd) + 1:].strip().lstrip("/")
            await self._search_scrollback(query)
        elif cmd == "copy":
            target = raw[len("/copy"):].strip() or None
            await self._copy_message(target)
        elif cmd in ("edit", "e"):
            await self._edit_last_founder_message()
        elif cmd in ("session", "sessions"):
            await self._open_session_picker()
        elif cmd == "new":
            await self._start_new_session()
        elif cmd in ("agent", "agents", "to"):
            await self._open_agent_picker()
        elif cmd == "history":
            limit = 30
            try:
                arg = raw[len(cmd) + 1:].strip().lstrip("/")
                if arg:
                    limit = int(arg)
            except ValueError:
                pass
            await self._show_history(limit=limit)
        elif cmd in ("theme", "themes", "skin"):
            arg = raw[len(cmd) + 1:].strip()
            if arg:
                # Direct selection by name
                themes = all_themes()
                if arg not in themes:
                    self._chat_log.write(
                        f"[red]Unknown theme:[/] {arg}. "
                        f"Choices: {', '.join(sorted(themes))}\n"
                    )
                else:
                    self._apply_theme(arg)
                    set_active_theme_name(arg)
                    self._chat_log.write(
                        f"[green]✓ Theme set to {arg}[/]\n"
                    )
            else:
                await self._open_theme_picker()
        elif cmd in ("skills", "list-skills"):
            await self._show_skills_catalog()
        elif cmd == "me":
            await self._show_me()
        elif cmd in ("methods", "rpc"):
            await self._show_methods_catalog()
        elif cmd == "version":
            self._show_version()
        elif cmd == "save":
            self._save_draft()
            self._chat_log.write(
                f"[dim]✓ saved draft to {_draft_path()}[/]\n"
            )
        elif cmd == "sidebar":
            self.action_toggle_sidebar()
        elif cmd == "detail":
            self.action_toggle_detail()
        elif cmd in ("paste-image", "paste", "image", "img"):
            await self._paste_image()
        elif cmd in ("undo", "u"):
            steps = 1
            try:
                arg = raw[len(cmd) + 1:].strip().lstrip("/")
                if arg:
                    steps = int(arg)
            except ValueError:
                pass
            await self._undo_messages(steps)
        elif cmd in ("branch", "fork"):
            await self._branch_session()
        elif cmd in ("subagents", "subagent", "running"):
            await self._show_running_subagents()
        elif cmd in ("kill", "stop"):
            arg = raw[len(cmd) + 1:].strip().lstrip("/").lower()
            if not arg:
                self._chat_log.write(
                    "[red]Usage: /kill cto|cmo|coo|worker[/]\n"
                )
            else:
                await self._interrupt_subagent(arg)
        elif cmd in ("remember", "memorize"):
            text = raw[len(cmd) + 1:].strip()
            if not text:
                self._chat_log.write(
                    "[red]Usage: /remember <text> "
                    "(e.g. /remember Mike targets freelance designers)[/]\n"
                )
            else:
                await self._memory_remember(text)
        elif cmd in ("recall", "memory"):
            query = raw[len(cmd) + 1:].strip()
            if not query:
                self._chat_log.write(
                    "[red]Usage: /recall <query>[/]\n"
                )
            else:
                await self._memory_recall(query)
        elif cmd == "cron":
            sub_raw = raw[len(cmd) + 1:].strip()
            parts = sub_raw.split(None, 1)
            sub = parts[0].lower() if parts else ""
            arg = parts[1].strip() if len(parts) > 1 else ""
            if sub == "list" or sub == "":
                await self._cron_list()
            elif sub == "run" and arg:
                await self._cron_run(arg)
            elif sub == "toggle" and arg:
                await self._cron_toggle(arg)
            elif sub == "delete" and arg:
                await self._cron_delete(arg)
            else:
                self._chat_log.write(
                    "[red]Usage: /cron list | /cron run <name> | "
                    "/cron toggle <name> | /cron delete <name>[/]\n"
                )
        elif cmd == "kanban":
            sub_raw = raw[len(cmd) + 1:].strip()
            parts = sub_raw.split(None, 1)
            sub = parts[0].lower() if parts else ""
            arg = parts[1].strip() if len(parts) > 1 else ""
            if sub == "list" or sub == "":
                await self._kanban_list()
            elif sub == "add" and arg:
                await self._kanban_add(arg)
            elif sub == "move" and arg:
                await self._kanban_move(arg)
            elif sub == "archive" and arg:
                await self._kanban_archive(arg)
            else:
                self._chat_log.write(
                    "[red]Usage: /kanban list | /kanban add <title> | "
                    "/kanban move <card_id> <column> | "
                    "/kanban archive <card_id>[/]\n"
                )
        elif cmd == "team":
            sub_raw = raw[len(cmd) + 1:].strip()
            parts = sub_raw.split(None, 1)
            sub = parts[0].lower() if parts else ""
            arg = parts[1].strip() if len(parts) > 1 else ""
            if sub in ("list", ""):
                await self._team_list_slash()
            elif sub == "hire" and arg:
                await self._team_hire_slash(arg)
            elif sub == "fire" and arg:
                await self._team_fire_slash(arg)
            else:
                self._chat_log.write(
                    "[red]Usage: /team list | "
                    "/team hire <specialty> | "
                    "/team fire <id-prefix>[/]\n"
                )
        elif cmd == "note":
            sub_raw = raw[len(cmd) + 1:].strip()
            parts = sub_raw.split(None, 2)
            sub = parts[0].lower() if parts else ""
            store = parts[1].lower() if len(parts) > 1 else "memory"
            text = parts[2] if len(parts) > 2 else ""
            if sub == "list":
                await self._note_list_slash(store)
            elif sub == "add" and text:
                await self._note_add_slash(store, text)
            elif sub == "remove" and text:
                await self._note_remove_slash(store, text)
            else:
                self._chat_log.write(
                    "[red]Usage: /note list <memory|user> | "
                    "/note add <memory|user> <text> | "
                    "/note remove <memory|user> <substring>[/]\n"
                )
        elif cmd == "goal":
            await self._dispatch_goal_slash(raw)
        else:
            self._chat_log.write(
                f"[red]Unknown slash command:[/] /{cmd}. "
                f"Try /help.\n"
            )

    async def _dispatch_goal_slash(self, raw: str) -> None:
        """Handle /goal in chat. Parses via the shared parser, runs
        against the active thread's GoalManager, writes the reply
        into the chat log."""
        from sqlmodel import Session, select as _select

        from korpha.business.model import Business
        from korpha.cofounder.model import (
            Thread, ThreadPlatform, ThreadStatus,
        )
        from korpha.db._session import get_engine
        from korpha.goals import GoalManager, execute_goal_slash, parse_goal_slash

        intent = parse_goal_slash(raw)
        try:
            engine = get_engine()
        except Exception as exc:
            self._chat_log.write(f"[red]/goal failed:[/] {exc}\n")
            return

        with Session(engine) as session:
            business = session.exec(_select(Business)).first()
            if business is None:
                self._chat_log.write(
                    "[red]/goal needs a business — onboard first.[/]\n",
                )
                return
            thread = session.exec(
                _select(Thread)
                .where(Thread.business_id == business.id)
                .where(Thread.platform == ThreadPlatform.WEB)
                .where(Thread.status == ThreadStatus.ACTIVE)
                .order_by(Thread.last_message_at.desc())  # type: ignore[attr-defined]
                .limit(1)
            ).first()
            if thread is None:
                self._chat_log.write(
                    "[red]No active web thread for this business.[/]\n",
                )
                return
            mgr = GoalManager(
                session=session, thread_id=thread.id,
                business_id=business.id, cost_tracker=None,
            )
            reply = execute_goal_slash(intent, mgr)
        self._chat_log.write(f"[cyan]{reply}[/]\n")

    async def _interrupt_active_stream(self) -> None:
        if self.client is None or self._active_request_id is None:
            self._chat_log.write("[dim](nothing to interrupt)[/]\n")
            return
        try:
            await self.client.call(
                "prompt.interrupt",
                {"request_id": self._active_request_id},
                timeout=5.0,
            )
            self._chat_log.write("[yellow]✗ interrupt sent[/]\n")
        except (RpcClientError, RpcClosed) as exc:
            self._chat_log.write(f"[red]interrupt failed:[/] {exc}\n")

    # ---- Send to CEO via RPC ----

    async def _send_to_ceo(self, text: str) -> None:
        if self.client is None:
            self._chat_log.write(
                "[red]Not connected — cannot send.[/]\n"
            )
            return
        if self._streaming:
            self._chat_log.write(
                "[yellow](still streaming previous reply — wait or "
                "/interrupt)[/]\n"
            )
            return

        self._chat_log.write(f"\n[bold green]You[/] · {_now()}\n  {text}\n")
        self._append_to_log({"role": "founder", "content": text, "ts": _now_iso()})
        self._set_status("submitting…")
        self._chat_log.write(f"\n[bold magenta]Cofounder[/] · {_now()}\n  ")
        self._streaming = True

        try:
            request_id = await self.client.call_no_wait_id(
                "prompt.submit", {"message": text},
            )
            self._active_request_id = request_id
            await self.client.wait_for_id(request_id, timeout=600.0)
        except RpcClosed:
            self._chat_log.write("\n[red](connection lost)[/]\n")
            self._streaming = False
            self._active_request_id = None
            self._set_status("disconnected")
        except RpcClientError as exc:
            self._chat_log.write(f"\n[red]Error:[/] {exc.message}\n")
            self._streaming = False
            self._active_request_id = None
            self._set_status("error")
        except asyncio.TimeoutError:
            self._chat_log.write("\n[red](timed out)[/]\n")
            self._streaming = False
            self._active_request_id = None
            self._set_status("error")

    # ---- Approvals ----

    async def _poll_approvals(self) -> None:
        if self.client is None:
            return
        try:
            pending = await self.client.call(
                "approvals.list", timeout=5.0,
            )
        except (RpcClientError, RpcClosed):
            return
        # Update sidebar badge regardless — the badge should reflect
        # the current count even if there's no auto-modal to surface.
        self._render_sidebar_approvals(len(pending or []))
        # Refresh sidebar sessions / agents on every poll tick — same
        # 5s heartbeat we already have, no extra timer.
        await self._refresh_sidebar()
        if not pending:
            return
        approval = pending[0]
        if str(approval["id"]) == self._last_approval_shown:
            return
        self._last_approval_shown = str(approval["id"])
        await self._present_approval(approval)

    async def _review_approvals(self) -> None:
        if self.client is None:
            self._chat_log.write("[red](not connected)[/]\n")
            return
        try:
            pending = await self.client.call("approvals.list", timeout=5.0)
        except (RpcClientError, RpcClosed) as exc:
            self._chat_log.write(f"[red]approvals.list failed:[/] {exc}\n")
            return
        if not pending:
            self._chat_log.write("[dim](no pending approvals)[/]\n")
            return
        await self._present_approval(pending[0])

    async def _present_approval(self, approval: dict[str, Any]) -> None:
        decision = await self.push_screen_wait(
            ApprovalScreen(
                summary=str(approval.get("summary") or ""),
                action_class=str(approval.get("action_class") or ""),
            )
        )
        if decision in ("approve", "reject"):
            try:
                await self.client.call(
                    "approval.respond",
                    {
                        "approval_id": approval["id"],
                        "decision": decision,
                    },
                    timeout=15.0,
                )
                marker = "[green]✓ Approved[/]" if decision == "approve" else "[yellow]✗ Rejected[/]"
                summary = str(approval.get("summary") or "")
                self._chat_log.write(
                    f"\n{marker} · {summary[:80]}\n"
                )
            except (RpcClientError, RpcClosed) as exc:
                self._chat_log.write(
                    f"\n[red]approval failed:[/] {exc}\n"
                )
        elif decision == "view":
            self._chat_log.write(
                f"\n[bold]Approval detail[/]\n"
                f"  id: {approval.get('id')}\n"
                f"  action_class: {approval.get('action_class')}\n"
                f"  summary:\n    {approval.get('summary')}\n"
            )

    # ---- Message log + draft persistence ----

    def _append_to_log(self, entry: dict[str, Any]) -> None:
        """Add to the in-memory parallel log used by /search /copy
        /edit. Trim oldest if we exceed the cap so a long-running
        session doesn't grow unbounded."""
        self._message_log.append(entry)
        if len(self._message_log) > 500:
            # Drop the oldest 50 in one swoop — cheaper than
            # popping once per append.
            self._message_log = self._message_log[-450:]

    async def _restore_draft(self) -> None:
        """Restore a draft input the founder had typed-but-not-sent
        the last time the TUI quit. Hermes does this; saves Mike
        retyping a long ask if his SSH drops."""
        path = _draft_path()
        if not path.exists():
            return
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return
        text = text.strip()
        if not text or self._composer is None:
            return
        self._composer.value = text
        self._chat_log.write(
            f"\n[dim](restored {len(text)}-char draft from last session)[/]\n"
        )

    def _save_draft(self) -> None:
        """Persist whatever's currently in the composer. Best-effort
        — failures are silent so a borked filesystem doesn't surface
        as a crash on quit."""
        if self._composer is None:
            return
        text = self._composer.value or ""
        try:
            _state_path().mkdir(parents=True, exist_ok=True)
            _draft_path().write_text(text, encoding="utf-8")
        except OSError:
            pass

    def _clear_draft_file(self) -> None:
        """Delete the draft file once the message has been sent
        (or the user hit /clear). Avoids re-injecting stale
        content next launch."""
        try:
            _draft_path().unlink(missing_ok=True)
        except OSError:
            pass

    # ---- /search /copy /edit ----

    async def _search_scrollback(self, query: str) -> None:
        """Substring search through the parallel message log + dump
        matches as a panel. Case-insensitive. Hermes has full-screen
        search; we keep it inline because long sessions still fit
        comfortably in a panel."""
        if not query.strip():
            self._chat_log.write("[dim](search needs a query)[/]\n")
            return
        q = query.strip().lower()
        hits: list[dict[str, Any]] = []
        for entry in self._message_log:
            content = str(entry.get("content") or "")
            if q in content.lower():
                hits.append(entry)
        if not hits:
            self._chat_log.write(f"[dim](no matches for {query!r})[/]\n")
            return
        body_lines: list[str] = []
        for entry in hits[-20:]:  # most recent 20
            role = entry.get("role", "?")
            ts = str(entry.get("ts") or "")[:16]
            content = str(entry.get("content") or "")
            preview = content[:160].replace("\n", " ")
            if len(content) > 160:
                preview += " …"
            body_lines.append(f"[{ts}] {role}: {preview}")
        panel = Panel(
            Text("\n".join(body_lines)),
            title=f"[cyan]search[/] · {len(hits)} match{'es' if len(hits) != 1 else ''} for {query!r}",
            border_style="cyan",
        )
        self._chat_log.write(panel)

    async def _copy_message(self, which: str | None) -> None:
        """Copy a message's text to the clipboard.

        ``which`` selects:
          * ``"last"`` (default) — last agent reply
          * ``"me"``  — last founder message
          * ``"<n>"`` — Nth most recent (1 = newest)

        Falls back to printing the text in a panel if the clipboard
        isn't available (typical on a remote SSH session). Mike can
        copy from there with mouse-select + the terminal's own
        clipboard.
        """
        which = (which or "last").lower()
        target: dict[str, Any] | None = None
        if which == "last":
            for entry in reversed(self._message_log):
                if entry.get("role") == "agent":
                    target = entry
                    break
        elif which == "me":
            for entry in reversed(self._message_log):
                if entry.get("role") == "founder":
                    target = entry
                    break
        else:
            try:
                n = int(which)
                if n > 0:
                    target = self._message_log[-n]
            except (ValueError, IndexError):
                target = None

        if target is None:
            self._chat_log.write(
                f"[red]copy: no message matches {which!r}[/]\n"
            )
            return
        content = str(target.get("content") or "")

        # Try the system clipboard via xclip / pbcopy / wl-copy.
        # Headless / SSH sessions usually don't have any of those —
        # fall back to a panel + tell Mike to mouse-select.
        if await self._copy_to_system_clipboard(content):
            self._chat_log.write(
                f"[green]✓ copied {len(content)} chars to clipboard[/]\n"
            )
            return
        panel = Panel(
            Text(content),
            title="[cyan]copy[/] (no system clipboard — select text manually)",
            border_style="cyan",
        )
        self._chat_log.write(panel)

    async def _copy_to_system_clipboard(self, text: str) -> bool:
        """Try the three common Linux clipboard binaries + macOS's
        pbcopy. Returns True on success, False if none available
        (in which case the caller falls back to a panel)."""
        import shutil
        candidates: list[list[str]] = []
        if shutil.which("pbcopy"):
            candidates.append(["pbcopy"])
        if shutil.which("wl-copy"):
            candidates.append(["wl-copy"])
        if shutil.which("xclip"):
            candidates.append(["xclip", "-selection", "clipboard"])
        if shutil.which("xsel"):
            candidates.append(["xsel", "--clipboard", "--input"])
        for cmd in candidates:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.communicate(input=text.encode("utf-8"))
                if proc.returncode == 0:
                    return True
            except Exception:
                continue
        return False

    async def _edit_last_founder_message(self) -> None:
        """Pull the most recent founder message back into the
        composer. Common when Mike realizes he typed the wrong
        thing 2 messages ago and wants to re-send a corrected
        version. Doesn't delete the original — that lives in the
        thread history."""
        for entry in reversed(self._message_log):
            if entry.get("role") == "founder":
                if self._composer is None:
                    return
                self._composer.value = str(entry.get("content") or "")
                self._composer.focus()
                self._chat_log.write(
                    "[dim](pulled last message into composer — edit + Enter to resend)[/]\n"
                )
                return
        self._chat_log.write(
            "[dim](no previous founder message to edit)[/]\n"
        )

    # ---- Navigation: sessions + agents ----

    async def _open_session_picker(self) -> None:
        """List past sessions, let Mike pick one to view or resume."""
        if self.client is None:
            self._chat_log.write("[red](not connected)[/]\n")
            return
        try:
            sessions = await self.client.call(
                "session.list", timeout=5.0,
            )
        except (RpcClientError, RpcClosed) as exc:
            self._chat_log.write(f"[red]session.list failed:[/] {exc}\n")
            return
        if not sessions:
            self._chat_log.write("[dim](no past sessions)[/]\n")
            return
        rows: list[dict[str, Any]] = []
        for s in sessions:
            label = (
                f"{'●' if s.get('is_active') else '○'} "
                f"{s.get('topic') or 'Untitled':<32}  "
                f"{(s.get('last_message_at') or '')[:16]}"
            )
            rows.append({**s, "__display": label})
        choice = await self.push_screen_wait(
            _PickerScreen("Sessions — pick one to resume", rows)
        )
        if not choice:
            return
        thread_id = choice.get("id")
        if not thread_id:
            return
        if choice.get("is_active"):
            self._chat_log.write(
                f"[dim](session {thread_id[:8]} is already active)[/]\n"
            )
            return
        try:
            await self.client.call(
                "session.resume", {"thread_id": thread_id}, timeout=10.0,
            )
            self._chat_log.write(
                f"[green]✓ Resumed session {thread_id[:8]}[/]\n"
                "[dim](use /history to replay or just keep typing)[/]\n"
            )
        except (RpcClientError, RpcClosed) as exc:
            self._chat_log.write(f"[red]session.resume failed:[/] {exc}\n")

    async def _start_new_session(self) -> None:
        """Archive the active web thread + tell the user."""
        if self.client is None:
            self._chat_log.write("[red](not connected)[/]\n")
            return
        try:
            result = await self.client.call(
                "session.new", timeout=5.0,
            )
            n = int(result.get("closed") or 0)
            self._chat_log.write(
                f"[green]✓ Started new session[/] "
                f"(archived {n} previous active thread{'s' if n != 1 else ''})\n"
            )
        except (RpcClientError, RpcClosed) as exc:
            self._chat_log.write(f"[red]session.new failed:[/] {exc}\n")

    async def _open_agent_picker(self) -> None:
        """List active agents, let Mike pick one to address directly.

        Picking an agent injects a ``[CTO]``-style tag at the start
        of the next message so the CEO router knows to delegate
        rather than respond. This is the same mechanism the web
        chat uses; the picker just makes it discoverable in the TUI.
        """
        if self.client is None:
            self._chat_log.write("[red](not connected)[/]\n")
            return
        try:
            agents = await self.client.call(
                "agents.list", timeout=5.0,
            )
        except (RpcClientError, RpcClosed) as exc:
            self._chat_log.write(f"[red]agents.list failed:[/] {exc}\n")
            return
        if not agents:
            self._chat_log.write(
                "[dim](no agents hired yet — ask the cofounder to "
                "build the team first)[/]\n"
            )
            return
        rows: list[dict[str, Any]] = [
            {
                **a,
                "__display": (
                    f"{a.get('role_type', '?').upper():<5}  "
                    f"{a.get('title') or '?':<24}  "
                    f"{a.get('specialty') or ''}"
                ),
            }
            for a in agents
        ]
        choice = await self.push_screen_wait(
            _PickerScreen("Address an agent directly", rows)
        )
        if not choice:
            return
        role_type = str(choice.get("role_type") or "").upper()
        if not role_type or self._composer is None:
            return
        # Hermes-style bracket-tag delegation: CEO router recognises
        # the prefix + dispatches to the right Director.
        existing = (self._composer.value or "").lstrip()
        if existing.startswith(f"[{role_type}]"):
            self._chat_log.write(
                f"[dim](composer already addressed to {role_type})[/]\n"
            )
            return
        self._composer.value = f"[{role_type}] {existing}".strip() + " "
        self._composer.focus()
        self._chat_log.write(
            f"[dim](composer now addressed to {role_type} — "
            f"finish your message + Enter to send)[/]\n"
        )

    async def _show_history(self, limit: int = 30) -> None:
        """Replay the active session's recent history. Helpful
        after /resume so Mike sees what was said before."""
        if self.client is None:
            return
        try:
            history = await self.client.call(
                "session.history", {"limit": limit}, timeout=10.0,
            )
        except (RpcClientError, RpcClosed) as exc:
            self._chat_log.write(f"[red]session.history failed:[/] {exc}\n")
            return
        if not history:
            self._chat_log.write("[dim](no history yet)[/]\n")
            return
        self._chat_log.write(f"\n[dim]── replaying {len(history)} messages ──[/]")
        for msg in history:
            sender = msg.get("sender_type") or "system"
            content = msg.get("content") or ""
            if sender == "founder":
                self._chat_log.write(f"\n[bold green]You[/]")
                self._chat_log.write(content)
            elif sender == "agent":
                title = msg.get("sender_role_title") or "Cofounder"
                self._chat_log.write(f"\n[bold magenta]{title}[/]")
                self._chat_log.write(Markdown(content))
            else:
                self._chat_log.write(f"\n[dim]{content}[/]")
        self._chat_log.write("\n[dim]── end replay ──[/]\n")

    # ---- Catalogs + identity ----

    async def _open_theme_picker(self) -> None:
        themes = all_themes()
        rows: list[dict[str, Any]] = []
        for theme in sorted(themes.values(), key=lambda t: t.name):
            marker = "●" if theme.name == self._active_theme_name else "○"
            rows.append({
                "name": theme.name,
                "__display": (
                    f"{marker} {theme.label:<32}  {theme.description}"
                ),
            })
        choice = await self.push_screen_wait(
            _PickerScreen("Theme — pick one to apply", rows)
        )
        if not choice:
            return
        name = choice.get("name")
        if not name:
            return
        self._apply_theme(str(name))
        set_active_theme_name(str(name))
        self._chat_log.write(f"[green]✓ Theme set to {name}[/]\n")

    async def _show_skills_catalog(self) -> None:
        if self.client is None:
            self._chat_log.write("[red](not connected)[/]\n")
            return
        try:
            skills = await self.client.call("skills.list", timeout=5.0)
        except (RpcClientError, RpcClosed) as exc:
            self._chat_log.write(f"[red]skills.list failed:[/] {exc}\n")
            return
        if not skills:
            self._chat_log.write("[dim](no skills registered)[/]\n")
            return
        body_lines = [
            f"  [cyan]{s['name']:<36}[/] {s.get('description', '')[:64]}"
            for s in skills
        ]
        panel = Panel(
            Text("\n".join(line.replace("[cyan]", "").replace("[/]", "") for line in body_lines)),
            title=f"[cyan]skills[/] · {len(skills)} registered",
            border_style="cyan",
        )
        self._chat_log.write(panel)

    async def _show_me(self) -> None:
        if self.client is None:
            self._chat_log.write("[red](not connected)[/]\n")
            return
        try:
            data = await self.client.call("me", timeout=5.0)
        except (RpcClientError, RpcClosed) as exc:
            self._chat_log.write(f"[red]me failed:[/] {exc}\n")
            return
        founder = data.get("founder") or {}
        business = data.get("business") or {}
        self._chat_log.write(
            f"\n[bold]Identity[/]\n"
            f"  Founder:  {founder.get('display_name') or founder.get('email')}\n"
            f"  Email:    {founder.get('email')}\n"
            f"  Business: {business.get('name')}\n"
            f"  About:    {business.get('description')}\n"
        )

    async def _show_methods_catalog(self) -> None:
        if self.client is None:
            self._chat_log.write("[red](not connected)[/]\n")
            return
        try:
            methods = await self.client.call("methods.list", timeout=5.0)
        except (RpcClientError, RpcClosed) as exc:
            self._chat_log.write(f"[red]methods.list failed:[/] {exc}\n")
            return
        body = "\n".join(f"  • {m}" for m in sorted(methods))
        self._chat_log.write(
            f"\n[bold]RPC methods[/] (server-side, {len(methods)} total)\n"
            f"{body}\n"
        )

    def _show_version(self) -> None:
        from korpha import __version__ as ag_version
        try:
            from importlib.metadata import version as _v
            textual_v = _v("textual")
            websockets_v = _v("websockets")
        except Exception:
            textual_v = websockets_v = "?"
        self._chat_log.write(
            f"\n[bold]Korpha TUI[/]\n"
            f"  korpha:  {ag_version}\n"
            f"  textual:    {textual_v}\n"
            f"  websockets: {websockets_v}\n"
            f"  ws_url:     {self.ws_url}\n"
            f"  theme:      {self._active_theme_name}\n"
        )

    # ---- Sidebar + detail pane ----

    async def _refresh_sidebar(self) -> None:
        """Pull session + agent + approval counts from the server +
        repaint the sidebar. Cheap — runs in the 5s approval poll
        loop so it's already on a heartbeat."""
        if self.client is None:
            return
        try:
            sessions = await self.client.call("session.list", timeout=5.0)
            agents = await self.client.call("agents.list", timeout=5.0)
            pending = await self.client.call("approvals.list", timeout=5.0)
        except (RpcClientError, RpcClosed):
            return
        self._sidebar_sessions_cache = sessions or []
        self._sidebar_agents_cache = agents or []
        self._render_sidebar_sessions()
        self._render_sidebar_agents()
        self._render_sidebar_approvals(len(pending or []))

    def _render_sidebar_sessions(self) -> None:
        try:
            container = self.query_one("#sidebar-sessions", Vertical)
        except Exception:
            return
        container.remove_children()
        if not self._sidebar_sessions_cache:
            container.mount(Label("(none yet)", classes="sidebar-row"))
            return
        # Show up to 8 most recent — sidebar is narrow, scrollable
        # for the rest if Mike wants the full list (use /sessions).
        for s in self._sidebar_sessions_cache[:8]:
            marker = "●" if s.get("is_active") else "○"
            topic = (s.get("topic") or "Untitled")[:18]
            ts = (s.get("last_message_at") or "")[5:10]  # MM-DD
            text = f"{marker} {topic:<18} {ts}"
            row = Label(text, classes="sidebar-row")
            if s.get("is_active"):
                row.add_class("is-active")
            row.tooltip = (
                f"Click to {'continue' if s.get('is_active') else 'resume'} — {s.get('topic') or 'Untitled'}"
            )
            row.session_id = s.get("id")  # type: ignore[attr-defined]
            container.mount(row)

    def _render_sidebar_agents(self) -> None:
        try:
            container = self.query_one("#sidebar-agents", Vertical)
        except Exception:
            return
        container.remove_children()
        if not self._sidebar_agents_cache:
            container.mount(Label("(none hired)", classes="sidebar-row"))
            return
        for a in self._sidebar_agents_cache:
            role = str(a.get("role_type") or "?").upper()
            title = (a.get("title") or "?")[:14]
            row = Label(f"{role:<5} {title}", classes="sidebar-row")
            row.tooltip = (
                f"Click to address [{role}] — Enter prefixed message"
            )
            row.agent_role_type = role  # type: ignore[attr-defined]
            container.mount(row)

    def _render_sidebar_approvals(self, count: int) -> None:
        if self._sidebar_approvals_label is not None:
            text = (
                f"[bold yellow]{count} pending[/]" if count
                else "0 pending"
            )
            self._sidebar_approvals_label.update(text)
        if self._sidebar_status_badge is not None:
            self._sidebar_status_badge.update(
                f"[bold yellow]⏵ {count} pending[/]" if count
                else "0 pending"
            )

    def action_toggle_sidebar(self) -> None:
        try:
            sidebar = self.query_one("#sidebar", Vertical)
        except Exception:
            return
        self._sidebar_visible = not self._sidebar_visible
        if self._sidebar_visible:
            sidebar.remove_class("is-collapsed")
        else:
            sidebar.add_class("is-collapsed")

    def action_toggle_detail(self) -> None:
        try:
            pane = self.query_one("#detail-pane", Vertical)
        except Exception:
            return
        self._detail_visible = not self._detail_visible
        if self._detail_visible:
            pane.remove_class("is-collapsed")
        else:
            pane.add_class("is-collapsed")

    # ---- Mouse handlers — clicking a sidebar row resumes / addresses ----

    def on_click(self, event: events.Click) -> None:
        widget = event.widget
        if widget is None:
            return
        # Click on a session row → resume that session (or surface
        # the resume modal if it's the active one).
        sid = getattr(widget, "session_id", None)
        if sid:
            self.run_worker(self._resume_session_by_id(str(sid)))
            return
        # Click on an agent row → inject [ROLE] prefix into composer.
        role_type = getattr(widget, "agent_role_type", None)
        if role_type:
            existing = (self._composer.value or "").lstrip()
            if not existing.startswith(f"[{role_type}]"):
                self._composer.value = f"[{role_type}] {existing}".strip() + " "
                self._composer.focus()
            return

    async def _resume_session_by_id(self, thread_id: str) -> None:
        if self.client is None:
            return
        try:
            await self.client.call(
                "session.resume", {"thread_id": thread_id}, timeout=5.0,
            )
            self._chat_log.write(
                f"[green]✓ Resumed session {thread_id[:8]}[/]\n"
            )
            await self._refresh_sidebar()
        except (RpcClientError, RpcClosed) as exc:
            self._chat_log.write(
                f"[red]session.resume failed:[/] {exc}\n"
            )

    # ---- Detail pane writes ----

    def _detail_write(self, content: Any) -> None:
        """Write to the right detail pane. Auto-shows the pane the
        first time something writes — so heavy reasoning traces +
        skill outputs always have a home, even when Mike forgot to
        Ctrl-D first."""
        if self._detail_log is None:
            return
        self._detail_log.write(content)
        if not self._detail_visible:
            self.action_toggle_detail()

    # ---- Image paste ----

    async def _paste_image(self) -> None:
        """Capture clipboard image → save → inline-render if the
        terminal supports it, otherwise write a path link.

        The chat message references the file path; the agent gets
        the image as an attachment in a future commit (server-side
        prompt.submit doesn't accept attachments yet — that's the
        v2 hook. For now Mike sees the image inline and the agent
        sees a textual reference.)
        """
        self._chat_log.write("[dim](reading clipboard…)[/]")
        try:
            image = await capture_clipboard_image()
        except Exception as exc:
            self._chat_log.write(
                f"[red]clipboard read failed:[/] {exc}\n"
                f"[dim](need pbpaste / wl-paste / xclip on PATH)[/]\n"
            )
            return
        if image is None:
            self._chat_log.write(
                "[dim](no image on the clipboard — copy one first)[/]\n"
                "[dim]Need a clipboard binary: macOS=pbpaste, "
                "Wayland=wl-paste, X11=xclip[/]\n"
            )
            return

        path = save_image(image)
        size_kb = len(image.data) / 1024.0
        self._chat_log.write(
            f"\n[bold green]image attached[/] · "
            f"{image.mime} · {size_kb:.1f} KB · {path}\n"
        )

        if inline_render_supported(image):
            # Emit the raw escape sequence directly. RichLog wraps
            # via Rich, which may eat ESC bytes; print to the
            # underlying console instead.
            try:
                escape = inline_render_escape(image)
                if escape:
                    # Write directly to the terminal's stdout. This
                    # bypasses Textual's render pipeline — the
                    # escape sequence renders inline, then Textual
                    # repaints around it on the next frame.
                    import sys
                    sys.stdout.write(escape + "\n")
                    sys.stdout.flush()
                    self._chat_log.write(
                        "[dim](rendered inline above)[/]\n"
                    )
                else:
                    self._chat_log.write(
                        f"[dim](open with: xdg-open / open {path})[/]\n"
                    )
            except Exception as exc:
                self._chat_log.write(
                    f"[yellow](inline render failed: {exc})[/]\n"
                    f"[dim](open with: xdg-open / open {path})[/]\n"
                )
        else:
            self._chat_log.write(
                f"[dim](your terminal doesn't support inline images — "
                f"open with: xdg-open / open {path})[/]\n"
            )

        # Append to message log so /search can find it later
        self._append_to_log({
            "role": "founder",
            "content": f"[image attached: {path}]",
            "ts": _now_iso(),
        })

    def action_paste_image(self) -> None:
        self.run_worker(self._paste_image(), exclusive=False)

    def action_undo_last(self) -> None:
        self.run_worker(self._undo_messages(1), exclusive=False)

    # ---- Server-driven undo / branch / subagent ops ----

    async def _undo_messages(self, steps: int) -> None:
        if self.client is None:
            self._chat_log.write("[red](not connected)[/]\n")
            return
        if self._streaming:
            self._chat_log.write(
                "[yellow](still streaming — /interrupt first, then /undo)[/]\n"
            )
            return
        try:
            result = await self.client.call(
                "session.undo", {"steps": steps}, timeout=10.0,
            )
        except (RpcClientError, RpcClosed) as exc:
            self._chat_log.write(f"[red]undo failed:[/] {exc}\n")
            return
        n = int(result.get("undone") or 0)
        if n == 0:
            self._chat_log.write("[dim](nothing to undo)[/]\n")
        else:
            self._chat_log.write(
                f"[yellow]↶ Undid {n} message{'s' if n != 1 else ''}.[/] "
                f"[dim](Type your next ask — the thread continues from "
                f"before the dropped turn.)[/]\n"
            )
            # Drop the corresponding entries from the in-memory log
            # so /search + /edit reflect the new reality.
            del self._message_log[-min(n, len(self._message_log)):]

    async def _branch_session(self) -> None:
        """Branch from a specific message — picks from the last 20
        in the in-memory log so the founder can rewind to a known
        good point. The picker rows show role + timestamp +
        content preview."""
        if self.client is None:
            self._chat_log.write("[red](not connected)[/]\n")
            return
        if not self._message_log:
            self._chat_log.write(
                "[dim](no messages to branch from yet)[/]\n"
            )
            return
        # The in-memory log lacks message_ids — pull the recent
        # messages with ids from the server. session.history
        # already returns ids.
        try:
            history = await self.client.call(
                "session.history", {"limit": 20}, timeout=10.0,
            )
        except (RpcClientError, RpcClosed) as exc:
            self._chat_log.write(f"[red]session.history failed:[/] {exc}\n")
            return
        if not history:
            self._chat_log.write(
                "[dim](no message history to branch from)[/]\n"
            )
            return
        rows: list[dict[str, Any]] = []
        for msg in history:
            sender = (msg.get("sender_type") or "?")[:7]
            content = (msg.get("content") or "").replace("\n", " ")[:54]
            ts = (msg.get("created_at") or "")[11:16]
            rows.append({
                **msg,
                "__display": f"{ts} {sender:<7} {content}",
            })
        choice = await self.push_screen_wait(
            _PickerScreen("Branch — pick a message to fork from", rows)
        )
        if not choice:
            return
        msg_id = choice.get("id")
        if not msg_id:
            return
        try:
            result = await self.client.call(
                "session.branch", {"message_id": msg_id}, timeout=15.0,
            )
        except (RpcClientError, RpcClosed) as exc:
            self._chat_log.write(f"[red]session.branch failed:[/] {exc}\n")
            return
        new_id = result.get("new_thread_id")
        copied = result.get("messages_copied")
        self._chat_log.write(
            f"[green]✓ Branched to new session {new_id[:8] if new_id else '?'}[/] "
            f"({copied} messages copied). "
            f"[dim]The original is now archived (in /sessions). "
            f"Type your next ask to continue on the new branch.[/]\n"
        )
        await self._refresh_sidebar()

    async def _show_running_subagents(self) -> None:
        if self.client is None:
            self._chat_log.write("[red](not connected)[/]\n")
            return
        try:
            running = await self.client.call(
                "subagent.list", timeout=5.0,
            )
        except (RpcClientError, RpcClosed) as exc:
            self._chat_log.write(f"[red]subagent.list failed:[/] {exc}\n")
            return
        if not running:
            self._chat_log.write(
                "[dim](no sub-agents running right now)[/]\n"
            )
            return
        body = "\n".join(
            f"  • [{r.get('role_type', '?').upper()}] running"
            for r in running
        )
        self._chat_log.write(
            f"\n[bold]Running sub-agents[/] ({len(running)})\n{body}\n"
            "[dim](use /kill <role> to interrupt one — e.g. "
            "/kill cto)[/]\n"
        )

    async def _memory_remember(self, text: str) -> None:
        if self.client is None:
            self._chat_log.write("[red](not connected)[/]\n")
            return
        try:
            result = await self.client.call(
                "memory.remember", {"text": text}, timeout=5.0,
            )
        except (RpcClientError, RpcClosed) as exc:
            self._chat_log.write(
                f"[red]memory.remember failed:[/] {exc}\n"
            )
            return
        mid = result.get("memory_id", "?")
        self._chat_log.write(
            f"[green]✓ Remembered:[/] {text[:120]}"
            f" [dim](id={mid[:8]}, provider={result.get('provider')})[/]\n"
        )

    async def _memory_recall(self, query: str) -> None:
        if self.client is None:
            self._chat_log.write("[red](not connected)[/]\n")
            return
        try:
            result = await self.client.call(
                "memory.recall", {"query": query, "limit": 8},
                timeout=5.0,
            )
        except (RpcClientError, RpcClosed) as exc:
            self._chat_log.write(
                f"[red]memory.recall failed:[/] {exc}\n"
            )
            return
        rows = result.get("results") or []
        if not rows:
            self._chat_log.write(
                f"[dim]No memories matching {query!r}.[/]\n"
            )
            return
        body = "\n".join(
            f"  • {r.get('text', '')}"
            + (
                f" [dim]({', '.join(r.get('tags') or [])})[/]"
                if r.get("tags") else ""
            )
            for r in rows
        )
        self._chat_log.write(
            f"[bold]Memory recall[/] for {query!r}"
            f" ({len(rows)} match{'es' if len(rows) != 1 else ''}):\n"
            f"{body}\n"
        )

    # ---- Cron slash helpers ----

    async def _cron_list(self) -> None:
        if self.client is None:
            self._chat_log.write("[red](not connected)[/]\n")
            return
        try:
            rows = await self.client.call("cron.list", timeout=5.0)
        except (RpcClientError, RpcClosed) as exc:
            self._chat_log.write(f"[red]cron.list failed:[/] {exc}\n")
            return
        if not rows:
            self._chat_log.write(
                "[dim]No cron jobs. Add via "
                "`korpha cron add` or ask the cofounder.[/]\n"
            )
            return
        body_lines = []
        for r in rows:
            status = r.get("last_status") or "?"
            disabled = "" if r.get("enabled") else " [dim](paused)[/]"
            cadence = r.get("cadence") or "?"
            name = r.get("name") or "?"
            color = {
                "ok": "green", "silent": "dim",
                "failed": "red", "never_run": "dim",
            }.get(status, "")
            body_lines.append(
                f"  [{color}]{status:<10}[/] {name:<24} "
                f"{cadence}{disabled}"
            )
        body = "\n".join(body_lines)
        self._chat_log.write(
            f"\n[bold]Cron jobs[/] ({len(rows)}):\n{body}\n"
        )

    async def _cron_run(self, name: str) -> None:
        if self.client is None:
            self._chat_log.write("[red](not connected)[/]\n")
            return
        try:
            result = await self.client.call(
                "cron.run", {"name": name}, timeout=120.0,
            )
        except (RpcClientError, RpcClosed) as exc:
            self._chat_log.write(f"[red]cron.run failed:[/] {exc}\n")
            return
        status = result.get("status", "?")
        color = {
            "ok": "green", "silent": "dim",
            "failed": "red",
        }.get(status, "")
        suffix = ""
        if result.get("error"):
            suffix = f" — error: {result['error']}"
        elif result.get("stdout"):
            suffix = f" — stdout: {result['stdout'][:200]}"
        delivered = (
            " (delivered ✓)" if result.get("delivered") else ""
        )
        self._chat_log.write(
            f"[{color}]cron {name}: {status}{delivered}[/]{suffix}\n"
        )

    async def _cron_toggle(self, name: str) -> None:
        if self.client is None:
            self._chat_log.write("[red](not connected)[/]\n")
            return
        try:
            result = await self.client.call(
                "cron.toggle", {"name": name}, timeout=5.0,
            )
        except (RpcClientError, RpcClosed) as exc:
            self._chat_log.write(f"[red]cron.toggle failed:[/] {exc}\n")
            return
        state = "enabled" if result.get("enabled") else "paused"
        self._chat_log.write(
            f"[yellow]✓ Cron {name}:[/] {state}.\n"
        )

    async def _cron_delete(self, name: str) -> None:
        if self.client is None:
            self._chat_log.write("[red](not connected)[/]\n")
            return
        try:
            result = await self.client.call(
                "cron.delete", {"name": name}, timeout=5.0,
            )
        except (RpcClientError, RpcClosed) as exc:
            self._chat_log.write(f"[red]cron.delete failed:[/] {exc}\n")
            return
        if result.get("deleted"):
            self._chat_log.write(
                f"[red]✗ Deleted cron {name}.[/]\n"
            )

    async def _kanban_list(self) -> None:
        """Render the board snapshot, columns left → right."""
        if self.client is None:
            self._chat_log.write("[red](not connected)[/]\n")
            return
        try:
            result = await self.client.call("kanban.list", timeout=5.0)
        except (RpcClientError, RpcClosed) as exc:
            self._chat_log.write(f"[red]kanban.list failed:[/] {exc}\n")
            return

        snapshot = result.get("snapshot", {})
        # column ordering matches the model
        order = [
            "backlog", "specify", "ready", "in_progress",
            "review", "done", "blocked",
        ]
        any_cards = False
        for col in order:
            cards = snapshot.get(col, [])
            if not cards:
                continue
            any_cards = True
            self._chat_log.write(
                f"[bold]{col.replace('_', ' ').upper()}[/] "
                f"[dim]({len(cards)})[/]\n"
            )
            for c in cards:
                pri = c.get("priority", "normal")
                pri_pill = (
                    f"[red]({pri})[/] " if pri == "high"
                    else f"[dim]({pri})[/] " if pri == "low" else ""
                )
                owner = (
                    f"[yellow][{c.get('owner_role', '').upper()}][/] "
                    if c.get("owner_role") else ""
                )
                ev_pill = (
                    "[green]✓ev[/] " if c.get("has_evidence") else ""
                )
                claim_pill = (
                    "[cyan]●[/] " if c.get("claimed") else ""
                )
                self._chat_log.write(
                    f"  {pri_pill}{owner}{ev_pill}{claim_pill}"
                    f"{c.get('title', '')} "
                    f"[dim]{c.get('id', '')[:8]}[/]\n"
                )
        if not any_cards:
            self._chat_log.write(
                "[dim]Board is empty. Add a card with "
                "/kanban add <title> or via the chat.[/]\n"
            )

    async def _kanban_add(self, title: str) -> None:
        if self.client is None:
            self._chat_log.write("[red](not connected)[/]\n")
            return
        try:
            result = await self.client.call(
                "kanban.add", {"title": title}, timeout=5.0,
            )
        except (RpcClientError, RpcClosed) as exc:
            self._chat_log.write(f"[red]kanban.add failed:[/] {exc}\n")
            return
        self._chat_log.write(
            f"[green]✓ Added to BACKLOG:[/] {result.get('title')} "
            f"[dim]{result.get('id', '')[:8]}[/]\n"
        )

    async def _kanban_move(self, arg: str) -> None:
        """Parse '<card_id_prefix> <column>' and call kanban.move.
        Accepts a UUID prefix — we resolve to a full UUID by listing
        the board first and matching the prefix."""
        parts = arg.split(None, 1)
        if len(parts) != 2:
            self._chat_log.write(
                "[red]Usage: /kanban move <card_id> <column>[/]\n"
            )
            return
        prefix, to_column = parts[0].strip(), parts[1].strip().lower()
        full_id = await self._kanban_resolve_prefix(prefix)
        if full_id is None:
            return
        if self.client is None:
            return
        try:
            result = await self.client.call(
                "kanban.move",
                {"card_id": full_id, "to_column": to_column},
                timeout=5.0,
            )
        except (RpcClientError, RpcClosed) as exc:
            self._chat_log.write(f"[red]kanban.move failed:[/] {exc}\n")
            return
        self._chat_log.write(
            f"[green]✓ Moved[/] [dim]{result.get('id', '')[:8]}[/] "
            f"→ [bold]{result.get('column')}[/]\n"
        )

    async def _kanban_archive(self, prefix: str) -> None:
        full_id = await self._kanban_resolve_prefix(prefix)
        if full_id is None:
            return
        if self.client is None:
            return
        try:
            await self.client.call(
                "kanban.archive", {"card_id": full_id}, timeout=5.0,
            )
        except (RpcClientError, RpcClosed) as exc:
            self._chat_log.write(
                f"[red]kanban.archive failed:[/] {exc}\n"
            )
            return
        self._chat_log.write(
            f"[red]✗ Archived[/] [dim]{full_id[:8]}[/]\n"
        )

    async def _kanban_resolve_prefix(self, prefix: str) -> str | None:
        """Match a UUID prefix against the board snapshot. Returns
        the full id if exactly one card matches; logs + returns None
        otherwise."""
        if self.client is None:
            self._chat_log.write("[red](not connected)[/]\n")
            return None
        if not prefix:
            self._chat_log.write("[red]card_id is required[/]\n")
            return None
        # Full UUID — no need to resolve.
        if len(prefix) >= 36 and prefix.count("-") == 4:
            return prefix
        try:
            snap = await self.client.call("kanban.list", timeout=5.0)
        except (RpcClientError, RpcClosed) as exc:
            self._chat_log.write(f"[red]kanban.list failed:[/] {exc}\n")
            return None
        matches: list[str] = []
        for cards in (snap.get("snapshot") or {}).values():
            for c in cards:
                cid = c.get("id", "")
                if cid.startswith(prefix):
                    matches.append(cid)
        if not matches:
            self._chat_log.write(
                f"[red]No card matches prefix {prefix!r}.[/]\n"
            )
            return None
        if len(matches) > 1:
            self._chat_log.write(
                f"[red]Prefix {prefix!r} matches {len(matches)} "
                "cards — be more specific.[/]\n"
            )
            return None
        return matches[0]

    # ---- /team ----

    async def _team_list_slash(self) -> None:
        if self.client is None:
            self._chat_log.write("[red](not connected)[/]\n")
            return
        try:
            result = await self.client.call("team.list", timeout=5.0)
        except (RpcClientError, RpcClosed) as exc:
            self._chat_log.write(f"[red]team.list failed:[/] {exc}\n")
            return
        c_suite = result.get("c_suite", [])
        workers = result.get("workers", [])
        if c_suite:
            self._chat_log.write(
                f"[bold]C-suite[/] [dim]({len(c_suite)})[/]\n"
            )
            for r in c_suite:
                self._chat_log.write(
                    f"  [yellow]{r['role_type'].upper():>16}[/]  "
                    f"{r['title']}\n"
                )
        if workers:
            self._chat_log.write(
                f"[bold]Workers[/] [dim]({len(workers)})[/]\n"
            )
            for r in workers:
                self._chat_log.write(
                    f"  [dim]{r['id'][:8]}[/]  {r['title']} "
                    f"— [cyan]{r['specialty']}[/]\n"
                )
        if not c_suite and not workers:
            self._chat_log.write("[dim](no team yet)[/]\n")

    async def _team_hire_slash(self, specialty: str) -> None:
        if self.client is None:
            return
        try:
            result = await self.client.call(
                "team.hire", {"specialty": specialty}, timeout=5.0,
            )
        except (RpcClientError, RpcClosed) as exc:
            self._chat_log.write(f"[red]team.hire failed:[/] {exc}\n")
            return
        self._chat_log.write(
            f"[green]✓ Hired[/] {result.get('title')} "
            f"([cyan]{result.get('specialty')}[/]) "
            f"[dim]{result.get('id', '')[:8]}[/]\n"
        )

    async def _team_fire_slash(self, prefix: str) -> None:
        if self.client is None:
            return
        # Resolve the prefix against team.list first
        try:
            team = await self.client.call("team.list", timeout=5.0)
        except (RpcClientError, RpcClosed) as exc:
            self._chat_log.write(f"[red]team.list failed:[/] {exc}\n")
            return
        full_id: str | None = None
        if len(prefix) >= 36 and prefix.count("-") == 4:
            full_id = prefix
        else:
            matches = [
                w["id"] for w in team.get("workers", [])
                if w["id"].startswith(prefix)
            ]
            if not matches:
                self._chat_log.write(
                    f"[red]No worker matches prefix {prefix!r}.[/]\n"
                )
                return
            if len(matches) > 1:
                self._chat_log.write(
                    f"[red]Prefix matches {len(matches)} workers — "
                    "be more specific.[/]\n"
                )
                return
            full_id = matches[0]
        try:
            result = await self.client.call(
                "team.fire", {"agent_role_id": full_id}, timeout=5.0,
            )
        except (RpcClientError, RpcClosed) as exc:
            self._chat_log.write(f"[red]team.fire failed:[/] {exc}\n")
            return
        self._chat_log.write(
            f"[red]✗ Fired[/] {result.get('title')} "
            f"[dim]{result.get('id', '')[:8]}[/]\n"
        )

    # ---- /note ----

    async def _note_list_slash(self, store: str) -> None:
        if self.client is None:
            return
        try:
            result = await self.client.call(
                "note.list", {"store": store}, timeout=5.0,
            )
        except (RpcClientError, RpcClosed) as exc:
            self._chat_log.write(f"[red]note.list failed:[/] {exc}\n")
            return
        used = result.get("used", 0)
        limit = result.get("limit", 0)
        pct = int((used / limit) * 100) if limit else 0
        self._chat_log.write(
            f"[bold]{store.upper()}[/] [dim]({used}/{limit} "
            f"chars, {pct}%)[/]\n"
        )
        for e in result.get("entries", []):
            self._chat_log.write(f"  {e['content']}\n")
        if not result.get("entries"):
            self._chat_log.write("[dim](empty)[/]\n")

    async def _note_add_slash(self, store: str, text: str) -> None:
        if self.client is None:
            return
        try:
            result = await self.client.call(
                "note.add",
                {"store": store, "content": text}, timeout=5.0,
            )
        except (RpcClientError, RpcClosed) as exc:
            self._chat_log.write(f"[red]note.add failed:[/] {exc}\n")
            return
        self._chat_log.write(
            f"[green]✓ Saved to {store}:[/] {result.get('content')}\n"
        )

    async def _note_remove_slash(
        self, store: str, substring: str,
    ) -> None:
        if self.client is None:
            return
        try:
            await self.client.call(
                "note.remove",
                {"store": store, "old_text": substring},
                timeout=5.0,
            )
        except (RpcClientError, RpcClosed) as exc:
            self._chat_log.write(
                f"[red]note.remove failed:[/] {exc}\n"
            )
            return
        self._chat_log.write(
            f"[red]✗ Removed from {store}:[/] {substring}\n"
        )

    async def _interrupt_subagent(self, role_type: str) -> None:
        if self.client is None:
            self._chat_log.write("[red](not connected)[/]\n")
            return
        try:
            result = await self.client.call(
                "subagent.interrupt", {"role_type": role_type},
                timeout=5.0,
            )
        except (RpcClientError, RpcClosed) as exc:
            self._chat_log.write(f"[red]subagent.interrupt failed:[/] {exc}\n")
            return
        if result.get("cancelled"):
            self._chat_log.write(
                f"[yellow]✗ Interrupted [{role_type.upper()}][/] "
                f"[dim](still streaming overall — the CEO will "
                f"summarize what other directors finished)[/]\n"
            )
        else:
            self._chat_log.write(
                f"[dim](no [{role_type.upper()}] sub-agent currently running)[/]\n"
            )

    # ---- Helpers + bindings ----

    def _set_status(self, text: str) -> None:
        if self._status_text is not None:
            self._status_text.update(text)

    def action_clear_chat(self) -> None:
        if self._chat_log is not None:
            self._chat_log.clear()

    def action_review_approvals(self) -> None:
        self.run_worker(self._review_approvals(), exclusive=False)

    def action_interrupt_stream(self) -> None:
        self.run_worker(self._interrupt_active_stream(), exclusive=False)

    def action_toggle_reasoning(self) -> None:
        self._show_reasoning = not self._show_reasoning
        state = "ON" if self._show_reasoning else "OFF"
        self._chat_log.write(
            f"[dim]reasoning trace display: [bold]{state}[/][/]\n"
        )

    def action_toggle_operator(self) -> None:
        self._operator_mode = not self._operator_mode
        state = "ON" if self._operator_mode else "OFF"
        self._chat_log.write(
            f"[dim]operator mode: [bold]{state}[/] "
            f"({'raw payloads + req ids visible' if self._operator_mode else 'friendly defaults'})[/]\n"
        )

    def action_open_search(self) -> None:
        """Ctrl-F: prompt for query inline by inserting /search at
        cursor. Lighter than spawning a modal for one-off lookup."""
        if self._composer is not None:
            self._composer.value = "/search "
            self._composer.focus()

    def action_edit_last(self) -> None:
        self.run_worker(self._edit_last_founder_message(), exclusive=False)

    def action_open_sessions(self) -> None:
        self.run_worker(self._open_session_picker(), exclusive=False)

    def action_open_agents(self) -> None:
        self.run_worker(self._open_agent_picker(), exclusive=False)

    def action_start_new_session(self) -> None:
        self.run_worker(self._start_new_session(), exclusive=False)

    async def on_unmount(self) -> None:
        # Save the in-flight draft so reopening the TUI restores it.
        # Do this BEFORE closing the WS so a flaky network can't
        # eat the save.
        self._save_draft()
        if self.client is not None:
            await self.client.close()


def _now() -> str:
    return datetime.now(UTC).strftime("%H:%M")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _state_path() -> Path:
    """Where the TUI persists draft input + future cross-restart
    state. ``KORPHA_DATA_DIR`` overrides for tests."""
    base = os.getenv("KORPHA_DATA_DIR")
    return Path(base) if base else Path.home() / ".korpha"


def _draft_path() -> Path:
    return _state_path() / "tui_draft.txt"


# ---------------------------------------------------------------------------
# Entry point — picks the URL + boots the app
# ---------------------------------------------------------------------------


def run_tui(*, ws_url: str | None = None) -> None:
    """Boot the TUI. ``ws_url`` defaults to the local server.

    Order of resolution:
      1. ``ws_url`` argument (test / CLI flag override)
      2. ``KORPHA_TUI_WS_URL`` env var
      3. ``ws://localhost:8765/api/tui/ws``

    Doesn't pre-flight the connection — the app itself shows a clear
    error in chat if the server is unreachable. That keeps the TUI's
    UX consistent across "wrong URL" / "server down" / "auth failure"
    rather than a stack trace at startup.
    """
    if ws_url is None:
        ws_url = os.getenv(
            "KORPHA_TUI_WS_URL", "ws://localhost:8765/api/tui/ws",
        )
    KorphaTUI(ws_url=ws_url).run()


__all__ = ["KorphaTUI", "ApprovalScreen", "run_tui"]
