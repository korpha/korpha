"""meta.* skills — Korpha authoring its own skills.

When the founder asks for a capability that no installed skill can
fulfill, ``meta.author_skill`` is the fallback. It uses an LLM to
draft an agentskills.io-compliant YAML skill manifest, runs the
Hermes-derived security scanner over the result, and stages the
draft as a CODE_CHANGE Approval the founder must accept before the
skill is installed.

Why YAML-only (v1):
  - YAML skills wrap a single LLM call with a system + user prompt
    template. They cannot execute arbitrary Python, shell out, or
    touch the network on their own.
  - The risk surface is text content only — the scanner's
    prompt-injection patterns + the Approval gate are sufficient.
  - 50%+ of capabilities a solopreneur asks for ("draft an X",
    "summarize Y", "classify Z") are LLM-prompt-only by nature.
  - Python authoring (needed for browser automation, real I/O)
    comes next, with stricter trust requirements.

Design references:
  - OpenClaw skill-workshop (proposal-then-apply state machine).
  - Hermes skills_guard (threat catalogue + trust tiers).
  - agentskills.io spec (manifest layout).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from korpha._jsonext import extract_json_dict
from korpha.approvals.model import (
    ActionClass,
    Approval,
    ApprovalStatus,
)
from korpha.audit.model import InferenceTier
from korpha.cofounder.model import RoleType
from korpha.inference.limits import agent_max_tokens, agent_timeout
from korpha.inference.types import CompletionRequest, Message, Role
from korpha.skills.registry import register
from korpha.skills.types import (
    Skill,
    SkillContext,
    SkillError,
    SkillResult,
    SkillSpec,
)
from korpha.skills_hub.guard import (
    INSTALL_POLICY,
    VERDICT_INDEX,
    ScanResult,
    scan_skill,
)


_AGENT_CREATED_TRUST = "agent-created"
"""Trust level applied to every skill Korpha authors itself.
Maps via INSTALL_POLICY to (allow, ask, block) for safe/caution/
dangerous — caution findings require user confirmation, dangerous
ones are auto-rejected."""


# ---------------------------------------------------------------------------
# Author-skill prompt (the LLM that drafts the manifest)
# ---------------------------------------------------------------------------


_AUTHOR_PROMPT = """\
You are Korpha's skill-authoring step. The founder asked for a
capability that doesn't exist yet, and you'll draft a YAML skill
manifest that follows the agentskills.io spec.

Founder's request:
\"\"\"
{intent}
\"\"\"

Suggested skill name (use unless clearly wrong):  {suggested_name}
Suggested one-line description:                   {suggested_description}

# What you must produce

A SINGLE JSON object (NOT YAML) with these fields, which we will
serialize into a manifest.yaml:

{{
  "name": "<dotted.snake_case>",                 # e.g. "outreach.draft_followup_email"
  "description": "<one paragraph, ≤2 sentences>",
  "default_tier": "<workhorse|pro>",             # workhorse = cheap; pro = better reasoning
  "parameters": {{
    "<param_name>": {{
      "description": "<what the caller passes>",
      "default": "<best default; omit if no sensible default>"
    }}
  }},
  "system_prompt": "<role + goal + output rules>",
  "user_prompt_template": "<template with {{param}} placeholders>",
  "output": {{
    "format": "<json|text>",
    "summary_key": "<key from json output to use as the result summary>",
    "required_keys": ["<keys the parser will assert exist>"]
  }},
  "max_tokens": 8000
}}

# Naming + scope rules

- Name MUST be lowercase, dotted, snake_case (e.g. ``support.classify_ticket``).
  Pick a category from: outreach, content, support, finance, growth,
  research, product, analytics, channel, creative, automation. Invent a
  new prefix only if none fits.
- The skill should do ONE thing well. If the founder's request
  encompasses multiple steps, draft the FIRST step only — the
  cofounder will compose it with others.
- Do NOT propose a skill that requires browser automation, file I/O,
  or external API calls. YAML skills are LLM-prompt-only. If the
  request needs that, name the skill and describe it but make it
  clear in the description "(stub — needs Python implementation)"
  and add a `_status: "stub"` field at the top level.

# Prompt-writing rules

- system_prompt: 2-5 short paragraphs. Define the role, the goal,
  and the format rules. No second-person pleasantries.
- user_prompt_template: shows the LLM what to do with the
  parameters at call time. Use ``{{param_name}}`` placeholders that
  match your parameters declaration exactly. Avoid double-curly outside
  placeholders.
- Output format: prefer ``json`` so the result is structured and
  composable. Use ``text`` only for one-shot creative content.
