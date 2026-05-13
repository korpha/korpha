# Approvals + trust envelope — staying in the captain's chair

**Audience**: every Founder. This is core UX.

The cofounder hypothesis from BRIEF.md: *"It doesn't ask Mike what
to do — it shows him what it would do and asks for approval. Mike
feels in control through approval, not direction."*

This page covers what approvals are, how to use the three buttons
(Approve / Deny / Discuss), and how the trust envelope auto-promotes
agents from "draft proposals" to "execute and log" once you've
demonstrated trust on a particular action class.

---

## What's an approval

Every time an agent wants to do something that **changes state in the
real world** (send an email, create a Stripe link, ship code, post
on Twitter), it **does NOT just do it**. Instead it produces an
`Approval` — a structured proposal:

- **Proposal summary** — one line: "Send cold email to bob@example.com
  about deploy-automation tool"
- **Action class** — e.g. `OUTREACH_EMAIL`, `SHIP_CODE`, `PAYMENT_LINK`
- **Action payload** — the full machine-readable spec (recipient, body,
  amount, etc.) so when you approve, Korpha knows exactly what to do
- **Platform** — for things tied to a specific platform (`twitter`,
  `linkedin`, `email`)

The approval surfaces in 3 places:

1. **Dashboard** — `/app/approvals` page + sidebar badge
2. **Live channel** — Telegram / Discord if running
3. **Email digest** — daily / weekly summary

You can act on any of them; they're synchronized.

---

## The three buttons

### ✓ Approve

Says "yes, do this exactly as proposed." The action fires. Logged
to the audit trail with timestamp + your founder id.

Use when: the proposal is good as-is.

### ✗ Deny

Says "no, don't do this." The action does NOT fire. Logged.

Use when: the proposal is wrong, the timing is bad, or you've
changed your mind.

### 💬 Discuss

Says "let's talk before I commit." Opens a thread with the agent
that produced the proposal so you can:

- Ask why they proposed it that way
- Suggest a variant
- Get them to revise + re-propose

Use when: the proposal is *close* but not quite right. Faster than
denying-and-asking-for-a-rewrite — keeps context.

---

## Trust envelope (auto-promotion)

You don't approve forever. The **trust envelope** tracks how many
times you've approved each `(action_class × platform)` pair in a row.
Once you hit a threshold, the agent gets promoted from `DRAFT` mode
(propose-and-wait) to `AUTO` mode (execute-and-log) for that
specific action class.

### How it works

```
First 5 approvals on `OUTREACH_EMAIL × email` → DRAFT mode
                                                 (agent proposes, you approve)

5th consecutive approval                        → AUTO mode for that pair
                                                 (agent executes; you see in audit)

Any DENY                                        → resets the counter to 0
                                                 (agent proposes again)
```

Threshold defaults to **5** consecutive approvals. Configurable per
action class.

### Three modes

| Mode | What the agent does |
| --- | --- |
| **DRAFT** (default) | Propose; wait for approval. Nothing happens without your click. |
| **AUTO** | Execute immediately; log to audit. You see it after the fact. |
| **OFF** | Don't even propose. The agent must escalate / find another path. |

### How to manage trust envelopes

- View current state: `/app/settings` → Trust Envelopes section
  (or `korpha status` for CLI)
- Manually flip a mode: `/app/settings` → click the mode dropdown for
  any action class
- Reset to DRAFT: deny any action in that class, OR manually flip in
  settings

### What's in DRAFT vs AUTO by default

| Action class | Default mode | Why |
| --- | --- | --- |
| `RESPOND_TO_FOUNDER` | AUTO | Talking back to you isn't a side effect |
| `RUN_SKILL` (read-only skills) | AUTO | Niche discovery, scoring — no real-world action |
| `OUTREACH_EMAIL` | DRAFT | First impressions are real; your reputation |
| `SHIP_CODE` | DRAFT | Code changes are irreversible |
| `PAYMENT_LINK` | DRAFT | Money flows |
| `POST_SOCIAL` | DRAFT | Public-permanent |

Subscription-paid skills (`code.ship_via_codex`) stay DRAFT
indefinitely by design — Codex CLI changes files in YOUR repo, the
trust envelope doesn't auto-promote that.

---

## Discuss flow walkthrough

Click 💬 Discuss on any pending approval. What happens:

1. A new conversation thread opens with the agent that produced the
   proposal (CEO if it was a strategic call, CMO if it was a
   marketing draft, etc.)
2. The first message preloaded for you is the original proposal +
   "what would you change?"
3. You type a critique / new direction
4. The agent revises and produces a NEW approval — old one is
   marked superseded, new one becomes the active proposal

You can iterate as many rounds as you want. The trust envelope only
counts the **final** decision, so a discuss → revise → approve
flow counts as one approval (not zero, not infinite).

---

## Approval audit trail

Every Approve / Deny / Discuss is logged immutably. Find it at:

- **Dashboard**: `/app/activity` — chronological timeline of every
  state transition
- **CLI**: `korpha status --activity` for the last 50 events
- **Database**: every `Activity` row (the audit log table) has actor
  + timestamp + payload. Append-only, no deletes.

Audit log is the source of truth for "who approved what when" —
useful for debugging "why did the cofounder send that" and for any
team / accountability use case.

---

## Approving from non-dashboard surfaces

### CLI

```bash
korpha pending                   # list pending approvals with their ids
korpha approve <approval_id>     # ✓
korpha reject <approval_id>      # ✗
korpha execute <approval_id>     # explicit re-execute (rare; for retries)
```

### HTTP API

```bash
curl http://localhost:8765/approvals/pending
curl -X POST http://localhost:8765/approvals/<id>/approve
curl -X POST http://localhost:8765/approvals/<id>/reject
curl -X POST http://localhost:8765/approvals/<id>/execute
```

### Telegram / Discord

Inline buttons or `/approve <id>` / `/deny <id>` slash commands.
Same effect.

---

## Why this exists (the BRIEF.md rationale)

The whole point is: Mike (non-technical, busy, treating Korpha as
a real cofounder, not a chatbot) needs **leverage** without losing
**control**. Without approvals: the cofounder is a wild horse — fast
but throws you off. With approvals: it's a horse with reins — Mike
guides direction, the cofounder does the work.

The trust envelope solves "but I don't want to approve every cold
email forever." After 5 approvals on a class, Korpha learns
your taste and stops asking. One bad call (one DENY) and it goes
back to asking.

---

## Reference

- Approval model: [`korpha/approvals/model.py`](../korpha/approvals/model.py)
  — `Approval`, `TrustEnvelope`, `AutonomyMode`, `ActionClass` enums
- Approval gate: [`korpha/approvals/gate.py`](../korpha/approvals/gate.py)
- Activity / audit log: [`korpha/audit/model.py`](../korpha/audit/model.py)
- Live API: `GET /approvals/pending`, `POST /approvals/{id}/approve|reject|execute`
- Dashboard: `/app/approvals` page, `/app/activity` page
