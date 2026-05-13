"""RankMyAnswer.com client — GEO + SEO audit and schema generation.

Two distinct things this covers:

  - **GEO (Generative Engine Optimization)** — getting cited by ChatGPT,
    Perplexity, Claude, Gemini answers. Different signals than classic
    SEO: clear answer-shaped paragraphs, schema.org markup, citation-
    friendly structure.
  - **SEO (Search Engine Optimization)** — getting found on Google.
    Classic on-page + technical signals.

The cofounder uses these together: when the CMO ships a landing page,
RankMyAnswer audits it for both ranking surfaces. When new content
goes up, RankMyAnswer suggests the schema and answer structure that
both Google and the LLMs will pick up.

Auth: ``RANKMYANSWER_API_KEY`` env var, or stored in providers.yaml
under the ``integrations:`` section by the
``korpha config-rankmyanswer-add`` wizard.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx

from korpha.inference.limits import request_timeout

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.rankmyanswer.com/v1"


class RankMyAnswerError(RuntimeError):
    """Generic RankMyAnswer API failure."""


@dataclass
class RankMyAnswerClient:
    """Thin async wrapper around the RankMyAnswer.com API.

    The skill layer does formatting + storage; this layer only handles
    the HTTP wire + auth. Same pattern as ``StripeClient`` and
    ``ResendEmailNotifier``.
    """

    api_key: str
    base_url: str = DEFAULT_BASE_URL
    timeout_seconds: float = field(default_factory=request_timeout)
    _client: httpx.AsyncClient | None = field(default=None, init=False, repr=False)

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url.rstrip("/"),
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "korpha-cofounder/0.1",
                },
                timeout=self.timeout_seconds,
            )
        return self._client

    async def balance(self) -> dict[str, Any]:
        """``GET /credits/balance``. Returns balance + plan_tier."""
        body = await self._get("/credits/balance")
        return body if isinstance(body, dict) else {}

    async def list_projects(self) -> list[dict[str, Any]]:
        """``GET /projects``. The Founder's tracked sites."""
        body = await self._get("/projects")
        if isinstance(body, list):
            return [b for b in body if isinstance(b, dict)]
        if isinstance(body, dict) and isinstance(body.get("projects"), list):
            return [b for b in body["projects"] if isinstance(b, dict)]
        return []

    async def niche_templates(self) -> list[dict[str, Any]]:
        """``GET /niche-templates``. The 40 niche templates RankMyAnswer
        ships out of the box (used to seed schema + content shape per
        category)."""
        body = await self._get("/niche-templates")
        if isinstance(body, list):
            return [b for b in body if isinstance(b, dict)]
        if isinstance(body, dict) and isinstance(body.get("templates"), list):
            return [b for b in body["templates"] if isinstance(b, dict)]
        return []

    async def audit_url(
        self,
        url: str,
        *,
        target_query: str | None = None,
    ) -> dict[str, Any]:
        """``POST /audit``. Audits a URL for both GEO (LLM citations)
        and SEO (Google) and returns scores + recommendations.

        The ``target_query`` is the search intent / question the page
        is supposed to answer — used by the GEO scoring path.
        """
        payload: dict[str, Any] = {"url": url}
        if target_query:
            payload["target_query"] = target_query
        body = await self._post("/audit", payload)
        return body if isinstance(body, dict) else {}

    async def generate_schema(
        self,
        project_id: str,
        *,
        url: str,
        schema_type: str = "LocalBusiness",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """``POST /projects/{project_id}/content/schema/generate``.
        Returns a JSON-LD blob the Founder pastes into the page head.
        """
        payload: dict[str, Any] = {"url": url, "schema_type": schema_type}
        if extra:
            payload["extra"] = extra
        body = await self._post(
            f"/projects/{project_id}/content/schema/generate",
            payload,
        )
        return body if isinstance(body, dict) else {}

    # ------------------------------------------------------------------ HTTP

    async def _get(self, path: str) -> Any:
        client = self._get_client()
        try:
            resp = await client.get(path)
        except httpx.RequestError as exc:
            raise RankMyAnswerError(f"network error calling {path}: {exc}") from exc
        return _parse(resp, path)

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        client = self._get_client()
        try:
            resp = await client.post(path, json=payload)
        except httpx.RequestError as exc:
            raise RankMyAnswerError(f"network error calling {path}: {exc}") from exc
        return _parse(resp, path)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _parse(resp: httpx.Response, path: str) -> Any:
    if resp.status_code == 401:
        raise RankMyAnswerError(
            "RankMyAnswer auth failed (401). Check your API key — "
            "configure with `korpha config-rankmyanswer-add`."
        )
    if resp.status_code == 402:
        raise RankMyAnswerError(
            "RankMyAnswer credits exhausted (402). Top up at "
            "https://rankmyanswer.com — or `korpha skill run "
            "geo_seo.balance` to confirm."
        )
    if resp.status_code == 429:
        raise RankMyAnswerError(
            "RankMyAnswer rate-limit (429). Wait a moment + retry."
        )
    if resp.status_code >= 400:
        raise RankMyAnswerError(
            f"RankMyAnswer {path} → {resp.status_code}: {resp.text[:300]}"
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise RankMyAnswerError(
            f"RankMyAnswer returned non-JSON for {path}: {resp.text[:200]}"
        ) from exc


def client_from_env_or_config() -> RankMyAnswerClient | None:
    """Build a client from ``RANKMYANSWER_API_KEY`` env var first,
    falling back to providers.yaml's ``integrations:`` section. Returns
    None if no key is configured anywhere — caller decides how to
    surface that."""
    import os
    from pathlib import Path

    key = os.getenv("RANKMYANSWER_API_KEY")
    base_url = os.getenv("RANKMYANSWER_BASE_URL") or DEFAULT_BASE_URL
    if not key:
        try:
            import yaml

            from korpha.inference.config import config_path

            p = config_path()
            if p.exists():
                body = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                for entry in body.get("integrations") or []:
                    if not isinstance(entry, dict):
                        continue
                    if entry.get("kind") == "rank_my_answer":
                        # api_key inline OR pulled from env_var name
                        candidate = entry.get("api_key")
                        if isinstance(candidate, str) and candidate.strip():
                            key = candidate.strip()
                        else:
                            env_name = entry.get("api_key_env")
                            if isinstance(env_name, str) and env_name.strip():
                                key = os.getenv(env_name.strip()) or None
                        if entry.get("base_url"):
                            base_url = str(entry["base_url"])
                        break
            del Path  # quiet unused-import if anything
        except Exception:
            pass
    if not key:
        return None
    return RankMyAnswerClient(api_key=key, base_url=base_url)


__all__ = [
    "DEFAULT_BASE_URL",
    "RankMyAnswerClient",
    "RankMyAnswerError",
    "client_from_env_or_config",
]
