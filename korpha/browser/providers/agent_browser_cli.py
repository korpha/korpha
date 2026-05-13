"""agent-browser CLI provider.

Wraps the npm ``agent-browser`` binary (the same one Hermes ships) as a
subprocess. Useful when:

  - You already have agent-browser installed via ``npm i -g agent-browser``
    and don't want a second Chromium pulled in by Python Playwright.
  - You're following a Hermes-style workflow and want behavior parity
    with ``hermes/tools/browser_tool.py`` (aria snapshots, session-scoped
    socket dirs, daemon idle-timeout).
  - You want to reuse Hermes browser skills that emit ``open`` / ``click``
    / ``ariaSnapshot`` commands directly.

This provider speaks the *fetch* shape of ``BrowserTask``: open the URL,
return the rendered text (or aria snapshot) and an optional screenshot.
For multi-step LLM-driven actions stay on ``PlaywrightActionProvider`` —
the action loop is a different concern and reusing it here would force
us to also implement the daemon lifecycle in two places.

Discovery order: ``$PATH`` → ``./node_modules/.bin/agent-browser`` →
``npx agent-browser``. We don't try to install anything; if nothing is
found we raise BrowserError with the install hint and the BrowserService
falls through to the next provider.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from korpha.browser.service import (
    BrowserError,
    BrowserProvider,
    BrowserResult,
    BrowserTask,
)

_DEFAULT_TEXT_LIMIT = 32_000
_INSTALL_HINT = (
    "agent-browser CLI not found. Install with: "
    "npm install -g agent-browser && agent-browser install --with-deps"
)


def _resolve_agent_browser() -> list[str] | None:
    """Locate the agent-browser executable.

    Returns the argv prefix to use (e.g. ``["/usr/local/bin/agent-browser"]``
    or ``["npx", "agent-browser"]``). Returns None when nothing is found.
    """
    direct = shutil.which("agent-browser")
    if direct:
        return [direct]
    # Local install in repo
    cwd_local = Path.cwd() / "node_modules" / ".bin" / "agent-browser"
    if cwd_local.exists():
        return [str(cwd_local)]
    npx = shutil.which("npx")
    if npx:
        return [npx, "agent-browser"]
    return None


@dataclass
class AgentBrowserCliProvider(BrowserProvider):
    """Fetch-style provider backed by the agent-browser npm CLI."""

    name: str = "agent-browser-cli"
    text_char_limit: int = _DEFAULT_TEXT_LIMIT
    """Trim aria/text output. agent-browser snapshots can be large."""

    use_aria_snapshot: bool = True
    """Use ariaSnapshot (semantic accessibility tree) instead of plain text.
    Aria snapshots are more useful for downstream LLM action loops since
    they include role/label/ref refs Hermes skills can act on. Flip to
    False if you just want innerText."""

    daemon_idle_timeout_seconds: int = 90
    """How long agent-browser daemons stick around between calls. Mirrors
    Hermes's BROWSER_SESSION_INACTIVITY_TIMEOUT default. Raise it if you
    chain several tasks rapidly; lower it to free RAM faster."""

    _session_name: str = field(default="", init=False, repr=False)
    _socket_dir: str = field(default="", init=False, repr=False)
    _argv_prefix: list[str] | None = field(default=None, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    async def run(self, task: BrowserTask) -> BrowserResult:
        if not task.start_url:
            raise BrowserError(
                "AgentBrowserCliProvider requires task.start_url — it "
                "doesn't navigate from natural language"
            )

        async with self._lock:
            if self._argv_prefix is None:
                resolved = _resolve_agent_browser()
                if resolved is None:
                    raise BrowserError(_INSTALL_HINT)
                self._argv_prefix = resolved
            if not self._session_name:
                # Short prefix to stay under the 104-byte AF_UNIX limit
                # on macOS (matches the "agent-browser-h_" convention
                # Hermes uses, but namespaced to korpha).
                self._session_name = f"aig_{uuid.uuid4().hex[:8]}"
            if not self._socket_dir:
                base = tempfile.gettempdir()
                self._socket_dir = os.path.join(
                    base, f"agent-browser-{self._session_name}"
                )
                os.makedirs(self._socket_dir, mode=0o700, exist_ok=True)

        # Open the URL
        open_args: list[str] = [task.start_url]
        if not task.headless:
            # agent-browser >=0.20 supports --headed for supervised runs.
            open_args = ["--headed", task.start_url]
        open_res = await self._invoke(
            "open", open_args, timeout=task.timeout_seconds
        )
        if not open_res.get("success", False):
            return BrowserResult(
                success=False,
                error=f"open failed: {open_res.get('error') or open_res}",
                raw={"open": open_res},
            )

        # Snapshot the page
        text = ""
        snapshot_raw: dict[str, Any] = {}
        if task.extract_text:
            cmd = "ariaSnapshot" if self.use_aria_snapshot else "text"
            snap = await self._invoke(cmd, [], timeout=task.timeout_seconds)
            snapshot_raw = snap
            if snap.get("success", False):
                payload = snap.get("snapshot") or snap.get("text") or snap.get("result") or ""
                if isinstance(payload, dict):
                    payload = json.dumps(payload, indent=2)
                text = str(payload)
                if len(text) > self.text_char_limit:
                    text = text[: self.text_char_limit] + "\n…[truncated]"

        # Optional screenshot
        screenshot_bytes: bytes | None = None
        if task.take_screenshot:
            shot = await self._invoke(
                "screenshot", [], timeout=task.timeout_seconds
            )
            path = shot.get("path") or shot.get("screenshot")
            if path and os.path.exists(path):
                with open(path, "rb") as fh:
                    screenshot_bytes = fh.read()

        # Title + final URL via eval. Cheap and avoids parsing aria.
        meta = await self._invoke(
            "eval",
            ["() => ({ title: document.title, url: location.href })"],
            timeout=task.timeout_seconds,
        )
        title = None
        final_url = task.start_url
        meta_result = meta.get("result") if isinstance(meta, dict) else None
        if isinstance(meta_result, dict):
            title = meta_result.get("title")
            final_url = meta_result.get("url") or final_url

        return BrowserResult(
            success=True,
            final_url=final_url,
            extracted_text=text,
            title=title,
            screenshot_png=screenshot_bytes,
            raw={"open": open_res, "snapshot": snapshot_raw},
        )

    async def _invoke(
        self,
        command: str,
        args: list[str],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        """Run one agent-browser CLI command and parse its JSON output.

        Failures (non-zero exit, malformed JSON, timeout) are returned as
        ``{"success": False, "error": "..."}`` rather than raised — the
        provider's ``run`` decides whether to raise BrowserError or
        return a failed BrowserResult.
        """
        assert self._argv_prefix is not None
        cmd = [
            *self._argv_prefix,
            "--session",
            self._session_name,
            "--json",
            command,
            *args,
        ]
        env = {**os.environ}
        env["AGENT_BROWSER_SOCKET_DIR"] = self._socket_dir
        env.setdefault(
            "AGENT_BROWSER_IDLE_TIMEOUT_MS",
            str(self.daemon_idle_timeout_seconds * 1000),
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.DEVNULL,
                env=env,
            )
        except FileNotFoundError as exc:
            return {"success": False, "error": f"spawn failed: {exc}"}

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except TimeoutError:
            proc.kill()
            return {
                "success": False,
                "error": f"agent-browser {command} timed out after {timeout}s",
            }

        if proc.returncode != 0:
            return {
                "success": False,
                "error": (
                    stderr.decode("utf-8", "replace").strip()
                    or f"agent-browser exited {proc.returncode}"
                ),
            }

        body = stdout.decode("utf-8", "replace").strip()
        if not body:
            return {"success": True, "result": ""}
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            # CLI sometimes prints a banner before the JSON line. Try the
            # last non-empty line as a fallback.
            for line in reversed(body.splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        parsed = json.loads(line)
                        break
                    except json.JSONDecodeError:
                        continue
            else:
                return {
                    "success": False,
                    "error": f"non-JSON output: {body[:200]}",
                }
        if isinstance(parsed, dict):
            parsed.setdefault("success", True)
            return parsed
        return {"success": True, "result": parsed}

    async def close(self) -> None:
        """Tell the daemon to shut down and clean up the socket dir."""
        import contextlib

        if self._argv_prefix is not None and self._session_name:
            with contextlib.suppress(Exception):
                await self._invoke("close", [], timeout=10)
        if self._socket_dir and os.path.isdir(self._socket_dir):
            with contextlib.suppress(Exception):
                # Best-effort cleanup; daemon writes may race us.
                for entry in os.listdir(self._socket_dir):
                    with contextlib.suppress(Exception):
                        os.remove(os.path.join(self._socket_dir, entry))
                with contextlib.suppress(Exception):
                    os.rmdir(self._socket_dir)
        self._session_name = ""
        self._socket_dir = ""


__all__ = ["AgentBrowserCliProvider"]
