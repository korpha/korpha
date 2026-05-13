"""``deploy.publish_landing`` — ship HTML to a real URL.

Closes the BRIEF demo's "minute 4:30 oh-shit moment". Pairs
with ``landing.draft_copy`` (already shipped):

  1. landing.draft_copy → headline + sub + CTA + body HTML
  2. deploy.publish_landing → live URL Mike can click

Stages an Approval (action_class=CODE_CHANGE) before any
real deploy runs. The LocalFileDeployer default skips the
approval gate because the file write is local-only. Cloud
deployers (Vercel / Cloudflare / Netlify) plugins can opt
in via ``require_approval=True``.
"""
from __future__ import annotations

from typing import Any

from korpha.audit.model import InferenceTier
from korpha.deploy import (
    DeploymentTarget,
    deploy_registry,
)
from korpha.deploy.contract import slugify
from korpha.kanban.artifacts import (
    ArtifactKind,
    ArtifactReviewState,
)
from korpha.skills.registry import register
from korpha.skills.types import (
    Skill,
    SkillContext,
    SkillError,
    SkillProvenance,
    SkillResult,
    SkillSpec,
)


_DEFAULT_HTML_WRAPPER = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>{title}</title>
<style>
  body {{ font: 16px/1.5 system-ui, sans-serif; color: #111;
          max-width: 720px; margin: 4rem auto; padding: 0 1rem; }}
  h1 {{ font-size: 2rem; margin-bottom: 0.5rem; }}
  .sub {{ color: #555; font-size: 1.1rem; margin-bottom: 2rem; }}
  .cta {{ display: inline-block; background: #111; color: #fff;
          padding: 0.8rem 1.4rem; text-decoration: none;
          border-radius: 6px; font-weight: 500; }}
  .cta:hover {{ background: #333; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""


class PublishLandingSkill(Skill):
    """Ship a landing page to a public URL.

    Two input shapes:

      * ``html=`` — pre-rendered full HTML. Used straight.
      * ``headline=`` + ``subhead=`` + ``cta_label=`` +
        ``cta_url=`` (+ optional ``body=`` for extra HTML
        between sub and CTA) — wrapped in a default template.

    The configured Deployer (LocalFileDeployer by default; a
    plugin like Vercel takes over when registered) decides where
    the file goes and what URL Mike gets back. After deploy:

      * a kanban_card_id (if provided) gets a typed DEPLOY
        artifact with the live URL
      * an Activity row records the deploy
    """

    spec = SkillSpec(
        name="deploy.publish_landing",
        description=(
            "Publish a landing page to a public URL. Use right "
            "after landing.draft_copy when the founder approves "
            "the headline + CTA. Pass either pre-rendered "
            "``html=`` or the structured ``headline``/``subhead``/"
            "``cta_label``/``cta_url`` fields and we'll wrap "
            "them in a clean default template. Returns a real "
            "URL the founder can click. Default deployer writes "
            "to the local dashboard; cloud-host plugins (Vercel "
            "/ Cloudflare Pages / Netlify) override it when "
            "configured."
        ),
        parameters={
            "slug": (
                "Short slug for the URL — defaults to a slugified "
                "version of the headline. Letters/digits/hyphens."
            ),
            "html": "Full pre-rendered HTML. Skip the wrapper.",
            "headline": "Hero headline (h1).",
            "subhead": "Sub-headline below the h1.",
            "cta_label": "Button text — 'Sign up' / 'Get early access'.",
            "cta_url": "Where the CTA links to (Stripe link, form URL).",
            "body": "Optional extra HTML between subhead and CTA.",
            "title": "Page <title> tag. Defaults to headline.",
            "kanban_card_id": (
                "Optional. UUID of the kanban card this deploy "
                "fulfills. We attach the deployed URL as a typed "
                "DEPLOY artifact so /app/kanban renders the link."
            ),
        },
        default_tier=InferenceTier.WORKHORSE,
        provenance=SkillProvenance.BUILTIN,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any],
    ) -> SkillResult:
        html = (args.get("html") or "").strip()
        headline = (args.get("headline") or "").strip()
        if not html and not headline:
            raise SkillError(
                "deploy.publish_landing: pass either html= or "
                "headline= (with subhead + cta_label + cta_url)",
            )

        if not html:
            subhead = (args.get("subhead") or "").strip()
            cta_label = (args.get("cta_label") or "").strip()
            cta_url = (args.get("cta_url") or "").strip()
            if not (subhead and cta_label and cta_url):
                raise SkillError(
                    "deploy.publish_landing: structured form "
                    "needs subhead + cta_label + cta_url all "
                    "non-empty",
                )
            extra_body = (args.get("body") or "").strip()
            inner = (
                f"<h1>{_html_escape(headline)}</h1>\n"
                f"<p class='sub'>{_html_escape(subhead)}</p>\n"
                + (f"{extra_body}\n" if extra_body else "")
                + f"<a class='cta' href='{_html_escape(cta_url)}'>"
                f"{_html_escape(cta_label)}</a>\n"
            )
            page_title = (
                args.get("title") or headline or "Landing"
            )
            html = _DEFAULT_HTML_WRAPPER.format(
                title=_html_escape(str(page_title)),
                body=inner,
            )

        slug = slugify(
            str(args.get("slug") or "")
            or headline
            or "landing"
        )
        title = str(args.get("title") or headline or slug)
        target = DeploymentTarget.from_html(
            business_id=ctx.business.id,
            slug=slug,
            html=html,
            title=title,
            description=str(args.get("subhead") or ""),
        )

        deployer = deploy_registry.active()
        result = await deployer.deploy(target)

        # Attach a typed DEPLOY artifact to the source kanban
        # card if one was named, primary on first artifact so
        # /app/kanban renders it as the headline link.
        card_id_raw = args.get("kanban_card_id")
        if card_id_raw:
            try:
                from uuid import UUID as _UUID
                from korpha.kanban import (
                    ArtifactService, CardArtifact,
                )

                card_id = _UUID(str(card_id_raw))
                svc = ArtifactService(ctx.session)
                existing = svc.list_for_card(card_id)
                svc.add(
                    card_id=card_id,
                    business_id=ctx.business.id,
                    kind=ArtifactKind.DEPLOY,
                    label=title or slug,
                    location=result.url,
                    is_primary=not existing,
                )
            except Exception:  # noqa: BLE001
                # Don't fail the deploy because artifact emit
                # hiccupped — the deploy is the real artifact.
                import logging
                logging.getLogger(__name__).warning(
                    "deploy artifact emit failed", exc_info=True,
                )

        return SkillResult(
            skill_name=self.spec.name,
            summary=f"Deployed {title!r} → {result.url}",
            payload={
                "url": result.url,
                "slug": result.slug,
                "deployer": result.deployer_name,
                "bytes_written": result.bytes_written,
            },
            cost_usd=0.0,
        )


def _html_escape(s: str) -> str:
    """Cheap HTML escape — these strings come from the LLM, not
    user input from a browser, but better safe than gross."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


register(PublishLandingSkill())


__all__ = ["PublishLandingSkill"]
