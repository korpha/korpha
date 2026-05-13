"""Tests for the SSRF guard + OSV malware check.

URL-safety tests stub ``socket.getaddrinfo`` to inject the resolution
result so we can drive the address-class check without needing a
real DNS roundtrip. OSV tests stub ``urllib.request.urlopen``.

We don't test against real OSV / real DNS — the goal is to verify
the policy gate, not the dependencies.
"""
from __future__ import annotations

import json
import socket
from typing import Any

import pytest

from korpha.security import (
    check_package_for_malware,
    is_always_blocked_url,
    is_safe_url,
)
from korpha.security import url_safety


# ---- Fixtures ----


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    """The lab-mode toggle is process-cached; reset between tests so
    KORPHA_ALLOW_PRIVATE_URLS in one test doesn't bleed into the
    next."""
    url_safety._reset_allow_private_cache()
    yield
    url_safety._reset_allow_private_cache()


def _patch_resolution(monkeypatch: pytest.MonkeyPatch, ip: str) -> None:
    """Make socket.getaddrinfo return ``ip`` for any hostname."""
    def fake(*_a: Any, **_k: Any) -> list:
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        return [(family, 0, 0, "", (ip, 0))]
    monkeypatch.setattr(socket, "getaddrinfo", fake)


def _patch_resolution_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(*_a: Any, **_k: Any) -> list:
        raise socket.gaierror("no such host")
    monkeypatch.setattr(socket, "getaddrinfo", fake)


# ---- is_safe_url: literal IP addresses ----


def test_safe_url_blocks_aws_metadata_literal() -> None:
    assert is_safe_url("http://169.254.169.254/latest/meta-data/") is False


def test_safe_url_blocks_azure_metadata_literal() -> None:
    assert is_safe_url("http://169.254.169.253/wireserver") is False


def test_safe_url_blocks_alibaba_metadata_literal() -> None:
    assert is_safe_url("http://100.100.100.200/") is False


def test_safe_url_blocks_loopback_literal() -> None:
    assert is_safe_url("http://127.0.0.1:8080/") is False
    assert is_safe_url("http://[::1]/") is False


def test_safe_url_blocks_rfc1918_literal() -> None:
    assert is_safe_url("http://10.0.0.5/") is False
    assert is_safe_url("http://192.168.1.1/") is False
    assert is_safe_url("http://172.16.0.1/") is False


def test_safe_url_blocks_cgnat_literal() -> None:
    """100.64/10 is not is_private — must be blocked explicitly."""
    assert is_safe_url("http://100.64.0.1/") is False
    assert is_safe_url("http://100.127.255.1/") is False


def test_safe_url_blocks_link_local_literal() -> None:
    """Entire 169.254/16 is the always-blocked floor."""
    assert is_safe_url("http://169.254.5.5/") is False


def test_safe_url_allows_public_literal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A public IP literal that's not in any blocklist passes."""
    # 8.8.8.8 is public; we don't even need to mock since it parses
    # to a public IP and there's no DNS lookup for literal IPs.
    assert is_safe_url("https://8.8.8.8/") is True


# ---- is_safe_url: hostname resolution ----


def test_safe_url_blocks_when_hostname_resolves_to_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_resolution(monkeypatch, "169.254.169.254")
    assert is_safe_url("https://attacker.example/") is False


def test_safe_url_blocks_when_hostname_resolves_to_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_resolution(monkeypatch, "127.0.0.1")
    assert is_safe_url("https://attacker.example/") is False


