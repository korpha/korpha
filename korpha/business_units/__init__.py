"""Business Units — recursive org tree under each Business.

A ``BusinessUnit`` is a node in a self-referential tree representing one
slice of a founder's portfolio: a Line (KDP, POD, SaaS, …), a Type
(Romance under KDP, T-Shirts under POD), an Audience (AI-marketers
segment under Affiliate), a Series (Highland Rogue under Romance), etc.

The tree shape is *fractal*: a Line can contain Types, a Type can
contain Series, a Series can contain Niches. The agents decide depth,
not the founder. Each node has an optional owner agent role, an
optional playbook (skill bundle from the hub), an optional niche
profile (audience compatibility metadata), and its own memory
namespace (hard-isolated from siblings — see ``BUSINESS_UNITS.md``).

``Product`` is the *leaf* under a BusinessUnit — a specific book,
design, course, SaaS app, or affiliate campaign. Products never have
children; they're operational work output, not org structure.

This package ships only the data model in PR1. CRUD + tree operations
land in ``board.py`` (alongside KanbanBoard's pattern). HR skills
(``hr.start_business_line`` etc.) land in PR6. Memory namespace
enforcement lands in PR9.
"""
from korpha.business_units.model import (
    BusinessUnit,
    BusinessUnitKind,
    DeploymentMode,
    NicheProfile,
    Product,
    ProductKind,
)

__all__ = [
    "BusinessUnit",
    "BusinessUnitKind",
    "DeploymentMode",
    "NicheProfile",
    "Product",
    "ProductKind",
]
