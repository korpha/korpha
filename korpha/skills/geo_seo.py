"""geo_seo.* skills — RankMyAnswer.com integration.

Two ranking surfaces matter for getting eyeballs in 2026:

  - **GEO** (Generative Engine Optimization) — getting cited by
    ChatGPT, Perplexity, Claude, Gemini answers. The new front door.
  - **SEO** (Search Engine Optimization) — getting found on Google.
    Still the long-tail traffic engine.

These skills wrap the RankMyAnswer.com API so the CMO + Workers can
audit pages, generate the JSON-LD schema both Google + LLMs cite, and
track ranking-relevant changes as part of normal cofounder work.

Optional integration. Configure with ``korpha config-rankmyanswer-add``
or ``RANKMYANSWER_API_KEY`` env var. Skills raise a clear error if
nothing's configured.
"""
from __future__ import annotations

from typing import Any

from korpha.audit.model import InferenceTier
from korpha.integrations.rank_my_answer import (
    RankMyAnswerError,
    client_from_env_or_config,
)
from korpha.skills.registry import register
from korpha.skills.types import (
    Skill,
    SkillContext,
    SkillError,
    SkillResult,
    SkillSpec,
)

_NOT_CONFIGURED = (
    "RankMyAnswer API key not configured. Run "
    "`korpha config-rankmyanswer-add` to add one — that lets "
    "Korpha work on getting eyeballs to your product (GEO + SEO)."
)


class AuditUrlSkill(Skill):
    spec = SkillSpec(
        name="geo_seo.audit_url",
        description=(
            "Audit a URL for both GEO (LLM citations) and SEO (Google) "
            "via RankMyAnswer. Returns scores per surface plus concrete "
            "recommendations. Optional integration — set up via "
            "`korpha config-rankmyanswer-add`."
        ),
        parameters={
            "url": "The page to audit, e.g. https://yoursite.com/landing",
            "target_query": (
                "Optional: the search intent / question this page is "
                "supposed to answer. Used by the GEO scorer."
            ),
        },
        default_tier=InferenceTier.WORKHORSE,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any]
    ) -> SkillResult:
        del ctx
        url = str(args.get("url") or "").strip()
        if not url:
            raise SkillError("geo_seo.audit_url requires `url`.")
        client = client_from_env_or_config()
        if client is None:
            raise SkillError(_NOT_CONFIGURED)
        try:
            try:
                report = await client.audit_url(
                    url, target_query=args.get("target_query") or None
                )
            finally:
                await client.close()
        except RankMyAnswerError as exc:
            raise SkillError(f"RankMyAnswer audit failed: {exc}") from exc

        geo_score = report.get("geo_score") or report.get("scores", {}).get("geo")
        seo_score = report.get("seo_score") or report.get("scores", {}).get("seo")
        summary = (
            f"Audit {url}: GEO={geo_score or '?'} / SEO={seo_score or '?'}"
        )
        return SkillResult(
            skill_name=self.spec.name,
            summary=summary,
            payload={"url": url, "report": report},
            cost_usd=0.0,
        )


class GenerateSchemaSkill(Skill):
    spec = SkillSpec(
        name="geo_seo.generate_schema",
        description=(
            "Generate JSON-LD schema (LocalBusiness / Product / Article / "
            "FAQPage / etc.) for a URL via RankMyAnswer. Both Google and "
            "LLMs cite better-structured pages. Output is a JSON-LD blob "
            "the Founder pastes into the page <head>."
        ),
        parameters={
            "project_id": (
                "RankMyAnswer project ID (run `geo_seo.list_projects` "
                "to find it)."
            ),
            "url": "The page to generate schema for.",
            "schema_type": (
                "Optional schema.org type (LocalBusiness | Product | "
                "Article | FAQPage | Service | Event). Default LocalBusiness."
            ),
        },
        default_tier=InferenceTier.WORKHORSE,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any]
    ) -> SkillResult:
        del ctx
        project_id = str(args.get("project_id") or "").strip()
        url = str(args.get("url") or "").strip()
        if not project_id or not url:
            raise SkillError(
                "geo_seo.generate_schema requires `project_id` and `url`."
            )
        client = client_from_env_or_config()
        if client is None:
            raise SkillError(_NOT_CONFIGURED)
        try:
            try:
                result = await client.generate_schema(
                    project_id,
                    url=url,
                    schema_type=str(args.get("schema_type") or "LocalBusiness"),
                )
            finally:
                await client.close()
        except RankMyAnswerError as exc:
            raise SkillError(f"RankMyAnswer schema generation failed: {exc}") from exc
        return SkillResult(
            skill_name=self.spec.name,
            summary=f"Schema generated for {url}",
            payload={"url": url, "schema": result},
            cost_usd=0.0,
        )


class ListProjectsSkill(Skill):
    spec = SkillSpec(
        name="geo_seo.list_projects",
        description="List the Founder's RankMyAnswer projects (sites the cofounder is tracking).",
        parameters={},
        default_tier=InferenceTier.WORKHORSE,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any]
    ) -> SkillResult:
        del ctx, args
        client = client_from_env_or_config()
        if client is None:
            raise SkillError(_NOT_CONFIGURED)
        try:
            try:
                projects = await client.list_projects()
            finally:
                await client.close()
        except RankMyAnswerError as exc:
            raise SkillError(f"RankMyAnswer project list failed: {exc}") from exc
        return SkillResult(
            skill_name=self.spec.name,
            summary=f"{len(projects)} RankMyAnswer project(s)",
            payload={"projects": projects},
            cost_usd=0.0,
        )


class BalanceSkill(Skill):
    spec = SkillSpec(
        name="geo_seo.balance",
        description="Show the Founder's RankMyAnswer credit balance + plan tier.",
        parameters={},
        default_tier=InferenceTier.WORKHORSE,
    )

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any]
    ) -> SkillResult:
        del ctx, args
        client = client_from_env_or_config()
        if client is None:
            raise SkillError(_NOT_CONFIGURED)
        try:
            try:
                bal = await client.balance()
            finally:
                await client.close()
        except RankMyAnswerError as exc:
            raise SkillError(f"RankMyAnswer balance check failed: {exc}") from exc
        balance = bal.get("balance")
        plan = bal.get("plan_tier") or bal.get("plan")
        return SkillResult(
            skill_name=self.spec.name,
            summary=f"RankMyAnswer balance: {balance} ({plan or 'plan unknown'})",
            payload=bal,
            cost_usd=0.0,
        )


register(AuditUrlSkill())
register(GenerateSchemaSkill())
register(ListProjectsSkill())
register(BalanceSkill())


__all__ = [
    "AuditUrlSkill",
    "BalanceSkill",
    "GenerateSchemaSkill",
    "ListProjectsSkill",
]
