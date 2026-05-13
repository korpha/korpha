"""DM pairing flow — code-based authorization for new chat users.

Replaces static allowlists with founder-mediated consent:

  1. Unknown user DMs the bot.
  2. The channel router checks ``is_authorized``. Not authorized →
     ``initiate_pairing`` mints an 8-char code, returns the message
     to send back to the user ("You're not authorized; ask the
     owner to run ``/approve <CODE>``").
  3. The user shares the code out-of-band with the founder.
  4. Founder runs ``korpha pair approve <CODE>`` (CLI) or
     replies ``/approve <CODE>`` in their own session. The
     ``approve`` call burns the code + adds the (platform, user_id)
     pair to the authorization store.
  5. Subsequent messages from that user are authorized via
     ``is_authorized``.

Anti-abuse: each platform user gets at most ``MAX_PENDING_CODES``
unburned codes; after ``MAX_FAILED_APPROVES`` bad approves in
``LOCKOUT_WINDOW`` the platform user is temporarily blocked.

Persistence: JSON file at ``~/.korpha/pairing.json``. Single-
process expectation — multi-process deploys would need DB persistence,
but the founder's deploy is single-process today.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


CODE_LENGTH = 8
"""Codes are 8 chars from a 28-char alphabet (no I/O/0/1) →
~10^11 possibilities. Plenty for the rate-limit window without
making the user type a paragraph."""

CODE_TTL_SECONDS = 30 * 60  # 30 min
MAX_PENDING_CODES = 3
"""How many active codes one platform user can have. Prevents
flooding the founder's inbox with auth requests."""

MAX_FAILED_APPROVES = 5
LOCKOUT_WINDOW_SECONDS = 60 * 60  # 1 hour
LOCKOUT_DURATION_SECONDS = 24 * 60 * 60  # 24 hours

_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
"""Code alphabet — no I/O/0/1 to avoid OCR / read-aloud confusion."""


