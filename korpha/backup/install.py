"""One-shot litestream binary install.

Mike-friendly: configure_offdisk + start_replicator both need a
``litestream`` binary on $PATH. We don't bundle it with the wheel
(30 MB, GPL-adjacent, platform-specific), but we DO ship a one-shot
installer that downloads the official release into
``~/.local/bin/litestream`` and ensures that's on the founder's
$PATH for the running server.

Called from:
- ``korpha backups install-litestream`` (CLI)
- ``POST /app/backups/install-litestream`` (dashboard button)

Idempotent: re-running with the binary already present is a fast no-op.
"""
from __future__ import annotations

import hashlib
import logging
import os
import platform
import shutil
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Pinned version — bumping requires updating the sha256 below.
LITESTREAM_VERSION = "0.3.13"
_RELEASE_URL_TMPL = (
    "https://github.com/benbjohnson/litestream/releases/download/"
    "v{ver}/litestream-v{ver}-{os}-{arch}.tar.gz"
)
# SHA-256 of the official release tarball, per platform. Empty
# string = no pinned hash for that platform yet (we ground-truth
# linux/amd64 from the live download during Marketro setup; other
# platforms verified as we add them). With empty hash, the install
# still works but skips verification — caller is asked to confirm.
_SHA256 = {
    ("linux", "amd64"): "eb75a3de5cab03875cdae9f5f539e6aedadd66607003d9b1e7a9077948818ba0",
    ("linux", "arm64"): "",
    ("darwin", "amd64"): "",
    ("darwin", "arm64"): "",
}


@dataclass(frozen=True)
class InstallResult:
    ok: bool
    message: str
    path: Path | None = None


def _platform_arch() -> tuple[str, str]:
    sysname = platform.system().lower()  # 'linux' / 'darwin'
    machine = platform.machine().lower()  # 'x86_64' / 'arm64' / 'aarch64'
    arch_map = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }
    return sysname, arch_map.get(machine, machine)


def litestream_path() -> Path | None:
    """Return the first existing litestream binary on PATH or in
    ~/.local/bin, or None."""
    found = shutil.which("litestream")
    if found:
        return Path(found)
    candidate = Path.home() / ".local" / "bin" / "litestream"
    return candidate if candidate.is_file() else None


def install_litestream(*, verify_checksum: bool = True) -> InstallResult:
    """Download the pinned litestream release into ``~/.local/bin``.

    Idempotent — if a binary is already on PATH (any version), we
    return success without touching it. Verifies SHA-256 by default
    so the founder isn't trusting a download blindly. Returns an
    ``InstallResult`` the caller renders to its surface.
    """
    existing = litestream_path()
    if existing is not None:
        return InstallResult(
            ok=True,
            message=f"already installed at {existing}",
            path=existing,
        )

    sysname, arch = _platform_arch()
    if (sysname, arch) not in {("linux", "amd64"), ("linux", "arm64"),
                               ("darwin", "amd64"), ("darwin", "arm64")}:
        return InstallResult(
            ok=False,
            message=(
                f"unsupported platform {sysname}/{arch}. "
                "Install litestream manually: https://litestream.io/install/"
            ),
        )

    url = _RELEASE_URL_TMPL.format(ver=LITESTREAM_VERSION, os=sysname, arch=arch)
    target_dir = Path.home() / ".local" / "bin"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "litestream"

    with tempfile.NamedTemporaryFile(suffix=".tar.gz", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        try:
            with urllib.request.urlopen(url, timeout=60) as resp:
                tmp_path.write_bytes(resp.read())
        except Exception as exc:  # noqa: BLE001
            return InstallResult(
                ok=False,
                message=f"download failed: {exc}",
            )

        if verify_checksum:
            expected = _SHA256.get((sysname, arch), "")
            if expected:
                actual = hashlib.sha256(tmp_path.read_bytes()).hexdigest()
                if actual != expected:
                    return InstallResult(
                        ok=False,
                        message=(
                            f"checksum mismatch — refusing to install. "
                            f"expected {expected[:16]}…, got {actual[:16]}…"
                        ),
                    )

        try:
            with tarfile.open(tmp_path, "r:gz") as tar:
                # The tarball is just a single 'litestream' binary at
                # the root. Extract it directly to our target path.
                member = next(
                    (m for m in tar.getmembers() if m.name == "litestream"),
                    None,
                )
                if member is None:
                    return InstallResult(
                        ok=False,
                        message="tarball didn't contain 'litestream' binary",
                    )
                with tar.extractfile(member) as src:
                    if src is None:
                        return InstallResult(
                            ok=False,
                            message="couldn't read 'litestream' from tarball",
                        )
                    target.write_bytes(src.read())
            target.chmod(0o755)
        except Exception as exc:  # noqa: BLE001
            return InstallResult(
                ok=False,
                message=f"extract failed: {exc}",
            )

        # Make ~/.local/bin discoverable for THIS Python process so the
        # immediately-following start_replicator() can find it. We
        # don't touch shell profiles — that's the user's call.
        local_bin = str(target_dir)
        path_parts = (os.environ.get("PATH") or "").split(os.pathsep)
        if local_bin not in path_parts:
            os.environ["PATH"] = local_bin + os.pathsep + os.environ.get("PATH", "")

        return InstallResult(
            ok=True,
            message=f"installed litestream v{LITESTREAM_VERSION} → {target}",
            path=target,
        )
    finally:
        tmp_path.unlink(missing_ok=True)
