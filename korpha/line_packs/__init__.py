"""Line Pack contract + 6 reference packs (POD, KDP, Info, SaaS,
Affiliate, Agency).

A Line Pack is a packaged playbook for one business line. It bundles:

  * Default ``NicheProfile`` (core/adjacent/off-limits topics for the
    audience this line typically serves)
  * Default KPI shape (what metrics matter for this line — MRR for
    SaaS, BSR for KDP, design publishes/wk for POD, list size for
    Affiliate, etc.)
  * Suggested worker specialties for the Line VP to hire
  * Optional preset kanban cards the Line VP creates on spawn
  * Required external services (KDP needs KDP_API + amazon_ads;
    Affiliate needs an ESP)
  * Customer support default autonomy level per PRODUCT_LIFECYCLE.md

When ``hr.start_business_line(kind=..., playbook=...)`` runs with a
``playbook`` pack id, the matching LinePack's ``setup_unit`` hook runs
to configure the new unit with these defaults.

The 6 reference packs ship as builtins; the community can publish
additional packs (KDP Romance Type Pack, etc.) via the skill hub.
"""
from korpha.line_packs.contract import (
    LinePack,
    LinePackError,
    LinePackRegistry,
    default_registry,
)
from korpha.line_packs.builtin import (
    AffiliateLinePack,
    AgencyLinePack,
    InfoLinePack,
    KdpLinePack,
    PodLinePack,
    SaasLinePack,
)

__all__ = [
    "AffiliateLinePack",
    "AgencyLinePack",
    "InfoLinePack",
    "KdpLinePack",
    "LinePack",
    "LinePackError",
    "LinePackRegistry",
    "PodLinePack",
    "SaasLinePack",
    "default_registry",
]