def _generate_code() -> str:
    """8-char code from the safe alphabet."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(CODE_LENGTH))


@dataclass
class PendingCode:
    code: str
    platform: str
    user_id: str
    display_name: str | None = None
    created_at: float = field(default_factory=time.time)

    def is_expired(self, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return (now - self.created_at) > CODE_TTL_SECONDS

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "platform": self.platform,
            "user_id": self.user_id,
            "display_name": self.display_name,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PendingCode | None":
        try:
            return cls(
                code=str(data["code"]),
                platform=str(data["platform"]),
                user_id=str(data["user_id"]),
                display_name=data.get("display_name"),
                created_at=float(data.get("created_at") or time.time()),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass
class FailedAttempts:
    """Tracks bad approve attempts per platform user for lockout."""

    timestamps: list[float] = field(default_factory=list)
    locked_until: float = 0.0

    def is_locked(self, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        return now < self.locked_until

    def record_failure(self, now: float | None = None) -> bool:
        """Append a failure timestamp + return True if this trips
        the lockout."""
        now = now if now is not None else time.time()
        # Drop stale failures outside the window before counting
        cutoff = now - LOCKOUT_WINDOW_SECONDS
        self.timestamps = [t for t in self.timestamps if t > cutoff]
        self.timestamps.append(now)
        if len(self.timestamps) >= MAX_FAILED_APPROVES:
            self.locked_until = now + LOCKOUT_DURATION_SECONDS
            self.timestamps.clear()
            return True
        return False

    def to_dict(self) -> dict:
        return {
            "timestamps": self.timestamps,
            "locked_until": self.locked_until,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "FailedAttempts":
        return cls(
            timestamps=list(data.get("timestamps") or []),
            locked_until=float(data.get("locked_until") or 0.0),
        )


@dataclass
class PairingStore:
    """In-memory + on-disk state. The CLI / channel router both
    construct one against the same JSON path so they see the same
    authorizations."""

    path: Path
    pending: list[PendingCode] = field(default_factory=list)
    authorized: list[tuple[str, str]] = field(default_factory=list)
    """(platform, user_id) tuples. Set semantics; we use a list for
    JSON-friendly persistence."""

    failed: dict[str, FailedAttempts] = field(default_factory=dict)
    """Key: ``f"{platform}:{user_id}"``. Track for lockout."""

    @classmethod
    def load(cls, path: Path | None = None) -> "PairingStore":
        if path is None:
            path = _default_path()
        if not path.exists():
            return cls(path=path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning(
                "pairing: corrupt store at %s; starting fresh", path,
            )
            return cls(path=path)
        store = cls(path=path)
        for raw in data.get("pending") or []:
            pc = PendingCode.from_dict(raw)
            if pc is not None and not pc.is_expired():
                store.pending.append(pc)
        for raw in data.get("authorized") or []:
            if isinstance(raw, list) and len(raw) == 2:
                store.authorized.append((str(raw[0]), str(raw[1])))
        failed = data.get("failed") or {}
        if isinstance(failed, dict):
            for key, val in failed.items():
                if isinstance(val, dict):
                    store.failed[str(key)] = FailedAttempts.from_dict(val)
        return store

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Drop expired pending codes before persisting; keeps the
        # file from growing.
        self.pending = [p for p in self.pending if not p.is_expired()]
        payload = {
            "pending": [p.to_dict() for p in self.pending],
            "authorized": [list(t) for t in self.authorized],
            "failed": {k: v.to_dict() for k, v in self.failed.items()},
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            tmp.write_text(
                json.dumps(payload, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except OSError as exc:
            logger.warning("pairing: save failed: %s", exc)

    # ---- public API ----

    def is_authorized(self, platform: str, user_id: str) -> bool:
        return (str(platform), str(user_id)) in self.authorized

    def initiate_pairing(
        self,
        platform: str,
        user_id: str,
        *,
        display_name: str | None = None,
    ) -> str | None:
        """Mint a new code for ``(platform, user_id)``. Returns the
        code string, or ``None`` when the user is rate-limited
        (already has MAX_PENDING_CODES unburned codes, or is in
        lockout)."""
        if self.is_locked(platform, user_id):
            return None
        # Drop expired codes for this user before counting
        existing = [
            p for p in self.pending
            if p.platform == platform and p.user_id == user_id
            and not p.is_expired()
        ]
        if len(existing) >= MAX_PENDING_CODES:
            return None
        code = _generate_code()
        # Don't issue duplicate codes (vanishingly unlikely but
        # check anyway)
        while any(p.code == code for p in self.pending):
            code = _generate_code()
        self.pending.append(PendingCode(
            code=code, platform=str(platform), user_id=str(user_id),
            display_name=display_name,
        ))
        self.save()
        return code

    def approve(self, code: str) -> tuple[bool, str]:
        """Burn a code, authorize the corresponding (platform, user).
        Returns ``(success, message)`` — ``message`` is a one-line
        explanation safe to surface to the founder."""
        code = code.strip().upper()
        for i, p in enumerate(self.pending):
            if p.code == code:
                if p.is_expired():
                    del self.pending[i]
                    self.save()
                    return (False, f"Code {code} expired.")
                self.pending.pop(i)
                pair = (p.platform, p.user_id)
                if pair not in self.authorized:
                    self.authorized.append(pair)
                # Reset failed counter for this user — they made it
                key = f"{p.platform}:{p.user_id}"
                self.failed.pop(key, None)
                self.save()
                return (
                    True,
                    f"Approved {p.display_name or p.user_id} on "
                    f"{p.platform}.",
                )
        # No match — record a failure against… well, we don't know
        # who. Failed approves are tracked by FOUNDER user, not
        # the unknown requester, since a brute-force attempt would
        # be against the approver. We don't have founder context
        # here — caller decorates failures via record_failed_approve.
        return (False, f"Unknown or expired code: {code!r}.")

    def record_failed_approve(
        self, platform: str, user_id: str,
    ) -> bool:
        """Caller-driven failure tracking. The founder's identity
        comes from whoever issued the bad approve command. Returns
        True if this trips the lockout."""
        key = f"{platform}:{user_id}"
        rec = self.failed.get(key) or FailedAttempts()
        tripped = rec.record_failure()
        self.failed[key] = rec
        self.save()
        return tripped

    def is_locked(self, platform: str, user_id: str) -> bool:
        rec = self.failed.get(f"{platform}:{user_id}")
        return rec is not None and rec.is_locked()

    def revoke(self, platform: str, user_id: str) -> bool:
        """Drop a previously-authorized (platform, user). Returns
        True if a row was actually removed."""
        pair = (str(platform), str(user_id))
        if pair not in self.authorized:
            return False
        self.authorized.remove(pair)
        self.save()
        return True

    def list_authorized(self) -> list[tuple[str, str]]:
        return list(self.authorized)

    def list_pending(self) -> list[PendingCode]:
        # Drop expired before returning so callers don't see stale
        self.pending = [p for p in self.pending if not p.is_expired()]
        return list(self.pending)


def _default_path() -> Path:
    base = os.environ.get("KORPHA_DATA_DIR")
    return (
        (Path(base) / "pairing.json") if base
        else (Path.home() / ".korpha" / "pairing.json")
    )


__all__ = [
    "CODE_LENGTH",
    "CODE_TTL_SECONDS",
    "FailedAttempts",
    "LOCKOUT_DURATION_SECONDS",
    "LOCKOUT_WINDOW_SECONDS",
    "MAX_FAILED_APPROVES",
    "MAX_PENDING_CODES",
    "PairingStore",
    "PendingCode",
]
