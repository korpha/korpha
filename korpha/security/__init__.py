"""Pre-flight safety checks: SSRF guard + OSV malware blocklist.

These run BEFORE the agent makes a potentially-dangerous outbound
call (URL fetch, MCP server spawn). They are not a replacement for
sandboxing / egress-proxy enforcement on production deploys —
they're a cheap, fail-closed first line that blocks the obvious
SSRF + supply-chain attack classes.
"""
from korpha.security.osv_check import check_package_for_malware
from korpha.security.url_safety import (
    UnsafeUrlError,
    is_always_blocked_url,
    is_safe_url,
)

__all__ = [
    "UnsafeUrlError",
    "check_package_for_malware",
    "is_always_blocked_url",
    "is_safe_url",
]
