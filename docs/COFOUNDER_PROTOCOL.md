# Cofounder Protocol — partner spec

**Audience**: SaaS founders + dev teams who want their service to
register as an Korpha-native cofounder tool.

**Why this exists**: Korpha is not an integrator. It's a standard
solopreneur services target. Your service plus one ``cofounder.yaml``
file = part of every Korpha user's cofounder loop. Users discover
you via:

```bash
korpha cofounder install https://your-service.com/.well-known/cofounder.yaml
```

That single command validates your manifest, copies it to
``~/.korpha/cofounders/<your-name>/``, surfaces your branding +
auth setup command, and routes future ``korpha doctor`` runs to
report your install status. The Founder is one ``korpha
config-<your>-add`` away from using your skills inside their cofounder.

---

## Spec v1

A manifest is YAML. Required and optional fields:

```yaml
spec_version: 1                  # required: must be 1
name: rank_my_answer             # required: snake_case identifier (a-z, 0-9, _)
display_name: "RankMyAnswer.com — GEO + SEO"   # required: human-readable
description: |                                  # required: 1-3 sentences
  Audit landing pages for both Google SEO and LLM-citation
  signals (GEO).
homepage: https://rankmyanswer.com              # required: http(s) URL
docs_url: https://rankmyanswer.com/docs/agents/korpha   # optional

provides:                                       # required
  skills:                                       # required, ≥1 entry
    - geo_seo.audit_url
    - geo_seo.generate_schema

auth:                                           # optional but expected
  kind: api_key                                 # api_key | oauth | none
  api_key_env: RANKMYANSWER_API_KEY            # env var Korpha reads
  setup_command: korpha config-rankmyanswer-add  # exact CLI to wire up
  signup_url: https://rankmyanswer.com/signup  # where to get an account

branding:                                       # optional
  primary_color: "#1f7a4d"                      # #RRGGBB hex only
  logo_url: https://rankmyanswer.com/logo.svg   # public https URL

requires:                                       # optional, advisory
  korpha_version: ">=0.1.0"
  network_egress:                               # hostnames partner skills hit
    - api.rankmyanswer.com
```

### Field rules (validator catches all of these)

- ``name`` must be ``[a-z0-9_]+`` snake_case. It namespaces your skills
  and identifies you on disk.
- ``homepage`` and ``branding.logo_url`` must be ``http(s)://``.
- ``branding.primary_color`` must be exactly ``#RRGGBB`` (7 chars,
  hex only).
- ``provides.skills`` must list ≥ 1 entry. **Each skill must already
  be registered in the Korpha core** (see "Shipping a new skill"
  below).
- ``auth.kind`` must be one of ``api_key`` / ``oauth`` / ``none``. Pick
  ``none`` only if your API is fully public (rare).

---

## How partners ship a manifest

### 1. Land your skills in Korpha core

The current spec (v1) requires that every skill named in
``provides.skills`` already exists in ``korpha.skills.default_registry``.
We're strict about this so users never install a manifest that
silently does nothing.

The path:

1. Open a PR against
   [github.com/korpha/korpha](https://github.com/korpha/korpha)
   that adds your skills under ``korpha/skills/<your_namespace>.py``.
2. Skills live in the public repo (MIT-licensed). Your hosted API is
   what's proprietary, not the wrapper.
3. Once merged + released, publish your manifest.

(v2 of the spec will let you ship YAML skills directly from the
manifest. We're saving that for after a few partner manifests have
shipped so we get the format right.)

### 2. Author the manifest

Copy `korpha/protocol/examples/rank_my_answer.cofounder.yaml`
in this repo as a starting point. Validate locally:

```bash
# from the Korpha repo
uv run python -c "
from korpha.protocol import load_manifest
print(load_manifest('your.cofounder.yaml'))
"
```

If it parses without exception, you're spec-compliant.

### 3. Host the manifest

Publish at a stable HTTPS URL. The convention is
`https://your-service.com/.well-known/cofounder.yaml`, mirroring the
`.well-known` pattern from RFC 8615 — it's a known location for
machine-readable site-level metadata. Other paths work but
`.well-known` is what Korpha dashboards default to looking for
when discovering partners.

### 4. Tell users to install it

```bash
korpha cofounder install https://your-service.com/.well-known/cofounder.yaml
```

After a successful install, the user sees:

```
✓ Installed cofounder partner: RankMyAnswer.com — GEO + SEO
   Audit landing pages for both Google SEO and LLM-citation signals (GEO).
   Stored at: /home/.../cofounders/rank_my_answer

Next: link your account by running
   korpha config-rankmyanswer-add
   No account yet? Sign up: https://rankmyanswer.com/signup
```

That's the full onboarding flow — no docs the user has to dig through.

---

## What partners get

- **Discovery**. Users find you via the manifest catalog (coming) or
  by you publishing the install command on your "Korpha
  integration" docs page.
- **Trust**. Your ``branding`` block (color + logo) renders
  consistently in the Korpha dashboard. You stay on-brand even
  inside someone else's tool.
- **No SDK lock-in**. The manifest is plain YAML. Any language that
  writes YAML can ship one.
- **Clean uninstall**. ``korpha cofounder uninstall <your-name>``
  drops the manifest dir. Your users keep control.

## What partners do **not** get (yet)

- **Code execution**. ``install`` does not run your code. We validate
  the manifest, copy it, surface your setup command. Your skills run
  through the same Korpha skill executor every other skill uses,
  with the same approval-gate / cost-tracker / observability story.
- **Token payment routing**. Your skills bill against the user's
  account on your service. Korpha doesn't proxy or pre-pay.

## Roadmap

**v2** (next, post-feedback): YAML skill dirs shippable directly from
the manifest, OAuth flow helpers, branded dashboard cards.

**v3** (later): hosted catalog, signed manifests, paid-tier rev share.

---

## Reference example

Working example: [`korpha/protocol/examples/rank_my_answer.cofounder.yaml`](../korpha/protocol/examples/rank_my_answer.cofounder.yaml)
ships in this repo. Copy it, change the name + skills, publish.

## Source code

Spec implementation lives at:

- `korpha/protocol/manifest.py` — schema + validator
- `korpha/protocol/installer.py` — install/list/uninstall
- `korpha/cli.py` — `korpha cofounder install/list/uninstall` commands
- `tests/test_cofounder_protocol.py` — what the validator accepts/rejects

24 tests cover the spec. If something's unclear or you hit an
edge case, open an issue — partner ergonomics is what makes this
protocol actually work.
