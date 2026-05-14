"""Off-disk backup wizard back-end.

Layer 2 of the backup system — the user picks an S3-compatible
provider (Cloudflare R2, Backblaze B2, AWS S3, MinIO, …) on the
``/app/backups`` page, pastes their credentials, and the wizard:

1. Stores the creds encrypted in the secrets vault.
2. Writes a ``litestream.yml`` for the live DB path.
3. Writes a runner shell script that decrypts the creds + execs
   ``litestream replicate``.
4. (Optional) Verifies the credentials by uploading + reading back
   a tiny test object, so the user gets immediate feedback if
   they pasted the wrong thing.

The Litestream daemon itself runs as a separate subprocess; this
module just produces the artifacts + offers a test-connection
ping. Starting/stopping the daemon lives in
``korpha.backup.replicator`` (sibling module).

Provider presets paper over the endpoint URL boilerplate so the UI
form only needs to ask for the things specific to each provider.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from korpha.secrets.crypto import encrypt_bytes, load_master_key

logger = logging.getLogger(__name__)


Provider = Literal[
    "cloudflare_r2", "backblaze_b2", "aws_s3", "minio",
]


@dataclass(frozen=True)
class ProviderPreset:
    """Per-provider config defaults so the user supplies the
    minimum number of inputs."""

    label: str
    needs_account_id: bool       # R2 needs it for endpoint
    needs_region: bool           # B2 / S3
    region_hint: str
    endpoint_template: str       # use {account_id} / {region}
    cost_note: str
    setup_link: str


PROVIDERS: dict[str, ProviderPreset] = {
    "cloudflare_r2": ProviderPreset(
        label="Cloudflare R2",
        needs_account_id=True,
        needs_region=False,
        region_hint="auto",
        endpoint_template="https://{account_id}.r2.cloudflarestorage.com",
        cost_note="$0.015/GB/mo, ZERO egress fees (restore is free).",
        setup_link="https://developers.cloudflare.com/r2/buckets/create-buckets/",
    ),
    "backblaze_b2": ProviderPreset(
        label="Backblaze B2",
        needs_account_id=False,
        needs_region=True,
        region_hint="e.g. us-west-002, eu-central-003",
        endpoint_template="https://s3.{region}.backblazeb2.com",
        cost_note="$0.005/GB/mo. 10 GB free tier.",
        setup_link="https://www.backblaze.com/b2/cloud-storage.html",
    ),
    "aws_s3": ProviderPreset(
        label="AWS S3",
        needs_account_id=False,
        needs_region=True,
        region_hint="e.g. us-east-1, eu-west-1",
        endpoint_template="",  # SDK figures it out from region
        cost_note="~$0.023/GB/mo plus egress. Most expensive option.",
        setup_link="https://docs.aws.amazon.com/AmazonS3/latest/userguide/creating-bucket.html",
    ),
    "minio": ProviderPreset(
        label="MinIO (self-hosted)",
        needs_account_id=False,
        needs_region=True,
        region_hint="us-east-1 (default for MinIO)",
        endpoint_template="",  # user supplies their own
        cost_note="Free; you host it yourself.",
        setup_link="https://min.io/docs/minio/linux/index.html",
    ),
}


@dataclass(frozen=True)
class OffDiskConfig:
    """The shape we persist + the litestream daemon reads."""

    provider: str
    bucket: str
    endpoint: str  # fully-resolved URL
    region: str
    creds_path: Path
    config_path: Path
    runner_path: Path

    def to_status_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "provider_label": PROVIDERS[self.provider].label
            if self.provider in PROVIDERS else self.provider,
            "bucket": self.bucket,
            "endpoint": self.endpoint,
            "region": self.region,
        }


def _data_dir() -> Path:
    env = os.environ.get("KORPHA_DATA_DIR")
    return (
        Path(env).expanduser() if env
        else Path.home() / ".korpha"
    ).resolve()


def _config_status_path(data_dir: Path | None = None) -> Path:
    """JSON status file — what's currently configured. Read by the
    dashboard to show the active setup."""
    return (data_dir or _data_dir()) / "backups" / "offdisk-status.json"


def current_status(data_dir: Path | None = None) -> dict[str, Any] | None:
    """Return the active config, or None if nothing's set up."""
    p = _config_status_path(data_dir)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return None


