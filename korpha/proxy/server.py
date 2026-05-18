"""FastAPI app implementing the OpenAI ``/v1/chat/completions`` shape
on top of AIgenteur's OAuth-authed Responses providers.

Endpoints (only the ones IDEs actually call):

  GET  /v1/models                — enumerate available aliases
  POST /v1/chat/completions      — main entrypoint, stream+non-stream

Out of scope (return 404 with hint):
  - /v1/completions  (legacy completion API, deprecated upstream)
  - /v1/embeddings   (not provided by subscription endpoints)
  - /v1/images       (separate skill)
  - /v1/audio        (separate skill)
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from korpha.proxy.aliases import (
    ModelAlias, available_aliases, resolve_alias,
)
from korpha.proxy.translator import (
    chat_to_completion_request,
    completion_response_to_chat,
    completion_stream_to_chat_sse,
)

logger = logging.getLogger(__name__)


DEFAULT_PROXY_HOST = "127.0.0.1"
DEFAULT_PROXY_PORT = 8645
PROXY_API_BASE = f"http://{DEFAULT_PROXY_HOST}:{DEFAULT_PROXY_PORT}/v1"


def build_proxy_app() -> FastAPI:
    """Construct the proxy FastAPI app. No shared state with the
    main dashboard server — runs as its own process."""
    app = FastAPI(
        title="AIgenteur OAuth proxy",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
    )

    @app.get("/v1/models")
    async def list_models() -> dict[str, Any]:
        """OpenAI-compat model list. IDEs hit this on startup to
        populate the model picker."""
        aliases = available_aliases()
        return {
            "object": "list",
            "data": [
                {
                    "id": a.alias,
                    "object": "model",
                    "created": int(time.time()),
                    "owned_by": a.provider,
                    "description": a.description,
                    "real_model": a.real_model,
                }
                for a in aliases
            ],
        }

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        """Sanity check + summary for ``aigenteur proxy status``."""
        aliases = available_aliases()
        return {
            "status": "ok",
            "available_aliases": [a.alias for a in aliases],
            "available_count": len(aliases),
        }

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Any:
        try:
            body = await request.json()
        except ValueError as exc:
            raise HTTPException(400, f"bad JSON: {exc}") from exc

        model_alias = str(body.get("model") or "").strip()
        if not model_alias:
            raise HTTPException(400, "missing 'model'")
        alias = resolve_alias(model_alias)
        if alias is None:
            avail = ", ".join(a.alias for a in available_aliases())
            raise HTTPException(
                404,
                f"unknown model '{model_alias}'. "
                f"Available: {avail or '(none — no OAuth providers configured)'}",
            )
        if not alias.available():
            raise HTTPException(
                503,
                f"'{model_alias}' provider '{alias.provider}' has no "
                f"credentials. Run `aigenteur auth add {alias.provider}` "
                f"or check `aigenteur auth status`.",
            )

        stream = bool(body.get("stream", False))
        try:
            req = chat_to_completion_request(body, alias=alias)
        except ValueError as exc:
            raise HTTPException(400, f"request shape: {exc}") from exc

        provider = _build_provider_for(alias)
        account = _build_account_for(alias)

        if stream:
            return StreamingResponse(
                _stream_chat(provider, req, account, alias),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )
        # Non-streaming path.
        try:
            resp = await provider.complete(req, account)
        except Exception as exc:  # noqa: BLE001
            logger.exception("proxy: provider call failed")
            return JSONResponse(
                {
                    "error": {
                        "message": f"{type(exc).__name__}: {exc}",
                        "type": "upstream_error",
                    }
                },
                status_code=502,
            )
        return completion_response_to_chat(resp, alias=alias)

    @app.api_route(
        "/v1/{rest:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    )
    async def unsupported(rest: str) -> JSONResponse:
        """All other /v1/* return a structured 404 with a hint —
        IDEs that probe ``/v1/embeddings`` etc. get a readable
        error instead of a generic FastAPI 404."""
        return JSONResponse(
            {
                "error": {
                    "message": (
                        f"/v1/{rest} not supported by the AIgenteur "
                        f"OAuth proxy. Only /v1/chat/completions + "
                        f"/v1/models are implemented — subscription "
                        f"endpoints don't expose embeddings/images/audio "
                        f"over OpenAI-compat shape."
                    ),
                    "type": "endpoint_unsupported",
                }
            },
            status_code=404,
        )

    return app


async def _stream_chat(
    provider, req, account, alias: ModelAlias,
) -> AsyncIterator[bytes]:
    """Bridge our StreamChunk async iterator into OpenAI SSE shape.

    OpenAI's stream protocol emits ``data: {...}\\n\\n`` lines, each
    a partial chat.completion.chunk with a delta. Terminator is
    ``data: [DONE]\\n\\n``."""
    completion_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created_ts = int(time.time())
    try:
        async for sse_line in completion_stream_to_chat_sse(
            provider.stream_complete(req, account),
            completion_id=completion_id,
            created_ts=created_ts,
            alias=alias,
        ):
            yield sse_line
    except Exception as exc:  # noqa: BLE001
        logger.exception("proxy stream: provider failed")
        err_chunk = json.dumps({
            "error": {
                "message": f"{type(exc).__name__}: {exc}",
                "type": "upstream_error",
            }
        })
        yield f"data: {err_chunk}\n\n".encode()
        yield b"data: [DONE]\n\n"


def _build_provider_for(alias: ModelAlias):
    """Construct the right Provider instance for this alias.
    Cached at module level would help latency but providers are
    stateless, so per-request is fine for now."""
    if alias.provider == "xai-oauth":
        from korpha.inference.providers.xai_responses import (
            XaiResponsesProvider,
        )
        return XaiResponsesProvider()
    if alias.provider == "codex":
        from korpha.inference.providers.codex_responses import (
            CodexResponsesProvider,
        )
        return CodexResponsesProvider()
    if alias.provider == "claude-code":
        from korpha.inference.providers.claude_code import (
            ClaudeCodeProvider,
        )
        return ClaudeCodeProvider()
    raise HTTPException(
        500, f"no provider builder for {alias.provider!r}",
    )


def _build_account_for(alias: ModelAlias):
    """Synthesize a minimal ProviderAccount for the call. The
    proxy doesn't read providers.yaml — aliases are the source
    of truth."""
    from korpha.audit.model import InferenceTier
    from korpha.inference.registry import AuthType, ProviderAccount

    auth_type_map = {
        "xai-oauth": AuthType.OAUTH,
        "codex": AuthType.OAUTH,
        "claude-code": AuthType.SUBSCRIPTION_CLI,
    }
    return ProviderAccount(
        provider_name=alias.provider,
        auth_type=auth_type_map.get(alias.provider, AuthType.OAUTH),
        tier_models={
            InferenceTier.PRO: alias.real_model,
            InferenceTier.WORKHORSE: alias.real_model,
        },
        label=f"proxy:{alias.alias}",
    )


__all__ = [
    "DEFAULT_PROXY_HOST",
    "DEFAULT_PROXY_PORT",
    "PROXY_API_BASE",
    "build_proxy_app",
]
