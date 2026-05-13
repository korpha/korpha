"""LocalFileDeployer — the default zero-config target.

Writes the deployment files to ``$KORPHA_DATA_DIR/deploys/
<business_id>/<slug>/`` and returns the URL ``/app/deploys/
<slug>/`` that the FastAPI server (see api/server.py) serves
statically.

This isn't a real public-internet deploy; Mike sees the page in
his own browser through the dashboard. That's enough for the
demo's "minute 4:30 oh-shit" moment when the cofounder's just
been onboarded — Mike clicks the link in the chat reply and
sees a real rendered landing page. Going public is the cloud
plugin's job.
"""
from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from korpha.deploy.contract import (
    Deployer,
    DeploymentResult,
    DeploymentTarget,
)

logger = logging.getLogger(__name__)


def _data_root() -> Path:
    base = os.environ.get("KORPHA_DATA_DIR")
    return Path(base) if base else (Path.home() / ".korpha")


def _deploys_root() -> Path:
    return _data_root() / "deploys"


def _slug_dir(business_id: UUID, slug: str) -> Path:
    return _deploys_root() / str(business_id) / slug


def _public_url_base() -> str:
    """Where the dashboard serves these. Override via env for
    deployments behind a proxy. Defaults to localhost:8765."""
    return os.environ.get(
        "KORPHA_PUBLIC_URL_BASE", "http://localhost:8765",
    ).rstrip("/")


@dataclass
class LocalFileDeployer(Deployer):
    name: str = "local-file"

    async def deploy(
        self, target: DeploymentTarget,
    ) -> DeploymentResult:
        if "index.html" not in target.files:
            raise ValueError(
                "deploy: target.files missing required index.html",
            )
        out_dir = _slug_dir(target.business_id, target.slug)
        # Wipe previous deploy of the same slug so removed files
        # don't linger.
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        bytes_written = 0
        for rel, content in target.files.items():
            # Defensive: refuse path traversal in keys
            if rel.startswith("/") or ".." in rel.split("/"):
                raise ValueError(
                    f"deploy: refusing unsafe path {rel!r}",
                )
            full = out_dir / rel
            full.parent.mkdir(parents=True, exist_ok=True)
            data = (
                content.encode("utf-8")
                if isinstance(content, str)
                else content
            )
            full.write_bytes(data)
            bytes_written += len(data)

        url = (
            f"{_public_url_base()}/app/deploys/"
            f"{target.business_id}/{target.slug}/"
        )
        logger.info(
            "deploy: %s → %s (%d bytes)",
            target.slug, url, bytes_written,
        )
        return DeploymentResult(
            url=url,
            slug=target.slug,
            deployer_name=self.name,
            bytes_written=bytes_written,
            extra={
                "directory": str(out_dir),
                "file_count": len(target.files),
            },
        )

    async def teardown(
        self, *, slug: str, business_id: UUID,
    ) -> bool:
        out_dir = _slug_dir(business_id, slug)
        if not out_dir.is_dir():
            return False
        shutil.rmtree(out_dir)
        return True


__all__ = ["LocalFileDeployer"]
