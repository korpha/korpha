"""marketing.* skills — high-level marketing automation.

  - ``marketing.video_from_post`` — chain skill: takes a blog post URL
    or text, drafts a script via LLM, renders an avatar talking-head
    via ``creative.heygen_avatar``, composes a polished MP4 via
    ``creative.hyperframes``. One call for Mike, three steps under
    the hood.

This is the actual user-facing feature. The two creative.* skills
are infrastructure — Mike rarely calls them directly. He says "make
me a launch video for this blog post" and the chain handles the rest.
"""
from __future__ import annotations

from typing import Any

from korpha._jsonext import extract_json_dict
from korpha.audit.model import InferenceTier
from korpha.inference.limits import agent_max_tokens, agent_timeout
from korpha.inference.types import CompletionRequest, Message, Role
from korpha.skills.registry import default_registry, register
from korpha.skills.types import (
    Skill,
    SkillContext,
    SkillError,
    SkillResult,
    SkillSpec,
)


_SCRIPT_PROMPT = """\
You are Korpha's marketing video copywriter. Distill the source
content below into a SHORT video script the avatar will speak. The
script must:

  - Open with a hook in the first 5 words ("If you've ever…",
    "What most founders miss is…", a counterintuitive claim, etc.)
  - Stay tight to the duration target — solopreneurs trim, not pad
  - End with one clear next step (a CTA: visit a URL, comment "yes",
    sign up, etc.). Keep the URL out — say "link in description" so
    the script works across platforms.
  - Sound like spoken words, not written copy. Contractions, short
    sentences, no semicolons or em-dashes.
  - Avoid filler ("As you know", "Today I want to talk about", etc.)

Source content:
\"\"\"
{source}
\"\"\"

Target duration: {duration_seconds} seconds.
Brand voice: {brand_voice}
Audience: {audience}
Optional intent override: {intent}

Return ONLY this JSON. No prose, no fences:

{{
  "title": "<title for the on-screen card, ≤80 chars>",
  "script": "<the avatar's spoken lines, plain text, ~{words_target} words>",
  "hook": "<the opening 5 words verbatim, for thumbnail / preview>",
  "cta": "<one short line restating the call to action>"
}}

Rules:
  - script word count should be ~{words_target} (±20). Speech pace is
    ~150 wpm.
  - Do NOT include URLs, brand-specific tags, or stage directions.
  - The avatar will speak ``script`` literally — write it as you'd
    say it aloud.
"""