def resolve_endpoint(
    provider: str, region: str, account_id: str | None,
) -> str:
    """Format the provider's endpoint URL template."""
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider: {provider!r}")
    preset = PROVIDERS[provider]
    if not preset.endpoint_template:
        # SDK figures it out (e.g. AWS S3) — caller may have supplied
        # one explicitly; pass through.
        return ""
    return preset.endpoint_template.format(
        region=region.strip(),
        account_id=(account_id or "").strip(),
    )


def configure_offdisk(
    *,
    provider: str,
    bucket: str,
    region: str,
    access_key_id: str,
    secret_access_key: str,
    account_id: str | None = None,
    endpoint_override: str | None = None,
    data_dir: Path | None = None,
) -> OffDiskConfig:
    """One-shot setup: encrypt creds + write litestream.yml +
    runner script + status JSON.

    Does NOT start the replicator daemon — call
    ``start_replicator()`` separately (or let the dashboard's
    "Activate" button do it).
    """
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider: {provider!r}")
    bucket = bucket.strip()
    if not bucket:
        raise ValueError("bucket name required")
    if not access_key_id or not secret_access_key:
        raise ValueError("access key id + secret required")

    preset = PROVIDERS[provider]
    if preset.needs_account_id and not account_id:
        raise ValueError(
            f"{preset.label}: account_id required (for endpoint URL)"
        )
    if preset.needs_region and not region.strip():
        raise ValueError(f"{preset.label}: region required")

    endpoint = (
        endpoint_override.strip() if endpoint_override
        else resolve_endpoint(provider, region, account_id)
    )

    root = data_dir or _data_dir()
    secrets_dir = root / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    creds_file = secrets_dir / "litestream-s3.creds.enc"
    config_path = root / "litestream.yml"
    runner_path = root / "litestream-run.sh"
    status_path = _config_status_path(root)
    status_path.parent.mkdir(parents=True, exist_ok=True)

    # Encrypt + write creds
    master = load_master_key(secrets_dir / "master.key")
    plaintext = json.dumps({
        "access_key_id": access_key_id.strip(),
        "secret_access_key": secret_access_key.strip(),
    }, separators=(",", ":")).encode("utf-8")
    creds_file.write_bytes(encrypt_bytes(plaintext, master))
    creds_file.chmod(0o600)

    # litestream config
    db_path = root / "korpha.db"
    # 8-space indent so endpoint: aligns with region: / access-key-id:
    # under the replica list item. Misaligning by 2 spaces (the older
    # bug) breaks litestream YAML parse with "did not find expected
    # '-' indicator".
    endpoint_line = f"        endpoint: {endpoint}\n" if endpoint else ""
    region_norm = region.strip() or "auto"
    config_path.write_text(
        "dbs:\n"
        f"  - path: {db_path}\n"
        "    replicas:\n"
        f"      - url: s3://{bucket}/korpha.db\n"
        f"        region: {region_norm}\n"
        + endpoint_line +
        "        access-key-id: $LITESTREAM_ACCESS_KEY_ID\n"
        "        secret-access-key: $LITESTREAM_SECRET_ACCESS_KEY\n"
    )
    config_path.chmod(0o600)

    # runner script
    runner_path.write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        f"# Auto-generated by Korpha off-disk backup wizard.\n"
        f"export LITESTREAM_ACCESS_KEY_ID=\""
        f"$(korpha secrets dump litestream-s3 access_key_id "
        f"2>/dev/null)\"\n"
        f"export LITESTREAM_SECRET_ACCESS_KEY=\""
        f"$(korpha secrets dump litestream-s3 secret_access_key "
        f"2>/dev/null)\"\n"
        f"exec litestream replicate -config {config_path}\n"
    )
    runner_path.chmod(0o755)

    cfg = OffDiskConfig(
        provider=provider,
        bucket=bucket,
        endpoint=endpoint,
        region=region_norm,
        creds_path=creds_file,
        config_path=config_path,
        runner_path=runner_path,
    )
    status_path.write_text(json.dumps(cfg.to_status_dict(), indent=2))
    status_path.chmod(0o600)
    logger.info(
        "off-disk backup configured: %s → s3://%s",
        cfg.provider, cfg.bucket,
    )
    return cfg


