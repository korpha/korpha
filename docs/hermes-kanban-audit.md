# Hermes kanban vs Korpha kanban — audit (2026-05-15)

Hermes upstream pulled fresh at v0.13.0 (tag `v2026.5.7`). Our local
`/home/code4/aigenteur_agent/hermes/` was at v0.12.0 — 4 days behind.
This audit reads the latest upstream code from `/tmp/hermes-latest/`
and compares to Korpha's kanban implementation under
`korpha/kanban/`, `korpha/skills/kanban_skills.py`, and
`korpha/cofounder/{workforce,auto_dispatch}.py`.

**Goal:** find where each implementation is stronger and decide what
to port from Hermes into Korpha. Where Korpha is better, hold the
line and document why.

---

## Code-size baseline

| | Hermes 0.13 | Korpha (today) |
|---|---|---|
| Core model + ops | `hermes_cli/kanban_db.py` (4,839 lines) | `korpha/kanban/{model,board,artifacts,refs}.py` (1,170 lines) |
| Tool / skill wrappers | `tools/kanban_tools.py` (1,139) | `korpha/skills/kanban_skills.py` (926) |
| CLI | `hermes_cli/kanban.py` (2,252) | inside `korpha/cli.py` (~400 of kanban CLI) |
| Triage→Spec | `hermes_cli/kanban_specify.py` (NEW in 0.13) | — |
| Diagnostics | `hermes_cli/kanban_diagnostics.py` (NEW in 0.13) | — |
| Dispatcher / workforce | embedded in gateway/run.py | `korpha/cofounder/workforce.py` (814) + `auto_dispatch.py` (342) |
| **Total** | **~10,000 lines** | **~3,250 lines** |

Hermes is ~3× the size. The extra mass is mostly: claim-TTL machinery,
crash detection, comments/links tables, diagnostics, worker context
builder, embedded dispatcher.

---

## State machine

**Hermes:** `triage → todo → ready → running → blocked → done → archived`

**Korpha:** `BACKLOG → SPECIFY → READY → IN_PROGRESS → REVIEW → DONE` + `BLOCKED, ARCHIVED`

| Aspect | Hermes | Korpha | Verdict |
|---|---|---|---|
| Pre-spec stage | `triage` — rough idea | `BACKLOG` — same | tied |
| Spec stage | implicit in `todo` (auto-promoted) | explicit `SPECIFY` column with criteria + owner gate | **Korpha** for clarity |
| Pre-claim | `ready` (parents-done gate) | `READY` (no parent gate) | **Hermes** for dependency awareness |
| Active | `running` | `IN_PROGRESS` | tied |
| Verification | none — work goes `running → done` | `REVIEW` column with `review_evidence` gate | **Korpha (big)** — anti-hallucination critical for our use case |
| Terminal | `done` / `blocked` / `archived` | same | tied |

**Korpha holds REVIEW.** Hermes's `running → done` is too optimistic
for solopreneur workflows where a CEO claim of "shipped the listing"
without evidence is a disaster. Keep our column.

---

## Claim model

**Hermes:** atomic CAS on `tasks.status` + `tasks.claim_lock`. Stale
claim TTL = 15 minutes. Worker calls `heartbeat_claim()` to extend.
Crashed worker → claim reclaimed on next tick.

**Korpha:** `KanbanCard.claimed_by_agent_role_id` set on claim. No
TTL, no heartbeat, no crash recovery.

**Verdict: Hermes wins big.** This is the single biggest reliability
gap. Our IN_PROGRESS cards from yesterday sat overnight because no
mechanism reclaimed them when the in-process director crashed/never
ran. Port priority **#1**.

---

## Dispatcher

