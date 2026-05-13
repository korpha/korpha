"""Tests for the off-disk backup wizard back-end."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from korpha.backup.offdisk import (
    PROVIDERS, OffDiskConfig, configure_offdisk, current_status,
    replicator_status, resolve_endpoint,
)


def _seed_master_key(tmp_path: Path) -> None:
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    (secrets_dir / "master.key").write_bytes(b"x" * 32)


def test_providers_registered() -> None:
    assert "cloudflare_r2" in PROVIDERS
    assert "backblaze_b2" in PROVIDERS
    assert "aws_s3" in PROVIDERS
    assert "minio" in PROVIDERS


def test_resolve_endpoint_r2() -> None:
    url = resolve_endpoint("cloudflare_r2", "auto", "abc123def456")
    assert url == "https://abc123def456.r2.cloudflarestorage.com"


def test_resolve_endpoint_b2() -> None:
    url = resolve_endpoint("backblaze_b2", "us-west-002", None)
    assert url == "https://s3.us-west-002.backblazeb2.com"


def test_resolve_endpoint_unknown_provider() -> None:
    with pytest.raises(ValueError, match="unknown provider"):
        resolve_endpoint("not-a-provider", "x", None)


def test_configure_offdisk_b2(tmp_path: Path) -> None:
    _seed_master_key(tmp_path)
    cfg = configure_offdisk(
        provider="backblaze_b2",
        bucket="marketro-backups",
        region="us-west-002",
        access_key_id="K001testaccesskey",
        secret_access_key="K001testsecretkey1234567890",
        data_dir=tmp_path,
    )
    assert isinstance(cfg, OffDiskConfig)
    assert cfg.bucket == "marketro-backups"
    assert cfg.region == "us-west-002"
    assert cfg.endpoint == "https://s3.us-west-002.backblazeb2.com"
    # Files written
    assert cfg.config_path.is_file()
    assert cfg.runner_path.is_file()
    assert cfg.creds_path.is_file()
    # Runner is executable
    assert cfg.runner_path.stat().st_mode & 0o100
    # Litestream config references our bucket
    yml = cfg.config_path.read_text()
    assert "s3://marketro-backups/korpha.db" in yml
    assert "us-west-002" in yml
    # Creds are encrypted (no plaintext)
    blob = cfg.creds_path.read_bytes()
    assert b"K001testaccesskey" not in blob
    assert b"K001testsecretkey1234567890" not in blob


def test_configure_offdisk_r2_needs_account_id(tmp_path: Path) -> None:
    _seed_master_key(tmp_path)
    with pytest.raises(ValueError, match="account_id required"):
        configure_offdisk(
            provider="cloudflare_r2",
            bucket="b",
            region="auto",
            access_key_id="k",
            secret_access_key="s",
            data_dir=tmp_path,
        )


def test_configure_offdisk_bucket_required(tmp_path: Path) -> None:
    _seed_master_key(tmp_path)
    with pytest.raises(ValueError, match="bucket name required"):
        configure_offdisk(
            provider="backblaze_b2", bucket="",
            region="us-west-002",
            access_key_id="k", secret_access_key="s",
            data_dir=tmp_path,
        )


def test_configure_offdisk_creds_required(tmp_path: Path) -> None:
    _seed_master_key(tmp_path)
    with pytest.raises(ValueError, match="access key id"):
        configure_offdisk(
            provider="backblaze_b2", bucket="b", region="r",
            access_key_id="", secret_access_key="",
            data_dir=tmp_path,
        )


def test_configure_offdisk_unknown_provider(tmp_path: Path) -> None:
    _seed_master_key(tmp_path)
    with pytest.raises(ValueError, match="unknown provider"):
        configure_offdisk(
            provider="dropbox", bucket="b", region="r",
            access_key_id="k", secret_access_key="s",
            data_dir=tmp_path,
        )


def test_current_status_returns_none_when_not_configured(tmp_path: Path) -> None:
    assert current_status(tmp_path) is None


def test_current_status_after_configure(tmp_path: Path) -> None:
    _seed_master_key(tmp_path)
    configure_offdisk(
        provider="backblaze_b2",
        bucket="m", region="us-west-002",
        access_key_id="k", secret_access_key="s",
        data_dir=tmp_path,
    )
    st = current_status(tmp_path)
    assert st is not None
    assert st["provider"] == "backblaze_b2"
    assert st["provider_label"] == "Backblaze B2"
    assert st["bucket"] == "m"


def test_replicator_status_no_pid_file(tmp_path: Path) -> None:
    st = replicator_status(tmp_path)
    assert st["running"] is False
    assert st["pid"] is None


def test_replicator_status_stale_pid_cleaned(tmp_path: Path) -> None:
    pid_file = tmp_path / "litestream.pid"
    pid_file.write_text("999999999")  # implausibly large pid
    st = replicator_status(tmp_path)
    assert st["running"] is False
    # Stale pid file got auto-removed
    assert not pid_file.exists()


def test_minio_uses_endpoint_override(tmp_path: Path) -> None:
    _seed_master_key(tmp_path)
    cfg = configure_offdisk(
        provider="minio",
        bucket="m", region="us-east-1",
        access_key_id="k", secret_access_key="s",
        endpoint_override="https://minio.example.com:9000",
        data_dir=tmp_path,
    )
    yml = cfg.config_path.read_text()
    assert "https://minio.example.com:9000" in yml
