"""Robust JSON extraction from LLM output.

Models wrap JSON in Markdown fences, prepend reasoning, append commentary,
or do all three at once. This helper accepts that and finds the first
valid object embedded anywhere in the text.
"""
from __future__ import annotations

import json
from typing import Any


def extract_json_dict(text: str) -> dict[str, Any] | None:
    """Best-effort extraction of a JSON object from arbitrary LLM text.

    LLMs commonly produce *almost*-valid JSON: literal newlines inside string
    values, single instead of double quotes, trailing commas, surrounding
    Markdown code fences, leading prose. We tolerate these.

    Order of attempts:
    1. Strip Markdown code fences and parse with strict=False (allows
       control characters like literal newlines/tabs in string values).
    2. Use json.JSONDecoder(strict=False).raw_decode() to find the first
       valid object embedded anywhere.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1] if "\n" in stripped else stripped
        if stripped.endswith("```"):
            stripped = stripped.rsplit("```", 1)[0]
        stripped = stripped.strip()
        if stripped.startswith("json"):
            stripped = stripped[4:].lstrip()
    try:
        result = json.loads(stripped, strict=False)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder(strict=False)
    for idx in range(len(text)):
        if text[idx] != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None
