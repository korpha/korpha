# HTTP API reference

**Audience**: anyone integrating Korpha with another tool (a
custom UI, a Slack app, automated tests, a shell script).

The dashboard server (`korpha server`) exposes a small JSON API
at `http://localhost:8765` plus the HTML dashboard under `/app`.
Swagger / OpenAPI spec auto-generated at `/docs`.

> **Note**: the API has **no auth** by default — assumes localhost
> single-user. If you bind it with `--host 0.0.0.0` (LAN exposure),
> add a reverse proxy with auth in front. SaaS-mode auth lands in
> Phase 4.

---

## Health + identity

### `GET /healthz`

Liveness check. Returns `{"status": "ok"}` when the server is up.

```bash
curl http://localhost:8765/healthz
# → {"status": "ok"}
```

### `GET /me`

Current founder + business + spend rollup.

```bash
curl http://localhost:8765/me
```

Response:
```json
{
  "founder": {"id": "uuid", "email": "you@x.com", "display_name": "Mike"},
  "business": {"id": "uuid", "name": "WidgetCo", "status": "live"},
  "spend": {"today": "0.0312", "week": "0.41", "month": "1.84"}
}
```

---

## Conversation (CEO chat)

### `POST /ask`

Send a message to CEO. Returns a complete response (non-streaming).

```bash
curl -X POST http://localhost:8765/ask \
  -H 'Content-Type: application/json' \
  -d '{"message": "Help me pick a niche"}'
```

Response:
```json
{
  "content": "Here's the plan...",
  "skills_used": [{"skill_name": "niche.find_micro_niches", "summary": "..."}],
  "cost_usd": 0.0,
  "reasoning": null
}
```

### `POST /ask/stream`

Same as `/ask`, but Server-Sent Events. Each chunk is delivered as
the model generates. Best for UIs that want token-by-token streaming.

```bash
curl -N -X POST http://localhost:8765/ask/stream \
  -H 'Content-Type: application/json' \
  -d '{"message": "Help me pick a niche"}'
```

Stream format:
```
event: chunk
data: {"text": "Here"}

event: chunk
data: {"text": "'s the"}

...

event: done
data: {"cost_usd": 0.0, "reasoning": null}
```

### `POST /propose`

Ask CEO to produce a structured multi-task plan (vs free-form chat).

```bash
curl -X POST http://localhost:8765/propose \
  -H 'Content-Type: application/json' \
  -d '{"message": "Plan a parallel push: landing page + interviews + analytics."}'
```

Response includes the `[CTO] / [CMO] / [COO]` task tags + dispatch state.

---

## Approvals

### `GET /approvals/pending`

List pending approvals.

```bash
curl http://localhost:8765/approvals/pending
```

### `POST /approvals/{id}/approve`

Approve a pending action — the side effect fires.

```bash
curl -X POST http://localhost:8765/approvals/<approval_id>/approve
```

### `POST /approvals/{id}/reject`

Reject a pending action.

```bash
curl -X POST http://localhost:8765/approvals/<approval_id>/reject
```

### `POST /approvals/{id}/execute`

Explicit re-execute (rare). For retrying after a transient failure.

```bash
curl -X POST http://localhost:8765/approvals/<approval_id>/execute
```

---

## Blockers

### `GET /blockers`

Open blockers + the Chief of Staff digest.

```bash
curl http://localhost:8765/blockers
```

Response:
```json
{
  "open": [{"id": "uuid", "kind": "missing_input", "summary": "..."}],
  "digest_text": "...",
  "auto_resolved": 3,
  "dropped": 0
}
```

---

## Skills

### `GET /skills`

List all registered skills (built-in + YAML-loaded).

```bash
curl http://localhost:8765/skills
```

Response:
```json
[
  {
    "name": "niche.find_micro_niches",
    "description": "Pick promising micro-niches given...",
    "parameters": {
      "skills": "comma-separated, e.g. 'Python, B2B SaaS'",
      "time_budget_hours": "hours/week realistically available",
      "savings_usd": "cash on the line"
    }
  },
  ...
]
```

