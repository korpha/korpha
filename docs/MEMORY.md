# Memory + FTS5 — what your cofounder remembers

**Audience**: anyone wondering "does the CEO remember what I told it
last week" or "where does that decision history live?"

The CEO has persistent memory across sessions. Tell it a constraint
once ("we're ignoring B2C, only B2B SaaS") and the next ask
remembers. This page covers what's stored, how it's retrieved, and
how to clear / export it.

---

## What persists

| What | Where | Retention |
| --- | --- | --- |
| Your conversations with CEO | `korpha.db` → `Message` table | Forever (until you wipe) |
| Your conversations with Directors | Same table | Forever |
| Founder-stated facts ("I have $2k savings") | `Business.founder_brief` field | Until you re-do onboarding |
| Approvals + decisions (every Approve/Deny/Discuss) | `Approval` + `Activity` tables | Forever, immutable |
| Skill run results (validation reports, niche picks) | `SkillRun` table | Forever |
| Cost / token counts per call | `Cost` table | Forever (rollups derived on-demand) |
| LLM-summarized conversation digests | `MemorySummary` table | Forever, regenerated periodically |

What does NOT persist:

- Raw LLM provider request/response pairs (only the message we
  surface to the user is logged; we don't keep the full reasoning
  CoT unless you opt in)
- Streaming intermediate tokens (only the final assembled message)
- Provider API keys (those live in `~/.korpha/.env`, never the DB)

---

## How retrieval works (FTS5)

When the CEO answers your message, it pulls relevant context via
**SQLite FTS5** (full-text search) on the message history:

1. Your incoming message tokenizes
2. FTS5 returns the top-N most-relevant past messages (BM25-ranked)
3. Plus the most recent N messages regardless of relevance (recency)
4. Plus the LLM-generated rolling summary of older history (so we
   don't blow context window on conversations from 6 months ago)
5. Plus the founder brief (always)

The combined context goes into the prompt; the CEO answers with
that context loaded.

### Why FTS5 not vector embeddings

- **Free** — built into SQLite, no separate vector DB
- **Fast** — sub-millisecond on 50k messages
- **Deterministic** — same query → same results, no embedding-model
  drift
- **Good enough** — for the Founder/CEO conversation pattern (where
  the human anchors topics with concrete keywords), keyword search
  outperforms vector search at retrieval-cost-vs-relevance

Vector embeddings are on the NEXT_STEPS roadmap as a Phase 4 add-on
for the cases where keyword search underperforms (mostly: cross-
language similarity, obscure synonyms). FTS5 is the default.

### Memory summarization

Every N messages (default: 50), the LLM summarizer runs and
condenses old conversation into a 200-word rolling digest. The
digest replaces verbatim history in the prompt while preserving the
*facts* the CEO remembered. This caps context cost — you can have a
100k-message conversation and still fit it in a 16k window.

The summarizer runs on the Workhorse tier (cheap) and writes to the
`MemorySummary` table. You can disable it via `~/.korpha/config.yaml`:

```yaml
memory:
  summarization_enabled: false      # default true
  summarization_threshold: 50        # how many messages between summaries
```

---

## Inspecting what the CEO remembers

### From the dashboard

`/app/agents/<ceo_id>` → "Memory" tab — shows the recent message
history + the rolling summary the CEO is currently working from.

### From the CLI

```bash
korpha status --memory          # founder brief + recent thread + summary
```

### Direct DB query (advanced)

```bash
sqlite3 ~/.korpha/korpha.db
> SELECT thread_id, role, substr(content, 1, 100) FROM message ORDER BY created_at DESC LIMIT 10;
```

The schema is documented at [`korpha/db/`](../korpha/db/);
read-only queries are safe.

---

## Clearing memory

### Soft clear — start a new thread

The CEO maintains separate **threads** for different topics (auto-
created by intent). To start fresh, ask "let's start over" or
"new conversation" — the CEO opens a new thread; old threads are
archived but still searchable.

### Per-thread delete

Dashboard → `/app/inbox` → click a thread → "..." menu → "Delete
thread". Removes the thread + its messages from the DB. Audit log
entries (approvals, costs) persist — only the chat goes.

### Full memory wipe

```bash
# Backup first
cp ~/.korpha/korpha.db ~/.korpha/korpha.db.bak

# Wipe just the conversation tables
sqlite3 ~/.korpha/korpha.db "
DELETE FROM message;
DELETE FROM thread;
DELETE FROM memory_summary;
"
```

The founder brief, business config, providers, and audit log
(approvals + activity) survive. Just the chat history goes.

---

## Exporting memory

For backup / portability:

```bash
korpha business-export --to /tmp/widgetco.tar.gz
```

This creates a tar with:

- Founder identity (email, display name)
- Business profile (name, brief, niche, etc.)
- All threads + messages + memory summaries
- Approvals + activity log
- Routines config
- Theme config

Secrets are scrubbed (provider keys, OAuth tokens) before tarring.
The export is portable — use `korpha business-import` on another
machine to restore.

See [`korpha business-export`](CLI_REFERENCE.md#business-export)
for full details.

---

## Privacy + local-first

By default, memory lives in **`~/.korpha/korpha.db` on your
machine only**. Nothing leaves except:

- LLM prompt context — sent to whichever provider you configured
  (OpenAI, Anthropic, OpenCode Go, etc.). Provider's privacy policy
  applies here. Run a local Ollama if this concerns you.
- Channels you opted into — Telegram bot replies, email digests
- Cost tracking — local-only, never uploaded

You can verify with `lsof | grep korpha` — only inbound HTTP for
your dashboard + outbound to provider APIs you configured. No
analytics calls home.

---

## Reference

- DB schema: [`korpha/db/_base.py`](../korpha/db/_base.py)
  + the `model.py` files in each subsystem
- FTS5 setup: [`korpha/cofounder/fts.py`](../korpha/cofounder/fts.py)
- Memory summarizer: [`korpha/cofounder/summarizer.py`](../korpha/cofounder/summarizer.py)
- CEO persistence: [`korpha/cofounder/ceo.py`](../korpha/cofounder/ceo.py)
  — see the `_load_memory_context` flow
- Business export/import: [`korpha/business/portability.py`](../korpha/business/portability.py)
