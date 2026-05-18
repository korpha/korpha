"""OAuth proxy — expose AIgenteur's OAuth-authed subscriptions as
an OpenAI-compatible HTTP endpoint.

Why this exists:
  Mike pays for X Premium+ (Grok), Claude Pro, and ChatGPT Plus.
  AIgenteur uses those subscriptions through OAuth for its own
  agents. But he ALSO wants to point Aider / Cline / Continue /
  Cursor / any IDE coding assistant at one of those subscriptions
  — without giving the IDE the OAuth tokens, without paying an
  extra subscription, without spinning up litellm.

  This proxy mounts an OpenAI-compatible ``/v1/chat/completions``
  (and ``/v1/models``) endpoint on http://127.0.0.1:8645/v1.
  Behind the scenes it picks the right OAuth provider per model
  alias and forwards through our existing Responses-API providers
  (codex_responses, xai_responses, claude_code).

  External IDE config becomes::

      OPENAI_API_BASE=http://127.0.0.1:8645/v1
      OPENAI_API_KEY=any-non-empty-string

  And the IDE now drives Grok / Claude / GPT through your subs.

Model aliases (configurable, sensible defaults):

  grok                    → xai-oauth :: grok-4.20-0309-reasoning
  grok-fast               → xai-oauth :: grok-4.20-0309-non-reasoning
  claude / claude-sonnet  → claude-code (subprocess CLI)
  claude-opus             → claude-code (subprocess CLI, model=opus)
  gpt / gpt-5             → codex-responses (subscription)

The proxy is OPT-IN — runs only when ``aigenteur proxy serve`` is
invoked. Not bound to the public dashboard port; localhost-only by
default so unauthenticated IDE connections can't escape the box.
"""
from korpha.proxy.server import (
    DEFAULT_PROXY_HOST,
    DEFAULT_PROXY_PORT,
    PROXY_API_BASE,
    build_proxy_app,
)

__all__ = [
    "DEFAULT_PROXY_HOST",
    "DEFAULT_PROXY_PORT",
    "PROXY_API_BASE",
    "build_proxy_app",
]
