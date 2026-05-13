"""HTTP API surface — FastAPI app exposing the cofounder loop over HTTP.

Single-user, self-hosted: runs on localhost with no auth (bind defaults
to 127.0.0.1). Operators who want auth / TLS / etc. should put a
reverse proxy in front.

Endpoints:

- ``GET  /healthz`` — liveness
- ``GET  /me`` — founder + business state
- ``POST /ask`` — Founder message → CEO.handle (skill-aware) reply
- ``POST /propose`` — CEO drafts a Plan + creates a pending Approval
- ``GET  /approvals/pending`` — list pending Approvals
- ``POST /approvals/{id}/approve`` — Founder approves
- ``POST /approvals/{id}/reject`` — Founder rejects
- ``POST /approvals/{id}/execute`` — Workforce dispatches the approved Plan
- ``GET  /blockers`` — CoS digest + open blockers
- ``GET  /skills`` — list available skills
- ``POST /skills/{name}/run`` — invoke a skill directly with args
"""
from __future__ import annotations

from korpha.api.server import build_app

__all__ = ["build_app"]
