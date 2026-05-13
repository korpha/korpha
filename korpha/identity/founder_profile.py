"""Founder deep-profile from Debriefeur (optional).

The Day-0 ``founder.intake_brief`` captures the obvious surface inputs
(goal, hours/week, savings, skills, constraints). For founders who want
their cofounder to actually understand *how they think*, the optional
20-minute Debriefeur interview produces a much richer profile —
decision style, risk tolerance, communication preferences, blindspots,
operating rhythm.

This module reads whatever Debriefeur writes to
``<data_dir>/founder_profile.json``. If the file doesn't exist, the
system prompts fall back to the basic brief. If it does exist, the
CEO + every Director / VP picks up a "How this founder thinks"
preamble.

Debriefeur lives at https://github.com/AIgenteur/debriefeur and runs
as a separate CLI — same one Hermes and OpenClaw users invoke.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FounderProfile:
    """Whatever Debriefeur extracted about the founder.

    Schema is deliberately loose — Debriefeur evolves, and we don't want
    Korpha to fail on a new field. We surface what we know and pass the
    rest through as the ``extra`` dict (rendered as-is in prompts)."""

    decision_style: str = ""
    risk_tolerance: str = ""
    communication_preferences: str = ""
    strengths: tuple[str, ...] = ()
    blindspots: tuple[str, ...] = ()
    operating_rhythm: str = ""
    raw_summary: str = ""
    extra: dict = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        # Frozen dataclass + mutable default: set via object.__setattr__
        if self.extra is None:
            object.__setattr__(self, "extra", {})

    def is_empty(self) -> bool:
        """True when nothing useful is in the profile — caller should
        skip the preamble entirely rather than render an empty section."""
        return not any([
            self.decision_style.strip(),
            self.risk_tolerance.strip(),
            self.communication_preferences.strip(),
            self.strengths,
            self.blindspots,
            self.operating_rhythm.strip(),
            self.raw_summary.strip(),
        ])

    def as_prompt_preamble(self) -> str:
        """Render the profile as a system-prompt section. Empty string
        when ``is_empty()`` so callers don't have to check separately."""
        if self.is_empty():
            return ""
        lines: list[str] = ["How this founder thinks (from their deep-dive):"]
        if self.decision_style.strip():
            lines.append(f"- Decision style: {self.decision_style.strip()}")
        if self.risk_tolerance.strip():
            lines.append(f"- Risk tolerance: {self.risk_tolerance.strip()}")
        if self.communication_preferences.strip():
            lines.append(
                f"- Communication preferences: "
                f"{self.communication_preferences.strip()}"
            )
        if self.strengths:
            lines.append(f"- Strengths: {', '.join(self.strengths)}")
        if self.blindspots:
            lines.append(f"- Blindspots to compensate for: {', '.join(self.blindspots)}")
        if self.operating_rhythm.strip():
            lines.append(f"- Operating rhythm: {self.operating_rhythm.strip()}")
        if self.raw_summary.strip():
            lines.append("")
            lines.append(self.raw_summary.strip())
        return "\n".join(lines)


def load_founder_profile(data_dir: Path) -> FounderProfile:
    """Read the profile from ``<data_dir>/founder_profile.json``.
    Returns an empty FounderProfile when the file doesn't exist or
    can't be parsed — callers can always call ``.as_prompt_preamble()``
    safely."""
    path = data_dir / "founder_profile.json"
    if not path.exists():
        return FounderProfile()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return FounderProfile()
    if not isinstance(raw, dict):
        return FounderProfile()

    known_keys = {
        "decision_style", "risk_tolerance", "communication_preferences",
        "strengths", "blindspots", "operating_rhythm", "raw_summary",
    }
    extra = {k: v for k, v in raw.items() if k not in known_keys}

    def _str(key: str) -> str:
        v = raw.get(key)
        return v if isinstance(v, str) else ""

    def _tuple(key: str) -> tuple[str, ...]:
        v = raw.get(key)
        if not isinstance(v, list):
            return ()
        return tuple(str(x) for x in v if isinstance(x, (str, int, float)))

    return FounderProfile(
        decision_style=_str("decision_style"),
        risk_tolerance=_str("risk_tolerance"),
        communication_preferences=_str("communication_preferences"),
        strengths=_tuple("strengths"),
        blindspots=_tuple("blindspots"),
        operating_rhythm=_str("operating_rhythm"),
        raw_summary=_str("raw_summary"),
        extra=extra,
    )


__all__ = ["FounderProfile", "load_founder_profile"]
