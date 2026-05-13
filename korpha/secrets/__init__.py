"""Local encrypted secrets vault.

Mike has API keys (Stripe, HeyGen, Replicate, Telegram, etc.).
Today they're scattered: env vars, providers.yaml, agent-pasted
into prompts. We want a single place: encrypted at rest, named
references, easy to cycle.

This subsystem ships:

  * Symmetric encryption using stdlib HKDF + HMAC-SHA256 +
    SHA256-keystream XOR (no new dep). Fernet-style envelope:
    ``[1-byte version][16-byte nonce][ciphertext][32-byte tag]``.
    Authenticated — tampering with the ciphertext fails the
    HMAC and decrypt raises.

  * SecretStore — JSON-on-disk at
    ``~/.korpha/secrets/vault.json.enc`` keyed by a master
    key at ``~/.korpha/secrets/master.key`` (chmod 0600,
    auto-generated on first use).

  * CLI: ``korpha secret set/get/list/delete`` for Mike to
    paste keys + see what's stored.

  * ``${secret:name}`` resolution helper for skills + plugin
    config — ``resolve_secrets({"key": "${secret:stripe}"})``
    expands at call time.

Inspired by Paperclip's ``server/src/secrets/local-encrypted-
provider.ts``. Single-tenant, single-host — no remote KMS, no
multi-machine sync. Mike runs on his laptop or one VPS; that
fits.
"""
from korpha.secrets.crypto import (
    SecretsCryptoError,
    decrypt_bytes,
    encrypt_bytes,
    generate_master_key,
    load_master_key,
)
from korpha.secrets.store import (
    SecretNotFound,
    SecretStore,
    resolve_secrets,
)

__all__ = [
    "SecretNotFound",
    "SecretStore",
    "SecretsCryptoError",
    "decrypt_bytes",
    "encrypt_bytes",
    "generate_master_key",
    "load_master_key",
    "resolve_secrets",
]
