"""YAML-driven skill manifests.

A YAML skill lives as a directory:

    my_skill/
      manifest.yaml      <- required
      prompts/system.md  <- optional, may also be inline in manifest
      prompts/user.md    <- optional, may also be inline in manifest

Manifest schema:

```yaml
name: niche.find_micro_niches      # required, dotted
version: 1.0.0                     # optional, semver
description: |                     # required, shown to LLM + UI
  Given Founder skills + time budget + savings, propose 3-5 micro-niches.
default_tier: pro                  # workhorse | pro | consultant
parameters:                        # name -> {description, default?}
  skills: {description: "Founder skills", default: "(unspecified)"}
  time_budget_hours: {description: "Hrs/week", default: "5"}
system_prompt: |                   # OR system_prompt_file: prompts/system.md
  You are the niche-discovery skill...
user_prompt_template: |            # str.format with parameter names + nothing else
  Founder skills: {skills}
  Weekly time: {time_budget_hours}h
output:
  format: json                     # json (default) | text
  summary_key: summary             # JSON key to use as SkillResult.summary
  required_keys: [candidates, summary]
session_key_template: "skill-niche-{business_id}"  # optional
max_tokens: 4000                   # optional
```

This deliberately mirrors the spirit of agentskills.io while staying small
enough that a non-Python contributor can publish a skill in 10 minutes
without touching code. Compatibility with the official spec can grow
over time without changing the loader interface.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from korpha._jsonext import extract_json_dict
from korpha.audit.model import InferenceTier
from korpha.inference.types import CompletionRequest, Message, Role
from korpha.skills.types import (
    Skill,
    SkillContext,
    SkillError,
    SkillResult,
    SkillSpec,
)


class YamlSkillError(SkillError):
    """Manifest-level problem: missing field, bad path, unknown enum, etc."""


class YamlSkill(Skill):
    """A skill whose prompts + metadata come from a YAML manifest.

    Constructed by ``load_yaml_skill(path)``. Don't instantiate directly —
    the loader fills in every required field from the manifest.
    """

    # Sentinel so Skill.__init_subclass__ sees a class-level `spec` and lets
    # the subclass exist; real spec is set per-instance by load_yaml_skill.
    spec: SkillSpec = SkillSpec(name="(yaml)", description="(populated at load)")

    def __init__(
        self,
        *,
        spec: SkillSpec,
        system_prompt: str,
        user_prompt_template: str,
        output_format: str,
        summary_key: str,
        required_keys: tuple[str, ...],
        session_key_template: str,
        max_tokens: int | None,
        parameter_defaults: dict[str, str],
        source_path: Path,
    ) -> None:
        self.spec = spec
        self.system_prompt = system_prompt
        self.user_prompt_template = user_prompt_template
        self.output_format = output_format
        self.summary_key = summary_key
        self.required_keys = required_keys
        self.session_key_template = session_key_template
        self.max_tokens = max_tokens
        self.parameter_defaults = parameter_defaults
        self.source_path = source_path

    async def run(
        self, *, ctx: SkillContext, args: dict[str, Any]
    ) -> SkillResult:
        merged = {**self.parameter_defaults, **{k: str(v) for k, v in args.items()}}
        try:
            user_content = self.user_prompt_template.format(**merged)
        except KeyError as exc:
            raise YamlSkillError(
                f"YAML skill {self.spec.name!r}: prompt references unknown "
                f"parameter {exc.args[0]!r}. Declared parameters: "
                f"{sorted(self.parameter_defaults)}"
            ) from exc

        session_key = self.session_key_template.format(
            business_id=ctx.business.id, **merged
        )
        request = CompletionRequest(
            messages=[
                Message(role=Role.SYSTEM, content=self.system_prompt),
                Message(role=Role.USER, content=user_content),
            ],
            tier=self.spec.default_tier,
            session_key=session_key,
            max_tokens=self.max_tokens,
            timeout_seconds=240,
        )
        response = await ctx.cost_tracker.complete(
            request,
            session=ctx.session,
            business_id=ctx.business.id,
            agent_role_id=ctx.invoking_agent_role_id,
        )

        if self.output_format == "text":
            return SkillResult(
                skill_name=self.spec.name,
                summary=response.content.strip().splitlines()[0][:200]
                if response.content.strip()
                else "(empty)",
                payload={"text": response.content},
                cost_usd=float(response.cost_usd),
                reasoning=response.reasoning,
                raw_response=response.content,
            )

        payload = extract_json_dict(response.content)
        if payload is None:
            raise SkillError(
                f"YAML skill {self.spec.name!r} returned unparseable JSON. "
                f"first 500 chars: {response.content[:500]}"
            )
        for key in self.required_keys:
            if key not in payload:
                raise SkillError(
                    f"YAML skill {self.spec.name!r} JSON missing required "
                    f"key {key!r}. Got keys: {sorted(payload)}"
                )
        summary = str(payload.get(self.summary_key, "")).strip()
        if not summary:
            summary = f"{self.spec.name} completed"
        return SkillResult(
            skill_name=self.spec.name,
            summary=summary,
            payload=payload,
            cost_usd=float(response.cost_usd),
            reasoning=response.reasoning,
            raw_response=response.content,
        )


def load_yaml_skill(skill_dir: Path) -> YamlSkill:
    """Build a YamlSkill from a directory containing ``manifest.yaml``."""
    manifest_path = skill_dir / "manifest.yaml"
    if not manifest_path.exists():
        raise YamlSkillError(f"no manifest.yaml in {skill_dir}")

    import yaml

    raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise YamlSkillError(f"{manifest_path}: top level must be a mapping")

    name = _require_str(raw, "name", manifest_path)
    description = _require_str(raw, "description", manifest_path)
    tier_str = str(raw.get("default_tier") or "pro").lower()
    try:
        default_tier = InferenceTier(tier_str)
    except ValueError as exc:
        valid = ", ".join(t.value for t in InferenceTier)
        raise YamlSkillError(
            f"{manifest_path}: default_tier {tier_str!r} unknown. Valid: {valid}"
        ) from exc

    params_raw = raw.get("parameters") or {}
    if not isinstance(params_raw, dict):
        raise YamlSkillError(f"{manifest_path}: parameters must be a mapping")
    parameters: dict[str, str] = {}
    parameter_defaults: dict[str, str] = {}
    for pname, pmeta in params_raw.items():
        if isinstance(pmeta, str):
            parameters[pname] = pmeta
            continue
        if not isinstance(pmeta, dict):
            raise YamlSkillError(
                f"{manifest_path}: parameter {pname!r} must be a string "
                f"or {{description, default?}} mapping"
            )
        parameters[str(pname)] = str(pmeta.get("description", ""))
        if "default" in pmeta:
            parameter_defaults[str(pname)] = str(pmeta["default"])

    system_prompt = _resolve_text(
        raw, "system_prompt", "system_prompt_file", skill_dir, manifest_path
    )
    user_prompt_template = _resolve_text(
        raw,
        "user_prompt_template",
        "user_prompt_template_file",
        skill_dir,
        manifest_path,
    )
    if not user_prompt_template:
        raise YamlSkillError(
            f"{manifest_path}: must define user_prompt_template "
            "(inline or via user_prompt_template_file)"
        )
    if not system_prompt:
        system_prompt = (
            "You are an Korpha skill helping a solo entrepreneur. "
            "Follow the user's instructions exactly."
        )

    output_block = raw.get("output") or {}
    if not isinstance(output_block, dict):
        raise YamlSkillError(f"{manifest_path}: output must be a mapping")
    output_format = str(output_block.get("format", "json")).lower()
    if output_format not in ("json", "text"):
        raise YamlSkillError(
            f"{manifest_path}: output.format must be 'json' or 'text', "
            f"got {output_format!r}"
        )
    summary_key = str(output_block.get("summary_key", "summary"))
    required_keys_raw = output_block.get("required_keys", [])
    if not isinstance(required_keys_raw, list):
        raise YamlSkillError(f"{manifest_path}: output.required_keys must be a list")
    required_keys = tuple(str(k) for k in required_keys_raw)

    session_key_template = str(
        raw.get("session_key_template") or f"skill-{name}-{{business_id}}"
    )
    max_tokens_raw = raw.get("max_tokens")
    max_tokens = int(max_tokens_raw) if max_tokens_raw is not None else None

    # platforms: optional whitelist. Empty list / missing key →
    # no restriction. Validated against sys.platform names so a
    # typo gets caught at load time, not when the LLM tries to
    # invoke the skill.
    platforms_raw = raw.get("platforms") or []
    if not isinstance(platforms_raw, list):
        raise YamlSkillError(
            f"{manifest_path}: platforms must be a list of strings "
            "(e.g. ['linux', 'darwin'])"
        )
    valid_platforms = ("linux", "darwin", "win32", "freebsd", "cygwin")
    platforms: list[str] = []
    for p in platforms_raw:
        ps = str(p).strip().lower()
        if ps not in valid_platforms:
            raise YamlSkillError(
                f"{manifest_path}: unknown platform {ps!r}. "
                f"Valid: {', '.join(valid_platforms)}"
            )
        platforms.append(ps)

    # provenance: defaults to USER_AUTHORED for hand-written YAML
    # skills the founder dropped into ~/.korpha/skills/. Agent-
    # authored YAML uses meta.author_skill which sets this
    # explicitly to AGENT_AUTHORED.
    from korpha.skills.types import SkillProvenance
    provenance_raw = str(
        raw.get("provenance") or "user_authored"
    ).strip().lower()
    try:
        provenance = SkillProvenance(provenance_raw)
    except ValueError as exc:
        valid_p = ", ".join(p.value for p in SkillProvenance)
        raise YamlSkillError(
            f"{manifest_path}: provenance {provenance_raw!r} unknown. "
            f"Valid: {valid_p}"
        ) from exc

    spec = SkillSpec(
        name=name,
        description=description.strip(),
        parameters=parameters,
        default_tier=default_tier,
        platforms=tuple(platforms),
        provenance=provenance,
    )
    return YamlSkill(
        spec=spec,
        system_prompt=system_prompt.strip(),
        user_prompt_template=user_prompt_template,
        output_format=output_format,
        summary_key=summary_key,
        required_keys=required_keys,
        session_key_template=session_key_template,
        max_tokens=max_tokens,
        parameter_defaults=parameter_defaults,
        source_path=skill_dir,
    )




def discover_yaml_skills(root: Path) -> list[YamlSkill]:
    """Find every immediate subdirectory of ``root`` that has a manifest.yaml
    and load it. Non-skill directories are silently ignored. Errors in any
    one manifest don't stop the others — they're collected and re-raised.
    """
    if not root.exists():
        return []
    skills: list[YamlSkill] = []
    errors: list[str] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if not (entry / "manifest.yaml").exists():
            continue
        try:
            skills.append(load_yaml_skill(entry))
        except YamlSkillError as exc:
            errors.append(f"  {entry.name}: {exc}")
    if errors:
        raise YamlSkillError(
            "Some YAML skills failed to load:\n" + "\n".join(errors)
        )
    return skills


def _require_str(raw: dict[str, Any], key: str, path: Path) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise YamlSkillError(f"{path}: missing required string {key!r}")
    return value


def _resolve_text(
    raw: dict[str, Any],
    inline_key: str,
    file_key: str,
    skill_dir: Path,
    manifest_path: Path,
) -> str:
    inline = raw.get(inline_key)
    file_ref = raw.get(file_key)
    if inline and file_ref:
        raise YamlSkillError(
            f"{manifest_path}: specify either {inline_key} or {file_key}, not both"
        )
    if isinstance(inline, str):
        return inline
    if isinstance(file_ref, str):
        path = skill_dir / file_ref
        if not path.exists():
            raise YamlSkillError(
                f"{manifest_path}: {file_key} points to missing file {file_ref!r}"
            )
        return path.read_text(encoding="utf-8")
    return ""


__all__ = [
    "YamlSkill",
    "YamlSkillError",
    "discover_yaml_skills",
    "load_yaml_skill",
]
