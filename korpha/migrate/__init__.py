"""Machine-migration tooling — bundle/restore Korpha state across hosts.

Built on top of the existing ``korpha backup`` / ``korpha restore``
(which already tar's the data dir), adding the *migration* mindset:

  - **Manifest** — source machine metadata, korpha version, list of
    creds that need re-auth on the target, pending state (cron jobs,
    background tasks, active business id).
  - **Pre-flight readiness check** — does the target have Python
    3.12+, enough disk space, network reachability? Friendly fail
    BEFORE the operator commits to migration.
  - **Re-auth wizard** — after restore, walk the operator through
    re-logging into Codex CLI / Claude Code / etc. (creds tied to
    machine identity that can't be transferred).
  - **One-shot ``migrate to user@host``** — SSHes the bundle to the
    target, runs install + restore. Wraps the manual flow.

Universal fallback remains the existing ``korpha backup`` tarball +
``korpha restore`` — works without SSH, without manifest, on any
target the operator can copy a file to.

Design principle: AIgenteur is "your AI cofounder, your stack,
your data". Moving between machines must be a 30-min routine, not
a rebuild project.
"""
from korpha.migrate.bundle import (
    BundleResult, create_migration_bundle,
)
from korpha.migrate.cred_audit import (
    MACHINE_TIED_CREDS, MachineTiedCred, scan_machine_tied,
)
from korpha.migrate.manifest import (
    MIGRATION_MANIFEST_FILENAME, Manifest, build_manifest, load_manifest,
)
from korpha.migrate.readiness import (
    Check, CheckLevel, has_blocking_failures, run_readiness_checks,
)
from korpha.migrate.restore import (
    ReauthStep, RestoreResult, format_source_banner,
    reauth_steps_from_manifest, restore_bundle,
)

__all__ = [
    "MACHINE_TIED_CREDS",
    "MIGRATION_MANIFEST_FILENAME",
    "BundleResult",
    "Check",
    "CheckLevel",
    "MachineTiedCred",
    "Manifest",
    "ReauthStep",
    "RestoreResult",
    "build_manifest",
    "create_migration_bundle",
    "format_source_banner",
    "has_blocking_failures",
    "load_manifest",
    "reauth_steps_from_manifest",
    "restore_bundle",
    "run_readiness_checks",
    "scan_machine_tied",
]