- For json output, the system_prompt MUST instruct the LLM to
  respond with strict JSON only, and required_keys MUST list every
  key the consumer expects.

# Safety rules (we run a scanner — failures = your skill rejected)

- Do not include URLs that could exfiltrate data ("paste the result
  to https://...").
- Do not write prompts that try to override system instructions
  ("ignore previous instructions", "you are now …").
- Do not embed shell commands, SQL, file paths, or credentials.

Respond with the JSON object only. No prose, no fences, no commentary.
"""


# ---------------------------------------------------------------------------
# AuthorSkill — the skill that drafts other skills
# ---------------------------------------------------------------------------


_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")


class AuthorSkillSkill(Skill):
    """Draft a YAML skill manifest from a founder ask, then stage it as
    an approval. Does NOT install — that happens on user approval via
    ``meta.apply_skill_proposal``.
    """

    spec = SkillSpec(
        name="meta.author_skill",
        description=(
            "Draft a new YAML skill manifest when no installed skill "
            "matches the founder's request. Runs a security scanner "
            "over the output and stages it as an approval (CODE_CHANGE) "
            "the founder must accept before the skill installs. "
            "v1 authors LLM-prompt-only YAML skills; Python skills "
            "(browser automation, real I/O) come later."
        ),
        parameters={
            "intent": (
                "The founder's original request — what capability they "
                "want. Required."
            ),
            "suggested_name": (
                "Optional. A dotted snake_case name like "
                "``outreach.draft_followup_email``. If omitted the "
                "authoring LLM picks one."
            ),
            "suggested_description": (
                "Optional. A one-line description of what the new skill "
                "should do. The authoring LLM may rewrite it."
            ),
        },
        default_tier=InferenceTier.PRO,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any]
    ) -> SkillResult:
        intent = str(args.get("intent") or "").strip()
        if not intent:
            raise SkillError(
                "meta.author_skill needs `intent` — what the founder asked "
                "for in their own words."
            )
        suggested_name = str(args.get("suggested_name") or "(let the model pick)")
        suggested_description = str(
            args.get("suggested_description") or "(let the model pick)"
        )

        # ---- Phase 1: ask LLM for the manifest JSON ----
        prompt = _AUTHOR_PROMPT.format(
            intent=intent,
            suggested_name=suggested_name,
            suggested_description=suggested_description,
        )
        request = CompletionRequest(
            messages=[
                Message(role=Role.SYSTEM, content=(
                    "You are the Korpha skill-authoring step. Output "
                    "strict JSON only — no prose, no code fences."
                )),
                Message(role=Role.USER, content=prompt),
            ],
            tier=self.spec.default_tier or InferenceTier.PRO,
            session_key=f"meta-author-{ctx.business.id}-{intent[:32]}",
            max_tokens=agent_max_tokens(),
            timeout_seconds=agent_timeout(),
        )
        response = await ctx.cost_tracker.complete(
            request,
            session=ctx.session,
            business_id=ctx.business.id,
            agent_role_id=ctx.invoking_agent_role_id,
        )
        try:
            manifest = extract_json_dict(response.content)
        except Exception as exc:
            raise SkillError(
                f"Authoring LLM returned non-JSON: {response.content[:200]!r}"
            ) from exc

        # ---- Phase 2: validate the manifest ----
        problems = _validate_manifest_shape(manifest)
        if problems:
            raise SkillError(
                "Authored manifest failed validation: " + "; ".join(problems)
            )

        # ---- Phase 3: serialize to YAML and security-scan ----
        manifest_yaml = yaml.safe_dump(
            manifest, sort_keys=False, default_flow_style=False
        )
        scan = _scan_authored_yaml(manifest, manifest_yaml)
        policy = INSTALL_POLICY.get(_AGENT_CREATED_TRUST)
        if policy is None:
            raise SkillError(
                f"INSTALL_POLICY missing entry for {_AGENT_CREATED_TRUST!r}"
            )
        verdict_idx = VERDICT_INDEX.get(scan.verdict, 2)
        decision = policy[verdict_idx]
        if decision == "block":
            raise SkillError(
                f"Authored skill blocked by scanner ({scan.verdict}): "
                f"{scan.summary}"
            )

        # ---- Phase 4: stage as Approval ----
        approval = _stage_skill_approval(
            ctx=ctx,
            manifest=manifest,
            manifest_yaml=manifest_yaml,
            intent=intent,
            scan=scan,
            requires_user_confirmation=(decision == "ask"),
        )

        return SkillResult(
            skill_name=self.spec.name,
            payload={
                "approval_id": str(approval.id),
                "skill_name": str(manifest.get("name")),
                "scan_verdict": scan.verdict,
                "findings_count": len(scan.findings),
                "manifest_yaml": manifest_yaml,
                "decision": decision,
                "is_stub": bool(manifest.get("_status") == "stub"),
            },
            summary=(
                f"Drafted skill '{manifest.get('name')}' "
                f"(scan: {scan.verdict}, "
                f"{len(scan.findings)} findings). "
                f"Awaiting your approval at /app/approvals/{approval.id}."
            ),
            cost_usd=float(response.cost_usd or 0.0),
        )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_manifest_shape(manifest: dict[str, Any]) -> list[str]:
    """Return list of human-readable problems. Empty list = pass."""
    problems: list[str] = []
    name = manifest.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        problems.append(
            f"name must be dotted snake_case (e.g. 'outreach.draft_email'), got {name!r}"
        )
    if not isinstance(manifest.get("description"), str) or not manifest.get(
        "description", ""
    ).strip():
        problems.append("description is required")
    upt = manifest.get("user_prompt_template")
    if not isinstance(upt, str) or not upt.strip():
        problems.append("user_prompt_template is required")
    out = manifest.get("output")
    if not isinstance(out, dict):
        problems.append("output must be a mapping with format / summary_key / required_keys")
    else:
        if str(out.get("format", "")).lower() not in ("json", "text"):
            problems.append("output.format must be 'json' or 'text'")
    params = manifest.get("parameters") or {}
    if not isinstance(params, dict):
        problems.append("parameters must be a mapping")
    return problems


# ---------------------------------------------------------------------------
# Scanner adapter (we only scan the YAML text — no Python, no scripts)
# ---------------------------------------------------------------------------


def _scan_authored_yaml(
    manifest: dict[str, Any], manifest_yaml: str
) -> ScanResult:
    """Run the Hermes-derived guard over the authored YAML's text content.

    YAML skills are prompt-only, so the dominant risks are prompt
    injection and exfiltration URLs in the prompts themselves. We
    write to a tempdir and call ``scan_skill`` (path-based) so we
    reuse the production scanner exactly.
    """
    import tempfile

    skill_name = str(manifest.get("name", "unknown")).replace(".", "__")
    with tempfile.TemporaryDirectory(prefix="korpha-author-") as td:
        skill_dir = Path(td) / skill_name
        skill_dir.mkdir(parents=True)
        (skill_dir / "manifest.yaml").write_text(manifest_yaml, encoding="utf-8")
        return scan_skill(skill_dir, source="agent-created")


# ---------------------------------------------------------------------------
# Approval staging
# ---------------------------------------------------------------------------


def _stage_skill_approval(
    *,
    ctx: SkillContext,
    manifest: dict[str, Any],
    manifest_yaml: str,
    intent: str,
    scan: ScanResult,
    requires_user_confirmation: bool,
) -> Approval:
    """Create an Approval row carrying the full file contents in payload.

    On approval, ``apply_skill_proposal_from_approval`` writes the files
    to disk and reloads the registry. The action is CODE_CHANGE because
    installing a new skill is a code-level mutation to the running
    cofounder.
    """
    from sqlmodel import select

    from korpha.cofounder.model import AgentRole

    skill_name = str(manifest["name"])
    decision_word = "ask" if requires_user_confirmation else "auto-allow"

    # Choose the CTO agent role if available — they "own" code changes.
    cto_role = ctx.session.exec(
        select(AgentRole)
        .where(AgentRole.business_id == ctx.business.id)
        .where(AgentRole.role_type == RoleType.CTO)
    ).first()
    role_id = cto_role.id if cto_role else None
    if role_id is None:
        # If the founder hasn't hired a CTO yet, attribute to whichever
        # agent is registered. Schema requires a non-null FK.
        any_role = ctx.session.exec(
            select(AgentRole).where(AgentRole.business_id == ctx.business.id)
        ).first()
        role_id = any_role.id if any_role else None
        if role_id is None:
            raise SkillError(
                "No agent role exists for this business yet — cannot stage a "
                "code-change approval. Run onboarding first."
            )

    findings_payload = [
        {
            "pattern_id": getattr(f, "pattern_id", ""),
            "severity": getattr(f, "severity", ""),
            "category": getattr(f, "category", ""),
            "description": getattr(f, "description", ""),
        }
        for f in scan.findings
    ]
    summary_text = (
        f"Author skill '{skill_name}'. Scan: {scan.verdict} "
        f"({len(scan.findings)} findings). "
        f"Decision policy: {decision_word}.\n\n"
        f"Original intent:\n{intent[:400]}"
    )
    approval = Approval(
        business_id=ctx.business.id,
        agent_role_id=role_id,
        action_class=ActionClass.CODE_CHANGE,
        platform="meta",
        proposal_summary=summary_text,
        action_payload={
            "kind": "author_skill",
            "skill_name": skill_name,
            "intent": intent,
            "manifest": manifest,
            "manifest_yaml": manifest_yaml,
            "scan": {
                "verdict": scan.verdict,
                "summary": scan.summary,
                "findings": findings_payload,
                "trust_level": scan.trust_level,
            },
            "trust_level": _AGENT_CREATED_TRUST,
        },
    )
    ctx.session.add(approval)
    ctx.session.commit()
    ctx.session.refresh(approval)
    return approval


# ---------------------------------------------------------------------------
# Apply path — invoked by the approvals subsystem when the user accepts
# ---------------------------------------------------------------------------


def apply_skill_proposal_from_approval(approval: Approval) -> Path:
    """Write the staged YAML manifest to disk + reload the skills registry.

    Caller is the approval-decision handler; we trust that ``approval``
    is already in APPROVED status. Returns the directory path the skill
    was written into. Raises on any error (caller marks the approval
    BACK to PENDING and reports to the founder).
    """
    payload = approval.action_payload or {}
    if payload.get("kind") != "author_skill":
        raise ValueError(
            f"Approval {approval.id} is not an author_skill payload; "
            f"got kind={payload.get('kind')!r}"
        )
    skill_name = str(payload.get("skill_name") or "").strip()
    manifest_yaml = str(payload.get("manifest_yaml") or "").strip()
    if not skill_name or not manifest_yaml:
        raise ValueError(
            f"Approval {approval.id} payload missing skill_name or manifest_yaml"
        )

    target_dir = (
        Path.home() / ".korpha" / "skills" / "agent_created"
        / skill_name.replace(".", "__")
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = target_dir / "manifest.yaml"
    manifest_path.write_text(manifest_yaml, encoding="utf-8")

    # Hot-load into the running registry so the next user message can
    # use the new skill without a restart.
    from korpha.skills import default_registry, load_yaml_skill
    skill = load_yaml_skill(target_dir)
    if skill.spec.name in default_registry.skills:
        # Replace existing — supports re-authoring with the same name.
        del default_registry.skills[skill.spec.name]
    default_registry.add(skill)
    return target_dir


# ---------------------------------------------------------------------------
# v1.5 — Python-skill authoring
# ---------------------------------------------------------------------------
#
# YAML skills wrap a single LLM call. They cannot do browser automation,
# real I/O, network calls beyond inference, or subprocess work. When the
# founder asks for a capability that needs any of those (e.g. "broadcast
# to my Teams contacts"), we draft a Python ``Skill`` subclass instead.
#
# Risk model: Python skills get the FULL Hermes scanner (regex + AST),
# stricter ``INSTALL_POLICY`` (caution → ask), bigger preview surface in
# the approval, and a self-contained import sandbox. The host imports
# the file via ``importlib`` so the skill class self-registers via its
# bottom-of-file ``register(...)`` call — same pattern every built-in
# skill already follows.


_AUTHOR_PYTHON_PROMPT = """\
You are Korpha's Python skill-authoring step. The founder asked for
a capability that needs more than a single LLM prompt — browser
automation, file I/O, network calls beyond inference, subprocess
execution, or stateful logic across calls. You'll draft a Python
``Skill`` subclass that follows Korpha's runtime contract.

Founder's request:
\"\"\"
{intent}
\"\"\"

Suggested skill name (use unless clearly wrong):  {suggested_name}
Suggested one-line description:                   {suggested_description}

# What you must produce

A SINGLE JSON object (NOT Python source!) with these fields. We will
serialize ``source`` into a .py file and ``manifest`` into a sibling
manifest.yaml so the loader can also surface metadata to the picker:

{{
  "name": "<dotted.snake_case>",                 # e.g. "channel.teams_broadcast"
  "description": "<one paragraph, ≤2 sentences>",
  "default_tier": "<workhorse|pro>",             # tier for any LLM calls inside
  "imports_required": ["<pkg>", "..."],          # third-party imports the skill needs (httpx, playwright, etc.)
  "manifest": {{                                 # picker / dashboard metadata
    "name": "<same as top-level name>",
    "description": "<same as top-level description>",
    "parameters": {{                             # what callers pass at run time
      "<param>": {{"description": "...", "default": "<optional>"}}
    }}
  }},
  "source": "<full Python source for skill.py — see template below>"
}}

# The Python source template

Your ``source`` field is the COMPLETE contents of skill.py. The host
will write it verbatim to disk and importlib it. Use this template
exactly — do not invent your own structure:

```python
\"\"\"<one-line module docstring describing the skill.>\"\"\"
from __future__ import annotations

from typing import Any

from korpha.audit.model import InferenceTier
from korpha.skills.registry import register
from korpha.skills.types import (
    Skill, SkillContext, SkillError, SkillProvenance, SkillResult, SkillSpec,
)

# Add other imports here — only stdlib + already-used third-party deps
# (httpx, yaml, sqlmodel) are guaranteed available. New deps must be
# called out in the manifest's imports_required list, but assume the
# host has already installed them by the time this code runs.


class _ImplementationSkill(Skill):
    spec = SkillSpec(
        name="<NAME>",
        description="<DESCRIPTION>",
        parameters={{
            "<PARAM>": "<param description>",
        }},
        default_tier=InferenceTier.<TIER>,  # PRO or WORKHORSE
        # Marks this as agent-authored so the curator + dashboard
        # can distinguish it from built-in skills. Don't change.
        provenance=SkillProvenance.AGENT_AUTHORED,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any]
    ) -> SkillResult:
        # Validate args first — raise SkillError with a clear message
        # if anything required is missing.
        # ...
        # Do the work. Use ctx.session for DB access, ctx.cost_tracker
        # for any LLM completions, ctx.business / ctx.founder for
        # identity context.
        # ...
        return SkillResult(
            skill_name=self.spec.name,
            summary="<short one-line summary of what happened>",
            payload={{<structured result the caller uses>}},
            cost_usd=0.0,
        )


register(_ImplementationSkill())
```

# Hard rules — violating any of these gets your skill rejected

1. **No subprocess / shell**: do not import ``subprocess``, ``os.system``,
   ``pty``, ``commands``. The agent runs in-process; spawning processes
   bypasses the trust envelope.
2. **No ``eval`` / ``exec`` / ``compile``**: pre-bake every code path.
3. **No file writes outside ``ctx.workspace`` if you need them**: don't
   touch ``~/.korpha`` or anywhere else on disk. Most skills don't
   need files at all — emit results in the SkillResult.
4. **Network only via httpx**: don't import ``socket``, ``urllib``,
   ``requests``. ``httpx`` is the project's HTTP client. Use the async
   ``httpx.AsyncClient`` with explicit timeouts.
5. **No environment-variable reads** for secrets: secrets the user
   provided live on ``ctx`` (not yet wired) — do not read ``os.environ``
   directly.
6. **No prompt injection**: don't write LLM prompts that try to
   override the host's system prompt ("ignore previous instructions").
7. **Outbound action calls go through ``ctx.gate``** (the approval
   gate) not directly. If the skill sends a message, posts to a
   third-party API, or charges money, raise SkillError suggesting the
   caller use the approval-gated wrapper. The CTO will wire it later.
8. **Browser automation skills**: do not import ``playwright`` or
   ``selenium`` directly inside ``run()``; gate them behind
   ``importlib.util.find_spec`` checks and raise a clear SkillError if
   the dep is missing. The user has not necessarily installed them.

If the founder's request can't be satisfied without violating these
rules, return ``"_status": "needs_human"`` at the top level of the
JSON and explain in ``description``. The host will surface that to the
founder so the CTO can build it manually.

# Naming

- Lowercase, dotted, snake_case (e.g. ``channel.teams_broadcast``).
- Pick a category prefix from: outreach, content, support, finance,
  growth, research, product, analytics, channel, creative,
  automation. Invent a new prefix only if none fits.

Respond with the JSON object only. No prose, no fences, no commentary.
"""


# Forbidden Python patterns we additionally check via lightweight
# substring match (cheap second filter on top of the regex/AST scanner).
# Hermes guard.py covers the major threats; this list is the Korpha-
# specific extras for the agent-authored Python case.
_FORBIDDEN_PYTHON_FRAGMENTS: tuple[str, ...] = (
    "import subprocess",
    "from subprocess",
    "import os.system",
    "os.system(",
    "import pty",
    "import commands",
    "import socket",
    "from socket",
    "import urllib",
    "from urllib",
    "import requests",
    "from requests",
    "eval(",
    "exec(",
    "compile(",
    "__import__(",
)


class AuthorPythonSkillSkill(Skill):
    """Draft a Python ``Skill`` subclass when YAML can't express the
    capability the founder asked for. Same approval+scan+install flow
    as ``meta.author_skill``; different output (real Python source +
    manifest) and stricter risk gating.

    Common cases that need this skill:
      - Browser automation (Playwright, etc.) — Teams broadcasting,
        scraping, SaaS UI integration without an official API.
      - File I/O — read a CSV, write a PDF, manipulate user files.
      - Network calls beyond inference — third-party REST APIs,
        webhooks, polling external services.
      - Stateful logic across calls — caches, in-memory queues,
        retry/backoff that holds state.
    """

    spec = SkillSpec(
        name="meta.author_python_skill",
        description=(
            "Draft a Python Skill subclass when YAML can't fulfill the "
            "founder's request — browser automation, file I/O, "
            "non-inference network calls, or stateful logic. Same "
            "approval + scan + install flow as meta.author_skill, "
            "stricter trust gating because Python can do anything "
            "Python can do. Use this when the request mentions "
            "scraping, browsing, downloading, sending DMs, integrating "
            "with a SaaS UI, or polling an external service."
        ),
        parameters={
            "intent": (
                "The founder's original request. Include enough context "
                "for the LLM to understand the desired behavior. Required."
            ),
            "suggested_name": (
                "Optional dotted snake_case name like "
                "``channel.teams_broadcast``."
            ),
            "suggested_description": (
                "Optional one-line description. The authoring LLM may "
                "rewrite it."
            ),
        },
        default_tier=InferenceTier.PRO,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any]
    ) -> SkillResult:
        intent = str(args.get("intent") or "").strip()
        if not intent:
            raise SkillError(
                "meta.author_python_skill needs `intent` — what the "
                "founder asked for in their own words."
            )
        suggested_name = str(args.get("suggested_name") or "(let the model pick)")
        suggested_description = str(
            args.get("suggested_description") or "(let the model pick)"
        )

        # ---- Phase 1: ask LLM for the JSON envelope ----
        prompt = _AUTHOR_PYTHON_PROMPT.format(
            intent=intent,
            suggested_name=suggested_name,
            suggested_description=suggested_description,
        )
        request = CompletionRequest(
            messages=[
                Message(role=Role.SYSTEM, content=(
                    "You are the Korpha Python skill-authoring step. "
                    "Output strict JSON only — no prose, no code fences."
                )),
                Message(role=Role.USER, content=prompt),
            ],
            tier=self.spec.default_tier or InferenceTier.PRO,
            session_key=f"meta-author-python-{ctx.business.id}-{intent[:32]}",
            max_tokens=agent_max_tokens(),
            timeout_seconds=agent_timeout(),
        )
        response = await ctx.cost_tracker.complete(
            request,
            session=ctx.session,
            business_id=ctx.business.id,
            agent_role_id=ctx.invoking_agent_role_id,
        )
        try:
            envelope = extract_json_dict(response.content)
        except Exception as exc:
            raise SkillError(
                f"Python authoring LLM returned non-JSON: "
                f"{response.content[:200]!r}"
            ) from exc

        # Skill said "this needs a human" — surface that, no install.
        if envelope.get("_status") == "needs_human":
            return SkillResult(
                skill_name=self.spec.name,
                payload={
                    "status": "needs_human",
                    "reason": str(envelope.get("description") or ""),
                    "intent": intent,
                },
                summary=(
                    "Authoring LLM declined: this request needs a human "
                    "implementation. Reason: "
                    f"{str(envelope.get('description') or '(no detail)')[:200]}"
                ),
                cost_usd=float(response.cost_usd or 0.0),
            )

        # ---- Phase 2: validate the envelope shape ----
        problems = _validate_python_envelope(envelope)
        if problems:
            raise SkillError(
                "Authored Python skill failed validation: "
                + "; ".join(problems)
            )

        source_text = str(envelope["source"])
        manifest = envelope.get("manifest") or {
            "name": envelope["name"],
            "description": envelope.get("description", ""),
            "parameters": {},
        }
        manifest_yaml = yaml.safe_dump(
            manifest, sort_keys=False, default_flow_style=False
        )

        # ---- Phase 3: scan the Python source + manifest together ----
        scan = _scan_authored_python(envelope, source_text, manifest_yaml)
        policy = INSTALL_POLICY.get(_AGENT_CREATED_TRUST)
        if policy is None:
            raise SkillError(
                f"INSTALL_POLICY missing entry for {_AGENT_CREATED_TRUST!r}"
            )
        verdict_idx = VERDICT_INDEX.get(scan.verdict, 2)
        decision = policy[verdict_idx]
        if decision == "block":
            raise SkillError(
                f"Authored Python skill blocked by scanner "
                f"({scan.verdict}): {scan.summary}"
            )

        # Stricter Python policy: caution = ask (not auto-allow). We
        # override the standard agent-created policy here because
        # Python can do anything Python can do.
        requires_user_confirmation = scan.verdict != "safe"

        # ---- Phase 4: stage as Approval ----
        approval = _stage_python_skill_approval(
            ctx=ctx,
            envelope=envelope,
            source_text=source_text,
            manifest_yaml=manifest_yaml,
            intent=intent,
            scan=scan,
            requires_user_confirmation=requires_user_confirmation,
        )

        return SkillResult(
            skill_name=self.spec.name,
            payload={
                "approval_id": str(approval.id),
                "skill_name": str(envelope.get("name")),
                "scan_verdict": scan.verdict,
                "findings_count": len(scan.findings),
                "source_lines": len(source_text.splitlines()),
                "decision": decision,
                "requires_user_confirmation": requires_user_confirmation,
                "imports_required": list(envelope.get("imports_required") or []),
            },
            summary=(
                f"Drafted Python skill '{envelope.get('name')}' "
                f"({len(source_text.splitlines())} lines, "
                f"scan: {scan.verdict}, "
                f"{len(scan.findings)} findings). "
                f"Awaiting your approval at /app/approvals/{approval.id}."
            ),
            cost_usd=float(response.cost_usd or 0.0),
        )


def _validate_python_envelope(envelope: dict[str, Any]) -> list[str]:
    """Shape + safety checks on the authored Python envelope."""
    problems: list[str] = []
    name = envelope.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        problems.append(
            f"name must be dotted snake_case, got {name!r}"
        )
    description = envelope.get("description")
    if not isinstance(description, str) or not description.strip():
        problems.append("description is required")

    source = envelope.get("source")
    if not isinstance(source, str) or not source.strip():
        problems.append("source is required")
        return problems  # rest of checks need source

    # Cheap substring scan for forbidden imports / calls before the
    # full guard runs — surface clear errors to the LLM-author loop
    # rather than waiting for the regex scanner's ranked verdict.
    for fragment in _FORBIDDEN_PYTHON_FRAGMENTS:
        if fragment in source:
            problems.append(
                f"source contains forbidden pattern {fragment!r}"
            )

    # AST sanity: must parse; must define at least one Skill subclass;
    # must call register(...) at module level so importlib hot-load
    # works without a separate registration step.
    import ast
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        problems.append(f"source has SyntaxError: {exc.msg} (line {exc.lineno})")
        return problems

    has_skill_subclass = False
    has_module_register_call = False
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            # Heuristic: class inherits from something named "Skill".
            for base in node.bases:
                if (
                    isinstance(base, ast.Name) and base.id == "Skill"
                ) or (
                    isinstance(base, ast.Attribute) and base.attr == "Skill"
                ):
                    has_skill_subclass = True
                    break
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            call = node.value
            fn = call.func
            if isinstance(fn, ast.Name) and fn.id == "register":
                has_module_register_call = True
            elif isinstance(fn, ast.Attribute) and fn.attr == "register":
                has_module_register_call = True

    if not has_skill_subclass:
        problems.append("source defines no class inheriting from Skill")
    if not has_module_register_call:
        problems.append(
            "source must call register(...) at module level so the "
            "skill is added to the registry on import"
        )

    return problems


def _scan_authored_python(
    envelope: dict[str, Any], source_text: str, manifest_yaml: str
) -> ScanResult:
    """Run the Hermes guard over a synthetic skill dir containing both
    the .py source and the manifest.yaml."""
    import tempfile

    skill_name = str(envelope.get("name", "unknown")).replace(".", "__")
    with tempfile.TemporaryDirectory(prefix="korpha-author-py-") as td:
        skill_dir = Path(td) / skill_name
        skill_dir.mkdir(parents=True)
        (skill_dir / "manifest.yaml").write_text(manifest_yaml, encoding="utf-8")
        (skill_dir / "skill.py").write_text(source_text, encoding="utf-8")
        return scan_skill(skill_dir, source="agent-created")


def _stage_python_skill_approval(
    *,
    ctx: SkillContext,
    envelope: dict[str, Any],
    source_text: str,
    manifest_yaml: str,
    intent: str,
    scan: ScanResult,
    requires_user_confirmation: bool,
) -> Approval:
    """Same staging shape as YAML approvals; payload kind differs so
    the apply path knows which writer to call."""
    from sqlmodel import select

    from korpha.cofounder.model import AgentRole

    skill_name = str(envelope["name"])
    decision_word = "ask" if requires_user_confirmation else "auto-allow"

    cto_role = ctx.session.exec(
        select(AgentRole)
        .where(AgentRole.business_id == ctx.business.id)
        .where(AgentRole.role_type == RoleType.CTO)
    ).first()
    role_id = cto_role.id if cto_role else None
    if role_id is None:
        any_role = ctx.session.exec(
            select(AgentRole).where(AgentRole.business_id == ctx.business.id)
        ).first()
        role_id = any_role.id if any_role else None
        if role_id is None:
            raise SkillError(
                "No agent role exists for this business yet — cannot stage a "
                "code-change approval. Run onboarding first."
            )

    findings_payload = [
        {
            "pattern_id": getattr(f, "pattern_id", ""),
            "severity": getattr(f, "severity", ""),
            "category": getattr(f, "category", ""),
            "description": getattr(f, "description", ""),
        }
        for f in scan.findings
    ]
    summary_text = (
        f"Author Python skill '{skill_name}'. "
        f"{len(source_text.splitlines())} lines of Python. "
        f"Scan: {scan.verdict} ({len(scan.findings)} findings). "
        f"Decision policy: {decision_word}.\n\n"
        f"Original intent:\n{intent[:400]}"
    )
    approval = Approval(
        business_id=ctx.business.id,
        agent_role_id=role_id,
        action_class=ActionClass.CODE_CHANGE,
        platform="meta",
        proposal_summary=summary_text,
        action_payload={
            "kind": "author_python_skill",
            "skill_name": skill_name,
            "intent": intent,
            "envelope": envelope,
            "source": source_text,
            "manifest_yaml": manifest_yaml,
            "scan": {
                "verdict": scan.verdict,
                "summary": scan.summary,
                "findings": findings_payload,
                "trust_level": scan.trust_level,
            },
            "trust_level": _AGENT_CREATED_TRUST,
        },
    )
    ctx.session.add(approval)
    ctx.session.commit()
    ctx.session.refresh(approval)
    return approval


def apply_python_skill_proposal_from_approval(approval: Approval) -> Path:
    """Write the staged Python skill to disk and importlib-load it so
    the new ``Skill`` subclass registers itself.

    Files land at:
      ``~/.korpha/skills/agent_created/python/<name_with_underscores>/skill.py``

    plus a sibling manifest.yaml (metadata only — the runtime never
    parses it for Python skills, but the dashboard / picker reads it).
    """
    payload = approval.action_payload or {}
    if payload.get("kind") != "author_python_skill":
        raise ValueError(
            f"Approval {approval.id} is not an author_python_skill payload; "
            f"got kind={payload.get('kind')!r}"
        )
    skill_name = str(payload.get("skill_name") or "").strip()
    source_text = str(payload.get("source") or "").strip()
    manifest_yaml = str(payload.get("manifest_yaml") or "").strip()
    if not skill_name or not source_text:
        raise ValueError(
            f"Approval {approval.id} payload missing skill_name or source"
        )

    target_dir = (
        Path.home() / ".korpha" / "skills" / "agent_created" / "python"
        / skill_name.replace(".", "__")
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    skill_path = target_dir / "skill.py"
    skill_path.write_text(source_text, encoding="utf-8")
    if manifest_yaml:
        (target_dir / "manifest.yaml").write_text(manifest_yaml, encoding="utf-8")

    # Hot-load: import the file with a stable module name so subsequent
    # re-authoring with the same name overrides cleanly.
    import importlib.util
    import sys

    module_name = f"_korpha_agent_skill_{skill_name.replace('.', '_')}"
    spec_obj = importlib.util.spec_from_file_location(module_name, skill_path)
    if spec_obj is None or spec_obj.loader is None:
        raise RuntimeError(
            f"importlib could not load {skill_path}"
        )
    # Drop any previously-imported version of this module so the new
    # source actually executes (its register() call refreshes the
    # registry entry).
    if module_name in sys.modules:
        del sys.modules[module_name]
    module = importlib.util.module_from_spec(spec_obj)
    sys.modules[module_name] = module
    spec_obj.loader.exec_module(module)
    return target_dir


register(AuthorSkillSkill())
register(AuthorPythonSkillSkill())


__all__ = [
    "AuthorPythonSkillSkill",
    "AuthorSkillSkill",
    "apply_python_skill_proposal_from_approval",
    "apply_skill_proposal_from_approval",
]
