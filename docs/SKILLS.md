# Skills reference — what your cofounder can do

**Audience**: Founders who want to know what to ask for + developers
adding new skills.

A **skill** is a named, parameterized procedure with a known input/output
shape. The CEO auto-routes your messages to the right skill based on what
you're asking — but you can also call any skill directly via
`korpha skill run <name>` or the dashboard's Skills page.

17 skills ship built-in. They're grouped by **Day-0 intake**, **Start
phase**, **Side effects** (real-world actions), and **Run phase**.

---

## Day-0 intake (what your cofounder asks first)

### `founder.intake_brief`

**Purpose**: Capture your goal, hours/week, savings, skills, constraints
into a structured brief that every later skill reads from.

**When CEO uses it**: First time you ask "help me start a business" or
"I want to make $5k/mo on the side." Triggered automatically by the
onboard flow at `/app/onboard`.

**Parameters**: free-form text from the Founder describing themselves +
goal.

**Output**: structured brief written to `Business.founder_brief`.

---

## Start phase (pick a niche → ship something)

### `niche.find_micro_niches`

**Purpose**: Propose 3-5 micro-niches that fit your skills + time + savings.
Each candidate gets a value-prop, target avatar, price band, and a
single this-week validation experiment.

**When CEO uses it**: "help me pick a niche", "what should I build",
"3 micro-niche ideas for a Python dev with 5h/week".

**Parameters**:
- `skills` (str): comma-separated, e.g. "Python, B2B SaaS, indie marketing"
- `time_budget_hours` (int): hours/week realistically available
- `savings_usd` (int): cash on the line
- `existing_audience` (str, optional): newsletter / Twitter / Show HN

**Example**:
```bash
korpha skill run niche.find_micro_niches \
  --arg "skills=Python, FastAPI, Docker" \
  --arg "time_budget_hours=5" \
  --arg "savings_usd=2000"
```

### `validate.score_idea`

**Purpose**: Score a niche idea against the BRIEF.md validation checklist
(market size, competition, cost-to-test, expected payoff). Returns a
GO / NO-GO / SOFT-GO verdict with a concrete experiment.

**When CEO uses it**: After `niche.find_micro_niches` picks a winner,
OR when you say "is X a good idea" / "validate this".

**Parameters**:
- `niche_description` (str): what the product is + who it's for
- `evidence` (str, optional): customer quotes, search-trend data,
  competitor screenshots — anything you've gathered

### `product.first_feature`

