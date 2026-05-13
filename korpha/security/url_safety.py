"""URL safety checks — block requests to private / internal / metadata IPs.

Why: an attacker-controlled prompt or compromised skill output can
trick the agent into fetching internal resources — cloud metadata
endpoints (169.254.169.254), localhost services on the VPS, private
network hosts behind the WireGuard tunnel. SSRF.

Two surfaces:

  - :func:`is_safe_url` — full check. Use as the pre-flight gate
    for arbitrary outbound HTTP from skills, the browser provider,
    fetch helpers. Fails closed on parse / DNS errors.

  - :func:`is_always_blocked_url` — narrow floor. Only checks for
    cloud metadata IPs / hostnames that have no legitimate agent
    use ever, regardless of the lab-mode toggle. Useful for callers
    that need to allow private-IP routing for legitimate reasons
    (a local Chromium sidecar) but still must enforce the floor.

Lab/dev escape hatch:

  Set ``KORPHA_ALLOW_PRIVATE_URLS=1`` to allow private/loopback
  /CGNAT addresses. Cloud metadata IPs are STILL blocked even with
  the toggle on — those are never legitimate. Useful for testing
  against a local server, OpenWrt routers that resolve external
  domains to RFC1918, or VPNs in 100.64/10.

Limits:

  - DNS rebinding (TOCTOU): a hostile DNS server with TTL=0 can
    return a public IP for the pre-flight check, then a private IP
    for the actual connection. Real fix is connection-level
    validation (Champion / Smokescreen-style egress proxy).
  - Redirect bypass: each redirect target needs its own check.
    Ports that use httpx event hooks should re-validate per-redirect.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class UnsafeUrlError(ValueError):
    """Raised when a URL targets a private/blocked address."""


# Hostnames that are ALWAYS blocked, regardless of resolution or
# the lab-mode toggle. Cloud metadata endpoints — the canonical
# SSRF target.
_BLOCKED_HOSTNAMES: frozenset[str] = frozenset({
    "metadata.google.internal",
    "metadata.goog",
})


# IPs / networks that are ALWAYS blocked. Floor.
_ALWAYS_BLOCKED_IPS: frozenset[ipaddress._BaseAddress] = frozenset({
    ipaddress.ip_address("169.254.169.254"),  # AWS / GCP / Azure / DO / Oracle metadata
    ipaddress.ip_address("169.254.170.2"),     # AWS ECS task metadata (task IAM creds)
    ipaddress.ip_address("169.254.169.253"),   # Azure IMDS wire server
    ipaddress.ip_address("fd00:ec2::254"),     # AWS metadata (IPv6)
    ipaddress.ip_address("100.100.100.200"),   # Alibaba Cloud metadata
})
_ALWAYS_BLOCKED_NETWORKS: tuple[ipaddress._BaseNetwork, ...] = (
    ipaddress.ip_network("169.254.0.0/16"),    # All link-local — no legit agent target
)


# 100.64.0.0/10 (CGNAT / RFC 6598) is NOT covered by ipaddress.is_private.
# Tailscale, WireGuard, carrier-grade NAT live here. Block explicitly.
_CGNAT_NETWORK = ipaddress.ip_network("100.64.0.0/10")


# Lab-mode toggle is read once and cached. Re-read by tests via
# :func:`_reset_allow_private_cache`.
_allow_private_resolved: bool = False
_cached_allow_private: bool = False


def _global_allow_private_urls() -> bool:
    """Return True when the operator opted out of private-IP blocking.

    Reads ``KORPHA_ALLOW_PRIVATE_URLS`` env var: ``1``/``true``/
    ``yes`` → allow. Cached for the process lifetime.
    """
    global _allow_private_resolved, _cached_allow_private
    if _allow_private_resolved:
        return _cached_allow_private
    _allow_private_resolved = True
    val = os.getenv("KORPHA_ALLOW_PRIVATE_URLS", "").strip().lower()
    _cached_allow_private = val in ("1", "true", "yes", "on")
    return _cached_allow_private


def _reset_allow_private_cache() -> None:
    """Reset the cache. Tests only."""
    global _allow_private_resolved, _cached_allow_private
    _allow_private_resolved = False
    _cached_allow_private = False


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True for any IP class we never want the agent to reach
    in normal operation. Loopback, private RFC1918, link-local,
    multicast, reserved, unspecified, CGNAT."""
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        return True
    if ip.is_multicast or ip.is_unspecified:
        return True
    if ip in _CGNAT_NETWORK:
        return True
    return False


