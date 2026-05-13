"""LinePack contract — the protocol community-shipped packs implement.

See ``docs/PRODUCT_LIFECYCLE.md`` §"How Line Packs Implement This Document"
for the YAML pack format that builds on this Python contract.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlmodel import Session

from korpha.business_units.model import (
    BusinessUnit, BusinessUnitKind, NicheProfile,
)
from korpha.credentials.model import ExternalServiceKind


class LinePackError(Exception):
    """Raised when a pack tries to configure an incompatible unit
    (e.g. installing a KDP pack on a POD line)."""


@dataclass(frozen=True)
class KpiDefinition:
    """One KPI a Line tracks. The Line VP's monthly review surfaces
    these. v1 just stores definitions on the unit's ``config`` JSON;
    a future PR builds the actual reporting view."""

    key: str            # "mrr" / "bsr_top_books" / "list_size" / etc.
    label: str
    unit: str           # "usd_per_month" / "rank" / "count" / "pct"
    target: float | None = None


class LinePack(ABC):
    """Subclass + register with ``default_registry`` to ship a pack."""

    # ---- pack metadata ----

    @property
    @abstractmethod
    def pack_id(self) -> str:
        """e.g. ``'pod-line-pack@1.0.0'``."""

    @property
    @abstractmethod
    def line_kind(self) -> str:
        """One of: pod | kdp | info | saas | affiliate | agency."""

    @property
    def description(self) -> str:
        return ""

    # ---- defaults the pack ships ----

    @abstractmethod
    def default_niche_profile(self) -> NicheProfile:
        """Pre-filled NicheProfile for new units created with this pack."""

    @abstractmethod
    def kpi_definitions(self) -> list[KpiDefinition]:
        """KPIs this line cares about. Monthly review consumes."""

    @abstractmethod
    def suggested_worker_specialties(self) -> list[str]:
        """Worker specialties the Line VP typically hires. Used by
        the future worker-hire suggestions in /app/units."""

    def required_services(self) -> list[ExternalServiceKind]:
        """External services the pack expects to have credentials for.
        Setup wizard prompts the founder for any that aren't configured
        on the line's unit (or company default)."""
        return []

    def default_support_autonomy_level(self) -> int:
        """Customer Support Autonomy Ladder default per
        PRODUCT_LIFECYCLE.md: 0=forward, 1=draft, 2=threshold,
        3=full FAQ, 4=full+actions."""
        return 3

    def initial_kanban_cards(self) -> list[dict[str, Any]]:
        """Optional preset cards the Line VP creates on spawn. Each
        dict shape: {title, body?, priority?, acceptance_criteria?}."""
        return []

    # ---- lifecycle hooks ----

    def setup_unit(
        self, session: Session, unit: BusinessUnit,
    ) -> None:
        """Apply pack defaults to a freshly-created BusinessUnit.

        Default implementation: write niche_profile + KPI defs +
        autonomy level + worker suggestions to unit.config + commit.
        Subclasses can override for richer behavior.
        """
        profile = self.default_niche_profile()
        kpis = self.kpi_definitions()
        cfg = dict(unit.config or {})
        cfg["playbook_pack_id"] = self.pack_id
        cfg["kpis"] = [
            {
                "key": k.key, "label": k.label,
                "unit": k.unit, "target": k.target,
            }
            for k in kpis
        ]
        cfg["support_autonomy_level"] = (
            self.default_support_autonomy_level()
        )
        cfg["suggested_worker_specialties"] = (
            self.suggested_worker_specialties()
        )
        cfg["required_services"] = [
            s.value for s in self.required_services()
        ]
        unit.config = cfg
        unit.niche_profile = profile.model_dump(mode="json")
        unit.playbook_skill_pack = self.pack_id
        session.add(unit)
        session.commit()
        session.refresh(unit)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class LinePackRegistry:
    """Discoverable map of registered LinePack instances by pack_id."""

    def __init__(self) -> None:
        self._packs: dict[str, LinePack] = {}

    def register(self, pack: LinePack) -> None:
        self._packs[pack.pack_id] = pack

    def get(self, pack_id: str) -> LinePack | None:
        return self._packs.get(pack_id)

    def for_line(self, line_kind: str) -> list[LinePack]:
        """All packs serving this line. Lets the dashboard show
        multiple packs available for KDP (e.g. KDP Romance Type Pack
        + community alternatives)."""
        return [
            p for p in self._packs.values() if p.line_kind == line_kind
        ]

    def all(self) -> list[LinePack]:
        return list(self._packs.values())

    def reset(self) -> None:
        self._packs.clear()


default_registry = LinePackRegistry()


__all__ = [
    "KpiDefinition",
    "LinePack",
    "LinePackError",
    "LinePackRegistry",
    "default_registry",
]
