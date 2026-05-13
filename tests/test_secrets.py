"""Tests for the secrets vault — crypto + store + resolver."""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from korpha.secrets import (
    SecretNotFound,
    SecretStore,
    SecretsCryptoError,
    decrypt_bytes,
    encrypt_bytes,
    generate_master_key,
    load_master_key,
    resolve_secrets,
)


# ---- crypto primitives ----


def test_encrypt_decrypt_roundtrip() -> None:
    key = generate_master_key()
    plaintext = b"hello, secret world"
    blob = encrypt_bytes(plaintext, key)
    assert decrypt_bytes(blob, key) == plaintext


def test_encrypt_produces_different_blobs_for_same_input() -> None:
    """Nonce is random per call → ciphertexts differ."""
    key = generate_master_key()
    a = encrypt_bytes(b"x", key)
    b = encrypt_bytes(b"x", key)
    assert a != b


def test_decrypt_with_wrong_key_fails() -> None:
    key1 = generate_master_key()
    key2 = generate_master_key()
    blob = encrypt_bytes(b"sensitive", key1)
    with pytest.raises(SecretsCryptoError, match="authentication"):
        decrypt_bytes(blob, key2)


def test_decrypt_tampered_ciphertext_fails() -> None:
    key = generate_master_key()
    blob = bytearray(encrypt_bytes(b"sensitive", key))
    # Flip one byte in the ciphertext region
    blob[20] ^= 0xff
    with pytest.raises(SecretsCryptoError, match="authentication"):
        decrypt_bytes(bytes(blob), key)


def test_decrypt_short_blob_fails() -> None:
    key = generate_master_key()
    with pytest.raises(SecretsCryptoError, match="too short"):
        decrypt_bytes(b"x" * 10, key)


def test_decrypt_unknown_version_fails() -> None:
    key = generate_master_key()
    blob = bytearray(encrypt_bytes(b"x", key))
    blob[0] = 99
    with pytest.raises(SecretsCryptoError, match="version"):
        decrypt_bytes(bytes(blob), key)


def test_load_master_key_creates_on_first_use(tmp_path: Path) -> None:
    p = tmp_path / "deep" / "master.key"
    key = load_master_key(p)
    assert len(key) == 32
    assert p.is_file()
    # Reload yields the same key
    assert load_master_key(p) == key
    # File mode is 0600
    assert oct(p.stat().st_mode)[-3:] == "600"


def test_load_master_key_rejects_wrong_length(tmp_path: Path) -> None:
    p = tmp_path / "bad.key"
    p.write_bytes(b"x" * 16)
    with pytest.raises(SecretsCryptoError, match="wrong length"):
        load_master_key(p)


# ---- SecretStore ----


@pytest.fixture
def store(tmp_path: Path) -> SecretStore:
    return SecretStore(
        vault_path=tmp_path / "vault.json.enc",
        master_key_path=tmp_path / "master.key",
    )


def test_set_get_roundtrip(store: SecretStore) -> None:
    store.set("stripe", "sk_test_abc")
    assert store.get("stripe") == "sk_test_abc"


def test_set_overwrite(store: SecretStore) -> None:
    store.set("k", "v1")
    store.set("k", "v2")
    assert store.get("k") == "v2"


def test_set_blank_rejected(store: SecretStore) -> None:
    with pytest.raises(ValueError, match="empty"):
        store.set("k", "")


def test_set_invalid_name_rejected(store: SecretStore) -> None:
    for bad in ["with space", "../escape", "0starts-numeric-but-then$", ""]:
        # Note: our regex accepts numeric-leading; only spaces/$/empty fail
        if bad in ("0starts-numeric-but-then$",):
            with pytest.raises(ValueError):
                store.set(bad, "x")
        elif bad in ("with space", "../escape", ""):
            with pytest.raises(ValueError):
                store.set(bad, "x")


def test_get_missing_raises(store: SecretStore) -> None:
    with pytest.raises(SecretNotFound):
        store.get("nope")


def test_get_or_none_missing(store: SecretStore) -> None:
    assert store.get_or_none("nope") is None


def test_delete_existing(store: SecretStore) -> None:
    store.set("k", "v")
    assert store.delete("k") is True
    with pytest.raises(SecretNotFound):
        store.get("k")


def test_delete_missing_returns_false(store: SecretStore) -> None:
    assert store.delete("missing") is False


