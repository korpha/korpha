"""External-service credential resolution scoped to BusinessUnits.

Unifies API-key storage for LLM providers AND non-LLM services
(Stripe, Resend, Printful, KDP, Etsy, JVZoo, Cloudflare, …) under
one ``ExternalServiceAccount`` model. Resolver walks the BusinessUnit
tree from leaf to root, returning the most-specific-active-uncapped
account for the requested service.

Deployment-mode + tier-aware: SaaS mode bypasses OAuth-CLI shared
resources entirely (impossible to share OAuth tokens across tenants);
local mode prefers OAuth CLIs for Pro tier work and per-unit API keys
for Workhorse tier work (subscription quotas are precious; bulk work
goes through cheap API keys).

PR4 ships the model + resolver + credentials.set skill. The shared
``ProviderAccount`` from ``korpha.inference.registry`` stays in
place for LLM inference routing; this module is the unified
credential layer that future PRs migrate everything onto.
"""
from korpha.credentials.model import (
    ExternalServiceAccount,
    ExternalServiceKind,
)
from korpha.credentials.resolver import (
    NoCredentialsAvailable,
    ResolvedCredentials,
    resolve_credentials,
)

__all__ = [
    "ExternalServiceAccount",
    "ExternalServiceKind",
    "NoCredentialsAvailable",
    "ResolvedCredentials",
    "resolve_credentials",
]
