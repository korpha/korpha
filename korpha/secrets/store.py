"""SecretStore — JSON-on-disk secrets keyed by name."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from korpha.secrets.crypto import (
    SecretsCryptoError,
    decrypt_bytes,
    encrypt_bytes,
    load_master_key,
)


class SecretNotFound(KeyError):
    pass


_SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,60}$")
_SECRET_REF_RE = re.compile(r"\$\{secret:([a-zA-Z0-9_.-]+)\}")


def _data_root() -> Path:
    base = os.environ.get("KORPHA_DATA_DIR")
    return (
        Path(base) if base
        else Path.home() / ".korpha"
    )


@dataclass
class SecretStore:
    """Per-process secret reader/writer.

    Construct one with default paths or pass ``vault_path`` +
    ``master_key_path`` for tests / alt deployments."""

    vault_path: Path | None = None
    master_key_path: Path | None = None

    def __post_init__(self) -> None:
        root = _data_root() / "secrets"
        if self.vault_path is None:
            self.vault_path = root / "vault.json.enc"
        if self.master_key_path is None:
            self.master_key_path = root / "master.key"

    # ---- helpers ----

    def _load(self) -> dict[str, dict]:
        """Decrypt and return the inner dict. Empty dict on
        first run."""
        if not self.vault_path.is_file():
            return {}
        master = load_master_key(self.master_key_path)
        blob = self.vault_path.read_bytes()
        plaintext = decrypt_bytes(blob, master)
        try:
            return json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SecretsCryptoError(
                f"vault contents not JSON: {exc}",
            ) from exc

    def _save(self, data: dict[str, dict]) -> None:
        master = load_master_key(self.master_key_path)
        plaintext = json.dumps(data, separators=(",", ":")).encode("utf-8")
        blob = encrypt_bytes(plaintext, master)
        self.vault_path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write
        tmp = self.vault_path.with_suffix(
            self.vault_path.suffix + ".tmp"
        )
        tmp.write_bytes(blob)
        os.chmod(tmp, 0o600)
        tmp.replace(self.vault_path)
        os.chmod(self.vault_path, 0o600)

    # ---- public API ----

    def set(
        self, name: str, value: str, *,
        description: str = "",
    ) -> None:
        """Store / overwrite a secret. Validates ``name`` is
        safe (alnum + `._-`)."""
        if not _SAFE_NAME_RE.match(name):
            raise ValueError(
                f"secret name {name!r} invalid; must be alnum "
                f"+ ._- starting with alphanum, ≤ 60 chars",
            )
        if not value:
            raise ValueError("secret value cannot be empty")
        data = self._load()
        data[name] = {
            "value": value,
            "description": description,
            "updated_at": datetime.now(tz=timezone.utc).isoformat(),
        }
        self._save(data)

    def get(self, name: str) -> str:
        data = self._load()
        if name not in data:
            raise SecretNotFound(name)
        return data[name]["value"]

    def get_or_none(self, name: str) -> Optional[str]:
        try:
            return self.get(name)
        except SecretNotFound:
            return None

    def delete(self, name: str) -> bool:
        data = self._load()
        if name not in data:
            return False
        del data[name]
        self._save(data)
        return True

    def list(self) -> list[dict]:
        """Metadata only — never returns secret values."""
        data = self._load()
        return [
            {
                "name": name,
                "description": entry.get("description", ""),
                "updated_at": entry.get("updated_at", ""),
                "length": len(entry.get("value", "")),
            }
            for name, entry in sorted(data.items())
        ]


def resolve_secrets(
    payload, *, store: SecretStore | None = None,
):
    """Recursively replace ``${secret:name}`` markers in strings
    inside dicts / lists / tuples / scalars.

    Missing secrets raise ``SecretNotFound`` rather than leaving
    the marker in place — silent passthrough would let a typo
    sneak unredacted ``${secret:typo}`` into a Stripe API call,
    which is worse than failing loudly."""
    store = store or SecretStore()

    def _resolve_string(s: str) -> str:
        def repl(m: re.Match) -> str:
            return store.get(m.group(1))
        return _SECRET_REF_RE.sub(repl, s)

    if isinstance(payload, str):
        return _resolve_string(payload)
    if isinstance(payload, dict):
        return {
            k: resolve_secrets(v, store=store)
            for k, v in payload.items()
        }
    if isinstance(payload, list):
        return [resolve_secrets(v, store=store) for v in payload]
    if isinstance(payload, tuple):
        return tuple(
            resolve_secrets(v, store=store) for v in payload
        )
    return payload


__all__ = [
    "SecretNotFound",
    "SecretStore",
    "resolve_secrets",
]