def test_list_returns_metadata_not_values(store: SecretStore) -> None:
    store.set("a", "supersecret", description="primary")
    store.set("b", "x")
    rows = store.list()
    assert len(rows) == 2
    assert all("value" not in r for r in rows)
    found = next(r for r in rows if r["name"] == "a")
    assert found["description"] == "primary"
    assert found["length"] == len("supersecret")


def test_list_sorted_alphabetically(store: SecretStore) -> None:
    for n in ["zebra", "apple", "mango"]:
        store.set(n, "x")
    rows = store.list()
    assert [r["name"] for r in rows] == ["apple", "mango", "zebra"]


def test_persist_across_instances(tmp_path: Path) -> None:
    """A second SecretStore reading the same files sees the same data."""
    s1 = SecretStore(
        vault_path=tmp_path / "v.enc",
        master_key_path=tmp_path / "m.key",
    )
    s1.set("k", "v")
    s2 = SecretStore(
        vault_path=tmp_path / "v.enc",
        master_key_path=tmp_path / "m.key",
    )
    assert s2.get("k") == "v"


def test_vault_file_is_binary_encrypted(
    tmp_path: Path, store: SecretStore,
) -> None:
    """Stored file does NOT contain the plaintext value."""
    store.set("k", "PLAINTEXT-MARKER-ZZ")
    blob = store.vault_path.read_bytes()
    assert b"PLAINTEXT-MARKER-ZZ" not in blob


# ---- resolve_secrets ----


def test_resolve_simple_string(store: SecretStore) -> None:
    store.set("k", "v")
    assert resolve_secrets(
        "${secret:k}", store=store,
    ) == "v"


def test_resolve_in_dict(store: SecretStore) -> None:
    store.set("stripe", "sk_test_abc")
    out = resolve_secrets({
        "api_key": "${secret:stripe}",
        "public": "no-secret-here",
    }, store=store)
    assert out["api_key"] == "sk_test_abc"
    assert out["public"] == "no-secret-here"


def test_resolve_nested(store: SecretStore) -> None:
    store.set("a", "AAA")
    store.set("b", "BBB")
    out = resolve_secrets({
        "outer": {"inner": ["${secret:a}", "${secret:b}"]},
    }, store=store)
    assert out["outer"]["inner"] == ["AAA", "BBB"]


def test_resolve_multiple_in_one_string(store: SecretStore) -> None:
    store.set("a", "X")
    store.set("b", "Y")
    assert resolve_secrets(
        "${secret:a}-${secret:b}", store=store,
    ) == "X-Y"


def test_resolve_missing_raises(store: SecretStore) -> None:
    with pytest.raises(SecretNotFound):
        resolve_secrets("${secret:typo}", store=store)


def test_resolve_passes_through_non_strings(store: SecretStore) -> None:
    out = resolve_secrets(
        {"n": 42, "b": True, "x": None}, store=store,
    )
    assert out == {"n": 42, "b": True, "x": None}


# ---- CLI ----


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    return CliRunner(), tmp_path


def test_cli_set_and_list(cli) -> None:
    cli_runner, tmp = cli
    from korpha.cli import app
    r = cli_runner.invoke(app, [
        "secret", "set", "stripe", "--value", "sk_test_abc",
        "--desc", "test mode key",
    ])
    assert r.exit_code == 0, r.stdout
    assert "Stored secret" in r.stdout

    r = cli_runner.invoke(app, ["secret", "list"])
    assert r.exit_code == 0
    assert "stripe" in r.stdout
    assert "test mode key" in r.stdout
    # Never list the value
    assert "sk_test_abc" not in r.stdout


def test_cli_delete(cli) -> None:
    cli_runner, _ = cli
    from korpha.cli import app
    cli_runner.invoke(app, [
        "secret", "set", "k", "--value", "v",
    ])
    r = cli_runner.invoke(app, ["secret", "delete", "k"])
    assert r.exit_code == 0
    assert "Deleted" in r.stdout


def test_cli_delete_missing(cli) -> None:
    cli_runner, _ = cli
    from korpha.cli import app
    r = cli_runner.invoke(app, ["secret", "delete", "ghost"])
    assert r.exit_code == 0
    assert "No secret" in r.stdout


def test_cli_set_invalid_name(cli) -> None:
    cli_runner, _ = cli
    from korpha.cli import app
    r = cli_runner.invoke(app, [
        "secret", "set", "bad name", "--value", "v",
    ])
    assert r.exit_code == 1


def test_cli_list_empty(cli) -> None:
    cli_runner, _ = cli
    from korpha.cli import app
    r = cli_runner.invoke(app, ["secret", "list"])
    assert r.exit_code == 0
    assert "no secrets" in r.stdout.lower()