### `POST /skills/{name}/run`

Invoke a skill directly. Same semantics as `korpha skill run` CLI.

```bash
curl -X POST http://localhost:8765/skills/niche.find_micro_niches/run \
  -H 'Content-Type: application/json' \
  -d '{
    "args": {
      "skills": "Python, FastAPI",
      "time_budget_hours": 5,
      "savings_usd": 2000
    }
  }'
```

Response:
```json
{
  "skill_name": "niche.find_micro_niches",
  "summary": "5 candidates ranked by fit...",
  "payload": {"candidates": [...]},
  "cost_usd": 0.0
}
```

---

## Themes (dashboard customization)

See [`THEMES.md`](THEMES.md) for the user-facing flow.

### `GET /api/dashboard/themes`

List built-in + user themes plus the active one. User themes
include their full `definition` inline so a client can render
palette swatches without a second call.

```bash
curl http://localhost:8765/api/dashboard/themes
```

Response:
```json
{
  "active": "default",
  "themes": [
    {
      "name": "default",
      "label": "Korpha Dark",
      "description": "...",
      "is_builtin": true,
      "definition": null
    },
    {
      "name": "ocean",
      "label": "Ocean",
      "description": "...",
      "is_builtin": false,
      "definition": { "palette": {...}, "typography": {...}, ... }
    }
  ]
}
```

### `PUT /api/dashboard/theme`

Set the active theme. Validates the name exists; returns 404 if not.

```bash
curl -X PUT http://localhost:8765/api/dashboard/theme \
  -H 'Content-Type: application/json' \
  -d '{"name": "midnight"}'
```

---

## Dashboard pages (HTML)

The HTML pages under `/app` are the human-facing dashboard, not API.
They return HTML, not JSON, and use HTMX for partial-update
interactions. Listed for completeness:

| Route | Page |
| --- | --- |
| `GET /app/dashboard` | Home view (KPIs, agent cards, recent activity) |
| `GET /app/inbox` | CEO conversation thread |
| `GET /app/issues` | Linear-style work list |
| `GET /app/issues/{ref}` | Issue detail |
| `GET /app/agents` | Org chart (CEO + Directors + Workers) |
| `GET /app/agents/{id}` | Per-agent panel (instructions / runs / config / budget) |
| `GET /app/approvals` | Pending approvals list |
| `GET /app/approvals/{id}/preview` | Render-as-page preview (e.g. landing copy) |
| `GET /app/skills` | Skill catalog + runner UI |
| `GET /app/routines` | Scheduled work |
| `GET /app/goals` | Goals + projects |
| `GET /app/costs` | Spend rollup + per-provider breakdown |
| `GET /app/activity` | Audit log |
| `GET /app/settings` | Trust envelopes + integrations + theme settings |

---

## Authentication

**There isn't any.** Korpha runs locally; the API is bound to
`localhost:8765` by default. If you `--host 0.0.0.0` to expose to
the LAN, put a reverse proxy (Caddy / Tailscale / nginx) with auth
in front of it.

SaaS-mode multi-tenant auth is on the Phase 4 NEXT_STEPS roadmap.
The DB schema is already multi-company-ready (per the
`Multi-company schema` task done back in #47); the missing piece
is auth + tenant isolation at the HTTP layer.

---

## Live OpenAPI spec

```bash
# Swagger UI
open http://localhost:8765/docs

# Raw OpenAPI JSON
curl http://localhost:8765/openapi.json
```

The spec is auto-generated from the FastAPI route handlers — always
in sync with the running code, more authoritative than this doc if
they ever disagree.

---

## Reference

- Server source: [`korpha/api/server.py`](../korpha/api/server.py)
- Dashboard router: [`korpha/api/dashboard.py`](../korpha/api/dashboard.py)
- Pydantic response shapes: same files (search for `class *Response`)
- Tests: [`tests/test_api.py`](../tests/test_api.py)
