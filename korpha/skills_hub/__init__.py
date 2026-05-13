"""skills_hub — install + scan skills from external registries.

Two halves:

  - **guard.py**: static security scanner. Regex-based threat-pattern
    matching + invisible-unicode detection + structural checks.
    Verdict: safe / caution / dangerous. Install policy applies trust
    levels to verdicts to decide allow/ask/block.

  - **client.py**: source adapters + install/quarantine flow. Pulls
    from skills.korpha.com (the Korpha hub) plus generic GitHub
    repos. Tracks provenance in a lock file so reinstalls are
    reproducible.

Heavily indebted to the upstream Hermes Agent (MIT, Nous Research) —
``hermes/tools/skills_hub.py`` + ``hermes/tools/skills_guard.py``.
The threat-pattern set, install-policy matrix, and trust-level shape
are direct ports with attribution. Korpha extends with cofounder-
protocol awareness (skill manifests can declare `cofounder_protocol:
true` to opt into Cofounder-Protocol-style install hooks).
"""
from __future__ import annotations

from korpha.skills_hub.client import (
    GitHubSource,
    HubLockFile,
    SkillSource,
    install_skill,
    quarantine_dir,
)
from korpha.skills_hub.guard import (
    INSTALL_POLICY,
    THREAT_PATTERNS,
    TRUSTED_REPOS,
    Finding,
    ScanResult,
    content_hash,
    format_scan_report,
    scan_skill,
    should_allow_install,
)

__all__ = [
    "INSTALL_POLICY",
    "THREAT_PATTERNS",
    "TRUSTED_REPOS",
    "Finding",
    "GitHubSource",
    "HubLockFile",
    "ScanResult",
    "SkillSource",
    "content_hash",
    "format_scan_report",
    "install_skill",
    "quarantine_dir",
    "scan_skill",
    "should_allow_install",
]
