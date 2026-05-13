# Marketro Multi-Line Test Scenario

A 10-minute browser walkthrough that exercises every layer of the
org-mode integration (PR-INT-1 through PR-INT-8).

**Server URL:** http://127.0.0.1:8765/app/dashboard

If the server isn't running:

```
.venv/bin/korpha server &
```

Tail the log in a second terminal while testing:

```
tail -f /tmp/korpha-server.log
```

---

## Step 0 — Current state check

| # | Action | Expected |
|---|---|---|
| 0a | Open http://127.0.0.1:8765/app/dashboard | KPIs render; sidebar shows **🏛 Lines** and **🔑 Credentials** under the "Company" section. |
| 0b | Click **Lines** in the sidebar | Table is empty or has a single default Marketro root unit. The "Start line" form sits at the top with a dropdown of 6 line kinds (POD / KDP / Info / SaaS / Affiliate / Agency). |
| 0c | Click **Credentials** | "OAuth CLI pool" + "Per-unit API accounts" sections — both empty. |

---

## Step 1 — Spawn the KDP line

| # | Action | Expected |
|---|---|---|
| 1a | On `/app/units`, in the **Start line** form: pick **Amazon KDP / books**, type `Romance KDP` in the name field, click **+ Start line** | Page reloads with a green flash: **✓ Started business line `Romance KDP`** |
| 1b | Look at the units table | Two rows: the default root and a new **Romance KDP** (kind=`line`), status pill `active`, **Owner VP** column shows **Line VP: KDP**. |

**What this proves:** PR-INT-1 (VP hiring at unit-create), PR6 (start_business_line skill), PR1 (BusinessUnit model).

---

## Step 2 — Spawn the POD line as a sibling

| # | Action | Expected |
|---|---|---|
| 2a | Same form, pick **Print on Demand**, name `Merch POD`, **+ Start line** | Flash + new row: **Merch POD**, kind=`line`, owner **Line VP: POD**. |
| 2b | Both KDP and POD show the same root unit in the **Parent** column | They're siblings — required for `cooperation.ask_about` to work without an explicit grant. |

**What this proves:** sibling resolution + default-parent fallback.

---

## Step 3 — Verify the VPs were really hired

| # | Action | Expected |
|---|---|---|
| 3a | Sidebar → **Team** (`/app/team`) | C-suite (CEO + Chief of Staff) unchanged. **Workers** table lists **Line VP: KDP** and **Line VP: POD** with specialty `kdp-line-vp` / `pod-line-vp`. |

**What this proves:** HiringService threads `business_unit_id` correctly (PR-INT-1 + PR3 FK).

---

## Step 4 — Kanban filter ribbon

