"""``domain.*`` skills — passive domain reconnaissance, zero API keys.

All data sources are public: crt.sh certificate transparency, public
WHOIS servers, Google DNS-over-HTTPS, system DNS. The reason this
ships as a Python skill instead of staying a knowledge pack is that
it's the foundation under nearly every other research workflow:

  - Niche scoring needs to know if the dot-com is available
  - SEO research needs the competitor's subdomain footprint
  - Outreach targeting needs valid MX records before sending
  - Security review wants the SSL cert posture

Each function is small, sync-friendly (httpx async client wraps the
network paths), and returns plain dicts so the agent can chain them
into other skills without parsing custom types.
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
import ssl
from datetime import datetime, timezone
from typing import Any

import httpx

from korpha.skills.registry import register
from korpha.skills.types import (
    Skill,
    SkillContext,
    SkillError,
    SkillProvenance,
    SkillResult,
    SkillSpec,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- crt.sh


_CRTSH_TIMEOUT = 30.0


class DomainSubdomainsSkill(Skill):
    """Find subdomains via crt.sh certificate transparency."""

    spec = SkillSpec(
        name="domain.subdomains",
        description=(
            "Discover subdomains for a registered domain by querying "
            "crt.sh certificate transparency logs. Picks up internal/"
            "staging/dev/api subdomains that aren't linked from the "
            "main site. Free, no auth. May take 5-30s depending on "
            "how busy the public crt.sh frontend is."
        ),
        parameters={
            "domain": (
                "Apex domain — 'example.com', not 'www.example.com'. "
                "Wildcards are returned as-is so the agent can filter."
            ),
            "exclude_wildcards": (
                "Optional, default true. Drop '*.example.com' entries "
                "since they expand to the apex's whole namespace and "
                "add no signal."
            ),
        },
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        domain = str(args.get("domain") or "").strip().lower()
        if not domain:
            raise SkillError("domain.subdomains: domain required")
        exclude_wildcards = bool(args.get("exclude_wildcards", True))

        try:
            async with httpx.AsyncClient(timeout=_CRTSH_TIMEOUT) as client:
                resp = await client.get(
                    "https://crt.sh/",
                    params={"q": f"%.{domain}", "output": "json"},
                )
        except httpx.HTTPError as exc:
            raise SkillError(
                f"crt.sh transport: {type(exc).__name__}: {exc}"
            ) from exc
        if resp.status_code != 200:
            raise SkillError(f"crt.sh returned HTTP {resp.status_code}")
        try:
            entries = resp.json()
        except json.JSONDecodeError as exc:
            raise SkillError(f"crt.sh non-JSON: {exc}") from exc

        names: set[str] = set()
        for e in entries or []:
            name_value = e.get("name_value", "")
            if not isinstance(name_value, str):
                continue
            # Some entries pack multiple SAN names newline-separated.
            for line in name_value.splitlines():
                name = line.strip().lower()
                if not name:
                    continue
                if exclude_wildcards and name.startswith("*."):
                    continue
                # Filter to subdomains of the requested apex.
                if name == domain or name.endswith(f".{domain}"):
                    names.add(name)
        sorted_names = sorted(names)
        return SkillResult(
            skill_name="domain.subdomains",
            summary=(
                f"Found {len(sorted_names)} subdomain(s) under {domain}"
            ),
            payload={
                "domain": domain,
                "count": len(sorted_names),
                "subdomains": sorted_names,
            },
        )


# ---------------------------------------------------------------- ssl


class DomainSslCertSkill(Skill):
    """Live SSL/TLS certificate inspection via a real TLS handshake."""

    spec = SkillSpec(
        name="domain.ssl_cert",
        description=(
            "Connect to a host on port 443 (or custom) and read the "
            "presented TLS certificate: subject, issuer, SANs, expiry, "
            "days_until_expiry, TLS version, cipher. Use to verify "
            "cert deployment before pointing a domain at a fresh VPS."
        ),
        parameters={
            "host": "Hostname (e.g. 'example.com').",
            "port": "Optional, default 443.",
        },
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        host = str(args.get("host") or "").strip().lower()
        if not host:
            raise SkillError("domain.ssl_cert: host required")
        port = int(args.get("port") or 443)

        def _fetch() -> dict[str, Any]:
            context = ssl.create_default_context()
            with socket.create_connection((host, port), timeout=10) as sock:
                with context.wrap_socket(sock, server_hostname=host) as ssock:
                    cert = ssock.getpeercert()
                    return {
                        "cert": cert,
                        "tls_version": ssock.version(),
                        "cipher": ssock.cipher(),
                    }
        try:
            blob = await asyncio.get_running_loop().run_in_executor(
                None, _fetch,
            )
        except (socket.gaierror, OSError, ssl.SSLError) as exc:
            raise SkillError(
                f"TLS connect failed: {type(exc).__name__}: {exc}"
            ) from exc

        cert = blob["cert"] or {}
        sans = [
            v for k, v in (cert.get("subjectAltName") or [])
            if k.lower() == "dns"
        ]
        # Issuer / subject come as nested tuples; flatten to dict.
        def _flatten(seq: Any) -> dict[str, str]:
            out: dict[str, str] = {}
            if not seq:
                return out
            for rdn in seq:
                for k, v in rdn:
                    out[k] = v
            return out

        not_after = cert.get("notAfter")
        days_until_expiry: int | None = None
        if not_after:
            try:
                exp = datetime.strptime(
                    not_after, "%b %d %H:%M:%S %Y %Z",
                ).replace(tzinfo=timezone.utc)
                days_until_expiry = (
                    exp - datetime.now(tz=timezone.utc)
                ).days
            except ValueError:
                pass

        return SkillResult(
            skill_name="domain.ssl_cert",
            summary=(
                f"{host}:{port} TLS cert valid until {not_after or '?'}"
                + (f" ({days_until_expiry}d)"
                   if days_until_expiry is not None else "")
            ),
            payload={
                "host": host, "port": port,
                "subject": _flatten(cert.get("subject")),
                "issuer": _flatten(cert.get("issuer")),
                "subject_alt_names": sans,
                "not_before": cert.get("notBefore"),
                "not_after": not_after,
                "days_until_expiry": days_until_expiry,
                "tls_version": blob["tls_version"],
                "cipher": blob["cipher"],
                "serial_number": cert.get("serialNumber"),
            },
        )


# ---------------------------------------------------------------- dns


_DOH_URL = "https://dns.google/resolve"


class DomainDnsRecordsSkill(Skill):
    """Fetch DNS records via Google DoH (no resolver config needed)."""

    spec = SkillSpec(
        name="domain.dns_records",
        description=(
            "Fetch DNS records for a domain via Google DNS-over-HTTPS. "
            "Defaults to A + AAAA + MX + NS + TXT + CNAME. Useful for "
            "MX hygiene checks (before sending email), nameserver "
            "discovery, SPF/DKIM existence."
        ),
        parameters={
            "domain": "Hostname to query.",
            "types": (
                "Optional. List of record types like ['A', 'MX']. "
                "Default: all six common types."
            ),
        },
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        domain = str(args.get("domain") or "").strip().lower()
        if not domain:
            raise SkillError("domain.dns_records: domain required")
        types = args.get("types") or ["A", "AAAA", "MX", "NS", "TXT", "CNAME"]
        if not isinstance(types, list):
            raise SkillError("domain.dns_records: types must be a list")

        async def _query(rtype: str) -> tuple[str, list[str]]:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(
                        _DOH_URL,
                        params={"name": domain, "type": rtype},
                        headers={"accept": "application/dns-json"},
                    )
            except httpx.HTTPError:
                return rtype, []
            if resp.status_code != 200:
                return rtype, []
            data = resp.json() or {}
            answers = data.get("Answer", []) or []
            return rtype, [
                str(a.get("data", "")) for a in answers
                if a.get("data")
            ]

        results = await asyncio.gather(
            *[_query(rt) for rt in types],
        )
        records = {rt: vals for rt, vals in results}
        total = sum(len(v) for v in records.values())
        return SkillResult(
            skill_name="domain.dns_records",
            summary=f"{total} record(s) across {len(types)} type(s)",
            payload={"domain": domain, "records": records},
        )


# ---------------------------------------------------------------- whois


# TLD → WHOIS server. Covers the ones AIgenteur users care about; for
# obscure TLDs we fall back to whois.iana.org which redirects.
_WHOIS_SERVERS: dict[str, str] = {
    "com": "whois.verisign-grs.com",
    "net": "whois.verisign-grs.com",
    "org": "whois.publicinterestregistry.org",
    "io": "whois.nic.io",
    "ai": "whois.nic.ai",
    "co": "whois.nic.co",
    "dev": "whois.nic.google",
    "app": "whois.nic.google",
    "me": "whois.nic.me",
    "us": "whois.nic.us",
    "uk": "whois.nic.uk",
    "ca": "whois.cira.ca",
    "biz": "whois.nic.biz",
    "info": "whois.nic.info",
    "xyz": "whois.nic.xyz",
    "store": "whois.nic.store",
    "shop": "whois.nic.shop",
    "online": "whois.nic.online",
    "site": "whois.nic.site",
}
_WHOIS_FALLBACK = "whois.iana.org"
_WHOIS_TIMEOUT = 8.0


def _whois_server_for(domain: str) -> str:
    tld = domain.rsplit(".", 1)[-1].lower() if "." in domain else ""
    return _WHOIS_SERVERS.get(tld, _WHOIS_FALLBACK)


class DomainWhoisSkill(Skill):
    """Raw WHOIS query via direct TCP — no API key, supports 100+ TLDs."""

    spec = SkillSpec(
        name="domain.whois",
        description=(
            "Query WHOIS for a domain via direct TCP on port 43. "
            "Returns raw text plus parsed fields (registrar, "
            "creation_date, expiry_date, registrant_country). Use to "
            "estimate domain age + check expiry."
        ),
        parameters={
            "domain": "Apex domain (e.g. 'example.com').",
        },
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        domain = str(args.get("domain") or "").strip().lower()
        if not domain:
            raise SkillError("domain.whois: domain required")
        server = _whois_server_for(domain)

        def _query(server_host: str) -> str:
            with socket.create_connection(
                (server_host, 43), timeout=_WHOIS_TIMEOUT,
            ) as sock:
                sock.sendall(f"{domain}\r\n".encode("ascii"))
                chunks: list[bytes] = []
                while True:
                    data = sock.recv(4096)
                    if not data:
                        break
                    chunks.append(data)
                return b"".join(chunks).decode("utf-8", errors="replace")

        try:
            raw = await asyncio.get_running_loop().run_in_executor(
                None, _query, server,
            )
        except OSError as exc:
            raise SkillError(
                f"WHOIS connect to {server}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        # IANA's response redirects to the authoritative server.
        if server == _WHOIS_FALLBACK:
            for line in raw.splitlines():
                if line.lower().lstrip().startswith("refer:"):
                    referred = line.split(":", 1)[1].strip()
                    try:
                        raw = await asyncio.get_running_loop().run_in_executor(
                            None, _query, referred,
                        )
                        server = referred
                        break
                    except OSError:
                        pass

        parsed: dict[str, str] = {}
        for line in raw.splitlines():
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            key = key.strip().lower()
            val = val.strip()
            if not val or val.startswith(">>>"):
                continue
            if key in (
                "registrar", "registrant country",
                "creation date", "created", "registered",
                "expiry date", "expiration date", "registry expiry date",
                "updated date", "last modified",
                "domain name", "name server",
            ):
                # Keep first occurrence — WHOIS often repeats fields.
                parsed.setdefault(key, val)

        return SkillResult(
            skill_name="domain.whois",
            summary=(
                f"WHOIS {domain} via {server}: "
                f"{parsed.get('registrar', 'unknown registrar')}"
            ),
            payload={
                "domain": domain,
                "server": server,
                "parsed": parsed,
                "raw": raw[:4000],
            },
        )


# ---------------------------------------------------------------- availability


class DomainCheckAvailabilitySkill(Skill):
    """Combine DNS + WHOIS signals to estimate if a domain is taken."""

    spec = SkillSpec(
        name="domain.check_availability",
        description=(
            "Estimate whether a domain is registered. Combines: "
            "(1) DNS A/NS lookup, (2) WHOIS for creation date. "
            "Returns is_available + confidence + the signals used. "
            "Not a registrar — for actual purchase check the "
            "registrar's API."
        ),
        parameters={
            "domain": "Apex domain to check.",
        },
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        domain = str(args.get("domain") or "").strip().lower()
        if not domain:
            raise SkillError(
                "domain.check_availability: domain required"
            )

        # DNS NS records: if present, domain is delegated → almost
        # certainly registered.
        async def _has_ns() -> bool:
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    resp = await client.get(
                        _DOH_URL,
                        params={"name": domain, "type": "NS"},
                        headers={"accept": "application/dns-json"},
                    )
                if resp.status_code != 200:
                    return False
                data = resp.json() or {}
                return bool(data.get("Answer"))
            except httpx.HTTPError:
                return False

        has_ns = await _has_ns()

        # WHOIS read — but skip if NS check is conclusive (faster).
        whois_says_registered = False
        creation_date: str | None = None
        if has_ns:
            whois_says_registered = True
        else:
            try:
                whois_result = await DomainWhoisSkill().run(
                    ctx=ctx, args={"domain": domain},
                )
                parsed = whois_result.payload.get("parsed", {})
                creation_date = (
                    parsed.get("creation date")
                    or parsed.get("created")
                    or parsed.get("registered")
                )
                whois_says_registered = bool(
                    creation_date or parsed.get("registrar"),
                )
            except SkillError:
                pass

        is_available = not (has_ns or whois_says_registered)
        confidence = "high" if has_ns else ("medium" if whois_says_registered else "low")
        return SkillResult(
            skill_name="domain.check_availability",
            summary=(
                f"{domain} appears "
                f"{'AVAILABLE' if is_available else 'TAKEN'} "
                f"(confidence: {confidence})"
            ),
            payload={
                "domain": domain,
                "is_available": is_available,
                "confidence": confidence,
                "has_ns_records": has_ns,
                "whois_says_registered": whois_says_registered,
                "creation_date": creation_date,
            },
        )


# ---------------------------------------------------------------- bulk


class DomainBulkCheckSkill(Skill):
    """Run check_availability over many domains in parallel."""

    spec = SkillSpec(
        name="domain.bulk_check",
        description=(
            "Check availability for up to 20 domains in parallel. "
            "Returns a per-domain status. Use during niche scoring "
            "to filter brand-name candidates before deep research."
        ),
        parameters={
            "domains": "List of apex domains (max 20).",
        },
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        raw = args.get("domains") or []
        if not isinstance(raw, list):
            raise SkillError(
                "domain.bulk_check: domains must be a list"
            )
        domains = [
            str(d).strip().lower() for d in raw if str(d).strip()
        ][:20]
        if not domains:
            raise SkillError(
                "domain.bulk_check: at least one domain required"
            )
        checker = DomainCheckAvailabilitySkill()

        async def _one(d: str) -> dict[str, Any]:
            try:
                r = await checker.run(ctx=ctx, args={"domain": d})
                return {
                    "domain": d,
                    "is_available": r.payload.get("is_available"),
                    "confidence": r.payload.get("confidence"),
                }
            except SkillError as exc:
                return {
                    "domain": d,
                    "is_available": None,
                    "confidence": "error",
                    "error": str(exc),
                }

        results = await asyncio.gather(*[_one(d) for d in domains])
        n_available = sum(1 for r in results if r.get("is_available") is True)
        return SkillResult(
            skill_name="domain.bulk_check",
            summary=(
                f"{n_available} of {len(domains)} appear available"
            ),
            payload={"results": results},
        )


def register_skills() -> None:
    register(DomainSubdomainsSkill())
    register(DomainSslCertSkill())
    register(DomainDnsRecordsSkill())
    register(DomainWhoisSkill())
    register(DomainCheckAvailabilitySkill())
    register(DomainBulkCheckSkill())


__all__ = [
    "DomainBulkCheckSkill",
    "DomainCheckAvailabilitySkill",
    "DomainDnsRecordsSkill",
    "DomainSslCertSkill",
    "DomainSubdomainsSkill",
    "DomainWhoisSkill",
    "register_skills",
]
