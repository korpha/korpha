"""Cofounder Protocol — third-party services register as Korpha-native.

The protocol lets a service (Stripe, Vercel, ConvertKit, Beehiiv,
RankMyAnswer, etc.) ship a single ``cofounder.yaml`` manifest that
declares: which skills it brings, how the user links their account,
where the docs are, and what branding it owns. Korpha fetches the
manifest, validates it, and registers the partner.

Why this exists: Korpha is not an integrator. It's a **standard**
solopreneur services target. A SaaS that publishes a working manifest
is one ``korpha cofounder install <url>`` away from being part of
every Founder's cofounder loop. That's the moat.

Spec: docs/COFOUNDER_PROTOCOL.md
"""
from __future__ import annotations

from korpha.protocol.installer import (
    InstalledManifest,
    install_manifest,
    list_installed,
    uninstall_manifest,
)
from korpha.protocol.manifest import (
    AuthSpec,
    BrandingSpec,
    CofounderManifest,
    ManifestError,
    ProvidesSpec,
    RequiresSpec,
    load_manifest,
    parse_manifest,
)

__all__ = [
    "AuthSpec",
    "BrandingSpec",
    "CofounderManifest",
    "InstalledManifest",
    "ManifestError",
    "ProvidesSpec",
    "RequiresSpec",
    "install_manifest",
    "list_installed",
    "load_manifest",
    "parse_manifest",
    "uninstall_manifest",
]
