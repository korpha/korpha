"""Deploy adapters — landing-page publish targets.

The BRIEF demo's "minute 4:30 oh-shit moment" is "Mike sees a
deployed landing page URL." We had every piece *except* the
deploy step. This module is the seam.

A ``Deployer`` is anything that can take a slug + a small set
of HTML/CSS/JS files and produce a public URL. The default is
``LocalFileDeployer`` — writes to
``~/.korpha/deploys/<slug>/`` and the existing FastAPI
server exposes ``/app/deploys/<slug>/`` so Mike can click + see
it without configuring a hosting provider.

Cloud-host deployers (Vercel CLI, Cloudflare Pages,
Netlify CLI, surge.sh) become plugins — they register their
adapter and the ``deploy.publish_landing`` skill picks the
configured one. The contract is intentionally narrow so the
plugin doesn't need to understand Korpha's wider object
model.

Inspired by Hermes's gateway-channel + inference-provider plugin
shape — same ABC + registry + single-active-default pattern.
"""
from korpha.deploy.contract import (
    Deployer,
    DeploymentResult,
    DeploymentTarget,
    NoDeployerConfigured,
    deploy_registry,
    set_active_deployer,
)
from korpha.deploy.local_file import LocalFileDeployer

__all__ = [
    "DeploymentResult",
    "DeploymentTarget",
    "Deployer",
    "LocalFileDeployer",
    "NoDeployerConfigured",
    "deploy_registry",
    "set_active_deployer",
]
