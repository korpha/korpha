"""OpenAI ``/v1/chat/completions`` ↔ AIgenteur CompletionRequest
translation.

Why a separate module: the proxy server handles HTTP/routing concerns;
this module is pure data shaping. Easier to unit-test the conversion
logic without spinning up FastAPI.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator

from korpha.audit.model import InferenceTier
from korpha.inference.types import (
    CompletionRequest,
    CompletionResponse,
    ImageRef,
    Message,
    Role,
    StreamChunk,
)
from korpha.proxy.aliases import ModelAlias


def chat_to_completion_request(
    body: dict[str, Any], *, alias: ModelAlias,
) -> CompletionRequest:
    """Parse an OpenAI chat-completions JSON body into our internal
    CompletionRequest.

    Supports:
      - ``messages`` — array of {role, content} (content can be str
        or list[{type, text|image_url}])
      - ``stream``, ``max_tokens``, ``temperature``, ``stop``
      - ``user`` — passed through as session_key for cache affinity
      - ``tools`` — opaque pass-through; only providers that handle
        tool_calls will use them

    Rejects:
      - empty messages
      - ``n > 1`` (we don't support multiple completions per call)
      - ``functions`` (legacy; use ``tools`` instead)
    """
    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list) or not raw_messages:
        raise ValueError("'messages' must be a non-empty list")

    if int(body.get("n") or 1) != 1:
        raise ValueError("only n=1 supported")
    if body.get("functions") is not None:
        raise ValueError(
            "'functions' is deprecated; use 'tools' instead",
        )

    messages: list[Message] = []
    for i, m in enumerate(raw_messages):
        if not isinstance(m, dict):
            raise ValueError(f"messages[{i}] must be an object")
        role_str = str(m.get("role") or "").strip().lower()
        try:
            role = Role(role_str)
        except ValueError as exc:
            raise ValueError(
                f"messages[{i}].role={role_str!r} invalid",
            ) from exc

        text_content, images = _extract_content(m.get("content"))
        messages.append(Message(
            role=role,
            content=text_content,
            images=tuple(images),
            name=m.get("name") or None,
        ))

    user_id = body.get("user")
    session_key = (
        str(user_id) if user_id else f"proxy:{uuid.uuid4().hex[:12]}"
    )

    max_tokens = body.get("max_tokens")
    try:
        max_tokens_int: int | None = (
            int(max_tokens) if max_tokens is not None else None
        )
    except (TypeError, ValueError):
        max_tokens_int = None

    temperature = body.get("temperature")
    try:
        temperature_float: float | None = (
            float(temperature) if temperature is not None else None
        )
    except (TypeError, ValueError):
        temperature_float = None

    stop_raw = body.get("stop") or ()
    if isinstance(stop_raw, str):
        stop_tuple = (stop_raw,)
    elif isinstance(stop_raw, (list, tuple)):
        stop_tuple = tuple(str(s) for s in stop_raw if s)
    else:
        stop_tuple = ()

    tools = body.get("tools") or []
    if not isinstance(tools, list):
        tools = []

    return CompletionRequest(
        messages=messages,
        tier=InferenceTier.PRO,  # proxy doesn't distinguish tiers
        session_key=session_key,
        max_tokens=max_tokens_int,
        temperature=temperature_float,
        stop=stop_tuple,
        tools=tools,
    )


def _extract_content(
    raw: Any,
) -> tuple[str, list[ImageRef]]:
    """Flatten OpenAI's multimodal content shape into (text, images).

    Accepts:
      - str: returned as-is, no images
      - list of {type: 'text', text: ...} | {type: 'image_url',
        image_url: {url}} — concat all text parts, collect images
      - None: empty string, no images
    """
    if raw is None:
        return "", []
    if isinstance(raw, str):
        return raw, []
    if isinstance(raw, list):
        text_parts: list[str] = []
        images: list[ImageRef] = []
        for part in raw:
            if not isinstance(part, dict):
                continue
            ptype = part.get("type", "")
            if ptype == "text":
                t = part.get("text")
                if isinstance(t, str):
                    text_parts.append(t)
            elif ptype == "image_url":
                url_obj = part.get("image_url") or {}
                if isinstance(url_obj, dict):
                    u = url_obj.get("url")
                    if isinstance(u, str) and u:
                        images.append(ImageRef(
                            url=u,
                            detail=url_obj.get("detail"),
                        ))
                elif isinstance(url_obj, str):
                    images.append(ImageRef(url=url_obj))
        return "\n".join(text_parts), images
    return str(raw), []


def completion_response_to_chat(
    resp: CompletionResponse, *, alias: ModelAlias,
) -> dict[str, Any]:
    """Build the OpenAI chat-completion response object from our
    CompletionResponse. Non-streaming path."""
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": alias.alias,
        "system_fingerprint": f"aigenteur-proxy:{alias.real_model}",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": resp.content or "",
                    **(
                        {"reasoning_content": resp.reasoning}
                        if resp.reasoning else {}
                    ),
                },
                "finish_reason": resp.finish_reason or "stop",
            }
        ],
        "usage": {
            "prompt_tokens": resp.input_tokens,
            "completion_tokens": resp.output_tokens,
            "total_tokens": resp.input_tokens + resp.output_tokens,
            **(
                {
                    "prompt_tokens_details": {
                        "cached_tokens": resp.cached_tokens,
                    },
                }
                if resp.cached_tokens else {}
            ),
        },
    }


async def completion_stream_to_chat_sse(
    stream: AsyncIterator[StreamChunk],
    *,
    completion_id: str,
    created_ts: int,
    alias: ModelAlias,
) -> AsyncIterator[bytes]:
    """Convert our StreamChunk iterator into OpenAI SSE shape.

    Per the OpenAI spec each frame is::

        data: {"id":..., "choices":[{"index":0, "delta": {...}}]}\n\n

    Terminated by a single ``data: [DONE]\\n\\n`` line."""
    first = True
    async for chunk in stream:
        delta: dict[str, Any] = {}
        if first:
            delta["role"] = "assistant"
            first = False
        if chunk.delta_content:
            delta["content"] = chunk.delta_content
        if chunk.delta_reasoning:
            # Non-standard OpenAI field, but Anthropic-compat clients
            # + OpenRouter expose this. Safe to include — IDEs that
            # don't know about it ignore unknown delta keys.
            delta["reasoning_content"] = chunk.delta_reasoning

        if not delta and chunk.finish_reason is None:
            continue

        frame = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created_ts,
            "model": alias.alias,
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": chunk.finish_reason,
                }
            ],
        }
        yield f"data: {json.dumps(frame)}\n\n".encode("utf-8")

    yield b"data: [DONE]\n\n"


__all__ = [
    "chat_to_completion_request",
    "completion_response_to_chat",
    "completion_stream_to_chat_sse",
]