class VideoFromPostSkill(Skill):
    """Chain: source content → script → avatar render → composed MP4.

    This is the feature most solopreneurs actually want — not "give me
    a HeyGen client," but "turn this blog post into a launch video."
    The chain hides three calls behind one skill invocation:

      1. Pro-tier LLM call: distill the source into a script.
      2. ``creative.heygen_avatar``: render the avatar talking-head.
      3. ``creative.hyperframes``: compose the final MP4 with title
         card, brand colors, CTA card.

    Why a chain skill rather than doing it in the CEO: the CEO already
    does multi-step orchestration, but exposing this as a single skill
    means it shows up in the catalog (so the agent can discover it),
    has a stable name (so other skills can call it), and has its own
    cost / approval shape. Same reasoning as the existing
    ``onboarding.chain`` flow.

    Approval model: the rendered MP4 is the *result*, not an outbound
    action. Mike reviews the video and decides where to publish it.
    Publishing skills (post to YouTube, etc.) are separate + go
    through their own approval gate.
    """

    spec = SkillSpec(
        name="marketing.video_from_post",
        description=(
            "Turn a blog post (or any source text) into a polished "
            "marketing video: script → HeyGen avatar → HyperFrames "
            "composition → final MP4. Three skills under the hood, "
            "one call for the founder. Use when the request is "
            "'make me a video about X' or 'turn this post into a "
            "30s social clip'."
        ),
        parameters={
            "source": (
                "Source content the script is distilled from. Plain "
                "text — paste a blog post, newsletter excerpt, or "
                "feature description. Required."
            ),
            "avatar_id": (
                "HeyGen avatar identifier. Required."
            ),
            "voice_id": (
                "HeyGen voice identifier. Required."
            ),
            "duration_seconds": (
                "Target video length. 15 / 30 / 60 typical. "
                "Default: 30."
            ),
            "kind": (
                "Composition template — social_ad / launch_reel / "
                "module_intro. Default: social_ad."
            ),
            "audience": (
                "Optional. Who's watching ('B2B founders', 'eCom "
                "shop owners', 'AI-curious devs'). Tunes the script "
                "voice."
            ),
            "brand_voice": (
                "Optional. Three adjectives ('direct, dry, "
                "specific'). Defaults to 'direct, specific, "
                "opinionated'."
            ),
            "brand_color_hex": (
                "Optional. '#RRGGBB' for accent elements. Defaults "
                "to Korpha blue."
            ),
            "intent": (
                "Optional. Override the script's inferred goal "
                "('drive signups', 'announce launch', 'recap "
                "yesterday'). Helps when source content is broad."
            ),
            "ratio": (
                "Optional. 16:9 / 9:16 / 1:1. Default 16:9."
            ),
        },
        default_tier=InferenceTier.PRO,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any]
    ) -> SkillResult:
        source = str(args.get("source") or "").strip()
        avatar_id = str(args.get("avatar_id") or "").strip()
        voice_id = str(args.get("voice_id") or "").strip()
        if not (source and avatar_id and voice_id):
            raise SkillError(
                "marketing.video_from_post requires `source`, "
                "`avatar_id`, and `voice_id`. The avatar/voice ids "
                "live in your HeyGen dashboard."
            )

        duration_seconds = int(args.get("duration_seconds") or 30)
        if duration_seconds < 5 or duration_seconds > 180:
            raise SkillError(
                f"duration_seconds must be 5-180; got {duration_seconds}"
            )
        words_target = max(10, int(duration_seconds * 2.5))  # ~150 wpm

        kind = str(args.get("kind") or "social_ad").strip()
        audience = str(args.get("audience") or "solopreneurs").strip()
        brand_voice = str(
            args.get("brand_voice") or "direct, specific, opinionated"
        ).strip()
        brand_color = str(args.get("brand_color_hex") or "#5e9eff").strip()
        intent = str(args.get("intent") or "(infer from the source)").strip()
        ratio = str(args.get("ratio") or "16:9").strip()

        total_cost = 0.0

        # ---- step 1: draft script via LLM ----
        prompt = _SCRIPT_PROMPT.format(
            source=source[:8000],  # cap to keep tokens bounded
            duration_seconds=duration_seconds,
            words_target=words_target,
            audience=audience,
            brand_voice=brand_voice,
            intent=intent,
        )
        request = CompletionRequest(
            messages=[
                Message(role=Role.SYSTEM, content=(
                    "You are Korpha's video script copywriter. "
                    "Output strict JSON only — no prose, no code "
                    "fences."
                )),
                Message(role=Role.USER, content=prompt),
            ],
            tier=self.spec.default_tier or InferenceTier.PRO,
            session_key=f"video-script-{ctx.business.id}",
            max_tokens=agent_max_tokens(),
            timeout_seconds=agent_timeout(),
        )
        script_response = await ctx.cost_tracker.complete(
            request,
            session=ctx.session,
            business_id=ctx.business.id,
            agent_role_id=ctx.invoking_agent_role_id,
        )
        total_cost += float(script_response.cost_usd or 0.0)

        try:
            script_obj = extract_json_dict(script_response.content)
        except Exception as exc:
            raise SkillError(
                f"Script LLM returned non-JSON: "
                f"{script_response.content[:200]!r}"
            ) from exc
        script_text = str(script_obj.get("script") or "").strip()
        title = str(script_obj.get("title") or "").strip()
        cta = str(script_obj.get("cta") or "").strip()
        if not script_text:
            raise SkillError(
                "Script LLM returned no `script` field; can't proceed "
                "to avatar render."
            )

        # ---- step 2: HeyGen avatar render ----
        heygen = default_registry.skills.get("creative.heygen_avatar")
        if heygen is None:
            raise SkillError(
                "creative.heygen_avatar not registered — internal "
                "skill missing. This is a bug; report it."
            )
        heygen_result = await heygen.run(
            ctx=ctx,
            args={
                "script": script_text,
                "avatar_id": avatar_id,
                "voice_id": voice_id,
                "ratio": ratio,
            },
        )
        total_cost += float(heygen_result.cost_usd or 0.0)
        avatar_video_url = str(
            (heygen_result.payload or {}).get("video_url") or ""
        )
        if not avatar_video_url:
            raise SkillError(
                "creative.heygen_avatar returned no video_url; can't "
                "compose final video."
            )

        # ---- step 3: HyperFrames composition ----
        hyperframes = default_registry.skills.get("creative.hyperframes")
        if hyperframes is None:
            raise SkillError(
                "creative.hyperframes not registered — internal skill "
                "missing. This is a bug; report it."
            )
        hf_result = await hyperframes.run(
            ctx=ctx,
            args={
                "avatar_clip": avatar_video_url,
                "kind": kind,
                "title": title,
                "brand_color_hex": brand_color,
            },
        )
        total_cost += float(hf_result.cost_usd or 0.0)
        output_path = str((hf_result.payload or {}).get("output_path") or "")

        return SkillResult(
            skill_name=self.spec.name,
            summary=(
                f"Video ready ({duration_seconds}s, {kind}). "
                f"Saved to {output_path}."
            ),
            payload={
                "output_path": output_path,
                "title": title,
                "script": script_text,
                "hook": script_obj.get("hook"),
                "cta": cta,
                "avatar_video_url": avatar_video_url,
                "duration_seconds": duration_seconds,
                "kind": kind,
                "ratio": ratio,
            },
            cost_usd=total_cost,
        )


register(VideoFromPostSkill())


__all__ = ["VideoFromPostSkill"]
