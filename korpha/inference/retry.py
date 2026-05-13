"""Jittered backoff for decorrelated retries.

When the cofounder's daily heartbeat fires across N businesses
sharing one DeepSeek key, fixed exponential backoff means every
session retries at exactly the same moments — they all collide
again. Jitter spreads the retry instants out so the provider
doesn't see a synchronized thundering herd.

Hermes' approach (``agent/retry_utils.py``) — port verbatim
since the formula is generic.
"""
from __future__ import annotations

import random
import threading
import time

# Monotonic counter ensures jitter seed uniqueness even when
# multiple coroutines retry within the same nanosecond. Cheap.
_jitter_counter: int = 0
_jitter_lock = threading.Lock()


def jittered_backoff(
    attempt: int,
    *,
    base_delay: float = 5.0,
    max_delay: float = 120.0,
    jitter_ratio: float = 0.5,
) -> float:
    """Compute a jittered exponential backoff delay in seconds.

    Args:
        attempt: 1-based retry attempt number.
        base_delay: Delay for attempt 1 (before capping).
        max_delay: Cap; we never sleep longer than this.
        jitter_ratio: Fraction of computed delay used as random
            jitter range. ``0.5`` → jitter uniform in
            ``[0, 0.5 * delay]``.

    Returns:
        ``min(base * 2 ** (attempt-1), max_delay) + jitter``.

    The jitter decorrelates concurrent retries so multiple
    sessions hitting the same provider don't all resume on the
    same instant.
    """
    global _jitter_counter
    with _jitter_lock:
        _jitter_counter += 1
        tick = _jitter_counter

    exponent = max(0, attempt - 1)
    if exponent >= 63 or base_delay <= 0:
        delay = max_delay
    else:
        delay = min(base_delay * (2 ** exponent), max_delay)

    # Time-ns + counter mixed via a Knuth multiplicative hash so
    # decorrelation works even on coarse-clock platforms.
    seed = (time.time_ns() ^ (tick * 0x9E3779B9)) & 0xFFFFFFFF
    rng = random.Random(seed)
    jitter = rng.uniform(0, jitter_ratio * delay)

    return delay + jitter


__all__ = ["jittered_backoff"]
