"""Tests for the DM pairing flow."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from korpha.identity.pairing import (
    CODE_LENGTH,
    LOCKOUT_DURATION_SECONDS,
    MAX_FAILED_APPROVES,
    MAX_PENDING_CODES,
    FailedAttempts,
    PairingStore,
    PendingCode,
)


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    return tmp_path / "pairing.json"


@pytest.fixture
def store(store_path: Path) -> PairingStore:
    return PairingStore(path=store_path)


# ---- code generation ----


def test_generated_codes_are_correct_length_and_alphabet(
    store: PairingStore,
) -> None:
    codes = {
        store.initiate_pairing("telegram", f"u{i}")
        for i in range(20)
    }
    assert all(c is not None and len(c) == CODE_LENGTH for c in codes)
    # No I/O/0/1 to avoid OCR confusion
    assert all(
        not any(ch in "IO01" for ch in c)
        for c in codes if c
    )
    # Vanishingly unlikely to collide in 20 draws (28^8 space)
    assert len(codes) == 20


# ---- initiate_pairing rate limit ----


def test_initiate_pairing_caps_pending_per_user(
    store: PairingStore,
) -> None:
    """One user gets at most MAX_PENDING_CODES live codes."""
    issued = []
    for _ in range(MAX_PENDING_CODES + 2):
        c = store.initiate_pairing("telegram", "spammer")
        issued.append(c)
    # First MAX_PENDING_CODES succeed
    assert all(c is not None for c in issued[:MAX_PENDING_CODES])
    # Beyond cap: None
    assert all(c is None for c in issued[MAX_PENDING_CODES:])


def test_initiate_pairing_isolated_per_user(
    store: PairingStore,
) -> None:
    """User A's quota doesn't affect user B."""
    for _ in range(MAX_PENDING_CODES):
        store.initiate_pairing("telegram", "alice")
    assert store.initiate_pairing("telegram", "bob") is not None


# ---- approve ----


def test_approve_burns_code_and_authorizes(
    store: PairingStore,
) -> None:
    code = store.initiate_pairing(
        "telegram", "12345", display_name="Mike VA",
    )
    assert code is not None
    ok, msg = store.approve(code)
    assert ok
    assert "Mike VA" in msg
    assert store.is_authorized("telegram", "12345")
    # Code is burned — second approve fails
    ok2, _ = store.approve(code)
    assert ok2 is False


def test_approve_unknown_code_returns_false(
    store: PairingStore,
) -> None:
    ok, msg = store.approve("ZZZZZZZZ")
    assert ok is False
    assert "Unknown" in msg


def test_approve_expired_code_returns_false(
    store: PairingStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    code = store.initiate_pairing("telegram", "u1")
    assert code is not None
    # Force the pending entry's created_at into the distant past
    store.pending[0].created_at = time.time() - 99 * 60 * 60
    ok, msg = store.approve(code)
    assert ok is False
    assert "expired" in msg.lower()


def test_approve_normalizes_case_and_whitespace(
    store: PairingStore,
) -> None:
    code = store.initiate_pairing("telegram", "u1")
    assert code is not None
    ok, _ = store.approve(f"  {code.lower()}  ")
    assert ok is True


# ---- failed-attempt lockout ----


def test_failed_attempts_trip_lockout() -> None:
    fa = FailedAttempts()
    tripped: list[bool] = []
    for _ in range(MAX_FAILED_APPROVES):
        tripped.append(fa.record_failure())
    assert tripped[-1] is True
    assert fa.is_locked() is True


def test_failed_attempts_reset_after_window() -> None:
    fa = FailedAttempts()
    # Record one failure, then back-date it past the window
    fa.record_failure()
    fa.timestamps = [time.time() - 99 * 60 * 60]
    fa.record_failure()
    # Only the recent failure counts; not tripped yet
    assert fa.is_locked() is False


def test_locked_user_cannot_initiate_pairing(
    store: PairingStore,
) -> None:
    """Once a user is locked out, initiate_pairing returns None
    even if they're under the per-user cap."""
    for _ in range(MAX_FAILED_APPROVES):
        store.record_failed_approve("telegram", "abuser")
    assert store.is_locked("telegram", "abuser")
    assert store.initiate_pairing("telegram", "abuser") is None


# ---- persistence ----


def test_save_and_load_round_trips(store_path: Path) -> None:
    s1 = PairingStore(path=store_path)
    code = s1.initiate_pairing("telegram", "u1", display_name="Alice")
    assert code is not None
    ok, _ = s1.approve(code)
    assert ok

    s2 = PairingStore.load(path=store_path)
    assert s2.is_authorized("telegram", "u1")


def test_load_recovers_from_corrupt_file(store_path: Path) -> None:
    store_path.write_text("{not json")
    s = PairingStore.load(path=store_path)
    # Empty store, no crash
    assert s.list_authorized() == []
    assert s.list_pending() == []


def test_save_drops_expired_pending(store: PairingStore) -> None:
    """Expired codes get filtered out on save so the file doesn't
    grow unboundedly."""
    store.initiate_pairing("telegram", "u1")
    store.pending[0].created_at = time.time() - 99 * 60 * 60
    store.save()
    s2 = PairingStore.load(path=store.path)
    assert s2.list_pending() == []


def test_save_atomicity_no_tmp_left(store_path: Path) -> None:
    s = PairingStore(path=store_path)
    s.initiate_pairing("telegram", "u1")
    files = sorted(p.name for p in store_path.parent.iterdir())
    assert "pairing.json" in files
    assert not any(f.endswith(".tmp") for f in files)


# ---- list / revoke ----


def test_revoke_drops_authorized_pair(store: PairingStore) -> None:
    code = store.initiate_pairing("telegram", "u1")
    assert code is not None
    store.approve(code)
    assert store.revoke("telegram", "u1") is True
    assert not store.is_authorized("telegram", "u1")


def test_revoke_unknown_pair_returns_false(
    store: PairingStore,
) -> None:
    assert store.revoke("telegram", "nobody") is False


def test_list_authorized_returns_pairs(store: PairingStore) -> None:
    for u in ("alice", "bob"):
        c = store.initiate_pairing("telegram", u)
        assert c is not None
        store.approve(c)
    pairs = store.list_authorized()
    assert ("telegram", "alice") in pairs
    assert ("telegram", "bob") in pairs


def test_list_pending_filters_expired(store: PairingStore) -> None:
    fresh = store.initiate_pairing("telegram", "u1")
    stale = store.initiate_pairing("telegram", "u1")
    assert fresh is not None
    assert stale is not None
    # Find the stale one and back-date it
    for p in store.pending:
        if p.code == stale:
            p.created_at = time.time() - 99 * 60 * 60
    rows = store.list_pending()
    assert all(p.code != stale for p in rows)
    assert any(p.code == fresh for p in rows)


# ---- successful approve resets failed counter ----


def test_successful_approve_clears_failed_counter(
    store: PairingStore,
) -> None:
    """A user who racked up some bad approves but eventually pairs
    successfully shouldn't carry that history forward."""
    for _ in range(MAX_FAILED_APPROVES - 1):
        store.record_failed_approve("telegram", "u1")
    code = store.initiate_pairing("telegram", "u1")
    assert code is not None
    ok, _ = store.approve(code)
    assert ok
    # Failed record cleared
    assert "telegram:u1" not in store.failed