def test_safe_url_blocks_when_hostname_resolves_to_rfc1918(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_resolution(monkeypatch, "10.0.0.5")
    assert is_safe_url("https://internal-tool.example/") is False


def test_safe_url_allows_when_hostname_resolves_to_public(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_resolution(monkeypatch, "151.101.1.140")
    assert is_safe_url("https://stackoverflow.com/") is True


def test_safe_url_blocks_metadata_hostname_literal() -> None:
    """metadata.google.internal is hardcoded — never reaches DNS."""
    assert is_safe_url("http://metadata.google.internal/") is False
    assert is_safe_url("http://metadata.goog/") is False


def test_safe_url_metadata_hostname_blocked_even_with_lab_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Floor — never bypassable."""
    monkeypatch.setenv("KORPHA_ALLOW_PRIVATE_URLS", "1")
    url_safety._reset_allow_private_cache()
    assert is_safe_url("http://metadata.google.internal/") is False


def test_safe_url_metadata_ip_blocked_even_with_lab_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_ALLOW_PRIVATE_URLS", "1")
    url_safety._reset_allow_private_cache()
    assert is_safe_url("http://169.254.169.254/") is False


# ---- is_safe_url: lab-mode toggle ----


def test_lab_mode_allows_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With KORPHA_ALLOW_PRIVATE_URLS=1, loopback / RFC1918 pass."""
    monkeypatch.setenv("KORPHA_ALLOW_PRIVATE_URLS", "1")
    url_safety._reset_allow_private_cache()
    assert is_safe_url("http://127.0.0.1:8080/") is True
    assert is_safe_url("http://10.0.0.5/") is True


def test_lab_mode_off_by_default() -> None:
    """No env var → blocking is on."""
    assert is_safe_url("http://127.0.0.1/") is False


def test_lab_mode_explicit_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_ALLOW_PRIVATE_URLS", "0")
    url_safety._reset_allow_private_cache()
    assert is_safe_url("http://127.0.0.1/") is False


# ---- is_safe_url: edge cases / failure modes ----


def test_safe_url_returns_false_for_empty_string() -> None:
    assert is_safe_url("") is False


def test_safe_url_returns_false_for_garbage_url() -> None:
    """Parse failures should fail closed, not bypass."""
    assert is_safe_url("not a url at all") is False


def test_safe_url_returns_false_when_dns_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DNS failure → fail closed. The HTTP client would fail anyway,
    so blocking loses nothing and prevents split-horizon weirdness."""
    _patch_resolution_fails(monkeypatch)
    assert is_safe_url("https://nonexistent.example/") is False


def test_safe_url_handles_url_without_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``urlparse('foo.com/bar')`` puts everything in path; hostname
    is None. Must not crash, must fail closed."""
    assert is_safe_url("example.com/path") is False


def test_safe_url_handles_userinfo_in_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-RFC URLs like http://user:pass@host/ — ``urlparse`` strips
    the userinfo, hostname is correct. Just verify we don't get
    confused and let the user@ part bypass the check."""
    _patch_resolution(monkeypatch, "169.254.169.254")
    assert is_safe_url("http://user:pass@attacker.example/") is False


# ---- is_always_blocked_url: floor only ----


def test_always_blocked_returns_true_for_metadata_hostname() -> None:
    assert is_always_blocked_url("http://metadata.google.internal/") is True


def test_always_blocked_returns_true_for_metadata_ip() -> None:
    assert is_always_blocked_url("http://169.254.169.254/") is True


def test_always_blocked_returns_false_for_loopback() -> None:
    """Floor is narrower than full check — loopback is NOT in the
    always-blocked floor (callers may legitimately want a sidecar
    at 127.0.0.1)."""
    assert is_always_blocked_url("http://127.0.0.1/") is False


def test_always_blocked_returns_false_for_public_url() -> None:
    assert is_always_blocked_url("https://example.com/") is False


def test_always_blocked_resolves_then_checks_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a hostname resolves to a metadata IP, floor still catches it."""
    _patch_resolution(monkeypatch, "169.254.169.254")
    assert is_always_blocked_url("https://attacker.example/") is True


def test_always_blocked_returns_false_when_dns_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Floor doesn't claim to know — caller's full check handles
    this."""
    _patch_resolution_fails(monkeypatch)
    assert is_always_blocked_url("https://nonexistent.example/") is False


# ---- OSV malware check ----


def _osv_response(*ids: str) -> bytes:
    """Build an OSV API response payload with the given vuln IDs."""
    return json.dumps({
        "vulns": [
            {"id": v, "summary": f"summary for {v}"} for v in ids
        ],
    }).encode("utf-8")


def _patch_osv(
    monkeypatch: pytest.MonkeyPatch, payload: bytes,
) -> None:
    class _FakeResp:
        def __init__(self, data: bytes) -> None:
            self._data = data
        def __enter__(self) -> "_FakeResp":
            return self
        def __exit__(self, *_: Any) -> None:
            return None
        def read(self) -> bytes:
            return self._data
    import urllib.request as ur
    monkeypatch.setattr(
        ur, "urlopen", lambda *_a, **_k: _FakeResp(payload),
    )


def test_osv_returns_none_for_unknown_command() -> None:
    """If we don't recognize the installer command, skip — don't
    waste a network round trip."""
    assert check_package_for_malware("docker", ["run", "foo"]) is None
    assert check_package_for_malware("ls", []) is None


def test_osv_returns_none_when_args_empty() -> None:
    assert check_package_for_malware("npx", []) is None


def test_osv_blocks_npm_package_with_malware_advisory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_osv(monkeypatch, _osv_response("MAL-2024-1234"))
    msg = check_package_for_malware(
        "npx", ["-y", "@evil/typo-squat-server"],
    )
    assert msg is not None
    assert "BLOCKED" in msg
    assert "MAL-2024-1234" in msg
    assert "@evil/typo-squat-server" in msg


def test_osv_skips_flag_args_to_find_package_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``npx -y --quiet @scope/pkg`` should still query for @scope/pkg."""
    _patch_osv(monkeypatch, _osv_response())  # no malware → allow
    assert check_package_for_malware(
        "npx", ["-y", "--quiet", "@modelcontextprotocol/server-fs"],
    ) is None


def test_osv_ignores_non_malware_advisories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regular CVEs need human triage — only ``MAL-*`` blocks."""
    _patch_osv(monkeypatch, _osv_response("CVE-2024-9999"))
    assert check_package_for_malware("npx", ["some-pkg"]) is None


def test_osv_fails_open_on_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OSV unreachable → allow. Asymmetric tradeoff — blocking on
    network errors would prevent every install when the box is
    air-gapped or OSV is having a bad day."""
    import urllib.request as ur

    def boom(*_a: Any, **_k: Any) -> Any:
        raise OSError("connection refused")

    monkeypatch.setattr(ur, "urlopen", boom)
    assert check_package_for_malware("npx", ["whatever"]) is None


def test_osv_parses_npm_scoped_with_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_osv(monkeypatch, _osv_response())
    # Should not raise; should call OSV with name=@scope/pkg, version=1.2.3
    captured: dict = {}
    import urllib.request as ur
    real_urlopen = ur.urlopen

    def capture(req: Any, *a: Any, **k: Any) -> Any:
        captured["body"] = req.data
        # Return empty
        class _R:
            def __enter__(self): return self
            def __exit__(self, *a): return None
            def read(self): return b'{"vulns":[]}'
        return _R()

    monkeypatch.setattr(ur, "urlopen", capture)
    check_package_for_malware("npx", ["-y", "@scope/pkg@1.2.3"])
    assert b"@scope/pkg" in captured["body"]
    assert b"1.2.3" in captured["body"]


def test_osv_parses_pypi_with_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}
    import urllib.request as ur

    def capture(req: Any, *a: Any, **k: Any) -> Any:
        captured["body"] = req.data
        class _R:
            def __enter__(self): return self
            def __exit__(self, *a): return None
            def read(self): return b'{"vulns":[]}'
        return _R()

    monkeypatch.setattr(ur, "urlopen", capture)
    check_package_for_malware("uvx", ["--from", "some-tool==2.0.0"])
    # uvx will pick `some-tool==2.0.0` (skipping --from since it
    # starts with -). Argument-skipping logic strips flag tokens
    # and the second positional becomes the package.
    assert captured["body"] is not None


# ---- MCP launcher integration ----


@pytest.mark.asyncio
async def test_mcp_spawn_refuses_known_malware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The OSV gate fires before subprocess spawn — verify a malware
    hit raises McpClientError instead of launching the package."""
    from korpha.mcp.client import McpClientError, StdioMcpClient

    monkeypatch.setattr(
        "korpha.security.check_package_for_malware",
        lambda cmd, args: "BLOCKED: package 'evil' has malware",
    )
    client = StdioMcpClient(command=["npx", "-y", "evil"])
    with pytest.raises(McpClientError, match="BLOCKED"):
        await client._spawn()


@pytest.mark.asyncio
async def test_mcp_spawn_passes_through_clean_packages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No malware hit → spawn proceeds normally (and crashes since
    the binary doesn't exist, but with FileNotFoundError-derived
    error, NOT the malware string)."""
    from korpha.mcp.client import McpClientError, StdioMcpClient

    monkeypatch.setattr(
        "korpha.security.check_package_for_malware",
        lambda cmd, args: None,
    )
    client = StdioMcpClient(command=["definitely-not-installed-binary"])
    with pytest.raises(McpClientError, match="not found"):
        await client._spawn()