**Purpose**: Decide what's IN the v1 vs deferred. Outputs a 2-3 feature
ship list with explicit trade-offs ("You COULD ship X but it adds 4
days; recommendation: defer to v2").

**When CEO uses it**: "what's the MVP", "scope this", "what should
v1 include".

**Parameters**:
- `niche` (str): the chosen niche / product description
- `time_budget_days` (int): realistic ship window

### `pricing.recommend_tiers`

**Purpose**: Recommend a pricing structure (free / $X / $XX) with
positioning rationale + comparison to known competitors.

**When CEO uses it**: "how should I price this", "$X or $XX",
"what's a fair price".

**Parameters**:
- `product` (str): what it is, who buys it
- `target_arpu_usd` (int, optional): your monthly-revenue-per-user target

### `landing.draft_copy`

**Purpose**: Headline + subhead + CTA + 3 supporting bullets for a
landing page. Pre-instrumented with the Copywriter Worker's "no
fluff" prompt rules (no "revolutionize", no "next-generation").

**When CEO uses it**: "write the landing page", "headline for X",
"draft hero copy".

**Parameters**:
- `niche` (str): the product / niche
- `audience` (str): who you're targeting
- `pain_point` (str, optional): the specific 2am problem

### `outreach.draft_cold_emails`

**Purpose**: 3 cold-email opener variants with distinct angles. Output
includes Subject lines + body for each variant.

**When CEO uses it**: "draft cold emails for X", "outreach openers",
"3 ways to open this email".

**Parameters**:
- `niche` (str): the offering
- `audience` (str): who's getting the email
- `channel` (str, optional): email / LinkedIn / X

### `research.scrape_url`

**Purpose**: Scrape a competitor / customer-research URL via the
browser and extract key facts. Uses Playwright (or agent-browser CLI
if installed).

**When CEO uses it**: "what's on stripe.com/pricing", "scrape this
competitor", "summarize this article".

**Parameters**:
- `url` (str)
- `goal` (str): what you want extracted

---

## Side effects (real-world actions, approval-gated)

### `outreach.send_cold_email`

**Purpose**: **Actually send** a cold email via Resend. Approval-gated
— produces a draft proposal, you approve, send fires.

**When CEO uses it**: "send this to <email>", "fire off the cold
email" — ALWAYS produces an approval first, never auto-sends.

**Parameters**:
- `to` (str): recipient email
- `subject` (str)
- `body` (str)
- `from_name` (str, optional): sender display name

**Required**: `RESEND_API_KEY` env var + verified sending domain.

### `commerce.create_payment_link`

**Purpose**: Create a Stripe payment link for a one-time purchase OR
subscription. Approval-gated.

**When CEO uses it**: "make a $99 payment link for X", "set up
checkout".

**Parameters**:
- `name` (str): product name
- `amount_usd` (int)
- `recurring` (bool, optional): default false

**Required**: `STRIPE_API_KEY` env var (`sk_test_*` for testing,
`sk_live_*` for production).

### `code.ship_via_codex`

**Purpose**: Dispatch a coding task to Codex CLI. Codex runs in your
repo with your ChatGPT subscription, edits files, reports back. The
diff sits in your working tree for you to review.

**When CTO uses it**: "add /healthz endpoint", "fix the typo on line
14 of README", "refactor X to use Y" — when CTO has scoped the work
and the user wants it actually done, not just planned.

**Parameters**:
- `prompt` (str): plain-English task. Be specific.
- `cwd` (str, optional): repo root. Defaults to the workspace dir.
- `sandbox_mode` (str, optional): `read-only` / `workspace-write`
  (default) / `danger-full-access`

**Required**: `npm install -g @openai/codex` + `codex login`.

### `imagery.generate_image`

**Purpose**: Generate an image from a text prompt via the configured
backend. Backends: Replicate / fal.ai / local SD WebUI / Codex CLI.

**When CEO uses it**: "make a logo", "generate a hero image for X",
"icon for this feature".

**Parameters**:
- `prompt` (str)
- `style_hint` (str, optional): "photorealistic" / "minimal" / etc.

**Required**: at least one image provider configured via
`korpha config-image-add`.

---

## Run phase (post-launch operations)

### `growth.draft_content_plan`

**Purpose**: Weekly content calendar — 5-7 posts/threads with hooks,
channels, and the metric each one should move.

**Parameters**:
- `audience` (str)
- `cadence` (str, optional): "daily" / "3x/week" / "weekly"

### `support.triage_inbox`

**Purpose**: Triage a batch of support tickets. For each: classify
(bug / question / refund / feature-req), draft a reply, flag for
approval if policy-edge.

**Parameters**:
- `tickets` (list): list of `{from, subject, body}` objects

### `finance.weekly_review`

**Purpose**: Pull the week's revenue, cost, and runway from configured
sources (Stripe + provider spend); produce a one-paragraph plain-English
summary.

**Parameters**:
- `start_date` (str, optional): defaults to last Monday

### `analytics.weekly_review`

**Purpose**: Surface the top KPI movements + 1-2 hypotheses for what
caused them. Reads from configured sources (placeholder: hardcoded for
now; adapter wiring planned).

**Parameters**: none (reads from this week's activity log).

---

## GEO + SEO (RankMyAnswer integration)

### `geo_seo.audit_url`

**Purpose**: Audit a URL for both Google SEO and GEO (LLM-citation)
signals. Returns scores + concrete recommendations.

**Parameters**:
- `url` (str)
- `target_query` (str, optional): the search intent the page targets

### `geo_seo.generate_schema`

**Purpose**: Generate a JSON-LD structured-data block the Founder
pastes into the page `<head>`.

**Parameters**:
- `project_id` (str): RankMyAnswer project id
- `url` (str)
- `schema_type` (str, optional): default `LocalBusiness`

### `geo_seo.list_projects`

**Purpose**: List the Founder's tracked sites in RankMyAnswer.

**Parameters**: none

### `geo_seo.balance`

**Purpose**: Show the Founder's RankMyAnswer credit balance + plan
tier.

**Parameters**: none

**Required for all 4**: `RANKMYANSWER_API_KEY` env var (set via
`korpha config-rankmyanswer-add`).

---

## How CEO routes messages → skills

When you DM the CEO (`/app/dashboard` chat, or `korpha ask`),
the CEO runs a **router pass** first:

1. Reads your message + the catalog of available skills
2. Returns either `{action: "respond", content: "..."}` (just chat)
   OR `{action: "use_skill", skill_name: "X", skill_args: {...}}`
3. If a skill is picked, it runs; the result is folded into the CEO's
   reply so you see one coherent answer

You can override the router by **calling a skill directly**:

```bash
korpha skill run niche.find_micro_niches --arg "skills=..."
```

…or via the dashboard Skills page (`/app/skills`).

---

## Adding your own skill

Two paths:

**(a) Python — full power**

Drop a class extending `korpha.skills.types.Skill` in
`korpha/skills/<your_name>.py`, register via
`register(YourSkill())`, add the import to
`korpha/skills/__init__._autoload_builtins`.

**(b) YAML — no Python required**

Author a YAML skill following the agentskills.io format and drop the
directory at `~/.korpha/skills/<your_skill>/manifest.yaml`. Loaded
automatically at startup.

See [`korpha/skills/yaml_skill.py`](../korpha/skills/yaml_skill.py)
for the YAML schema.

---

## Reference

- Skill base class: [`korpha/skills/types.py`](../korpha/skills/types.py)
- All built-ins: [`korpha/skills/`](../korpha/skills/)
- Live list: `korpha skill list` or `GET /skills`
- Spec format: each skill defines `SkillSpec(name, description, parameters, default_tier)`