def _resolve(hostname: str) -> list[ipaddress._BaseAddress]:
    """Resolve hostname to a list of ip_address objects. Empty list
    on DNS failure — caller decides how to handle."""
    try:
        addrinfo = socket.getaddrinfo(
            hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM,
        )
    except (socket.gaierror, UnicodeError):
        return []
    out: list[ipaddress._BaseAddress] = []
    for _family, _, _, _, sockaddr in addrinfo:
        try:
            out.append(ipaddress.ip_address(sockaddr[0]))
        except ValueError:
            continue
    return out


def is_always_blocked_url(url: str) -> bool:
    """Return True iff the URL targets a cloud metadata endpoint.

    This is the security floor — never passes regardless of the
    lab-mode toggle. Use when you have a legitimate reason to relax
    the full ``is_safe_url`` check (e.g. a sidecar at 127.0.0.1) but
    still need to slam the door on metadata exfil.
    """
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").strip().lower().rstrip(".")
        if not hostname:
            return False
        if hostname in _BLOCKED_HOSTNAMES:
            logger.warning(
                "url_safety: blocked metadata hostname %r", hostname,
            )
            return True
        # Literal IP
        try:
            ip = ipaddress.ip_address(hostname)
        except ValueError:
            ip = None
        if ip is not None:
            return _ip_in_floor(ip, hostname)
        # Hostname → resolve, check every answer
        for resolved in _resolve(hostname):
            if _ip_in_floor(resolved, hostname):
                return True
        return False
    except Exception as exc:  # noqa: BLE001
        # Don't claim "always blocked" on unexpected parse errors;
        # let the caller's full check handle malformed URLs.
        logger.debug("url_safety: is_always_blocked_url error %s", exc)
        return False


def _ip_in_floor(ip: ipaddress._BaseAddress, hostname: str) -> bool:
    if ip in _ALWAYS_BLOCKED_IPS or any(
        ip in net for net in _ALWAYS_BLOCKED_NETWORKS
    ):
        logger.warning(
            "url_safety: blocked metadata IP %s (host %r)", ip, hostname,
        )
        return True
    return False


def is_safe_url(url: str) -> bool:
    """Pre-flight gate. Returns False (= refuse) when the URL targets
    a private/internal/metadata address.

    Fails closed: parse errors, DNS errors, unexpected exceptions all
    return False. The agent gets a refusal; legitimate URLs cost one
    DNS lookup. Cache friendly — the OS resolver memoizes upstream.

    With ``KORPHA_ALLOW_PRIVATE_URLS=1`` (lab mode), private IPs
    are allowed except metadata floor. Always-block metadata.
    """
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").strip().lower().rstrip(".")
        if not hostname:
            return False
        if hostname in _BLOCKED_HOSTNAMES:
            logger.warning(
                "url_safety: blocked metadata hostname %r", hostname,
            )
            return False
        allow_private = _global_allow_private_urls()
        # Literal IP
        try:
            ip = ipaddress.ip_address(hostname)
        except ValueError:
            ip = None
        if ip is not None:
            if _ip_in_floor(ip, hostname):
                return False
            if not allow_private and _is_blocked_ip(ip):
                logger.warning(
                    "url_safety: blocked private IP %s", ip,
                )
                return False
            return True
        resolved = _resolve(hostname)
        if not resolved:
            # DNS failure — fail closed. The HTTP client would fail
            # too; blocking loses nothing and prevents weirdness from
            # split-horizon DNS attacks.
            logger.warning(
                "url_safety: blocked, DNS resolution failed for %r",
                hostname,
            )
            return False
        for ip in resolved:
            if _ip_in_floor(ip, hostname):
                return False
            if not allow_private and _is_blocked_ip(ip):
                logger.warning(
                    "url_safety: blocked private resolution %s -> %s",
                    hostname, ip,
                )
                return False
        return True
    except Exception as exc:  # noqa: BLE001
        # Don't let parse weirdness become an SSRF bypass. Fail closed.
        logger.warning(
            "url_safety: blocked, unexpected error for %r: %s", url, exc,
        )
        return False
