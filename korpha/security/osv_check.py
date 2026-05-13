"""OSV malware check for MCP packages.

Before launching an MCP server via ``npx`` / ``uvx`` / ``pipx``,
hit OSV (Open Source Vulnerabilities) for ``MAL-*`` advisories
on the package. Regular CVEs are ignored on purpose — those need
human triage. Only confirmed malware is blocked.

OSV is Google-run, free, public; typical latency ~300ms. Fail-open:
network errors allow the package to proceed. The wins from OSV are
asymmetric — it catches typo-squat / supply-chain attacks that the
agent would otherwise blindly install on Mike's machine because
ChatGPT recommended it. Missing one because OSV is down doesn't
make the situation worse than it was without the check.

Adapted from Hermes' ``tools/osv_check.py`` which credits Block/Goose
for the original idea.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)


_OSV_ENDPOINT = os.getenv("OSV_ENDPOINT", "https://api.osv.dev/v1/query")
_TIMEOUT_SECONDS = 10
_USER_AGENT = "korpha-osv-check/1.0"


def check_package_for_malware(
    command: str, args: list[str],
) -> str | None:
    """Return an error string if the package has known malware, else None.

    Inspects the ``command`` (``npx`` / ``uvx`` / ``pipx``) plus
    ``args`` to extract the package name + ecosystem. Queries OSV.
    Returns None on:
      - Unknown / non-installer commands
      - Unparseable args
      - OSV network / parse errors (fail-open)
      - No malware advisories
    """
    ecosystem = _infer_ecosystem(command)
    if not ecosystem:
        return None

    package, version = _parse_package_from_args(args, ecosystem)
    if not package:
        return None

    try:
        malware = _query_osv(package, ecosystem, version)
    except Exception as exc:  # noqa: BLE001
        # Network / timeout / parse failure → allow. Logged at debug
        # since this is the steady-state path when OSV is unreachable
        # (e.g. air-gapped install) — don't spam warnings.
        logger.debug(
            "osv_check: query failed for %s/%s (allowing): %s",
            ecosystem, package, exc,
        )
        return None

    if not malware:
        return None

    ids = ", ".join(m.get("id", "?") for m in malware[:3])
    summaries = "; ".join(
        (m.get("summary") or m.get("id") or "?")[:120]
        for m in malware[:3]
    )
    return (
        f"BLOCKED: package {package!r} ({ecosystem}) has known "
        f"malware advisories: {ids}. Details: {summaries}"
    )


def _infer_ecosystem(command: str) -> str | None:
    base = os.path.basename(command).lower()
    if base in ("npx", "npx.cmd"):
        return "npm"
    if base in ("uvx", "uvx.cmd", "pipx", "pipx.cmd"):
        return "PyPI"
    return None


def _parse_package_from_args(
    args: list[str], ecosystem: str,
) -> tuple[str | None, str | None]:
    if not args:
        return None, None
    package_token: str | None = None
    for arg in args:
        if not isinstance(arg, str):
            continue
        if arg.startswith("-"):
            continue
        package_token = arg
        break
    if not package_token:
        return None, None
    if ecosystem == "npm":
        return _parse_npm_package(package_token)
    if ecosystem == "PyPI":
        return _parse_pypi_package(package_token)
    return package_token, None


def _parse_npm_package(token: str) -> tuple[str | None, str | None]:
    """``@scope/name@version`` or ``name@version``."""
    if token.startswith("@"):
        match = re.match(r"^(@[^/]+/[^@]+)(?:@(.+))?$", token)
        if match:
            return match.group(1), match.group(2)
        return token, None
    if "@" in token:
        name, _, version = token.rpartition("@")
        if version == "latest":
            version = ""
        return name, (version or None)
    return token, None


def _parse_pypi_package(token: str) -> tuple[str | None, str | None]:
    """``name==version`` or ``name[extras]==version``."""
    match = re.match(
        r"^([a-zA-Z0-9._-]+)(?:\[[^\]]*\])?(?:==(.+))?$", token,
    )
    if match:
        return match.group(1), match.group(2)
    return token, None


def _query_osv(
    package: str, ecosystem: str, version: str | None = None,
) -> list[dict]:
    payload: dict = {"package": {"name": package, "ecosystem": ecosystem}}
    if version:
        payload["version"] = version
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        _OSV_ENDPOINT,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
        result = json.loads(resp.read())
    vulns = result.get("vulns", [])
    return [v for v in vulns if str(v.get("id", "")).startswith("MAL-")]
