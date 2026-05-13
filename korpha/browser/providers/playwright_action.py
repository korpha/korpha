"""LLM-driven Playwright action loop.

The fetch provider only navigates and reads. This one *acts*: it
takes an accessibility-tree snapshot, asks an LLM what the next
single-step action is (click / type / navigate / scroll / done),
executes it, and repeats until the LLM emits ``done``.

Inspired by Hermes's accessibility-tree pattern (each interactive
element gets a stable ``@e1`` / ``@e2`` ref) so the LLM can address
elements by short id rather than wrestling with selectors. We don't
import or vendor any Hermes code; the implementation is hand-rolled
on top of Playwright's accessibility snapshot APIs.

Both headless and headed via ``task.headless``. Headed = founder can
literally watch Chrome do the thing — useful for first-time setup,
debugging, or supervising a sensitive action.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from typing import Any

from korpha._jsonext import extract_json_dict
from korpha.audit.model import InferenceTier
from korpha.browser.service import (
    BrowserError,
    BrowserProvider,
    BrowserResult,
    BrowserTask,
)
from korpha.inference.limits import agent_max_tokens
from korpha.inference.pool import InferencePool
from korpha.inference.types import CompletionRequest, Message, Role

_DEFAULT_MAX_STEPS = 12
_TEXT_LIMIT = 32_000
_SNAPSHOT_LIMIT = 4000  # chars of element-tree text per step
_STEP_TIMEOUT_S = 25.0


_SYSTEM_PROMPT = """\
You drive a browser one step at a time on behalf of an AI cofounder.
At each step you receive a numbered list of interactive elements on
the current page; respond with ONE action.

Allowed actions (strict JSON only):
  {"action": "click", "ref": "@e3"}
  {"action": "type", "ref": "@e7", "text": "string", "submit": false}
  {"action": "navigate", "url": "https://..."}
  {"action": "scroll", "direction": "down"}             # or "up"
  {"action": "done", "result": "what you accomplished"}
  {"action": "abort", "reason": "why you stopped"}