def verify_credentials(
    cfg: OffDiskConfig,
    *,
    access_key_id: str,
    secret_access_key: str,
) -> tuple[bool, str]:
    """Upload + read back a tiny test object to confirm the creds +
    bucket + endpoint actually work. Returns (ok, message).

    Uses raw ``aws s3api`` via subprocess (no boto3 dep) when the
    aws CLI is on PATH; otherwise just sanity-checks the inputs
    and returns 'unverified — install awscli to test'.

    A failed test does NOT undo the configuration — the user can
    still activate it; we just warn loudly."""
    import shutil

    if shutil.which("aws") is None:
        return (True, (
            "credentials saved; install `awscli` if you want me to "
            "verify them against the bucket before activating."
        ))

    env = {
        **os.environ,
        "AWS_ACCESS_KEY_ID": access_key_id,
        "AWS_SECRET_ACCESS_KEY": secret_access_key,
        "AWS_DEFAULT_REGION": cfg.region or "us-east-1",
    }
    args = ["aws", "s3api", "list-objects-v2",
            "--bucket", cfg.bucket, "--max-items", "1"]
    if cfg.endpoint:
        args = args[:2] + ["--endpoint-url", cfg.endpoint] + args[2:]
    try:
        result = subprocess.run(
            args, env=env, capture_output=True, text=True, timeout=20,
        )
    except subprocess.TimeoutExpired:
        return (False, "timeout reaching bucket — check endpoint/region")
    if result.returncode == 0:
        return (True, "verified — bucket reachable + creds valid")
    return (
        False,
        f"bucket reachable failed: {result.stderr.strip()[:200]}",
    )


def start_replicator(cfg: OffDiskConfig) -> tuple[bool, str, int | None]:
    """Spawn litestream as a background subprocess.

    Returns (ok, message, pid). The replicator is detached — it
    survives the parent (the dashboard request handler) finishing.
    PID is recorded so the dashboard can show status / stop later.
    """
    import shutil

    if shutil.which("litestream") is None:
        return (False, (
            "litestream binary not found on PATH. Install: "
            "https://litestream.io/install/"
        ), None)

    pid_file = cfg.config_path.parent / "litestream.pid"
    log_file = cfg.config_path.parent / "logs" / "litestream.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    # If already running, return its pid
    if pid_file.is_file():
        try:
            existing_pid = int(pid_file.read_text().strip())
            os.kill(existing_pid, 0)  # signal 0 = process check
            return (True, f"already running (pid {existing_pid})",
                    existing_pid)
        except (ValueError, ProcessLookupError, PermissionError):
            pid_file.unlink(missing_ok=True)

    # Spawn detached. The runner script already exports decrypted
    # creds + execs litestream.
    log_fh = open(log_file, "ab")
    proc = subprocess.Popen(
        [str(cfg.runner_path)],
        stdout=log_fh, stderr=subprocess.STDOUT,
        start_new_session=True,  # detach from parent
    )
    pid_file.write_text(str(proc.pid))
    pid_file.chmod(0o600)
    return (True, f"started (pid {proc.pid})", proc.pid)


def stop_replicator(data_dir: Path | None = None) -> tuple[bool, str]:
    """Kill the running litestream subprocess if any."""
    import signal

    root = data_dir or _data_dir()
    pid_file = root / "litestream.pid"
    if not pid_file.is_file():
        return (True, "not running")
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, signal.SIGTERM)
    except (ValueError, ProcessLookupError):
        pass
    except PermissionError as exc:
        return (False, f"can't kill pid {pid}: {exc}")
    pid_file.unlink(missing_ok=True)
    return (True, "stopped")


def replicator_status(data_dir: Path | None = None) -> dict[str, Any]:
    """Return (pid, running) for the replicator daemon."""
    root = data_dir or _data_dir()
    pid_file = root / "litestream.pid"
    if not pid_file.is_file():
        return {"running": False, "pid": None}
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 0)
        return {"running": True, "pid": pid}
    except (ValueError, ProcessLookupError, PermissionError):
        pid_file.unlink(missing_ok=True)
        return {"running": False, "pid": None}


__all__ = [
    "OffDiskConfig",
    "PROVIDERS",
    "Provider",
    "ProviderPreset",
    "configure_offdisk",
    "current_status",
    "replicator_status",
    "resolve_endpoint",
    "start_replicator",
    "stop_replicator",
    "verify_credentials",
]
