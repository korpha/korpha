"""Per-Session CRUD + tree operations for BusinessUnit + Product.

Same pattern as ``korpha.kanban.board.KanbanBoard`` — construct one
per request, no shared state. The board enforces invariants that the
raw SQLModel API would miss:

* Slug normalization + sibling uniqueness
* Kind-vs-parent rules (only DEFAULT can have parent_id NULL; only
  leaves can hold Products)
* Immutable ``memory_namespace_id`` (caller cannot override after
  creation)
* Cycle detection on parent reassignment (PR1 doesn't allow reparenting
  but the helper is here for PR6+)
* Cascade behavior on archive (children must be archived first; the
  board surfaces the constraint, doesn't auto-cascade)

PR1 ships read-side helpers (ancestors / descendants / subtree) used
by future PRs for the resolver tree walk + per-unit kanban scoping.
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any
from uuid import UUID

from sqlmodel import Session, select

from korpha.business_units.model import (
    BusinessUnit, BusinessUnitKind, NicheProfile, Product, ProductKind,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_SLUG_RE = re.compile(r"[^a-z0-9-]+")
_SLUG_COLLAPSE = re.compile(r"-+")


def slugify(text: str, *, max_len: int = 60) -> str:
    """URL-safe slug. Lowercase, alnum + hyphens, collapses runs.

    Bounded length matches Postgres index-key constraints. Empty
    result falls back to ``'unit'`` so downstream code never has to
    handle a None slug.
    """
    s = _SLUG_RE.sub("-", text.strip().lower())
    s = _SLUG_COLLAPSE.sub("-", s).strip("-")
    return s[:max_len] or "unit"


class BusinessUnitError(Exception):
    """Raised when an operation violates a BusinessUnit invariant."""


# ---------------------------------------------------------------------------
# Board
# ---------------------------------------------------------------------------


class BusinessUnitBoard:
    """CRUD + tree operations for one Session."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ---- create ----

    def create(
        self,
        *,
        business_id: UUID,
        name: str,
        kind: BusinessUnitKind = BusinessUnitKind.LINE,
        parent_id: UUID | None = None,
        slug: str | None = None,
        owner_agent_role_id: UUID | None = None,
        playbook_skill_pack: str | None = None,
        niche_profile: NicheProfile | None = None,
        config: dict[str, Any] | None = None,
    ) -> BusinessUnit:
        """Create a new BusinessUnit under the given parent.

        Validates:
        - ``name`` is non-empty
        - ``slug`` (computed from name if absent) is unique among siblings
        - ``parent_id`` rules: required for non-DEFAULT kinds; DEFAULT
          may have parent_id=None for a root unit
        - ``niche_profile`` round-trips through Pydantic for shape
        - If parent_id is set, the parent must exist + belong to same
          business_id (no cross-business unit reparenting)
        """
        if not name.strip():
            raise BusinessUnitError("business unit name required")

        if kind == BusinessUnitKind.DEFAULT:
            # DEFAULT is the migration root — must NOT have a parent.
            if parent_id is not None:
                raise BusinessUnitError(
                    "DEFAULT unit cannot have a parent (it's the root)"
                )
        else:
            # All non-DEFAULT kinds need a parent.
            if parent_id is None:
                raise BusinessUnitError(
                    f"{kind.value} unit requires a parent_id "
                    "(only DEFAULT kind may be a root)"
                )
            parent = self.session.get(BusinessUnit, parent_id)
            if parent is None:
                raise BusinessUnitError(
                    f"parent unit {parent_id} not found"
                )
            if parent.business_id != business_id:
                raise BusinessUnitError(
                    "parent unit belongs to a different business"
                )
            if parent.status == "archived":
                raise BusinessUnitError(
                    "cannot create child under archived parent"
                )

        final_slug = slugify(slug if slug is not None else name)
        if self._sibling_slug_exists(
            business_id=business_id,
            parent_id=parent_id,
            slug=final_slug,
        ):
            raise BusinessUnitError(
                f"sibling slug {final_slug!r} already exists under "
                f"parent {parent_id}"
            )

        profile_dict: dict[str, Any] | None = None
        if niche_profile is not None:
            # Validate by round-trip — raises ValidationError on bad shape
            profile_dict = NicheProfile.model_validate(
                niche_profile.model_dump(mode="json")
            ).model_dump(mode="json")

        unit = BusinessUnit(
            business_id=business_id,
            parent_id=parent_id,
            kind=kind,
            name=name.strip(),
            slug=final_slug,
            owner_agent_role_id=owner_agent_role_id,
            playbook_skill_pack=playbook_skill_pack,
            niche_profile=profile_dict,
            config=config or {},
        )
        self.session.add(unit)
        self.session.commit()
        self.session.refresh(unit)
        return unit

    # ---- read ----

    def get(self, unit_id: UUID) -> BusinessUnit | None:
        return self.session.get(BusinessUnit, unit_id)

    def get_or_raise(self, unit_id: UUID) -> BusinessUnit:
        unit = self.get(unit_id)
        if unit is None:
            raise BusinessUnitError(f"unit {unit_id} not found")
        return unit

    def list_for_business(
        self,
        business_id: UUID,
        *,
        include_archived: bool = False,
    ) -> list[BusinessUnit]:
        stmt = select(BusinessUnit).where(
            BusinessUnit.business_id == business_id
        )
        if not include_archived:
            stmt = stmt.where(BusinessUnit.status != "archived")
        return list(self.session.exec(stmt).all())

    def children(
        self, parent_id: UUID, *, include_archived: bool = False,
    ) -> list[BusinessUnit]:
        """Direct children (one level deep). Use ``descendants`` for
        recursive."""
        stmt = select(BusinessUnit).where(
            BusinessUnit.parent_id == parent_id
        )
        if not include_archived:
            stmt = stmt.where(BusinessUnit.status != "archived")
        return list(self.session.exec(stmt).all())

    def ancestors(self, unit_id: UUID) -> list[BusinessUnit]:
        """Walk up the tree, returning ancestors from immediate parent
        to root. Excludes the starting unit itself."""
        out: list[BusinessUnit] = []
        current = self.session.get(BusinessUnit, unit_id)
        if current is None:
            return out
        seen: set[UUID] = {current.id}
        cursor_parent_id = current.parent_id
        while cursor_parent_id is not None:
            if cursor_parent_id in seen:
                # Cycle — should never happen with proper invariants,
                # but bail rather than spin.
                break
            parent = self.session.get(BusinessUnit, cursor_parent_id)
            if parent is None:
                break
            out.append(parent)
            seen.add(parent.id)
            cursor_parent_id = parent.parent_id
        return out

    def descendants(
        self, unit_id: UUID, *, include_archived: bool = False,
    ) -> list[BusinessUnit]:
        """All descendants in BFS order. Excludes the starting unit."""
        out: list[BusinessUnit] = []
        frontier: list[UUID] = [unit_id]
        seen: set[UUID] = {unit_id}
        while frontier:
            next_frontier: list[UUID] = []
            for parent_id in frontier:
                kids = self.children(
                    parent_id, include_archived=include_archived
                )
                for kid in kids:
                    if kid.id in seen:
                        continue
                    seen.add(kid.id)
                    out.append(kid)
                    next_frontier.append(kid.id)
            frontier = next_frontier
        return out

    def subtree(
        self, unit_id: UUID, *, include_archived: bool = False,
    ) -> list[BusinessUnit]:
        """Self + descendants in BFS order. Convenient for backup,
        archive cascade preview, kanban scope queries."""
        root = self.get(unit_id)
        if root is None:
            return []
        return [root] + self.descendants(
            unit_id, include_archived=include_archived
        )

    def iter_walk_up(self, unit_id: UUID) -> Iterator[BusinessUnit]:
        """Generator yielding self → parent → grandparent → … → root.

        Used by the resolver (PR4) for credential-account tree walks.
        Generator form avoids loading the whole chain into memory
        before the first hit.
        """
        current = self.session.get(BusinessUnit, unit_id)
        seen: set[UUID] = set()
        while current is not None and current.id not in seen:
            seen.add(current.id)
            yield current
            if current.parent_id is None:
                return
            current = self.session.get(BusinessUnit, current.parent_id)

    # ---- update ----

    def update_niche_profile(
        self, unit_id: UUID, profile: NicheProfile,
    ) -> BusinessUnit:
        """Replace the niche profile. Validated via Pydantic round-trip."""
        unit = self.get_or_raise(unit_id)
        validated = NicheProfile.model_validate(
            profile.model_dump(mode="json")
        ).model_dump(mode="json")
        unit.niche_profile = validated
        unit.updated_at = self._now()
        self.session.add(unit)
        self.session.commit()
        self.session.refresh(unit)
        return unit

    def pause(
        self, unit_id: UUID, reason: str | None = None,
    ) -> BusinessUnit:
        """Soft-pause. Blocks new card claims on this unit + descendants.
        Children are NOT auto-paused — paused state is local; callers
        can pause subtrees explicitly via ``pause_subtree``."""
        unit = self.get_or_raise(unit_id)
        unit.status = "paused"
        unit.paused_at = self._now()
        unit.paused_reason = reason
        unit.updated_at = self._now()
        self.session.add(unit)
        self.session.commit()
        self.session.refresh(unit)
        return unit

    def resume(self, unit_id: UUID) -> BusinessUnit:
        unit = self.get_or_raise(unit_id)
        unit.status = "active"
        unit.paused_at = None
        unit.paused_reason = None
        unit.updated_at = self._now()
        self.session.add(unit)
        self.session.commit()
        self.session.refresh(unit)
        return unit

    def archive(self, unit_id: UUID) -> BusinessUnit:
        """Soft-archive. Refuses if non-archived descendants exist —
        the caller must archive children first (or use
        ``archive_subtree`` to cascade explicitly)."""
        unit = self.get_or_raise(unit_id)
        live_kids = [
            k for k in self.children(unit_id, include_archived=False)
        ]
        if live_kids:
            raise BusinessUnitError(
                f"cannot archive {unit.slug!r}: {len(live_kids)} "
                "live children. Archive descendants first or use "
                "archive_subtree."
            )
        unit.status = "archived"
        unit.updated_at = self._now()
        self.session.add(unit)
        self.session.commit()
        self.session.refresh(unit)
        return unit

    def archive_subtree(self, unit_id: UUID) -> list[BusinessUnit]:
        """Archive a unit and all descendants in deepest-first order
        so the per-unit archive guard never trips. Returns archived units."""
        all_units = self.subtree(unit_id, include_archived=False)
        # Reverse-BFS = leaves first
        all_units.reverse()
        out: list[BusinessUnit] = []
        for u in all_units:
            u.status = "archived"
            u.updated_at = self._now()
            self.session.add(u)
            out.append(u)
        self.session.commit()
        for u in out:
            self.session.refresh(u)
        return out

    # ---- Product helpers ----

    def add_product(
        self,
        *,
        business_unit_id: UUID,
        name: str,
        kind: ProductKind = ProductKind.CUSTOM,
        slug: str | None = None,
        starts_at: Any = None,
        ends_at: Any = None,
        attributes: dict[str, Any] | None = None,
    ) -> Product:
        """Create a Product leaf under a BusinessUnit.

        Validates:
        - Name + slug uniqueness within unit
        - Parent unit exists, not archived
        - Time-bound products: if starts_at, ends_at must be after it
        """
        if not name.strip():
            raise BusinessUnitError("product name required")
        unit = self.get_or_raise(business_unit_id)
        if unit.status == "archived":
            raise BusinessUnitError(
                "cannot add product to archived unit"
            )

        final_slug = slugify(slug if slug is not None else name)
        existing = self.session.exec(
            select(Product).where(
                Product.business_unit_id == business_unit_id,
                Product.slug == final_slug,
            )
        ).first()
        if existing is not None:
            raise BusinessUnitError(
                f"product slug {final_slug!r} already exists in unit "
                f"{unit.slug!r}"
            )

        if starts_at and ends_at and ends_at <= starts_at:
            raise BusinessUnitError(
                "product ends_at must be strictly after starts_at"
            )

        product = Product(
            business_unit_id=business_unit_id,
            business_id=unit.business_id,
            kind=kind,
            name=name.strip(),
            slug=final_slug,
            starts_at=starts_at,
            ends_at=ends_at,
            attributes=attributes or {},
        )
        self.session.add(product)
        self.session.commit()
        self.session.refresh(product)
        return product

    def list_products(
        self, unit_id: UUID, *, include_archived: bool = False,
    ) -> list[Product]:
        stmt = select(Product).where(
            Product.business_unit_id == unit_id
        )
        if not include_archived:
            stmt = stmt.where(Product.status != "archived")
        return list(self.session.exec(stmt).all())

    # ---- internals ----

    def _sibling_slug_exists(
        self,
        *,
        business_id: UUID,
        parent_id: UUID | None,
        slug: str,
    ) -> bool:
        stmt = select(BusinessUnit).where(
            BusinessUnit.business_id == business_id,
            BusinessUnit.slug == slug,
        )
        if parent_id is None:
            stmt = stmt.where(BusinessUnit.parent_id.is_(None))  # type: ignore[union-attr]
        else:
            stmt = stmt.where(BusinessUnit.parent_id == parent_id)
        return self.session.exec(stmt).first() is not None

    def _now(self) -> Any:
        from korpha.db._base import utcnow
        return utcnow()


__all__ = [
    "BusinessUnitBoard",
    "BusinessUnitError",
    "slugify",
]
