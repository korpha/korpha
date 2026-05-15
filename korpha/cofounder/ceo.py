"""CEO — the AI cofounder Founder talks to by default.

Implements the cofounder hypothesis from BRIEF.md:

    "It doesn't ask Mike what to do — it shows him what it would do and asks
    for approval. Pushes back when Mike is wrong with a better path — never
    a dead-end 'no.' Mike feels in control through approval, not direction."

For now the CEO has two operations:

- :meth:`respond` — free-form LLM response in the cofounder's voice. Used by
  the conversation surface (web/Telegram/etc.) when the Founder asks a question
  and isn't waiting on a structured proposal.
- :meth:`propose` — produces a structured ``Plan`` and routes it through the
  ApprovalGate. The Plan has summary / rationale / next-action / time-estimate
  fields the UI can render as an approval card.

Both go through the Inference Pool (Pro tier by default). Reasoning content
from thinking models (DeepSeek V4 Pro/Flash) is captured but hidden from the
Founder unless they explicitly request it.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlmodel import Session

from korpha._jsonext import extract_json_dict
from korpha.approvals.gate import (
    ApprovalGate,
    ProposalAccepted,
    ProposalDenied,
    ProposalPending,
    ProposalResult,
)
from korpha.approvals.model import ActionClass
from korpha.audit.model import InferenceTier
from korpha.business.model import Business
from korpha.cofounder.chief_of_staff import ChiefOfStaff, Digest
from korpha.cofounder.clarify import ClarifyRequest, parse_clarify
from korpha.cofounder.contract import BASE_EXECUTION_CONTRACT
from korpha.cofounder.hiring import HiringService
from korpha.cofounder.model import AgentRole, RoleType
from korpha.cofounder.workforce import DispatchSummary, Workforce
from korpha.identity.model import Founder
from korpha.inference.cost_tracker import CostTracker
from korpha.inference.limits import agent_max_tokens, agent_timeout
from korpha.inference.types import (
    CompletionRequest,
    CompletionResponse,
    Message,
    Role,
)
from korpha.skills.registry import SkillRegistry, default_registry
from korpha.skills.types import SkillContext, SkillResult

# CEO system prompt = shared execution contract + role-specific block.
# Refined per docs/PROMPT_AUDIT.md (Paperclip-pattern lift). Defined as
# a module-level constant so the eval harness can import it directly.
_CEO_VOICE_DEFAULT = BASE_EXECUTION_CONTRACT + (
    "\n## Role: CEO (your role)\n"
    "\n"
    "You are the AI cofounder of an online business owned by the human "
    "Founder. The Founder is your boss; you are not their assistant — "
    "you are their second-in-command.\n"
    "\n"
    "Strategic posture:\n"
    "- Own the P&L. Every move rolls up to revenue, runway, and capital "
    "  efficiency. If you miss the economics, no one else will catch them.\n"
    "- Default to action. Stalling usually costs more than a bad call you "
    "  can revise.\n"
    "- Optimize for learning speed and reversibility. Move fast on two-way "
    "  doors; slow down on one-way doors.\n"
    "- Protect focus. Too many priorities are usually worse than one wrong "
    "  one.\n"
    "- Treat every dollar, agent-hour, and approval as a bet. Know the "
    "  thesis and the expected return.\n"
    "- Pull for bad news. If problems stop surfacing in your digests, "
    "  you've lost your information edge.\n"
    "\n"
    "What you DO personally:\n"
    "- Set priorities + propose plans the Founder can approve.\n"
    "- Resolve cross-team conflicts and ambiguity.\n"
    "- Approve / reject the Director's proposals before they ship.\n"
    "- Hire new Directors / Workers when capacity is missing.\n"
    "- Talk to the Founder directly (the one human in the loop).\n"
    "\n"
    "What you DON'T do (MUST NOT):\n"
    "- Write code, draft copy, run analytics, or answer support tickets "
    "  yourself. Even if it's small. DELEGATE it.\n"
    "- Ask the Founder open questions like 'what do you want?'. Propose "
    "  a path, then ask for approval.\n"
    "- Dead-end with 'no'. If the Founder's instinct is wrong, push back "
    "  with a *better* path.\n"
    "- Add fluff, corporate warm-ups, or hedged non-answers. Plain "
    "  language, lead with the point.\n"
    "\n"
    "Routing map (use this when delegating):\n"
    "- Code, deploys, infra, builds, MVP, landing-page tech → CTO\n"
    "- Marketing, content, social, ads, email, brand, growth → CMO\n"
    "- Support, ops, analytics, KPI, customer process → COO\n"
    "- Cross-functional → split into per-Director subtasks rather than "
    "  picking one\n"
    "- Niche / pricing / validation / first-feature decisions → call a "
    "  skill (niche.find_micro_niches, validate.score_idea, "
    "  pricing.recommend_tiers, product.first_feature) before delegating "
    "  execution\n"
    "\n"
    "Delegation format (use literally — the orchestrator parses these):\n"
    "- Tag each delegation with a bracket prefix: ``[CTO]``, ``[CMO]``, "
    "  or ``[COO]`` followed by the directive. Example:\n"
    "    [CTO] Stand up the waitlist landing page on Carrd by Friday.\n"
    "    [CMO] Draft 3 cold-email opener variants targeting indie devs.\n"
    "- Even if it's the obvious owner, include the tag — bare "
    "  'the CTO will…' isn't enough for the routing layer to pick it up.\n"
    "- For delegate-able work that doesn't need an immediate Director "
    "  (e.g. a typo fix), still tag it: ``[COO] Assign the typo fix to "
    "  a copywriter Worker.``\n"
    "\n"
    "Lenses to cite when reasoning: P&L lens, capital-as-bets, "
    "two-way-vs-one-way-doors, focus-as-strategy, hire-slow-fire-fast, "
    "stay-close-to-customer, pull-for-bad-news.\n"
    "\n"
    "Brevity discipline (do NOT exceed):\n"
    "- Quick yes/no question (ship vs wait, go vs no-go): ≤ 30 words. "
    "  One-line call + one-line reason. Don't ramble.\n"
    "- Plan from scratch (e.g. 6-month income plan, niche launch): "
    "  ≤ 350 words. **MAX 10 bullets total**, regardless of how "
    "  comprehensive the ask feels. If your plan doesn't fit in 10 "
    "  bullets, you're at the wrong level — group items into "
    "  phases / themes, don't enumerate. Include at least one "
    "  ``[CTO]`` / ``[CMO]`` / ``[COO]`` delegation tag. **Always "
    "  end with a single 'next action this week' line** that "
    "  literally uses the phrase *this week* (or *next week* if the "
    "  Founder's planning horizon starts later) — the orchestrator "
    "  surfaces it as the first work item.\n"
    "- Strategic recommendation / pushback: ≤ 250 words.\n"
    "- Routing-only reply (delegating without strategic context): "
    "  ≤ 100 words.\n"
    "- If you're padding to 'sound thorough', stop. Density beats "
    "  length. The Founder reads everything you write.\n"
    "\n"
    "Voice and tone:\n"
    "- Lead with the recommendation, then 3-5 reasons, then the single "
    "  next action this week, then estimated time + expected impact.\n"
    "- Direct, specific, brief. 'Use' not 'utilize'. 'Start' not "
    "  'initiate'. Active voice.\n"
    "- Match intensity to stakes. A pricing call gets gravity. A typo "
    "  question gets brevity.\n"
    "- No exclamation points unless something is genuinely on fire or "
    "  genuinely worth celebrating.\n"
    "- Trust the Founder's intelligence; don't over-explain.\n"
    "- Own uncertainty. 'I don't know yet, here's what I'd test' beats "
    "  a hedged non-answer."
)


@dataclass(frozen=True)
class HandleResult:
    """Output of CEO.handle() — the user-facing reply plus any skills run."""

    content: str
    """The final message to surface to the Founder."""

    skills_used: list[SkillResult]
    cost_usd: float
    reasoning: str | None
    router_response: CompletionResponse
    final_response: CompletionResponse
    clarify: "ClarifyRequest | None" = None
    """Set when the cofounder is asking a structured clarifying
    question. Channels render the question + choices appropriately
    (web: HTMX buttons; TUI: numbered list; messaging: numbered
    list appended to ``content``)."""


@dataclass(frozen=True)
class _RouterDecision:
    action: str  # "respond" | "use_skill" | "clarify"
    content: str | None = None
    skill_name: str | None = None
    skill_args: dict[str, Any] | None = None
    clarify: "ClarifyRequest | None" = None


def _skill_router_prompt(skill_specs: list[Any], founder_message: str) -> str:
    """Ask CEO to either reply directly, pick a skill, or propose authoring
    a new one when the request describes a capability nothing covers."""
    catalog_lines = []
    has_author_yaml = False
    has_author_python = False
    for spec in skill_specs:
        if spec.name == "meta.author_skill":
            has_author_yaml = True
            continue  # special-cased below
        if spec.name == "meta.author_python_skill":
            has_author_python = True
            continue  # special-cased below
        catalog_lines.append(f"- {spec.name}: {spec.description}")
        for pname, pdesc in spec.parameters.items():
            catalog_lines.append(f"    - {pname}: {pdesc}")
    catalog = "\n".join(catalog_lines) if catalog_lines else "(no skills available)"

    fallback_section = ""
    if has_author_yaml or has_author_python:
        fallback_section = (
            "\nFALLBACK — when the Founder is asking for a CAPABILITY (a tool, "
            "automation, or recurring task) but no listed skill above fits, "
            "Korpha can author a new skill and stage it for Founder "
            "approval. Use this whenever the ask describes *something to be "
            "done repeatedly* (\"give me a tool that…\", \"set up a thing to…\", "
            "\"I want to be able to…\", \"build me…\", \"can you classify / "
            "generate / track / send …\"). Do NOT use the fallback for "
            "one-shot questions, strategic discussions, or open-ended chat — "
            "those still go to `respond`.\n\n"
            "Pick the authoring skill based on what the new capability "
            "needs to do:\n\n"
            "  * **`meta.author_skill`** (YAML) — for capabilities that are\n"
            "    JUST an LLM call: classify, draft, summarize, translate,\n"
            "    extract, generate, score, rewrite. The skill takes inputs,\n"
            "    runs ONE LLM prompt, returns a structured result. Cannot\n"
            "    do browser automation, file I/O, or third-party API calls.\n\n"
            "  * **`meta.author_python_skill`** (Python) — for capabilities\n"
            "    that need real I/O. Pick this when the request mentions:\n"
            "      - browser automation (Teams, LinkedIn, scraping a site,\n"
            "        \"open Chrome and…\", \"navigate to…\", \"click…\")\n"
            "      - file I/O (\"read this CSV\", \"save to PDF\",\n"
            "        \"manipulate this image\")\n"
            "      - third-party REST APIs beyond the LLM (\"call Stripe\",\n"
            "        \"post to Slack\", \"look up DNS\", \"check weather\")\n"
            "      - sending DMs / emails / messages anywhere outside chat\n"
            "      - polling external services\n"
            "      - downloading or uploading files\n"
            "      - running multi-step interactive flows that hold state\n\n"
            "If you're unsure, prefer `meta.author_python_skill` when the\n"
            "request mentions any external system the agent has to talk to.\n"
            "YAML can't reach those; only Python can.\n\n"
            "Format for either:\n"
            '{"action":"use_skill","skill_name":"<meta.author_skill | '
            'meta.author_python_skill>",'
            '"skill_args":{"intent":"<the Founder\'s message verbatim>",'
            '"suggested_name":"<dotted.snake_case>",'
            '"suggested_description":"<one short line>"}}\n'
        )

    return (
        f"Founder's message:\n\n{founder_message}\n\n"
        f"Available skills:\n{catalog}\n"
        + fallback_section +
        "\nIf one of the listed skills above clearly fits, return JSON:\n"
        '{"action":"use_skill","skill_name":"<one of the names above>",'
        '"skill_args":{"<param>":"<value>"}}\n'
        "Pass concrete values inferred from the Founder's message; do not "
        "echo placeholder text.\n\n"
        "If the Founder is chatting / asking a question / wants strategic "
        "discussion (NOT a capability request), respond directly. Return:\n"
        '{"action":"respond","content":"<your full cofounder reply, in your '
        'normal voice — direct, specific, opinionated>"}\n\n'
        "If you NEED clarification before you can act — there's a meaningful "
        "choice the founder should weigh in on, not a low-stakes default — "
        "ask a structured question. Up to 4 concrete choices; the UI renders "
        "them as clickable options. Reserve this for genuine forks (which "
        "niche, which copy direction, which platform first), NOT for trivial "
        "yes/no confirmations or open-ended chat. Return:\n"
        '{"action":"clarify","question":"<short, specific question>",'
        '"choices":["<option 1>","<option 2>","<option 3>","<option 4>"]}\n'
        "Choices must be standalone phrases, not sentences. Omit the "
        '"choices" key entirely for an open-ended ask.\n\n'
        "Decision priority: (1) listed skill match → use_skill, "
        "(2) capability request, no match → meta.author_skill (YAML) or "
        "meta.author_python_skill (real I/O) per the rules above, "
        "(3) genuine fork that needs founder input → clarify, "
        "(4) chat / discussion → respond.\n\n"
        "**CRITICAL — no fake actions in `respond`:** If your "
        "response text would contain past-tense claims like "
        "'I spawned X', 'I created Y', 'I approved Z', 'I queued', "
        "'I hired', 'I added a card', 'I set up', 'I configured' — "
        "STOP. Those are actions you cannot perform inside a "
        "`respond` reply; they happen only via skills. You MUST "
        "pick a `use_skill` for any imperative the Founder asked "
        "you to do. If no listed skill fits the request, return "
        "`clarify` to ask the Founder what to call the new "
        "skill, or `meta.author_python_skill` to build one. The "
        "Founder treats your past-tense words as truth — claiming "
        "an action you didn't take is the worst kind of failure.\n\n"
        "Output strict JSON only. No surrounding prose."
    )


def _skill_synth_prompt(founder_message: str, skill_result: SkillResult) -> str:
    """After a skill ran, ask CEO to produce the user-facing message that
    incorporates the skill output naturally.

    Big skill payloads (e.g. ``research.scrape`` returning a 60K-char
    page) get spilled to disk before being embedded — the model sees
    a preview + path, can read more if needed, and we don't burn 15K
    tokens on every synth call. See ``korpha.limits``.
    """
    from korpha.limits import persist_if_oversized, serialize_for_prompt

    payload_text = serialize_for_prompt(skill_result.payload)
    payload_text = persist_if_oversized(
        payload_text,
        ref_id=f"skill-{skill_result.skill_name}",
    )
    return (
        f"Founder's original message:\n\n{founder_message}\n\n"
        f"You just ran the skill `{skill_result.skill_name}`. Its output:\n\n"
        f"{payload_text}\n\n"
        "Produce your cofounder reply to the Founder. Weave the skill output in "
        "naturally — do NOT just dump the JSON. Highlight the recommended path, "
        "explain why briefly, and end with a clear next-step question or ask "
        "for approval. Direct, specific, no marketing fluff."
    )


def _skill_chain_prompt(
    founder_message: str,
    skills_used: list[SkillResult],
    skill_specs: list[Any],
) -> str:
    """Ask the CEO 'do you need another skill, or are you done?'
    after a skill has already run. Same router JSON schema as
    ``_skill_router_prompt`` — so the LLM can pick another skill
    (chains the loop) or return ``respond`` (the synth happens
    next, NOT inside this call).

    Used by ``handle_stream``'s multi-skill chain loop so a single
    founder turn can call N skills in sequence — needed for
    requests like 'reassign these cards and then fire them' which
    take two skill calls.
    """
    from korpha.limits import serialize_for_prompt

    catalog_lines = [
        f"- {spec.name}: {spec.description}"
        for spec in skill_specs
        if spec.name not in {
            "meta.author_skill", "meta.author_python_skill",
        }
    ]
    catalog = "\n".join(catalog_lines) if catalog_lines else "(none)"
    chain_log_parts = []
    for i, sr in enumerate(skills_used, 1):
        payload = serialize_for_prompt(sr.payload)
        # Trim long payloads to keep the chain prompt cheap.
        if len(payload) > 800:
            payload = payload[:800] + " …[truncated]"
        chain_log_parts.append(
            f"{i}. {sr.skill_name} → {sr.summary}\n"
            f"   payload: {payload}"
        )
    chain_log = "\n".join(chain_log_parts)
    return (
        f"Founder's original message:\n\n{founder_message}\n\n"
        f"Skills run so far ({len(skills_used)}):\n{chain_log}\n\n"
        f"Available skills (you may pick another):\n{catalog}\n\n"
        "Decide what to do next. Output strict JSON only:\n\n"
        "- If you need ANOTHER skill to fulfill the founder's "
        "request (e.g. you ran reassign and now want to fire_sprint "
        "the same cards), return:\n"
        '  {"action":"use_skill","skill_name":"<one from above>",'
        '"skill_args":{"<param>":"<value>"}}\n\n'
        "- If all needed skills are done and you can now answer "
        "the founder directly, return:\n"
        '  {"action":"respond"}\n\n'
        "NEVER claim past-tense actions in this decision — those "
        "happen in the next phase. Pick another skill ONLY if it "
        "is strictly required by the founder's original ask. If "
        "in doubt, return `respond` and let the synth phase "
        "summarize what's done."
    )


def _skill_synth_prompt_multi(
    founder_message: str,
    skills_used: list[SkillResult],
) -> str:
    """Multi-skill version of ``_skill_synth_prompt``. Renders the
    final founder-facing message from a chain of skill results."""
    from korpha.limits import persist_if_oversized, serialize_for_prompt

    if len(skills_used) == 1:
        return _skill_synth_prompt(founder_message, skills_used[0])
    parts = []
    for i, sr in enumerate(skills_used, 1):
        payload = serialize_for_prompt(sr.payload)
        payload = persist_if_oversized(
            payload, ref_id=f"skill-{sr.skill_name}-{i}",
        )
        parts.append(
            f"### Skill {i}: `{sr.skill_name}`\n"
            f"Summary: {sr.summary}\n"
            f"Payload:\n{payload}"
        )
    chain_text = "\n\n".join(parts)
    return (
        f"Founder's original message:\n\n{founder_message}\n\n"
        f"You ran {len(skills_used)} skill(s) in this turn:\n\n"
        f"{chain_text}\n\n"
        "Produce your cofounder reply. Weave the skill outputs "
        "together naturally — don't list them one by one unless "
        "the founder needs the breakdown. Highlight the net "
        "outcome, what's now true in the business, and end with a "
        "clear next-step question. Direct, specific, no marketing "
        "fluff. Anti-hallucination rule: only state actions that "
        "are reflected in the skill summaries above — anything "
        "else, mark as 'pending' or 'will require'."
    )


_LINE_KIND_KEYWORDS: dict[str, tuple[str, ...]] = {
    "kdp": (
        "kdp", "kindle direct", "kindle publishing", "self-publish",
        "self publish", "ebook", "e-book", "amazon book",
        "scavenger hunt", "learn to draw", "learn-to-draw",
        "activity book", "coloring book", "journal", "workbook",
    ),
    "pod": (
        "print on demand", "print-on-demand", "printify", "printful",
        "t-shirt", "tshirt", "t shirt", "mug", "tote", "hoodie",
        "merch", "etsy shirt", "amazon merch",
    ),
    "info": (
        "online course", "info product", "membership site",
        "ebook course", "mini-course", "info pack",
    ),
    "saas": (
        "saas", "web app", "mobile app", "micro saas",
    ),
    "affiliate": (
        "affiliate site", "affiliate marketing", "review site",
        "comparison hub",
    ),
    "agency": (
        "done-for-you", "dfy service", "productized service",
    ),
    # Not a canonical kind but a strong "media" signal — the
    # bootstrap planner can map this into info/affiliate at parse
    # time, and seeing it alongside another kind is a multi-line tell.
    "media": (
        "youtube channel", "youtube shorts", "tiktok channel",
        "instagram channel", "reels channel", "video channel",
    ),
}

_MULTI_LINE_IMPERATIVES = (
    "build me", "build us", "set up", "set them up",
    "spawn", "execute these", "execute those", "launch these",
    "launch those", "kick off", "start these", "start those",
    "run all of", "run each of",
)

_MULTI_LINE_COUNTERS = (
    "method 1", "method 2", "method 3",
    "business 1", "business 2",
    "all of those", "every one", "each one of",
)


_SPAWN_VERBS = (
    "spawn", "spawn a", "spawn an", "spawn the",
    "hire", "hire a", "hire an", "hire the",
    "add a", "add an", "i need a", "i need an",
    "create a", "create an",
    "bring on", "stand up",
)

_CSUITE_TOKENS = {
    "cto": "cto",
    "c.t.o": "cto",
    "chief technology": "cto",
    "chief tech": "cto",
    "cmo": "cmo",
    "c.m.o": "cmo",
    "chief marketing": "cmo",
    "coo": "coo",
    "c.o.o": "coo",
    "chief operating": "coo",
    "chief operations": "coo",
}


_REASSIGN_PHRASES = (
    "reassign", "re-assign", "reassign them", "yes reassign",
    "apply the fixes", "apply the reassign", "switch owners",
    "swap owners", "redo the routing", "rebalance",
)


def _detect_reassign_pairs(
    msg: str, history: list["Message"] | None,
) -> list[dict[str, str]] | None:
    """If the founder said a reassign-trigger phrase AND the most
    recent agent message lists card-ID + role pairs, return the
    parsed [(card_id, new_owner_role), ...] list. Else None.

    Pair-finding heuristic: for each 8-char hex card id in the
    agent message, scan a ±80-char window around it for one of
    {CTO, CMO, COO}. Pair them. Multiple ids → multiple pairs.
    """
    if not msg:
        return None
    low = msg.strip().lower().rstrip(".!?")
    if low not in _REASSIGN_PHRASES and not any(
        p in low for p in _REASSIGN_PHRASES
    ):
        return None
    if not history:
        return None
    last_assistant = None
    for m in reversed(history):
        try:
            role_str = (
                m.role.value if hasattr(m.role, "value") else str(m.role)
            ).lower()
        except Exception:  # noqa: BLE001
            role_str = ""
        if role_str == "assistant":
            last_assistant = m
            break
    if last_assistant is None or not last_assistant.content:
        return None
    text = last_assistant.content
    # Group card IDs by the sentence they appear in. Each sentence
    # has at most one role assignment; cards in that sentence share
    # the role. Handles patterns like:
    #   "Reassign A and B to CMO. Keep C, D, E with CTO."
    # which a pure-nearest-token heuristic gets wrong because C is
    # closer to the previous 'CMO' than to the 'CTO' at the end.
    pairs: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    # Sentence delimiter — period + space, newline, or bullet boundary.
    for sentence in re.split(r"(?<=[.!?])\s+|\n+|(?<=:)\s+|(?<=-)\s+", text):
        role_match = re.search(r"\b(CTO|CMO|COO)\b", sentence, re.I)
        if not role_match:
            continue
        role = role_match.group(1).lower()
        for m in re.finditer(r"\b([0-9a-f]{8})\b", sentence, re.I):
            cid = m.group(1).lower()
            if cid in seen_ids:
                continue
            seen_ids.add(cid)
            pairs.append({"card_id": cid, "new_owner_role": role})
    return pairs if pairs else None


_FIRE_SPRINT_PHRASES = (
    "go", "yes go", "yes, go", "ok go", "ok, go",
    "fire", "fire it", "fire away", "fire the sprint",
    "proceed", "do it", "go ahead", "ship it",
    "approve", "approve it", "approved",
    "yes proceed", "yes, proceed",
)

# 8-32 hex chars, optionally with dashes; word-boundary delimited.
# Matches '7d4b8115', '7d4b8115-1234-5678-9abc-def0...'.
_CARD_ID_PATTERN = re.compile(
    r"\b[0-9a-f]{8}(?:-?[0-9a-f]{4,12})*\b", re.I,
)


def _detect_fire_sprint(
    msg: str, history: list["Message"] | None,
    *,
    business_id: "UUID | None" = None,
    session: "Any | None" = None,
) -> list[str] | None:
    """If the founder said a short approval phrase, return card IDs
    to fire. Tries three strategies in order:

    1. Walk back through the last 5 assistant messages, pulling
       8-char hex prefixes (the format the CEO writes in chat).
       Stops at the first message that has IDs.
    2. If no IDs found in chat and a live DB session is provided,
       fall back to "every card currently in READY or
       IN_PROGRESS" — the founder's intent was clear ("go"), it's
       just that the prior synth message didn't use IDs.
    3. Return None if neither works.

    Returns None on non-approval messages so the router LLM stays
    in control of unrelated turns."""
    if not msg:
        return None
    low = msg.strip().lower().rstrip(".!?")
    if low not in _FIRE_SPRINT_PHRASES and not any(
        low.startswith(p + " ") or low.startswith(p + ",")
        for p in _FIRE_SPRINT_PHRASES
    ):
        return None

    # Collect from BOTH chat-history (Strategy 1) AND the live
    # board (Strategy 3), then union + dedupe. Bare "go" / "yes" /
    # "fire it" expresses intent over the whole sprint, not just
    # the one card the CEO happened to cite in its last synth.
    # If the founder wanted a narrower scope they would have named
    # the card (handled outside this heuristic).
    seen: set[str] = set()
    out: list[str] = []
    if history:
        assistant_count = 0
        for m in reversed(history):
            try:
                role = (
                    m.role.value if hasattr(m.role, "value")
                    else str(m.role)
                )
            except Exception:  # noqa: BLE001
                role = str(getattr(m, "role", ""))
            if role.lower() != "assistant":
                continue
            assistant_count += 1
            if assistant_count > 5:
                break
            content = m.content or ""
            for tok in _CARD_ID_PATTERN.findall(content):
                norm = tok.replace("-", "").lower()
                if len(norm) < 8:
                    continue
                prefix = norm[:8]
                if prefix in seen:
                    continue
                seen.add(prefix)
                out.append(prefix)

    # Live DB pull: every READY/IN_PROGRESS card the founder could
    # mean. Unions with chat IDs so we never silently leave half
    # the sprint behind.
    if session is not None and business_id is not None:
        try:
            from sqlmodel import select
            from korpha.kanban.model import KanbanCard, KanbanColumn

            cards = list(session.exec(
                select(KanbanCard)
                .where(KanbanCard.business_id == business_id)
                .where(
                    KanbanCard.column.in_([  # type: ignore[union-attr]
                        KanbanColumn.READY,
                        KanbanColumn.IN_PROGRESS,
                    ])
                )
                .order_by(KanbanCard.created_at)  # type: ignore[union-attr]
            ))[:12]
            for c in cards:
                prefix = str(c.id).replace("-", "").lower()[:8]
                if prefix in seen:
                    continue
                seen.add(prefix)
                out.append(prefix)
        except Exception:  # noqa: BLE001
            pass

    return out if out else None


def _detect_spawn_csuite(msg: str) -> list[str] | None:
    """Return list of c-suite role-strings the founder asked to
    spawn (e.g. ['cto', 'cmo']), or None if no spawn intent.

    Triggers on imperative + c-suite-token co-occurrence. Tolerates
    'spawn CTO and CMO', 'hire a CTO', 'I need a CMO', 'spawn cto +
    cmo', etc. False-positives are caught by the skill (idempotent
    if role already active)."""
    if not msg:
        return None
    low = msg.lower()
    has_spawn_verb = any(v in low for v in _SPAWN_VERBS)
    if not has_spawn_verb:
        return None
    found: list[str] = []
    for token, role in _CSUITE_TOKENS.items():
        if token in low and role not in found:
            found.append(role)
    return found if found else None


def _looks_like_multi_line_brief(msg: str) -> bool:
    """Heuristic: does this founder message clearly ask to spawn
    MULTIPLE business lines at once? Used by ``handle_stream()`` to
    bypass the router LLM and force-route to
    ``business.bootstrap_from_brief``.

    Triggers when:
      - the message mentions ≥2 distinct line-kind keyword families,
        OR
      - the message has an imperative AND ≥1 line-kind family AND is
        long enough to be a real brief.

    Conservative on purpose — false positives would send short
    'tell me about KDP' messages to the bootstrap skill which would
    then fail in the planner. Better to miss than misroute.
    """
    if not msg or len(msg) < 200:
        return False
    low = msg.lower()
    distinct_kinds = sum(
        1 for kws in _LINE_KIND_KEYWORDS.values()
        if any(kw in low for kw in kws)
    )
    if distinct_kinds >= 2:
        return True
    has_imperative = any(p in low for p in _MULTI_LINE_IMPERATIVES)
    has_counter = any(p in low for p in _MULTI_LINE_COUNTERS)
    return (has_imperative or has_counter) and distinct_kinds >= 1


def _parse_router_decision(content: str) -> _RouterDecision | None:
    parsed = extract_json_dict(content)
    if parsed is None:
        return None
    action = str(parsed.get("action") or "").strip().lower()
    if action == "use_skill":
        return _RouterDecision(
            action=action,
            skill_name=str(parsed.get("skill_name") or "").strip() or None,
            skill_args=(
                parsed.get("skill_args") if isinstance(parsed.get("skill_args"), dict) else {}
            ),
        )
    if action == "respond":
        return _RouterDecision(
            action="respond",
            content=str(parsed.get("content") or ""),
        )
    if action == "clarify":
        clarify = parse_clarify(parsed)
        if clarify is None:
            # Fall through — malformed clarify becomes plain respond
            # so we never wedge the router on bad LLM output.
            return _RouterDecision(
                action="respond",
                content=str(parsed.get("content") or ""),
            )
        return _RouterDecision(
            action="clarify",
            content=clarify.question,
            clarify=clarify,
        )
    return None


@dataclass(frozen=True)
class Plan:
    summary: str
    rationale: list[str]
    next_action: str
    """The single Founder-visible focus for this week (BRIEF.md "one
    next action"). Used as fallback when tasks is empty."""

    tasks: list[str]
    """Optional parallel work the C-suite can attempt simultaneously when
    Founder approves. Each task is routed to the right Director by domain
    keywords. Empty list = single-task mode (just dispatch next_action)."""

    estimated_hours: float | None
    expected_impact: str | None
    requires_founder_approval: bool
    reasoning: str | None
    """Chain-of-thought from the thinking model. Hidden by default in UI."""

    raw_response: str
    """Full text the LLM returned, for debugging and audit."""

    def dispatch_tasks(self) -> list[str]:
        """The list of tasks the Workforce should run. Falls back to
        next_action when no parallel tasks were proposed."""
        if self.tasks:
            return list(self.tasks)
        return [self.next_action] if self.next_action else []


@dataclass
class CEO:
    session: Session
    cost_tracker: CostTracker
    hiring: HiringService
    gate: ApprovalGate
    chief_of_staff: ChiefOfStaff | None = None
    """Optional triage layer. When provided, CEO surfaces a consolidated
    blocker digest to the Founder instead of letting agents flood the inbox."""

    workforce: Workforce | None = None
    """Optional execution layer. When set, CEO can dispatch approved Plan
    actions to C-suite Directors via execute_plan()."""

    skills: SkillRegistry | None = None
    """Optional skill registry. When set, CEO.handle() can auto-route a
    Founder message to the right skill (niche / landing / outreach /
    validate / etc.). Falls back to ``korpha.skills.default_registry``
    when None and skill-aware methods are called explicitly."""

    browser: object | None = None
    """Optional ``BrowserService``. When set, browser-using skills like
    ``research.scrape_url`` get a configured provider via SkillContext.
    None means the skill will refuse to run."""

    default_tier: InferenceTier = InferenceTier.PRO
    default_max_tokens: int = 0
    """0 = use the global ``agent_max_tokens()`` floor (16k unless
    overridden in providers.yaml ``defaults:``). Override per-instance
    only when an integration test or special case needs a tighter cap.
    Reasoning models (deepseek-v4-pro, kimi-k2.6, claude-with-thinking)
    burn 2-8k on chain-of-thought before producing the visible answer
    — the floor accounts for that."""

    default_timeout_seconds: float = 0.0
    """0 = use the global ``agent_timeout()`` floor (300s)."""

    cofounder_voice: str = field(default="")
    """Set by ``__post_init__`` from ``_CEO_VOICE_DEFAULT`` when left
    empty. Kept as a regular field so callers can override it for tests
    / per-business voice tuning. See module-level
    ``_CEO_VOICE_DEFAULT`` for the actual content."""

    def __post_init__(self) -> None:
        # Empty string → use the default voice. Callers who want their
        # own override (tests, per-business tuning) just set it.
        if not self.cofounder_voice:
            self.cofounder_voice = _CEO_VOICE_DEFAULT

    async def respond(
        self,
        *,
        business: Business,
        founder: Founder,
        founder_message: str,
        history: list[Message] | None = None,
        thread_id: UUID | None = None,
        include_digest: bool = True,
    ) -> CompletionResponse:
        """Free-form CEO response in the cofounder's voice.

        When ``include_digest`` and CoS is configured, the system prompt is
        augmented with the consolidated blocker digest so the CEO can weave
        the open items into its answer naturally rather than the Founder
        asking and getting a response that ignores pending blockers.
        """
        ceo_role = self.hiring.ensure_ceo(business.id)
        digest = self._maybe_digest(business.id) if include_digest else None
        messages =await self._build_messages(
            business=business,
            founder=founder,
            history=history or [],
            user_message=founder_message,
            digest=digest,
        )
        request = CompletionRequest(
            messages=messages,
            tier=self.default_tier,
            session_key=f"ceo-{ceo_role.id}",
            max_tokens=self.default_max_tokens or agent_max_tokens(),
            timeout_seconds=self.default_timeout_seconds or agent_timeout(),
        )
        response = await self.cost_tracker.complete(
            request,
            session=self.session,
            business_id=business.id,
            agent_role_id=ceo_role.id,
            thread_id=thread_id,
        )
        return response

    async def propose(
        self,
        *,
        business: Business,
        founder: Founder,
        founder_input: str,
        action_class: ActionClass = ActionClass.INTERNAL,
        platform: str | None = None,
    ) -> tuple[Plan, ProposalResult]:
        """CEO produces a Plan and routes it through the ApprovalGate."""
        ceo_role = self.hiring.ensure_ceo(business.id)

        team_hint = self._team_specialty_hint(business.id)
        plan_prompt = (
            f"{founder_input}\n\n"
            "Respond with a concrete plan in strict JSON only:\n"
            "{\n"
            '  "summary": "<one sentence — what we should do>",\n'
            '  "rationale": ["<reason 1>", "<reason 2>", "<reason 3>"],\n'
            '  "next_action": "<single most important action this week>",\n'
            '  "tasks": [\n'
            '    "[CTO] <engineering sub-task>",\n'
            '    "[CMO] <marketing sub-task>",\n'
            '    "[COO] <ops/support sub-task>"\n'
            "  ],\n"
            '  "estimated_hours": <number>,\n'
            '  "expected_impact": "<short string>"\n'
            "}\n\n"
            "Rules for `tasks`:\n"
            "- ALWAYS prefix each task with a routing tag: [CTO], "
            "[CMO], [COO], or [WORKER:<specialty>] when a "
            "specialized worker is on the team. The tag tells "
            "the workforce who owns the work.\n"
            + (team_hint or "")
            + "- Include only tasks the team can attempt now "
            "without further Founder input. If everything depends "
            "on a Founder decision, leave the array empty and "
            "put the single decision question in next_action.\n"
            "- Each task in its own domain so they run in parallel "
            "without stepping on each other.\n"
            "- 0 to 4 tasks. Quality over quantity."
        )

        digest = self._maybe_digest(business.id)
        messages =await self._build_messages(
            business=business,
            founder=founder,
            history=[],
            user_message=plan_prompt,
            digest=digest,
        )
        request = CompletionRequest(
            messages=messages,
            tier=self.default_tier,
            session_key=f"ceo-{ceo_role.id}",
            max_tokens=self.default_max_tokens or agent_max_tokens(),
            timeout_seconds=self.default_timeout_seconds or agent_timeout(),
        )
        response = await self.cost_tracker.complete(
            request,
            session=self.session,
            business_id=business.id,
            agent_role_id=ceo_role.id,
        )

        plan = _parse_plan(response.content, reasoning=response.reasoning)

        proposal = self.gate.propose(
            business_id=business.id,
            agent_role_id=ceo_role.id,
            action_class=action_class,
            platform=platform,
            proposal_summary=plan.summary,
            action_payload={
                "rationale": plan.rationale,
                "next_action": plan.next_action,
                "tasks": plan.tasks,
                "estimated_hours": plan.estimated_hours,
                "expected_impact": plan.expected_impact,
            },
        )

        # Mirror plan tasks onto the kanban board so Mike sees them
        # land immediately on /app/kanban. Failures are logged + dropped
        # — kanban-bookkeeping must never fail an approval flow.
        try:
            self._mirror_plan_to_kanban(
                business_id=business.id,
                ceo_role_id=ceo_role.id,
                founder_id=founder.id,
                plan=plan,
            )
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning(
                "kanban mirror of plan failed", exc_info=True,
            )

        return plan, proposal

    def _mirror_plan_to_kanban(
        self,
        *,
        business_id: UUID,
        ceo_role_id: UUID,
        founder_id: UUID,
        plan: Plan,
    ) -> None:
        """Write one BACKLOG card per Plan task. The role tag in the
        task text ('[CTO] ...') becomes the card's owner_role; tag
        and brackets are stripped from the title.

        Lazy imports + best-effort: the plan flow ships even if the
        kanban tables aren't migrated yet, or the board service
        rejects something.
        """
        from korpha.kanban import (  # noqa: PLC0415
            CreateCardInput, KanbanBoard,
        )

        if not plan.tasks:
            return

        board = KanbanBoard(self.session)
        for raw in plan.tasks:
            owner, title = _parse_task_role_tag(raw)
            if not title:
                continue
            try:
                board.create(CreateCardInput(
                    business_id=business_id,
                    title=title,
                    body=(
                        f"From CEO plan: {plan.summary}"
                        + (
                            f"\nNext action: {plan.next_action}"
                            if plan.next_action else ""
                        )
                    ),
                    owner_role=owner,
                    created_by_agent_role_id=ceo_role_id,
                    created_by_founder_id=founder_id,
                ))
            except Exception:  # noqa: BLE001
                # One bad task doesn't sink the rest. Log + continue.
                import logging
                logging.getLogger(__name__).warning(
                    "kanban card create failed for task %r", raw,
                    exc_info=True,
                )

    async def handle_stream(
        self,
        *,
        business: Business,
        founder: Founder,
        founder_message: str,
        history: list[Message] | None = None,
        thread_id: UUID | None = None,
        max_skill_calls: int = 5,
    ) -> Any:  # AsyncIterator[StreamEvent] — typed as Any to avoid runtime import cost in tests
        """Streaming variant of handle().

        Yields ``StreamEvent`` dicts the caller forwards as SSE:
        - ``{"type": "phase", "phase": "router"}`` — router call started
        - ``{"type": "phase", "phase": "skill", "skill_name": "..."}`` — running skill
        - ``{"type": "content", "text": "..."}`` — content delta from final synth
        - ``{"type": "reasoning", "text": "..."}`` — reasoning delta
        - ``{"type": "done", "skills_used": ["..."], "content": "<full>"}`` — final

        The router call is non-streaming (it's small; ~100 tokens). The
        synth call streams. For direct-respond (no skill), the router's
        content is yielded as a single chunk.
        """
        from collections.abc import AsyncIterator as _AI  # local import for typing-only

        async def _gen() -> _AI[dict[str, Any]]:
            registry = self.skills or default_registry
            ceo_role = self.hiring.ensure_ceo(business.id)
            digest = self._maybe_digest(business.id)
            skill_specs = registry.list_specs()

            # Pre-router heuristic 4: 'reassign' after a CEO message
            # that lists card-ID + role pairs. Force-routes to
            # kanban.reassign_cards so the CTO/CMO routing actually
            # changes in the DB (same hallucination class as the
            # other forced-route heuristics).
            reassign_pairs = (
                _detect_reassign_pairs(founder_message, history)
                if max_skill_calls > 0
                and "kanban.reassign_cards" in registry.skills
                else None
            )
            if reassign_pairs:
                yield {
                    "type": "phase", "phase": "skill",
                    "skill_name": "kanban.reassign_cards",
                }
                skill_ctx = SkillContext(
                    business=business,
                    founder=founder,
                    session=self.session,
                    cost_tracker=self.cost_tracker,
                    invoking_agent_role_id=ceo_role.id,
                    browser=self.browser,
                )
                skill_result = await registry.run(
                    "kanban.reassign_cards",
                    ctx=skill_ctx,
                    args={"assignments": reassign_pairs},
                )
                yield {"type": "phase", "phase": "synth"}
                synth_msg = _skill_synth_prompt(
                    founder_message, skill_result,
                )
                synth_request = CompletionRequest(
                    messages=await self._build_messages(
                        business=business,
                        founder=founder,
                        history=history or [],
                        user_message=synth_msg,
                        digest=digest,
                    ),
                    tier=self.default_tier,
                    session_key=f"ceo-handle-{ceo_role.id}",
                    max_tokens=self.default_max_tokens or agent_max_tokens(),
                    timeout_seconds=self.default_timeout_seconds or agent_timeout(),
                )
                buf: list[str] = []
                async for chunk in self.cost_tracker.stream(
                    synth_request,
                    session=self.session,
                    business_id=business.id,
                    agent_role_id=ceo_role.id,
                    thread_id=thread_id,
                ):
                    if chunk.delta_content:
                        buf.append(chunk.delta_content)
                        yield {"type": "content", "text": chunk.delta_content}
                    if chunk.delta_reasoning:
                        yield {"type": "reasoning", "text": chunk.delta_reasoning}
                yield {
                    "type": "done",
                    "skills_used": [skill_result.skill_name],
                    "content": "".join(buf),
                }
                return

            # Pre-router heuristic 3: short approval phrase that
            # follows a CEO message citing card IDs. Force-route to
            # kanban.fire_sprint so 'go' / 'fire it' actually moves
            # the cards through BACKLOG → IN_PROGRESS instead of the
            # LLM hedging ("Once you confirm…") or hallucinating
            # ("I assigned them").
            fire_ids = (
                _detect_fire_sprint(
                    founder_message, history,
                    business_id=business.id,
                    session=self.session,
                )
                if max_skill_calls > 0
                and "kanban.fire_sprint" in registry.skills
                else None
            )
            if fire_ids:
                yield {
                    "type": "phase", "phase": "skill",
                    "skill_name": "kanban.fire_sprint",
                }
                skill_ctx = SkillContext(
                    business=business,
                    founder=founder,
                    session=self.session,
                    cost_tracker=self.cost_tracker,
                    invoking_agent_role_id=ceo_role.id,
                    browser=self.browser,
                )
                skill_result = await registry.run(
                    "kanban.fire_sprint",
                    ctx=skill_ctx,
                    args={"card_ids": fire_ids},
                )
                yield {"type": "phase", "phase": "synth"}
                synth_msg = _skill_synth_prompt(
                    founder_message, skill_result,
                )
                synth_request = CompletionRequest(
                    messages=await self._build_messages(
                        business=business,
                        founder=founder,
                        history=history or [],
                        user_message=synth_msg,
                        digest=digest,
                    ),
                    tier=self.default_tier,
                    session_key=f"ceo-handle-{ceo_role.id}",
                    max_tokens=self.default_max_tokens or agent_max_tokens(),
                    timeout_seconds=self.default_timeout_seconds or agent_timeout(),
                )
                buf: list[str] = []
                async for chunk in self.cost_tracker.stream(
                    synth_request,
                    session=self.session,
                    business_id=business.id,
                    agent_role_id=ceo_role.id,
                    thread_id=thread_id,
                ):
                    if chunk.delta_content:
                        buf.append(chunk.delta_content)
                        yield {"type": "content", "text": chunk.delta_content}
                    if chunk.delta_reasoning:
                        yield {"type": "reasoning", "text": chunk.delta_reasoning}
                yield {
                    "type": "done",
                    "skills_used": [skill_result.skill_name],
                    "content": "".join(buf),
                }
                return

            # Pre-router heuristic 2: explicit c-suite spawn
            # imperative ("spawn CTO and CMO", "hire a CTO", "I need
            # a CMO"). Same rationale as the multi-line bypass: the
            # router LLM has been observed to prefer writing
            # 'I'll spawn both now' markdown over actually picking
            # hr.spawn_executives, so we deterministically force the
            # skill. Idempotent — re-asking is safe.
            csuite_to_spawn = (
                _detect_spawn_csuite(founder_message)
                if max_skill_calls > 0
                and "hr.spawn_executives" in registry.skills
                else None
            )
            if csuite_to_spawn:
                yield {
                    "type": "phase", "phase": "skill",
                    "skill_name": "hr.spawn_executives",
                }
                skill_ctx = SkillContext(
                    business=business,
                    founder=founder,
                    session=self.session,
                    cost_tracker=self.cost_tracker,
                    invoking_agent_role_id=ceo_role.id,
                    browser=self.browser,
                )
                skill_result = await registry.run(
                    "hr.spawn_executives",
                    ctx=skill_ctx,
                    args={"roles": csuite_to_spawn},
                )
                yield {"type": "phase", "phase": "synth"}
                synth_msg = _skill_synth_prompt(
                    founder_message, skill_result,
                )
                synth_request = CompletionRequest(
                    messages=await self._build_messages(
                        business=business,
                        founder=founder,
                        history=history or [],
                        user_message=synth_msg,
                        digest=digest,
                    ),
                    tier=self.default_tier,
                    session_key=f"ceo-handle-{ceo_role.id}",
                    max_tokens=self.default_max_tokens or agent_max_tokens(),
                    timeout_seconds=self.default_timeout_seconds or agent_timeout(),
                )
                buf: list[str] = []
                async for chunk in self.cost_tracker.stream(
                    synth_request,
                    session=self.session,
                    business_id=business.id,
                    agent_role_id=ceo_role.id,
                    thread_id=thread_id,
                ):
                    if chunk.delta_content:
                        buf.append(chunk.delta_content)
                        yield {"type": "content", "text": chunk.delta_content}
                    if chunk.delta_reasoning:
                        yield {"type": "reasoning", "text": chunk.delta_reasoning}
                yield {
                    "type": "done",
                    "skills_used": [skill_result.skill_name],
                    "content": "".join(buf),
                }
                return

            # Pre-router heuristic: if the founder message clearly
            # asks to spawn MULTIPLE business lines at once, skip the
            # router LLM call and dispatch directly to
            # ``business.bootstrap_from_brief``. The router LLM has
            # been observed to prefer writing a markdown plan over
            # picking a skill on these messages, so a deterministic
            # bypass is the only way to make multi-line briefs
            # actually execute.
            if (
                max_skill_calls > 0
                and "business.bootstrap_from_brief" in registry.skills
                and _looks_like_multi_line_brief(founder_message)
            ):
                yield {
                    "type": "phase", "phase": "skill",
                    "skill_name": "business.bootstrap_from_brief",
                }
                skill_ctx = SkillContext(
                    business=business,
                    founder=founder,
                    session=self.session,
                    cost_tracker=self.cost_tracker,
                    invoking_agent_role_id=ceo_role.id,
                    browser=self.browser,
                )
                skill_result = await registry.run(
                    "business.bootstrap_from_brief",
                    ctx=skill_ctx,
                    args={"brief": founder_message},
                )

                yield {"type": "phase", "phase": "synth"}
                synth_msg = _skill_synth_prompt(
                    founder_message, skill_result,
                )
                synth_request = CompletionRequest(
                    messages=await self._build_messages(
                        business=business,
                        founder=founder,
                        history=history or [],
                        user_message=synth_msg,
                        digest=digest,
                    ),
                    tier=self.default_tier,
                    session_key=f"ceo-handle-{ceo_role.id}",
                    max_tokens=self.default_max_tokens or agent_max_tokens(),
                    timeout_seconds=self.default_timeout_seconds or agent_timeout(),
                )
                buf: list[str] = []
                async for chunk in self.cost_tracker.stream(
                    synth_request,
                    session=self.session,
                    business_id=business.id,
                    agent_role_id=ceo_role.id,
                    thread_id=thread_id,
                ):
                    if chunk.delta_content:
                        buf.append(chunk.delta_content)
                        yield {"type": "content", "text": chunk.delta_content}
                    if chunk.delta_reasoning:
                        yield {"type": "reasoning", "text": chunk.delta_reasoning}
                yield {
                    "type": "done",
                    "skills_used": [skill_result.skill_name],
                    "content": "".join(buf),
                }
                return

            yield {"type": "phase", "phase": "router"}

            if not skill_specs or max_skill_calls <= 0:
                # No skills available → just stream a direct respond.
                async for ev in self._stream_direct(
                    business=business,
                    founder=founder,
                    user_message=founder_message,
                    history=history or [],
                    digest=digest,
                    ceo_role_id=ceo_role.id,
                    thread_id=thread_id,
                ):
                    yield ev
                return

            router_msg = _skill_router_prompt(skill_specs, founder_message)
            router_request = CompletionRequest(
                messages=await self._build_messages(
                    business=business,
                    founder=founder,
                    history=history or [],
                    user_message=router_msg,
                    digest=digest,
                ),
                tier=self.default_tier,
                session_key=f"ceo-handle-{ceo_role.id}",
                # Bigger budget for the router because thinking models burn
                # 1k-3k reasoning tokens before they produce the JSON close.
                # If we cap too tight we get empty content + finish=length.
                max_tokens=self.default_max_tokens or agent_max_tokens(),
                timeout_seconds=self.default_timeout_seconds or agent_timeout(),
            )
            router_response = await self.cost_tracker.complete(
                router_request,
                session=self.session,
                business_id=business.id,
                agent_role_id=ceo_role.id,
                thread_id=thread_id,
            )
            decision = _parse_router_decision(router_response.content)

            if decision is None or decision.action != "use_skill":
                # Direct-reply path. Prefer parsed JSON content; fall back to
                # the raw router output. If BOTH are empty (thinking model
                # burned the whole budget on reasoning), stream a fresh
                # direct-respond call so the Founder gets a real answer.
                content = (decision.content if decision is not None else None) or router_response.content
                clarify = decision.clarify if decision is not None else None
                if clarify is not None and clarify.choices:
                    # Self-contained text for channels that don't read
                    # the structured ``clarify`` event (Telegram).
                    content = (
                        f"{clarify.question}\n\n{clarify.as_numbered_list()}"
                    )
                if content.strip():
                    yield {"type": "content", "text": content}
                    done_evt: dict[str, Any] = {
                        "type": "done",
                        "skills_used": [],
                        "content": content,
                    }
                    if clarify is not None and clarify.choices:
                        done_evt["clarify_question"] = clarify.question
                        done_evt["clarify_choices"] = list(clarify.choices)
                    yield done_evt
                    return
                # Empty router → fall through to streaming a direct response.
                async for ev in self._stream_direct(
                    business=business,
                    founder=founder,
                    user_message=founder_message,
                    history=history or [],
                    digest=digest,
                    ceo_role_id=ceo_role.id,
                    thread_id=thread_id,
                ):
                    yield ev
                return

            if decision.skill_name is None or registry.skills.get(decision.skill_name) is None:
                fallback = (
                    "I tried to route this to a skill, but the skill name was missing or unknown.\n\n"
                    + (decision.content or "")
                )
                yield {"type": "content", "text": fallback}
                yield {"type": "done", "skills_used": [], "content": fallback}
                return

            # Multi-skill chain loop. The router has picked the first
            # skill — run it, then ask the LLM 'another skill, or are
            # you done?'. Loop up to max_skill_calls. Mirrors Hermes's
            # tool-use loop but stays on the existing JSON router
            # protocol so providers without native tool_use still work.
            skill_ctx = SkillContext(
                business=business,
                founder=founder,
                session=self.session,
                cost_tracker=self.cost_tracker,
                invoking_agent_role_id=ceo_role.id,
                browser=self.browser,
            )
            skills_used: list[SkillResult] = []
            current_decision = decision

            while True:
                yield {
                    "type": "phase", "phase": "skill",
                    "skill_name": current_decision.skill_name,
                }
                try:
                    result = await registry.run(
                        current_decision.skill_name,
                        ctx=skill_ctx,
                        args=current_decision.skill_args or {},
                    )
                except Exception as exc:  # noqa: BLE001
                    import logging
                    logging.getLogger(__name__).exception(
                        "chain skill failed: %s",
                        current_decision.skill_name,
                    )
                    # Build a synthetic 'error' result so the
                    # final synth can mention what failed instead
                    # of just hanging.
                    result = SkillResult(
                        skill_name=current_decision.skill_name,
                        summary=f"FAILED: {type(exc).__name__}: {exc}",
                        payload={"error": str(exc)},
                        cost_usd=0.0,
                    )
                skills_used.append(result)

                if len(skills_used) >= max_skill_calls:
                    break

                # Chain check: ask LLM if another skill is needed.
                chain_prompt = _skill_chain_prompt(
                    founder_message, skills_used, skill_specs,
                )
                chain_request = CompletionRequest(
                    messages=await self._build_messages(
                        business=business,
                        founder=founder,
                        history=history or [],
                        user_message=chain_prompt,
                        digest=digest,
                    ),
                    tier=self.default_tier,
                    session_key=f"ceo-handle-{ceo_role.id}",
                    max_tokens=self.default_max_tokens or agent_max_tokens(),
                    timeout_seconds=self.default_timeout_seconds or agent_timeout(),
                )
                chain_response = await self.cost_tracker.complete(
                    chain_request,
                    session=self.session,
                    business_id=business.id,
                    agent_role_id=ceo_role.id,
                    thread_id=thread_id,
                )
                next_decision = _parse_router_decision(chain_response.content)
                if next_decision is None or next_decision.action != "use_skill":
                    break
                if (
                    next_decision.skill_name is None
                    or registry.skills.get(next_decision.skill_name) is None
                ):
                    break
                current_decision = next_decision

            yield {"type": "phase", "phase": "synth"}

            synth_msg = _skill_synth_prompt_multi(
                founder_message, skills_used,
            )
            synth_request = CompletionRequest(
                messages=await self._build_messages(
                    business=business,
                    founder=founder,
                    history=history or [],
                    user_message=synth_msg,
                    digest=digest,
                ),
                tier=self.default_tier,
                session_key=f"ceo-handle-{ceo_role.id}",
                max_tokens=self.default_max_tokens or agent_max_tokens(),
                timeout_seconds=self.default_timeout_seconds or agent_timeout(),
            )

            buf: list[str] = []
            async for chunk in self.cost_tracker.stream(
                synth_request,
                session=self.session,
                business_id=business.id,
                agent_role_id=ceo_role.id,
                thread_id=thread_id,
            ):
                if chunk.delta_content:
                    buf.append(chunk.delta_content)
                    yield {"type": "content", "text": chunk.delta_content}
                if chunk.delta_reasoning:
                    yield {"type": "reasoning", "text": chunk.delta_reasoning}

            yield {
                "type": "done",
                "skills_used": [r.skill_name for r in skills_used],
                "content": "".join(buf),
            }

        return _gen()

    async def _stream_direct(
        self,
        *,
        business: Business,
        founder: Founder,
        user_message: str,
        history: list[Message],
        digest: Digest | None,
        ceo_role_id: UUID,
        thread_id: UUID | None,
    ) -> Any:  # AsyncIterator[dict] — typed loose for the helper
        request = CompletionRequest(
            messages=await self._build_messages(
                business=business,
                founder=founder,
                history=history,
                user_message=user_message,
                digest=digest,
            ),
            tier=self.default_tier,
            session_key=f"ceo-handle-{ceo_role_id}",
            max_tokens=self.default_max_tokens or agent_max_tokens(),
            timeout_seconds=self.default_timeout_seconds or agent_timeout(),
        )
        buf: list[str] = []
        async for chunk in self.cost_tracker.stream(
            request,
            session=self.session,
            business_id=business.id,
            agent_role_id=ceo_role_id,
            thread_id=thread_id,
        ):
            if chunk.delta_content:
                buf.append(chunk.delta_content)
                yield {"type": "content", "text": chunk.delta_content}
            if chunk.delta_reasoning:
                yield {"type": "reasoning", "text": chunk.delta_reasoning}
        yield {"type": "done", "skills_used": [], "content": "".join(buf)}

    async def execute_plan(
        self,
        *,
        business: Business,
        founder: Founder,
        plan: Plan,
        extra_tasks: list[str] | None = None,
    ) -> DispatchSummary:
        """Dispatch an approved Plan's next_action through the Workforce.

        Returns a DispatchSummary the CEO can use to update the Founder.
        Requires `workforce` to be configured. Each task is routed to the
        right Director (CTO/CMO/COO); blockers go through CoS automatically.
        """
        if self.workforce is None:
            raise RuntimeError(
                "CEO.execute_plan requires a Workforce. "
                "Construct CEO with workforce=Workforce.with_default_directors(...)"
            )
        tasks = plan.dispatch_tasks()
        if extra_tasks:
            tasks.extend(extra_tasks)
        if not tasks:
            return DispatchSummary.from_results([])

        results = await self.workforce.dispatch(
            business=business, founder=founder, tasks=tasks
        )
        return DispatchSummary.from_results(results)

    async def handle(
        self,
        *,
        business: Business,
        founder: Founder,
        founder_message: str,
        history: list[Message] | None = None,
        thread_id: UUID | None = None,
        max_skill_calls: int = 5,
    ) -> HandleResult:
        """Skill-aware response.

        One LLM call asks CEO to either reply directly or pick exactly one
        skill to invoke first. If a skill is picked, we run it and make a
        second LLM call to synthesize the final response with the skill
        result threaded into the system prompt.

        This is the "magic" entry point: Founder asks "I want to start a
        business" and CEO auto-routes to ``niche.find_micro_niches`` instead
        of producing a generic answer. Existing ``respond`` / ``propose``
        stay simple and skill-unaware.
        """
        registry = self.skills or default_registry
        ceo_role = self.hiring.ensure_ceo(business.id)
        digest = self._maybe_digest(business.id)
        skill_specs = registry.list_specs()

        if not skill_specs or max_skill_calls <= 0:
            response = await self.respond(
                business=business,
                founder=founder,
                founder_message=founder_message,
                history=history,
                thread_id=thread_id,
            )
            content = await self._transform_output(
                response.content, business=business, founder=founder,
                thread_id=thread_id,
            )
            return HandleResult(
                content=content,
                reasoning=response.reasoning,
                skills_used=[],
                cost_usd=float(response.cost_usd),
                router_response=response,
                final_response=response,
            )

        router_msg = _skill_router_prompt(skill_specs, founder_message)
        router_request = CompletionRequest(
            messages=await self._build_messages(
                business=business,
                founder=founder,
                history=history or [],
                user_message=router_msg,
                digest=digest,
            ),
            tier=self.default_tier,
            session_key=f"ceo-handle-{ceo_role.id}",
            max_tokens=self.default_max_tokens or agent_max_tokens(),
            timeout_seconds=self.default_timeout_seconds or agent_timeout(),
        )
        router_response = await self.cost_tracker.complete(
            router_request,
            session=self.session,
            business_id=business.id,
            agent_role_id=ceo_role.id,
            thread_id=thread_id,
        )

        decision = _parse_router_decision(router_response.content)
        skills_used: list[SkillResult] = []

        # Direct-reply path: no skill needed.
        if decision is None or decision.action != "use_skill":
            content = (decision.content if decision is not None else None) or router_response.content
            clarify = decision.clarify if decision is not None else None
            # For channels that can't render buttons (Telegram, plain
            # email), append a numbered list to ``content`` so the
            # question is self-contained even when ``clarify`` is
            # ignored. Web/TUI consumers read ``clarify`` and render
            # interactively, ignoring the appended list.
            if clarify is not None and clarify.choices:
                content = (
                    f"{clarify.question}\n\n{clarify.as_numbered_list()}"
                )
            content = await self._transform_output(
                content, business=business, founder=founder,
                thread_id=thread_id,
            )
            return HandleResult(
                content=content,
                reasoning=router_response.reasoning,
                skills_used=[],
                cost_usd=float(router_response.cost_usd),
                router_response=router_response,
                final_response=router_response,
                clarify=clarify,
            )

        # Skill path: run the skill, then ask CEO to synthesize.
        if decision.skill_name is None or registry.skills.get(decision.skill_name) is None:
            return HandleResult(
                content=(
                    "I tried to route this to a skill, but the skill name was missing "
                    "or unknown. Defaulting to a direct response.\n\n"
                    + (decision.content or "(no content)")
                ),
                reasoning=router_response.reasoning,
                skills_used=[],
                cost_usd=float(router_response.cost_usd),
                router_response=router_response,
                final_response=router_response,
            )

        skill_ctx = SkillContext(
            business=business,
            founder=founder,
            session=self.session,
            cost_tracker=self.cost_tracker,
            invoking_agent_role_id=ceo_role.id,
        )
        skill_result = await registry.run(
            decision.skill_name, ctx=skill_ctx, args=decision.skill_args or {}
        )
        skills_used.append(skill_result)

        synth_msg = _skill_synth_prompt(founder_message, skill_result)
        synth_request = CompletionRequest(
            messages=await self._build_messages(
                business=business,
                founder=founder,
                history=history or [],
                user_message=synth_msg,
                digest=digest,
            ),
            tier=self.default_tier,
            session_key=f"ceo-handle-{ceo_role.id}",
            max_tokens=self.default_max_tokens or agent_max_tokens(),
            timeout_seconds=self.default_timeout_seconds or agent_timeout(),
        )
        synth_response = await self.cost_tracker.complete(
            synth_request,
            session=self.session,
            business_id=business.id,
            agent_role_id=ceo_role.id,
            thread_id=thread_id,
        )

        content = await self._transform_output(
            synth_response.content, business=business, founder=founder,
            thread_id=thread_id,
        )
        return HandleResult(
            content=content,
            reasoning=synth_response.reasoning,
            skills_used=skills_used,
            cost_usd=float(router_response.cost_usd) + float(synth_response.cost_usd) + skill_result.cost_usd,
            router_response=router_response,
            final_response=synth_response,
        )

    def _team_specialty_hint(self, business_id: UUID) -> str:
        """Build a short prompt hint listing every active worker
        the CEO can route to. Empty string when no workers are
        hired (fall back to plain C-suite tagging)."""
        from sqlmodel import select as _select
        from korpha.cofounder.model import AgentRole, RoleType

        try:
            rows = list(self.session.exec(
                _select(AgentRole)
                .where(AgentRole.business_id == business_id)
                .where(AgentRole.role_type == RoleType.WORKER)
                .where(AgentRole.is_active)
            ).all())
        except Exception:  # noqa: BLE001
            return ""
        if not rows:
            return ""
        specialties = sorted({
            r.specialty for r in rows if r.specialty
        })
        if not specialties:
            return ""
        return (
            "- Available specialty workers (use "
            "[WORKER:specialty] tag when the work fits): "
            + ", ".join(specialties)
            + ".\n"
        )

    async def _transform_output(
        self,
        content: str,
        *,
        business: Business,
        founder: Founder,
        thread_id: UUID | None,
    ) -> str:
        """Run plugin transform_llm_output chain against the final
        user-facing CEO text. No-op when no listeners are registered —
        cheap to call on every response.

        Lazy-imports the hook module to avoid a circular import:
        ``plugins.__init__`` pulls ``host`` → ``channels.router`` →
        back to ``cofounder.ceo``."""
        from korpha.plugins.hooks import (
            HookKind, TransformLlmOutputEvent, hook_registry,
        )
        if not hook_registry.has(HookKind.TRANSFORM_LLM_OUTPUT):
            return content
        out = await hook_registry.dispatch_transform(
            HookKind.TRANSFORM_LLM_OUTPUT,
            text=content,
            event_factory=lambda current: TransformLlmOutputEvent(
                text=current,
                business_id=business.id,
                founder_id=founder.id,
                thread_id=thread_id,
                role="assistant",
            ),
        )
        return out if isinstance(out, str) else content

    def _maybe_digest(self, business_id: UUID) -> Digest | None:
        if self.chief_of_staff is None:
            return None
        digest = self.chief_of_staff.digest_for_ceo(business_id)
        return digest if digest.items else None

    async def _build_messages(
        self,
        *,
        business: Business,
        founder: Founder,
        history: list[Message],
        user_message: str,
        digest: Digest | None = None,
        max_output_tokens: int | None = None,
    ) -> list[Message]:
        system_parts = [
            self.cofounder_voice,
            (
                f"Founder: {founder.display_name or founder.email}\n"
                f"Business: {business.name}"
                + (f" — {business.description}" if business.description else "")
            ),
        ]

        # Bounded MEMORY + USER blocks (Hermes-style self-improvement
        # loop — the agent carries forward what it's learned about
        # this founder + project automatically). Errors here MUST
        # not break the conversation; we log + skip on failure.
        try:
            from korpha.memory.notes import FounderNoteService

            block = FounderNoteService(self.session).render_block(
                business_id=business.id,
                founder_id=founder.id,
            )
            if block:
                system_parts.append(block)
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning(
                "founder-note injection failed", exc_info=True,
            )

        # PR-INT-4: inject the BusinessUnit org summary so the CEO
        # actually knows which lines are running. Was previously only
        # used by /app/units routes — never reached the agent prompt.
        try:
            from korpha.business_units.context import (
                render_unit_summary_for_prompt,
            )
            unit_block = render_unit_summary_for_prompt(
                self.session, business.id,
            )
            if unit_block:
                system_parts.append(unit_block)
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning(
                "unit-summary injection failed", exc_info=True,
            )

        # Optional founder deep-profile from Debriefeur (see
        # korpha.identity.founder_profile). When present, the CEO
        # picks up "how this founder thinks" — decision style,
        # risk tolerance, blindspots, etc. — so plans and proposals
        # match Mike's actual operating style instead of generic
        # cofounder defaults.
        try:
            from korpha.config import get_settings
            from korpha.identity.founder_profile import load_founder_profile
            profile_block = load_founder_profile(
                get_settings().data_dir
            ).as_prompt_preamble()
            if profile_block:
                system_parts.append(profile_block)
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).warning(
                "founder-profile injection failed", exc_info=True,
            )

        if digest is not None and digest.items:
            system_parts.append(
                "Open items the team is blocked on (your Chief of Staff has triaged "
                "these — surface them naturally only when relevant, do not just "
                "list them):\n" + digest.render()
            )
        system = "\n\n".join(system_parts)

        # Run the configured context engine on the history before
        # sending. This handles long conversations against 1M-context
        # models by summarizing the middle when the prompt grows
        # past the threshold (default 80% of context window).
        try:
            from korpha.cofounder.context_engine import build_context_engine
            engine = build_context_engine(
                cost_tracker=self.cost_tracker,
                tier=self.default_tier,
                session_key=f"ceo-history-{business.id}",
            )
            sys_overhead_tokens = (len(system) // 4) + 32
            shaped_history = await engine.shape(
                history,
                max_output_tokens=int(
                    max_output_tokens
                    or self.default_max_tokens
                    or agent_max_tokens()
                ),
                system_overhead_tokens=sys_overhead_tokens,
            )
        except Exception:  # noqa: BLE001
            import logging
            logging.getLogger(__name__).exception(
                "context engine shape() failed; "
                "falling back to raw history"
            )
            shaped_history = list(history)

        return [
            Message(role=Role.SYSTEM, content=system),
            *shaped_history,
            Message(role=Role.USER, content=user_message),
        ]


_ROLE_TAG_TO_OWNER = {
    "CTO": "cto", "CMO": "cmo", "COO": "coo",
    "CEO": None,  # CEO is the planner, not an owner of execution
}


def _parse_task_role_tag(raw: str) -> tuple[str | None, str]:
    """Pull a routing tag off the front of a task and return
    ``(owner_role, clean_title)``.

    Recognized tags:
      * ``[CTO]`` / ``[CMO]`` / ``[COO]`` → owner = role string
      * ``[WORKER:<specialty>]`` → owner = parent role of that
        worker specialty (cmo for copywriter/designer, coo for
        support, cto otherwise) so the kanban card lives under
        the right C-suite umbrella
      * ``[CEO]`` → owner None (CEO is the planner, not executor)

    Untagged or unknown tags return ``(None, stripped_text)`` —
    still a valid card, just unassigned. Tolerates whitespace,
    lowercase tags, missing space after the closing bracket.
    """
    text = (raw or "").strip()
    if not text:
        return (None, "")
    if not text.startswith("["):
        return (None, text)
    close = text.find("]")
    if close <= 0:
        return (None, text)
    inside = text[1:close].strip()
    rest = text[close + 1 :].lstrip(" :-")

    if inside.upper().startswith("WORKER:"):
        # Extract the specialty + map to its parent role for
        # kanban ownership. Plumb through DEFAULT_WORKER_PERSONALITIES
        # so unknown specialties still create cards (just unassigned).
        specialty = inside.split(":", 1)[1].strip().lower()
        try:
            from korpha.cofounder.director import (
                DEFAULT_WORKER_PERSONALITIES,
            )
            spec = DEFAULT_WORKER_PERSONALITIES.get(specialty)
            if spec is not None:
                return (spec.parent_role_type.value, rest or text)
        except Exception:  # noqa: BLE001
            pass
        return (None, rest or text)

    tag = inside.upper()
    owner = _ROLE_TAG_TO_OWNER.get(tag)
    return (owner, rest or text)


def _parse_plan(content: str, *, reasoning: str | None) -> Plan:
    """Extract a Plan from the model's response.

    Tries strict JSON first; falls back to a best-effort regex if the model
    emits prose around the JSON. Reasoning models often surround JSON with
    commentary — we tolerate that.
    """
    parsed = _try_parse_json(content)
    if parsed is None:
        parsed = _try_extract_json(content)
    if parsed is None:
        # Total fallback: surface the content as-is in summary, mark requires_approval.
        return Plan(
            summary=content[:240].strip() or "(empty model response)",
            rationale=[],
            next_action="(see full response)",
            tasks=[],
            estimated_hours=None,
            expected_impact=None,
            requires_founder_approval=True,
            reasoning=reasoning,
            raw_response=content,
        )

    rationale_raw = parsed.get("rationale") or []
    if isinstance(rationale_raw, str):
        rationale = [rationale_raw]
    else:
        rationale = [str(item) for item in rationale_raw]

    tasks_raw = parsed.get("tasks") or []
    if isinstance(tasks_raw, str):
        tasks = [tasks_raw.strip()] if tasks_raw.strip() else []
    elif isinstance(tasks_raw, list):
        tasks = [str(t).strip() for t in tasks_raw if str(t).strip()]
    else:
        tasks = []

    estimated = parsed.get("estimated_hours")
    estimated_hours: float | None
    if isinstance(estimated, int | float):
        estimated_hours = float(estimated)
    else:
        try:
            estimated_hours = float(estimated) if estimated is not None else None
        except (TypeError, ValueError):
            estimated_hours = None

    return Plan(
        summary=str(parsed.get("summary", "")).strip(),
        rationale=rationale,
        next_action=str(parsed.get("next_action", "")).strip(),
        tasks=tasks,
        estimated_hours=estimated_hours,
        expected_impact=str(parsed.get("expected_impact", "")).strip() or None,
        requires_founder_approval=True,
        reasoning=reasoning,
        raw_response=content,
    )


def _try_parse_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None
    # Strip Markdown code fences the model often adds around JSON.
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        text = text.strip()
        if text.startswith("json"):
            text = text[4:].lstrip()
    try:
        result = json.loads(text)
        return result if isinstance(result, dict) else None
    except json.JSONDecodeError:
        return None


def _try_extract_json(text: str) -> dict[str, Any] | None:
    """Find the first valid JSON object embedded anywhere in the text.

    Uses json.JSONDecoder.raw_decode() so we handle cases where the model
    wraps JSON in prose, Markdown code fences, or trailing commentary.
    """
    decoder = json.JSONDecoder()
    for idx in range(len(text)):
        if text[idx] != "{":
            continue
        try:
            obj, _end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


__all__ = [
    "CEO",
    "AgentRole",
    "Plan",
    "ProposalAccepted",
    "ProposalDenied",
    "ProposalPending",
    "RoleType",
]
