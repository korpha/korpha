"""Heartbeats + Routines.

Wakeups are one-shot timers — "remind me to do X at 9am tomorrow".
Routines are recurring — "every weekday at 9am, post a thread on X".

Both flow through the same dispatch path: ``HeartbeatService.tick()`` looks
for due work, marks it in_flight, calls registered handlers, and records
the result. Run ``korpha tick`` from cron or a sidecar process to keep
the cofounder humming.
"""
from korpha.heartbeats.dispatcher import (
    HandlerRegistry,
    HeartbeatService,
    HeartbeatTickResult,
    register_handler,
)
from korpha.heartbeats.model import (
    Routine,
    RoutineSchedule,
    Wakeup,
    WakeupKind,
    WakeupStatus,
)

__all__ = [
    "HandlerRegistry",
    "HeartbeatService",
    "HeartbeatTickResult",
    "Routine",
    "RoutineSchedule",
    "Wakeup",
    "WakeupKind",
    "WakeupStatus",
    "register_handler",
]
