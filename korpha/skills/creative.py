"""creative.* skills — video generation pipeline.

Two skills, one orchestrator chain (the chain lives in marketing.py):

  - ``creative.heygen_avatar`` — call HeyGen's API to render a
    talking-head video from a script + avatar id. HTTP wrapper around
    the HeyGen v2 API. Mike's existing HeyGen plan covers the cost.

  - ``creative.hyperframes`` — wrap the local HyperFrames CLI
    (Apache 2.0, by HeyGen) to compose a polished MP4 from raw
    inputs (avatar clip, brand colors, intro/outro, music, captions).
    Free + local — no per-render charge.

Why these are built-in not agent-authored:
  - Mike will use them often; reference quality matters.
  - HyperFrames needs Node 22 + FFmpeg + Chrome headless installed
    on the host. The skill can't bootstrap the OS for the user; we
    surface a clean SkillError with install instructions when deps
    are missing.
  - Built-ins can ``subprocess.run`` — agent-authored skills can't.
    HyperFrames is a Node CLI; subprocess is the right call shape.

The agent can still compose ON TOP of these via
``meta.author_python_skill`` — e.g. "build a skill that makes a
30-second product launch video with my brand colors" — and that
skill calls into ``creative.hyperframes`` rather than re-implementing
it.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

import httpx

from korpha.audit.model import InferenceTier
from korpha.skills.registry import register
from korpha.skills.types import (
    Skill,
    SkillContext,
    SkillError,
    SkillResult,
    SkillSpec,
)


# ---------------------------------------------------------------------------
# creative.heygen_avatar — HTTP wrapper around HeyGen v2 API
# ---------------------------------------------------------------------------


_HEYGEN_API_ROOT = "https://api.heygen.com"
_HEYGEN_DEFAULT_TIMEOUT = 600.0
"""HeyGen renders can take several minutes for longer scripts. We
poll up to 10 minutes by default."""

_HEYGEN_POLL_INTERVAL = 5.0
"""Seconds between status polls. HeyGen's docs recommend ≥3s; 5s is
generous + cuts unnecessary API calls."""


class HeyGenAvatarSkill(Skill):
    """Render a talking-head video via HeyGen's v2 API.

    Inputs: a script + avatar_id + voice_id. Output: an MP4 URL the
    caller can download or hand to ``creative.hyperframes`` for
    composition.

    Auth: ``HEYGEN_API_KEY`` env var. Mike pastes once via the
    interactive setup; never stored in YAML.

    Cost: counts against Mike's HeyGen plan, not per-token. We surface
    the API's reported credit consumption when available.

    Why no streaming: HeyGen's render is a long-running async job
    (poll until status=completed). The skill blocks until done +
    returns a single SkillResult — no partial progress events. Caller
    decides how to display "rendering…" in the UI.
    """

    spec = SkillSpec(
        name="creative.heygen_avatar",
        description=(
            "Render a talking-head avatar video via HeyGen's API. Takes "
            "a script + avatar_id + voice_id, polls until the render "
            "completes, returns the MP4 URL. Use as the first step of "
            "marketing.video_from_post (or directly when you just need "
            "a raw avatar clip)."
        ),
        parameters={
            "script": (
                "The text the avatar should say. Required. Plain text "
                "only — HeyGen handles the voice synthesis."
            ),
            "avatar_id": (
                "HeyGen avatar identifier. The user picks this in their "
                "HeyGen dashboard; it persists across calls. Required."
            ),
            "voice_id": (
                "HeyGen voice identifier. Picked from HeyGen's voice "
                "library or the user's cloned voice. Required."
            ),
            "background": (
                "Optional. HeyGen scene background — color hex like "
                "'#0c0d10' or an image URL. Defaults to HeyGen's default "
                "background for the chosen avatar."
            ),
            "ratio": (
                "Optional. '16:9' (default — landscape, YouTube/web), "
                "'9:16' (portrait — Reels/TikTok), or '1:1' (square — "
                "Instagram feed)."
            ),
        },
        default_tier=InferenceTier.WORKHORSE,  # no LLM call inside
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any]
    ) -> SkillResult:
        api_key = os.getenv("HEYGEN_API_KEY", "").strip()
        if not api_key:
            raise SkillError(
                "HEYGEN_API_KEY is not set. Get a key at "
                "https://app.heygen.com/settings → API and run "
                "`korpha setup heygen` (or set the env var directly)."
            )

        script = str(args.get("script") or "").strip()
        avatar_id = str(args.get("avatar_id") or "").strip()
        voice_id = str(args.get("voice_id") or "").strip()
        if not (script and avatar_id and voice_id):
            raise SkillError(
                "creative.heygen_avatar requires `script`, `avatar_id`, "
                "and `voice_id`."
            )
        ratio = str(args.get("ratio") or "16:9").strip()
        if ratio not in ("16:9", "9:16", "1:1"):
            raise SkillError(
                f"ratio must be one of 16:9 / 9:16 / 1:1; got {ratio!r}"
            )

        background = args.get("background")

        dimension = {
            "16:9": {"width": 1280, "height": 720},
            "9:16": {"width": 720, "height": 1280},
            "1:1": {"width": 720, "height": 720},
        }[ratio]

        scene: dict[str, Any] = {
            "character": {
                "type": "avatar",
                "avatar_id": avatar_id,
                "avatar_style": "normal",
            },
            "voice": {"type": "text", "input_text": script, "voice_id": voice_id},
        }
        if background:
            bg = str(background).strip()
            if bg.startswith("#"):
                scene["background"] = {"type": "color", "value": bg}
            else:
                scene["background"] = {"type": "image", "url": bg}

        payload = {"video_inputs": [scene], "dimension": dimension}
        headers = {"x-api-key": api_key, "Content-Type": "application/json"}

        async with httpx.AsyncClient(
            base_url=_HEYGEN_API_ROOT, timeout=_HEYGEN_DEFAULT_TIMEOUT,
        ) as client:
            # ---- submit render job ----
            try:
                gen_resp = await client.post(
                    "/v2/video/generate", json=payload, headers=headers,
                )
            except httpx.HTTPError as exc:
                raise SkillError(
                    f"HeyGen submit failed (network): {exc}"
                ) from exc
            if gen_resp.status_code >= 400:
                raise SkillError(
                    f"HeyGen submit returned {gen_resp.status_code}: "
                    f"{gen_resp.text[:300]}"
                )
            job_data = gen_resp.json()
            video_id = (
                job_data.get("data", {}).get("video_id")
                or job_data.get("video_id")
            )
            if not video_id:
                raise SkillError(
                    f"HeyGen submit returned no video_id: {gen_resp.text[:300]}"
                )

            # ---- poll until completed / failed ----
            deadline = asyncio.get_event_loop().time() + _HEYGEN_DEFAULT_TIMEOUT
            video_url: str | None = None
            credits_used: float | None = None
            duration_s: float | None = None
            while True:
                try:
                    status_resp = await client.get(
                        "/v1/video_status.get",
                        params={"video_id": video_id},
                        headers=headers,
                    )
                except httpx.HTTPError as exc:
                    raise SkillError(
                        f"HeyGen poll failed (network): {exc}"
                    ) from exc
                if status_resp.status_code >= 400:
                    raise SkillError(
                        f"HeyGen poll returned {status_resp.status_code}: "
                        f"{status_resp.text[:300]}"
                    )
                status_body = status_resp.json().get("data") or {}
                state = str(status_body.get("status", "")).lower()
                if state == "completed":
                    video_url = status_body.get("video_url")
                    credits_used = status_body.get("credits_used")
                    duration_s = status_body.get("duration")
                    break
                if state in ("failed", "error"):
                    raise SkillError(
                        f"HeyGen render failed: "
                        f"{status_body.get('error') or status_body}"
                    )
                if asyncio.get_event_loop().time() > deadline:
                    raise SkillError(
                        f"HeyGen render still {state!r} after "
                        f"{_HEYGEN_DEFAULT_TIMEOUT:.0f}s — bailing. The "
                        f"job may finish later; video_id={video_id}."
                    )
                await asyncio.sleep(_HEYGEN_POLL_INTERVAL)

        if not video_url:
            raise SkillError("HeyGen reported completed but returned no URL")

        return SkillResult(
            skill_name=self.spec.name,
            summary=(
                f"Rendered {duration_s:.1f}s avatar video "
                f"({credits_used or '?'} credits)."
                if duration_s else
                f"Rendered avatar video at {video_url}"
            ),
            payload={
                "video_url": video_url,
                "video_id": video_id,
                "duration_seconds": duration_s,
                "credits_used": credits_used,
                "ratio": ratio,
            },
            cost_usd=0.0,  # paid via HeyGen subscription, not per-token
        )


# ---------------------------------------------------------------------------
# creative.hyperframes — local HTML→MP4 composition via the HyperFrames CLI
# ---------------------------------------------------------------------------


_HYPERFRAMES_BINARIES = ("hyperframes", "ffmpeg", "node")
"""Required binaries on PATH. The skill checks each one; a clean
SkillError lists everything missing in one go."""


class HyperFramesSkill(Skill):
    """Compose a final MP4 by feeding inputs into the HyperFrames CLI.

    HyperFrames is HeyGen's open-source HTML→video renderer (Apache
    2.0, github.com/heygen-com/hyperframes). It uses headless Chrome
    + FFmpeg to turn an HTML composition (with GSAP / Anime.js /
    Lottie animations) into MP4. Free, local, no API quota.

    Inputs: an avatar_clip (URL or local path — Mike's HeyGen export),
    a brand spec (colors / logo), and the composition kind. Outputs:
    a path to the rendered MP4 in the workspace.

    Mike-non-technical rule: this skill REQUIRES Node 22 + FFmpeg +
    Chrome headless on the host. We don't try to install them — we
    fail with a clean message + the exact commands. Future work: ship
    a Docker image so Mike doesn't see this at all.

    The composition kinds (``social_ad``, ``launch_reel``,
    ``module_intro``) map to HyperFrames templates that the user can
    customize in their workspace.
    """

    spec = SkillSpec(
        name="creative.hyperframes",
        description=(
            "Compose a polished MP4 by piping inputs into the local "
            "HyperFrames CLI. Takes an avatar_clip + brand spec + "
            "composition kind, returns the rendered MP4 path. Used "
            "as the second step of marketing.video_from_post."
        ),
        parameters={
            "avatar_clip": (
                "URL or local path to the talking-head clip from "
                "creative.heygen_avatar (or a recorded MP4 the user "
                "supplied). Required."
            ),
            "kind": (
                "Composition template. One of: social_ad, launch_reel, "
                "module_intro. Default: social_ad."
            ),
            "title": (
                "Optional. Title text overlay rendered in intro card. "
                "Plain text, ≤80 chars."
            ),
            "brand_color_hex": (
                "Optional. Primary brand color in hex (e.g. '#5e9eff'). "
                "Used for accent elements. Defaults to Korpha blue."
            ),
            "music_track": (
                "Optional. Path to a background music file (MP3/WAV). "
                "Mixed under the avatar voice at low volume."
            ),
        },
        default_tier=InferenceTier.WORKHORSE,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any]
    ) -> SkillResult:
        missing = [b for b in _HYPERFRAMES_BINARIES if not shutil.which(b)]
        if missing:
            raise SkillError(
                "creative.hyperframes needs these binaries on PATH and "
                f"they're missing: {', '.join(missing)}.\n\n"
                "Install:\n"
                "  npm install -g hyperframes\n"
                "  # FFmpeg: brew install ffmpeg  (macOS)  or  "
                "apt install ffmpeg  (Linux)\n"
                "  # Node ≥22 + Chrome (Puppeteer headless): "
                "https://nodejs.org and Chrome's normal install\n\n"
                "Once installed, re-run the skill."
            )

        avatar_clip = str(args.get("avatar_clip") or "").strip()
        if not avatar_clip:
            raise SkillError(
                "creative.hyperframes requires `avatar_clip` — URL or "
                "local path of the talking-head clip to compose around."
            )

        kind = str(args.get("kind") or "social_ad").strip().lower()
        valid_kinds = {"social_ad", "launch_reel", "module_intro"}
        if kind not in valid_kinds:
            raise SkillError(
                f"kind must be one of {sorted(valid_kinds)}; got {kind!r}"
            )

        title = str(args.get("title") or "").strip()
        brand_color = str(args.get("brand_color_hex") or "#5e9eff").strip()
        music_track = args.get("music_track")

        # Run HyperFrames in a tempdir under the workspace so artifacts
        # are user-inspectable. Persistent + per-business so Mike can
        # find his renders later.
        workspace = getattr(ctx, "workspace", None) or Path.home() / ".korpha"
        out_dir = Path(workspace) / "videos" / kind
        out_dir.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory(prefix="korpha-hf-") as td:
            project_dir = Path(td) / "composition"
            project_dir.mkdir()

            # Project metadata HyperFrames consumes. Real-world templates
            # live in the user's workspace; for v1 we ship inline.
            project_meta = {
                "kind": kind,
                "title": title,
                "brand_color": brand_color,
                "avatar_clip": avatar_clip,
                "music_track": str(music_track) if music_track else None,
            }
            (project_dir / "project.json").write_text(
                _json_dumps(project_meta), encoding="utf-8"
            )

            # The skill ships a tiny HTML scaffold per kind. The
            # generated_html() helper produces it; we don't bundle a
            # fancy template library — keeps the skill self-contained.
            (project_dir / "index.html").write_text(
                _generate_hyperframes_html(project_meta), encoding="utf-8",
            )

            output_path = out_dir / f"{kind}_{int(asyncio.get_event_loop().time())}.mp4"

            # Run hyperframes render. Subprocess is allowed in built-ins;
            # the agent-authored validator forbids it for safety.
            cmd = [
                "hyperframes", "render",
                "--input", str(project_dir / "index.html"),
                "--output", str(output_path),
                "--no-audio-mix" if not music_track else "--mix-audio",
            ]

            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=600.0,
                )
            except TimeoutError as exc:
                raise SkillError(
                    "HyperFrames render exceeded 600s — bailing. "
                    "Long compositions may need a higher timeout; "
                    "shorten the avatar clip or split into segments."
                ) from exc

            if proc.returncode != 0:
                err = stderr_b.decode("utf-8", errors="replace")[:500]
                raise SkillError(
                    f"HyperFrames render failed (exit {proc.returncode}): {err}"
                )

            if not output_path.exists():
                raise SkillError(
                    "HyperFrames returned 0 but no output file was "
                    f"created at {output_path}."
                )

        size_mb = output_path.stat().st_size / (1024 * 1024)
        return SkillResult(
            skill_name=self.spec.name,
            summary=(
                f"Rendered {kind} video → {output_path.name} ({size_mb:.1f} MB)"
            ),
            payload={
                "output_path": str(output_path),
                "kind": kind,
                "size_bytes": output_path.stat().st_size,
                "title": title,
                "brand_color": brand_color,
                "has_music": bool(music_track),
            },
            cost_usd=0.0,  # local CPU only
        )


# ---------------------------------------------------------------------------
# Tiny helpers
# ---------------------------------------------------------------------------


def _json_dumps(obj: dict[str, Any]) -> str:
    """Standalone so we can adjust formatting without importing json
    at module level (keeps imports tight)."""
    import json
    return json.dumps(obj, indent=2)


def _generate_hyperframes_html(project: dict[str, Any]) -> str:
    """Render a minimal HyperFrames-compatible HTML composition.

    HyperFrames reads ``data-start`` / ``data-duration`` /
    ``data-track-index`` attributes off DOM elements and walks them
    frame-by-frame. The kinds map roughly to:

      - social_ad:     0:00 title card → 0:02 avatar talks → 0:13 CTA
      - launch_reel:   0:00 title → 0:03 avatar → 0:30 lower-third recap
      - module_intro:  0:00 logo flash → 0:02 title → 0:06 avatar handoff

    For v1 we ship one template per kind. Power users replace this
    function with their own composition by overriding the skill in a
    plugin.
    """
    title = project.get("title") or ""
    brand = project.get("brand_color") or "#5e9eff"
    avatar = project.get("avatar_clip") or ""
    kind = project.get("kind", "social_ad")

    # Same skeleton, different timings + copy depending on kind.
    timings = {
        "social_ad":     {"title_dur": 2, "avatar_start": 2, "avatar_dur": 11, "cta_start": 13, "cta_dur": 2},
        "launch_reel":   {"title_dur": 3, "avatar_start": 3, "avatar_dur": 25, "cta_start": 28, "cta_dur": 4},
        "module_intro":  {"title_dur": 2, "avatar_start": 2, "avatar_dur": 8,  "cta_start": 10, "cta_dur": 2},
    }[kind]

    return f"""<!doctype html>
<html><head><meta charset="utf-8" />
<title>{title} — {kind}</title>
<style>
  body {{ margin: 0; background: #0c0d10; color: white;
          font-family: ui-sans-serif, system-ui, sans-serif; }}
  .stage {{ position: relative; width: 1280px; height: 720px; overflow: hidden; }}
  .title-card {{ position: absolute; inset: 0; display: flex;
                 align-items: center; justify-content: center;
                 background: linear-gradient(135deg, #0c0d10, {brand}33); }}
  .title-card h1 {{ font-size: 64px; font-weight: 700; margin: 0;
                    color: white; text-align: center; max-width: 1100px;
                    border-bottom: 4px solid {brand}; padding-bottom: 16px; }}
  .avatar {{ position: absolute; inset: 0; }}
  .avatar video {{ width: 100%; height: 100%; object-fit: cover; }}
  .cta {{ position: absolute; inset: 0; display: flex;
          align-items: center; justify-content: center;
          background: rgba(12, 13, 16, 0.92); }}
  .cta div {{ background: {brand}; color: black; padding: 18px 36px;
              border-radius: 8px; font-size: 28px; font-weight: 600; }}
</style></head>
<body>
<div class="stage">
  <div class="title-card"
       data-start="0" data-duration="{timings['title_dur']}" data-track-index="0">
    <h1>{title or "Coming up"}</h1>
  </div>
  <div class="avatar"
       data-start="{timings['avatar_start']}"
       data-duration="{timings['avatar_dur']}" data-track-index="1">
    <video src="{avatar}" autoplay muted></video>
  </div>
  <div class="cta"
       data-start="{timings['cta_start']}"
       data-duration="{timings['cta_dur']}" data-track-index="2">
    <div>Learn more →</div>
  </div>
</div>
</body></html>
"""


register(HeyGenAvatarSkill())
register(HyperFramesSkill())


__all__ = [
    "HeyGenAvatarSkill",
    "HyperFramesSkill",
]