**Hermes:** `_kanban_dispatcher_watcher` embedded in the gateway. One
tick per `dispatch_interval_seconds` (default 60s). Each tick:
1. Reclaim stale claims (TTL expired)
2. Detect crashed workers (PID check + waitpid + protocol-violation auto-block)
3. Enforce max-runtime
4. Promote `todo → ready` where all parent deps are done
5. Claim + spawn workers up to `max_spawn` (live concurrency cap)
6. Failure counter per task → auto-block after N consecutive failures

**Korpha (after today's PR 2ed8937b):** three trigger modes —
`inline` (skill calls dispatch), `cron` (preset `add-card-dispatcher`,
default 5min), `hook` (POST_SKILL_CALL listener). Metadata stamp for
idempotency. No PID tracking, no crash detection, no failure counter,
no max-runtime, no parent-gate, no live concurrency cap.

**Verdict: Hermes wins big.** Our three modes are clever but each
trigger is shallow. We need the loop body. Port priority **#1**
alongside claim TTL.

---

## Worker context

**Hermes:** `build_worker_context(task_id)` returns a structured
prompt block:
- title + body (capped at 8 KB)
- prior attempts on this task (10 most recent, 4 KB per field)
- parent task results (handoffs from done deps)
- assignee's role-history across other tasks
- comment thread (30 most recent, 2 KB per comment)

All bounded so prompts stay ≤100 KB even on pathological boards.

**Korpha:** `Workforce.dispatch()` passes the raw task title string
(plus role tag) to executors. Executors look up the card by title.
No structured prompt, no prior-attempts context, no parent results,
no comments.

**Verdict: Hermes wins big.** This is the second-biggest gap and
explains why Korpha agents will repeat work or miss context when
they pick up a card. Port priority **#2**.

---

## Triage → Spec (NEW in Hermes 0.13)

**Hermes:** `kanban_specify.py` + `hermes kanban specify [id|--all]`.
Auxiliary LLM call takes a one-line triage task and produces:
- tightened title (only if materially better)
- body with goal + proposed approach + acceptance criteria

Then flips `triage → todo`. One-shot, lenient JSON parse, no retry.

**Korpha:** `kanban.specify_card` skill exists but requires the
caller to supply criteria + owner. No LLM-driven spec generation.

**Verdict: Hermes wins.** Worth porting because the CEO often
creates cards with just titles (from `business.bootstrap_from_brief`)
— specifying them automatically would close the loop. Port priority
**#4** (medium — useful but Korpha works without it).

---

## Diagnostics (NEW in Hermes 0.13)

**Hermes:** `kanban_diagnostics.py` — pure stateless rules over
`(task, events, runs)` emitting `Diagnostic(kind, severity, title,
detail, actions)`. Surfaces: hallucinated card-id, spawn crash-loop,
stuck-blocked, etc. Auto-clears when underlying issue resolves.
Dashboard renders structured actions as buttons.

**Korpha:** Liveness classifier (PR #202) flags stuck cards but no
structured diagnostic shape, no auto-clearing, no actions.

**Verdict: Hermes wins.** Port priority **#3** (the diagnostic
shape + a few rules would dramatically improve /app/kanban UX
when things go wrong).

---

## Task linking (parent → child dependency gate)

**Hermes:** `link_tasks(parent_id, child_id)` + `recompute_ready()`
walk parent edges and only promote `todo → ready` when all parents
are `done`. Dependency-aware execution out of the box.

**Korpha:** `kanban_card_ref` (PR #207) stores #-mentions but
**doesn't gate readiness**. A card whose dependency hasn't shipped
still becomes READY.

**Verdict: Hermes wins.** Port priority **#5**. Critical for multi-step
flows like "draft listing → upload to KDP → publish" where order
matters.

---

## Storage layout

**Hermes:** dedicated `$HERMES_HOME/kanban.db` (SQLite, WAL mode).
Profile-agnostic — multiple processes / profiles share one board.
The DB **is** the coordination primitive.

**Korpha:** SQLModel tables in the main `korpha.db`. Single FastAPI
process; no cross-process write contention; per-business isolation
via `business_id`.

**Verdict: Different requirements.** Hermes needs multi-process
because workers spawn as separate Python processes. Korpha runs the
director in-process inside the API server. Keep our shape; if we
ever spawn out-of-process workers, revisit.

---

## Comments / discussion

**Hermes:** `task_comments` table; `add_comment(task_id, author,
body)`. Workers leave breadcrumbs; humans + agents discuss in-task.

**Korpha:** `kanban_card_event` has a `note` field but no first-class
comments. Agent breadcrumbs go in `Activity` rows (separate concern).

**Verdict: Hermes wins for collaboration.** Port priority **#6** (low —
not blocking, but nice).

---

## Korpha-only strengths (DO NOT replace with Hermes pattern)

1. **REVIEW column + evidence gate** — Hermes goes straight to done.
   Our anti-hallucination architecture depends on REVIEW. Keep.
2. **Approval coupling** — `kanban_card.approval_id` ties high-risk
   cards (commerce, marketing publish) to an Approval row that
   requires founder consent. Hermes has no equivalent. Keep.
3. **business_unit_id scoping** — multi-business / multi-line
   support. Hermes is single-profile per board. Keep.
4. **Line Pack defaults** (PR #227) — kanban cards inherit niche
   profile + KPIs from the LinePack. Hermes is generic. Keep.
5. **Skill ecosystem on cards** — `commerce.create_payment_link`,
   `marketing.video_from_post`, etc. Cards point at skill names.
   Hermes is tool-oriented (terminal / read_file / write_file).
   Different domain; keep.

---

## Port priority list (rank-ordered)

| # | What | Source | Effort | Why |
|---|---|---|---|---|
| 1 | **Claim TTL + heartbeat** | `kanban_db.claim_task` + `heartbeat_claim` + `release_stale_claims` | ~2 hr | Cards stuck IN_PROGRESS overnight ← the #1 reliability bug |
| 2 | **`build_worker_context()` equivalent** | `kanban_db.build_worker_context` | ~3 hr | Agents re-do work / miss parent results today |
| 3 | **Embedded dispatcher in lifespan** (collapse our 3 trigger modes into 1 disciplined loop) | `gateway/run._kanban_dispatcher_watcher` + `kanban_db.dispatch_once` | ~3 hr | Reclaim, crash-detect, concurrency cap — clean replacement for our `auto_dispatch_mode` setting |
| 4 | **Diagnostics module** | `kanban_diagnostics.py` | ~3 hr | Surface stuck-blocked, hallucinated-id, spawn-crash-loop on /app/kanban |
| 5 | **`kanban.specify_triage` auto-flow** | `kanban_specify.py` | ~1.5 hr | Auto-flesh out title-only cards from `bootstrap_from_brief` |
| 6 | **task_links + parent-done gate** | `link_tasks` + `recompute_ready` | ~2 hr | Dependency-aware READY promotion |
| 7 | **task_comments** | `add_comment` + table | ~1.5 hr | Agent breadcrumbs in-task |

**Total porting effort:** ~16 hours of focused work. Highest leverage
in items 1-3, which together close the "cards sit forever" bug we
hit overnight and dramatically improve agent context quality.

---

## Recommendation for next sessions

**Phase 1 (this week):** items 1 + 2 + 3 — the dispatcher core. This
replaces today's `auto_dispatch_mode` setting and three trigger
paths with one disciplined Hermes-style loop. After this, IN_PROGRESS
cards actually progress.

**Phase 2 (next week):** items 4 + 5 — diagnostics + auto-specify.
These are the polish that makes /app/kanban honestly usable.

**Phase 3 (later):** items 6 + 7 — task linking + comments. Nice
collaboration upgrades; not blocking.

After Phase 1 ships, we delete `korpha/cofounder/auto_dispatch.py`
(today's PR) and replace with the Hermes-derived loop. The
Settings.workforce_auto_dispatch_mode field stays as a "compat
shim" → deprecation warning → eventual removal.
