"""Stdlib-only authenticated encryption.

Why not Fernet from ``cryptography``? It's a great library, but
adding a 5MB dep just to encrypt one JSON file is wrong ratio
for our footprint. We use:

  * HKDF-SHA256 to derive separate encrypt + MAC keys from the
    master key + per-blob nonce.
  * SHA256-counter keystream XOR for confidentiality (CTR mode
    over SHA256). Stream cipher, no padding needed.
  * HMAC-SHA256 over (nonce || ciphertext) for authentication.

Format:
  [1 byte version=1][16 byte nonce][N byte ciphertext][32 byte tag]

This is a fine construction for AT-REST file encryption with a
single trusted writer/reader. It is NOT for transmitting over
networks (CTR + HMAC is fine but TLS does it better) or for
multi-party scenarios. Don't reuse this code outside this module.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import struct
from pathlib import Path


class SecretsCryptoError(Exception):
    """Decrypt failed — wrong key, corrupted blob, or tampering."""


_VERSION = 1
_NONCE_LEN = 16
_TAG_LEN = 32
_MASTER_KEY_LEN = 32


def generate_master_key() -> bytes:
    """Cryptographically random 32-byte master key. Caller
    persists this with mode 0600 — losing it means losing
    every secret."""
    return secrets.token_bytes(_MASTER_KEY_LEN)


def load_master_key(path: Path) -> bytes:
    """Read or generate the master key file. On first call we
    create it with chmod 0600 in the parent dir."""
    path = path.expanduser().resolve()
    if path.is_file():
        data = path.read_bytes()
        if len(data) != _MASTER_KEY_LEN:
            raise SecretsCryptoError(
                f"master key at {path} has wrong length "
                f"({len(data)} != {_MASTER_KEY_LEN})"
            )
        return data
    path.parent.mkdir(parents=True, exist_ok=True)
    key = generate_master_key()
    # Atomic write: tmp → fsync → rename → chmod
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(key)
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    os.chmod(path, 0o600)
    return key


def _hkdf_expand(
    master: bytes, info: bytes, length: int,
) -> bytes:
    """Single-step HKDF-Expand using HMAC-SHA256.

    info = b"korpha:enc"|b"korpha:mac" so the same master
    key can derive distinct sub-keys for the cipher and the MAC."""
    out = b""
    counter = 1
    last = b""
    while len(out) < length:
        last = hmac.new(
            master, last + info + bytes([counter]),
            hashlib.sha256,
        ).digest()
        out += last
        counter += 1
    return out[:length]


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    """Generate ``length`` bytes by SHA256(key || nonce || counter)."""
    out = bytearray()
    counter = 0
    while len(out) < length:
        block = hashlib.sha256(
            key + nonce + struct.pack(">Q", counter),
        ).digest()
        out.extend(block)
        counter += 1
    return bytes(out[:length])


def encrypt_bytes(plaintext: bytes, master_key: bytes) -> bytes:
    """AEAD encrypt. Returns the wire format described in the
    module docstring."""
    if len(master_key) != _MASTER_KEY_LEN:
        raise SecretsCryptoError(
            f"master key must be {_MASTER_KEY_LEN} bytes",
        )
    nonce = secrets.token_bytes(_NONCE_LEN)
    enc_key = _hkdf_expand(master_key, b"korpha:enc", 32)
    mac_key = _hkdf_expand(master_key, b"korpha:mac", 32)
    keystream = _keystream(enc_key, nonce, len(plaintext))
    ciphertext = bytes(
        a ^ b for a, b in zip(plaintext, keystream)
    )
    tag = hmac.new(
        mac_key,
        bytes([_VERSION]) + nonce + ciphertext,
        hashlib.sha256,
    ).digest()
    return bytes([_VERSION]) + nonce + ciphertext + tag


def decrypt_bytes(blob: bytes, master_key: bytes) -> bytes:
    """Verify + decrypt. Raises SecretsCryptoError on tamper /
    wrong key / truncated blob."""
    if len(master_key) != _MASTER_KEY_LEN:
        raise SecretsCryptoError(
            f"master key must be {_MASTER_KEY_LEN} bytes",
        )
    if len(blob) < 1 + _NONCE_LEN + _TAG_LEN:
        raise SecretsCryptoError("ciphertext too short")
    version = blob[0]
    if version != _VERSION:
        raise SecretsCryptoError(
            f"unsupported version {version}; expected {_VERSION}",
        )
    nonce = blob[1 : 1 + _NONCE_LEN]
    ciphertext = blob[1 + _NONCE_LEN : -_TAG_LEN]
    tag = blob[-_TAG_LEN:]

    mac_key = _hkdf_expand(master_key, b"korpha:mac", 32)
    expected = hmac.new(
        mac_key,
        bytes([version]) + nonce + ciphertext,
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(expected, tag):
        raise SecretsCryptoError(
            "authentication failed — wrong key or tampered blob",
        )
    enc_key = _hkdf_expand(master_key, b"korpha:enc", 32)
    keystream = _keystream(enc_key, nonce, len(ciphertext))
    return bytes(
        a ^ b for a, b in zip(ciphertext, keystream)
    )


__all__ = [
    "SecretsCryptoError",
    "decrypt_bytes",
    "encrypt_bytes",
    "generate_master_key",
    "load_master_key",
]