Rules:
- Pick the SINGLE next action, never a list.
- Quote the ref exactly as shown (e.g. "@e5").
- For type, set submit=true only if you also want to press Enter.
- Stop with done() as soon as the goal is satisfied.
- Stop with abort() if blocked by login, captcha, or missing element.
- Never invent refs; only use ones from the current snapshot.
"""


@dataclass(frozen=True)
class _Action:
    kind: str
    ref: str | None = None
    text: str | None = None
    submit: bool = False
    url: str | None = None
    direction: str = "down"
    result: str | None = None
    reason: str | None = None


@dataclass
class PlaywrightActionProvider(BrowserProvider):
    """LLM-driven Playwright action loop.

    Stays transport-level: it takes an ``InferencePool`` directly and
    does NOT write Cost rows itself. The skill wrapping this provider
    (or a CostTracker layered on top of the pool) is responsible for
    accounting the per-step spend. Cumulative cost is reported on
    BrowserResult.raw['cost_usd'] for callers that want a quick total.
    """

    pool: InferencePool
    business_id: Any
    """Used as the session_key prefix for prompt-cache affinity."""

    name: str = "playwright-action"
    max_steps: int = _DEFAULT_MAX_STEPS
    tier: InferenceTier = InferenceTier.PRO
    text_limit: int = _TEXT_LIMIT
    user_agent: str | None = None

    _browser: Any = field(default=None, init=False, repr=False)
    _playwright: Any = field(default=None, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    async def run(self, task: BrowserTask) -> BrowserResult:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise BrowserError(
                "playwright not installed. Run: uv pip install playwright "
                "&& playwright install chromium"
            ) from exc

        async with self._lock:
            if self._playwright is None:
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=task.headless
                )

        # Gate concurrent action loops via the shared pool. Action
        # loops are heavier than fetches (LLM-driven, multi-step) so
        # this matters even more — one action loop per slot keeps
        # Mike's laptop from melting under parallel scrapes.
        from korpha.browser.pool import with_browser_slot
        unit_id = getattr(task, "consumer_unit_id", None)
        async with with_browser_slot(consumer_unit_id=unit_id):
            return await self._run_under_slot(task)

    async def _run_under_slot(self, task: BrowserTask) -> BrowserResult:
        ctx = await self._browser.new_context(
            user_agent=task.user_agent or self.user_agent,
            extra_http_headers=task.extra_http_headers or None,
        )
        page = await ctx.new_page()
        action_log: list[dict[str, Any]] = []
        result_text = ""
        final_screenshot: bytes | None = None
        cumulative_cost = 0.0
        try:
            if task.start_url:
                await page.goto(
                    task.start_url,
                    timeout=int(task.timeout_seconds * 1000),
                    wait_until="domcontentloaded",
                )

            for step in range(1, self.max_steps + 1):
                snapshot = await _accessibility_snapshot(page)
                rendered_text = ""
                if task.extract_text:
                    rendered_text = await _page_text(page, self.text_limit)
                action, step_cost = await self._ask_for_action(
                    task=task,
                    snapshot=snapshot,
                    rendered_text=rendered_text,
                    step=step,
                )
                cumulative_cost += step_cost
                action_log.append(_action_to_log(action))

                if action.kind == "done":
                    result_text = action.result or ""
                    if task.take_screenshot:
                        final_screenshot = await page.screenshot(type="png")
                    return BrowserResult(
                        success=True,
                        final_url=page.url,
                        extracted_text=result_text,
                        title=await page.title(),
                        screenshot_png=final_screenshot,
                        raw={"steps": action_log, "cost_usd": cumulative_cost},
                    )
                if action.kind == "abort":
                    return BrowserResult(
                        success=False,
                        final_url=page.url,
                        error=f"agent aborted: {action.reason or '(no reason)'}",
                        raw={"steps": action_log, "cost_usd": cumulative_cost},
                    )

                try:
                    await asyncio.wait_for(
                        _execute_action(page, action, snapshot),
                        timeout=_STEP_TIMEOUT_S,
                    )
                except (BrowserError, AssertionError) as exc:
                    return BrowserResult(
                        success=False,
                        final_url=page.url,
                        error=f"action {action.kind!r} failed: {exc}",
                        raw={"steps": action_log, "cost_usd": cumulative_cost},
                    )
                except TimeoutError:
                    return BrowserResult(
                        success=False,
                        final_url=page.url,
                        error=f"action {action.kind!r} timed out",
                        raw={"steps": action_log, "cost_usd": cumulative_cost},
                    )

            return BrowserResult(
                success=False,
                final_url=page.url,
                error=f"max_steps={self.max_steps} reached without done()",
                raw={"steps": action_log, "cost_usd": cumulative_cost},
            )
        finally:
            import contextlib

            with contextlib.suppress(Exception):
                await page.close()
            with contextlib.suppress(Exception):
                await ctx.close()

    async def close(self) -> None:
        import contextlib

        async with self._lock:
            if self._browser is not None:
                with contextlib.suppress(Exception):
                    await self._browser.close()
                self._browser = None
            if self._playwright is not None:
                with contextlib.suppress(Exception):
                    await self._playwright.stop()
                self._playwright = None

    async def _ask_for_action(
        self,
        *,
        task: BrowserTask,
        snapshot: list[dict[str, Any]],
        rendered_text: str,
        step: int,
    ) -> tuple[_Action, float]:
        elements_block = "\n".join(
            f"  {row['ref']}  {row['role']:8s}  {row.get('label','')[:80]}"
            for row in snapshot[:60]
        ) or "  (no interactive elements found)"
        if len(elements_block) > _SNAPSHOT_LIMIT:
            elements_block = elements_block[:_SNAPSHOT_LIMIT] + "\n  …[truncated]"

        user_prompt = (
            f"Goal: {task.instruction}\n"
            f"Current step: {step} / {self.max_steps}\n\n"
            f"Interactive elements on current page:\n{elements_block}\n\n"
            f"What's the single next action?"
        )
        request = CompletionRequest(
            messages=[
                Message(role=Role.SYSTEM, content=_SYSTEM_PROMPT),
                Message(role=Role.USER, content=user_prompt),
            ],
            tier=self.tier,
            session_key=f"browser-action-{self.business_id}",
            max_tokens=agent_max_tokens(),
            temperature=0.1,
            timeout_seconds=task.timeout_seconds,
        )
        response = await self.pool.complete(request)
        parsed = extract_json_dict(response.content)
        if parsed is None:
            raise BrowserError(
                f"action loop: model returned unparseable output. "
                f"first 300 chars: {response.content[:300]}"
            )
        return _parse_action(parsed), float(response.cost_usd)


# ─────────────────────────── Playwright helpers ───────────────────────────


async def _page_text(page: Any, limit: int) -> str:
    raw = await page.evaluate(
        "() => document.body ? document.body.innerText : ''"
    )
    text = str(raw)
    if len(text) > limit:
        return text[:limit] + "\n…[truncated]"
    return text


async def _accessibility_snapshot(page: Any) -> list[dict[str, Any]]:
    """Build a flat list of interactive elements with stable ``@eN`` refs.

    We don't use Playwright's accessibility.snapshot() because its tree
    representation is verbose and includes too much non-actionable noise.
    Instead, we query for the standard interactive HTML elements + ARIA
    roles in document order, attach ``data-ag-ref="@eN"`` so the LLM can
    address them, and return a plain list of {ref, role, label}."""
    js = r"""
    () => {
      const ROLE_FOR = {
        BUTTON: 'button', A: 'link', INPUT: 'input',
        TEXTAREA: 'textarea', SELECT: 'select',
      };
      const els = Array.from(document.querySelectorAll(
        'a, button, input:not([type=hidden]), textarea, select, [role=button], [role=link], [role=textbox], [role=combobox]'
      ));
      const out = [];
      let n = 0;
      for (const el of els) {
        // Skip invisible elements.
        const r = el.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) continue;
        const style = window.getComputedStyle(el);
        if (style.visibility === 'hidden' || style.display === 'none') continue;
        n++;
        const ref = '@e' + n;
        el.setAttribute('data-ag-ref', ref);
        const role = el.getAttribute('role')
          || ROLE_FOR[el.tagName.toUpperCase()]
          || el.tagName.toLowerCase();
        let label =
          el.getAttribute('aria-label') ||
          el.getAttribute('placeholder') ||
          el.getAttribute('name') ||
          el.getAttribute('value') ||
          (el.innerText || '').trim().slice(0, 120) ||
          el.getAttribute('title') ||
          '';
        label = label.replace(/\s+/g, ' ').trim();
        out.push({ref, role, label});
      }
      return out;
    }
    """
    raw = await page.evaluate(js)
    return list(raw) if isinstance(raw, list) else []


async def _execute_action(
    page: Any, action: _Action, snapshot: list[dict[str, Any]]
) -> None:
    if action.kind == "navigate":
        if not action.url:
            raise BrowserError("navigate requires url")
        await page.goto(action.url, wait_until="domcontentloaded")
        return
    if action.kind == "scroll":
        delta = 600 if action.direction == "down" else -600
        await page.evaluate(f"window.scrollBy(0, {delta})")
        return

    if action.ref is None or not _REF_RE.match(action.ref):
        raise BrowserError(f"action {action.kind!r}: invalid ref {action.ref!r}")
    valid_refs = {row["ref"] for row in snapshot}
    if action.ref not in valid_refs:
        raise BrowserError(
            f"action {action.kind!r}: ref {action.ref!r} not in snapshot "
            f"({len(valid_refs)} known refs)"
        )
    selector = f'[data-ag-ref="{action.ref}"]'

    if action.kind == "click":
        await page.click(selector)
        return
    if action.kind == "type":
        text = action.text or ""
        await page.fill(selector, text)
        if action.submit:
            await page.press(selector, "Enter")
        return
    raise BrowserError(f"unknown action kind {action.kind!r}")


_REF_RE = re.compile(r"^@e\d+$")


def _parse_action(parsed: dict[str, Any]) -> _Action:
    kind = str(parsed.get("action") or "").strip()
    if kind not in ("click", "type", "navigate", "scroll", "done", "abort"):
        raise BrowserError(f"unknown action kind {kind!r}")
    return _Action(
        kind=kind,
        ref=parsed.get("ref"),
        text=parsed.get("text"),
        submit=bool(parsed.get("submit", False)),
        url=parsed.get("url"),
        direction=str(parsed.get("direction", "down")),
        result=parsed.get("result"),
        reason=parsed.get("reason"),
    )


def _action_to_log(action: _Action) -> dict[str, Any]:
    body: dict[str, Any] = {"action": action.kind}
    for k in ("ref", "text", "url", "direction", "result", "reason"):
        v = getattr(action, k)
        if v not in (None, "", False, "down"):
            body[k] = v
    if action.submit:
        body["submit"] = True
    return body


__all__ = ["PlaywrightActionProvider"]