| # | Action | Expected |
|---|---|---|
| 4a | Sidebar → **Kanban** | Above the columns, a new **filter ribbon** with pills: `All` (active), `Company-wide`, `Romance KDP (line)`, `Merch POD (line)`. |
| 4b | Type `Launch KDP romance covers` in the quick-add input and submit | Card appears in BACKLOG. |
| 4c | Click the **Romance KDP** pill | Card disappears (it's company-wide; not yet unit-scoped). URL is now `/app/kanban?unit=<uuid>`. |
| 4d | Click **All** | Card reappears. |
| 4e | Click **Company-wide** (`?unit=__none__`) | Card stays visible — has no `business_unit_id`. |

**What this proves:** PR-INT-7 filter ribbon honors `business_unit_id`.

> Adding cards already scoped to a unit from the browser is the next UI gap. For now it's a CEO-driven action via chat or CLI.

---

## Step 5 — Credentials surface

| # | Action | Expected |
|---|---|---|
| 5a | Sidebar → **Credentials** | "OAuth CLI pool" section empty (no OAuth CLIs registered yet). "Per-unit API accounts" empty — lines inherit company defaults via tree-walk. |
| 5b | Page renders without errors despite no per-unit credentials | Resolver fallback working — units use the parent's account when they have none. |

**What this proves:** PR-INT-7 credentials view + PR4 resolver semantics.

---

## Step 6 — Chat with the CEO, confirm unit context awareness

> Requires LLM credentials configured (e.g. `OLLAMA_CLOUD_API_KEY` in `.env`). Skip this section if not configured — Steps 7 + 8 still cover the architecture.

| # | Action | Expected |
|---|---|---|
| 6a | Sidebar → **Chat** (`/app/chat`). Type: *"What business lines are running right now?"* | CEO replies listing KDP + POD + default. |
| 6b | Ask: *"Remember that the Highland Rogue series launches in 6 weeks."* | CEO calls `memory.remember`; the reply confirms the memory was stored. |
| 6c | Sidebar → **Memory** (`/app/memory`) | New entry visible. |

**What this proves:** PR-INT-4 CEO has the unit summary in its prompt context (via `render_unit_summary_for_prompt`).

---

## Step 7 — Verify the memory entry got the right namespace

The architectural punchline. Memory written by an agent in a unit should
get stamped with that unit's namespace; cross-unit recall shouldn't leak.

In a terminal:

```
sqlite3 ~/.korpha/korpha.db \
  "SELECT substr(text,1,40), namespace_id FROM long_term_memory_entry ORDER BY created_at DESC LIMIT 5;"
```

| # | Action | Expected |
|---|---|---|
| 7a | Run the query above | Most recent rows have a `namespace_id` UUID — not NULL. |
| 7b | Cross-check with `sqlite3 ~/.korpha/korpha.db "SELECT name, memory_namespace_id FROM business_unit;"` | The namespace_id from 7a matches the unit the CEO was scoped to. |

**What this proves:** PR-INT-2 (recall namespace enforcement) + the
memory.remember integration that auto-stamps `namespace_id` from caller's
unit. Before this batch, that column was always NULL.

---

## Step 8 — cooperation.ask_about cross-unit dispatch (CLI)

The browser doesn't expose a "ask another unit a question" form yet. From a terminal:

```
.venv/bin/korpha cooperation ask \
  --from "Romance KDP" \
  --to "Merch POD" \
  --question "Got merch capacity for Highland Rogue?"
```

| # | Action | Expected |
|---|---|---|
| 8a | Run the command | Output: an `answer` field naming **Line VP: POD** + any matching POD-namespace memories. **No** KDP-namespace memories appear (defense-in-depth filter). |
| 8b | Inspect the audit log: `sqlite3 ~/.korpha/korpha.db "SELECT from_unit_id, to_unit_id, question_summary, response_summary FROM cross_unit_query_log;"` | One row with both question + response captured. |

**What this proves:** PR-INT-6 synchronous dispatch + audit log + namespace-filtered response.

---

## Step 9 — Cross-tree blocked without a grant (CLI)

Spawn a grandchild under each line, then try cross-tree:

```
.venv/bin/korpha unit spawn-type --parent "Romance KDP" --name "Highland Series"
.venv/bin/korpha unit spawn-audience --parent "Merch POD" --name "Highland Audience"

.venv/bin/korpha cooperation ask \
  --from "Highland Series" \
  --to "Highland Audience" \
  --question "Want to cobrand?"
```

| # | Action | Expected |
|---|---|---|
| 9a | Run the ask command | Errors with: **cross-tree query from `<uuid>` to `<uuid>` requires an accepted CooperationProposal granting cross_tree_query** |
| 9b | The `cross_unit_query_log` table doesn't have a new row | Rejected at the authorization gate **before** the log was written. |

**What this proves:** PR8 cooperation authorization + PR-INT-6 gate ordering.

---

## What's been exercised

| Step | PR | What it proves |
|---|---|---|
| 1, 2 | PR-INT-1, PR6, PR1 | Line spawn auto-hires VP, sets owner_agent_role_id |
| 3 | PR3 | business_unit_id FK on AgentRole |
| 4 | PR-INT-7 | Kanban filter ribbon honors unit scope |
| 5 | PR-INT-7, PR4 | Credentials view + tree-walk fallback |
| 6 | PR-INT-4 | CEO has unit-org awareness in its prompt context |
| 7 | PR-INT-2 | memory.remember stamps namespace; recall enforces it |
| 8 | PR-INT-6 | cooperation.ask_about dispatches in-process, captures response |
| 9 | PR8 | Cross-tree query blocked without an accepted CooperationProposal |

---

## Troubleshooting

**Server returns 500 on a page.**
- Tail `/tmp/korpha-server.log`. Most likely cause is schema drift on a
  table whose model was added without a matching migration. Note the
  failing column or table and run a targeted `ALTER TABLE` against
  `~/.korpha/korpha.db`.

**`/app/units` shows no rows even though I'm sure migrations ran.**
- The default unit gets created during onboarding for fresh businesses.
  For an existing pre-PR1 business, run:
  `sqlite3 ~/.korpha/korpha.db` and check
  `SELECT * FROM business_unit WHERE kind='default';`. If empty, the
  backfill in PR2 didn't run — create one manually or re-run the
  migration on a fresh DB.

**The kanban filter ribbon doesn't show up.**
- The template renders the ribbon only when `unit_filter_options` is
  non-empty — i.e. after at least one BusinessUnit exists. Start a line
  via `/app/units` first.

**CEO chat doesn't list lines or ignores them.**
- The unit summary is injected only when the request flows through the
  onboarding chain or the CEO router. If you're using the older free-form
  chat endpoint it may bypass that. The unit list itself is correct in
  `/app/units` — chat integration is the verification, not the source of
  truth.
