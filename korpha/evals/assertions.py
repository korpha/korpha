"""Deterministic assertion checkers.

Every assertion has a ``kind`` (string) that maps to a checker function
``(response: str, params: dict) -> tuple[bool, str]``. Returns
``(passed, detail)`` — detail is only populated on failure.

Adding a new kind: write the function, register it in ``CHECKERS``.
Document the params in the docstring.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

CheckerResult = tuple[bool, str]
Checker = Callable[[str, dict[str, Any]], CheckerResult]


def _check_contains(response: str, params: dict[str, Any]) -> CheckerResult:
    """``contains``: substring must appear.

    Params:
      value: str (required)
      case_insensitive: bool (default false)
    """
    value = str(params.get("value", ""))
    ci = bool(params.get("case_insensitive", False))
    haystack = response.lower() if ci else response
    needle = value.lower() if ci else value
    if needle and needle in haystack:
        return True, ""
    return False, f"missing substring: {value!r}"


def _check_not_contains(response: str, params: dict[str, Any]) -> CheckerResult:
    """``not_contains``: substring must NOT appear (any of a list)."""
    values = params.get("values")
    if values is None:
        values = [params.get("value", "")]
    ci = bool(params.get("case_insensitive", False))
    haystack = response.lower() if ci else response
    for v in values:
        v = str(v)
        needle = v.lower() if ci else v
        if needle and needle in haystack:
            return False, f"forbidden substring present: {v!r}"
    return True, ""


def _check_contains_any(response: str, params: dict[str, Any]) -> CheckerResult:
    """``contains_any``: at least one of values must appear.

    Useful for delegation tags — accept any of [CTO]/[CMO]/[COO]."""
    values = params.get("values") or []
    ci = bool(params.get("case_insensitive", False))
    haystack = response.lower() if ci else response
    for v in values:
        v = str(v)
        needle = v.lower() if ci else v
        if needle and needle in haystack:
            return True, ""
    return False, f"none of {list(values)} present"


def _check_max_count(response: str, params: dict[str, Any]) -> CheckerResult:
    """``max_count``: substring must appear ≤ ``max`` times.

    Use case: ``{"value": "!", "max": 1}`` — no exclamation spam.
    """
    value = str(params.get("value", ""))
    cap = int(params.get("max", 0))
    if not value:
        return True, ""
    n = response.count(value)
    if n <= cap:
        return True, ""
    return False, f"{value!r} appears {n} times, max allowed {cap}"


def _check_min_count(response: str, params: dict[str, Any]) -> CheckerResult:
    """``min_count``: substring must appear ≥ ``min`` times."""
    value = str(params.get("value", ""))
    floor = int(params.get("min", 0))
    if not value:
        return True, ""
    n = response.count(value)
    if n >= floor:
        return True, ""
    return False, f"{value!r} appears {n} times, min required {floor}"


def _check_min_words(response: str, params: dict[str, Any]) -> CheckerResult:
    """``min_words``: response must have at least ``min`` whitespace tokens."""
    floor = int(params.get("min", 0))
    n = len(response.split())
    if n >= floor:
        return True, ""
    return False, f"only {n} words, min {floor}"


def _check_max_words(response: str, params: dict[str, Any]) -> CheckerResult:
    """``max_words``: response must be at most ``max`` whitespace tokens."""
    cap = int(params.get("max", 0))
    n = len(response.split())
    if n <= cap:
        return True, ""
    return False, f"{n} words, max {cap}"


def _check_regex_match(response: str, params: dict[str, Any]) -> CheckerResult:
    """``regex_match``: pattern must match somewhere in response.

    Params:
      pattern: str (required)
      case_insensitive: bool (default false)
    """
    pattern = str(params.get("pattern", ""))
    ci = bool(params.get("case_insensitive", False))
    flags = re.IGNORECASE if ci else 0
    if not pattern:
        return False, "no pattern supplied"
    try:
        if re.search(pattern, response, flags=flags):
            return True, ""
    except re.error as exc:
        return False, f"invalid regex {pattern!r}: {exc}"
    return False, f"pattern {pattern!r} not found"


def _check_regex_not_match(
    response: str, params: dict[str, Any]
) -> CheckerResult:
    """``regex_not_match``: pattern must NOT match anywhere."""
    matched, detail = _check_regex_match(response, params)
    if matched:
        return False, "forbidden pattern matched"
    if detail.startswith("invalid regex"):
        return False, detail
    return True, ""


def _check_starts_like_recommendation(
    response: str, params: dict[str, Any]
) -> CheckerResult:
    """``starts_like_recommendation``: response must lead with the call,
    not preamble. Heuristic: first non-empty line should be a complete
    statement (period-or-colon-terminated, ≥ ``min_words`` words) and
    must not start with hedging openers like 'Sure,' / 'Of course' /
    'Great question'.

    Params:
      min_words: int (default 5)
    """
    min_words = int(params.get("min_words", 5))
    lines = [line.strip() for line in response.splitlines() if line.strip()]
    if not lines:
        return False, "empty response"
    first = lines[0].lstrip("#*-> 0123456789.")
    forbidden_openers = (
        "sure,", "sure!", "of course", "great question", "happy to",
        "i'd be happy", "let me know", "thanks for", "as an ai",
    )
    lower = first.lower()
    for opener in forbidden_openers:
        if lower.startswith(opener):
            return False, f"hedging opener: {first[:60]!r}"
    if len(first.split()) < min_words:
        return False, f"first line too short: {first[:60]!r}"
    return True, ""


def _check_numbered_or_bulleted_list(
    response: str, params: dict[str, Any]
) -> CheckerResult:
    """``numbered_or_bulleted_list``: response must contain a list of
    between ``min_items`` and ``max_items`` items.

    Counts lines starting with ``1.`` / ``2.`` / ``-`` / ``*`` /
    ``•`` / ``→``. We don't try to parse markdown perfectly — just
    count plausible bullet lines.

    Params:
      min_items: int (default 3)
      max_items: int (optional — no upper bound when missing)
    """
    min_items = int(params.get("min_items", 3))
    max_items = params.get("max_items")
    pattern = re.compile(
        r"^\s*(?:[-*•→]|\d+[.)])\s+\S", flags=re.MULTILINE
    )
    n = len(pattern.findall(response))
    if n < min_items:
        return False, f"only {n} bullet lines, need at least {min_items}"
    if max_items is not None and n > int(max_items):
        return False, f"{n} bullet lines, max allowed {max_items}"
    return True, ""


CHECKERS: dict[str, Checker] = {
    "contains": _check_contains,
    "not_contains": _check_not_contains,
    "contains_any": _check_contains_any,
    "max_count": _check_max_count,
    "min_count": _check_min_count,
    "min_words": _check_min_words,
    "max_words": _check_max_words,
    "regex_match": _check_regex_match,
    "regex_not_match": _check_regex_not_match,
    "starts_like_recommendation": _check_starts_like_recommendation,
    "numbered_or_bulleted_list": _check_numbered_or_bulleted_list,
}


def run_assertion(response: str, kind: str, params: dict[str, Any]) -> CheckerResult:
    """Dispatch to the right checker. Unknown kind = fail with explanation."""
    checker = CHECKERS.get(kind)
    if checker is None:
        return False, f"unknown assertion kind: {kind!r}"
    return checker(response, params)


__all__ = ["CHECKERS", "run_assertion"]
