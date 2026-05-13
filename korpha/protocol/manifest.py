"""Cofounder Protocol manifest — schema, parser, validator.

The manifest is YAML. Required fields are:

  - ``spec_version`` (int) — currently ``1``
  - ``name`` (snake_case identifier — used as namespace prefix)
  - ``display_name`` (human-readable)
  - ``description``
  - ``homepage``
  - ``provides`` — what the partner brings to the cofounder loop

Everything else (auth, branding, requires, docs_url, signup_url) is
optional. The validator is strict on required fields so partners get
clear errors at install time, not later.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CURRENT_SPEC_VERSION = 1


class ManifestError(ValueError):
    """Manifest is malformed or references unknown features."""


@dataclass(frozen=True)
class ProvidesSpec:
    """What this cofounder partner contributes.

    v1 supports references to built-in skills already shipped in
    Korpha's ``default_registry`` (i.e. partners coordinate with us
    to land their skills in core, then publish a manifest to wire the
    auth/branding/setup-flow). v2 will add ``yaml_skill_dirs`` to ship
    skills directly from the manifest using the agentskills.io
    directory format. Future kinds (channels, providers, tools) extend
    this without breaking spec_version=1 by adding fields with safe
    defaults.
    """

    skills: tuple[str, ...] = ()
    """Skill names the partner declares. Each must resolve to a
    registered built-in skill at install time — otherwise the manifest
    is rejected so partners get a clean error instead of a silent
    stub."""


@dataclass(frozen=True)
class AuthSpec:
    """How the Founder links their account with this partner."""

    kind: str
    """``api_key`` | ``oauth`` | ``none``. ``none`` means no Founder-
    owned credentials needed (rare — only for fully-public APIs)."""

    api_key_env: str | None = None
    """Env var name Korpha reads to find the key. Used when
    ``kind=api_key``."""

    setup_command: str | None = None
    """The exact CLI command the user should run, e.g.
    ``korpha config-rankmyanswer-add``. Surfaced in ``doctor``."""

    signup_url: str | None = None
    """Where to get an account if the user doesn't have one yet."""

    oauth_authorize_url: str | None = None
    """For ``kind=oauth``: the partner's authorization endpoint."""


@dataclass(frozen=True)
class BrandingSpec:
    """Optional branding metadata. Used by the dashboard to render a
    consistent partner card."""

    primary_color: str | None = None
    """Hex (``#1f7a4d``). Validated as 7-char ``#RRGGBB`` only."""

    logo_url: str | None = None
    """Public HTTPS URL to an SVG / PNG logo. Dashboard caches it."""


@dataclass(frozen=True)
class RequiresSpec:
    """Constraints for installation — surfaced as a precheck."""

    korpha_version: str | None = None
    """semver range (``>=0.1.0``). Currently parsed but advisory only."""

    network_egress: tuple[str, ...] = ()
    """Hostnames the partner's skills will hit. For users running in
    locked-down environments (or just the curious)."""


@dataclass(frozen=True)
class CofounderManifest:
    """One partner manifest — what gets shipped and what the Founder
    needs to do to wire it up."""

    spec_version: int
    name: str
    display_name: str
    description: str
    homepage: str
    provides: ProvidesSpec

    # Optional
    docs_url: str | None = None
    auth: AuthSpec | None = None
    branding: BrandingSpec | None = None
    requires: RequiresSpec | None = None
    source_path: Path | None = field(default=None, compare=False)
    """Where the manifest was loaded from (for relative skill-file
    resolution + uninstall reference). Not part of the spec."""


_VALID_AUTH_KINDS = ("api_key", "oauth", "none")


def parse_manifest(raw: Any, *, source: Path | None = None) -> CofounderManifest:
    """Validate a parsed YAML mapping. Raises ManifestError on the
    first malformed field, with a path-like message ('provides.skills')
    so partners can fix issues without diffing examples."""
    if not isinstance(raw, dict):
        raise ManifestError(
            f"manifest must be a mapping, got {type(raw).__name__}"
        )

    spec_version = raw.get("spec_version")
    if not isinstance(spec_version, int):
        raise ManifestError(
            "missing or non-integer 'spec_version' (use 1)"
        )
    if spec_version != CURRENT_SPEC_VERSION:
        raise ManifestError(
            f"unsupported spec_version {spec_version}; "
            f"this Korpha supports {CURRENT_SPEC_VERSION}"
        )

    name = _required_str(raw, "name")
    if not name.replace("_", "").isalnum() or name != name.lower():
        raise ManifestError(
            f"'name' must be snake_case (a-z, 0-9, _ only); got {name!r}"
        )

    display_name = _required_str(raw, "display_name")
    description = _required_str(raw, "description")
    homepage = _required_str(raw, "homepage")
    if not (homepage.startswith("http://") or homepage.startswith("https://")):
        raise ManifestError("'homepage' must be an http(s) URL")

    provides = _parse_provides(raw.get("provides"))

    auth = _parse_auth(raw.get("auth")) if raw.get("auth") is not None else None
    branding = (
        _parse_branding(raw.get("branding"))
        if raw.get("branding") is not None
        else None
    )
    requires = (
        _parse_requires(raw.get("requires"))
        if raw.get("requires") is not None
        else None
    )

    docs_url = raw.get("docs_url")
    if docs_url is not None and not isinstance(docs_url, str):
        raise ManifestError("'docs_url' must be a string if present")

    return CofounderManifest(
        spec_version=spec_version,
        name=name,
        display_name=display_name,
        description=description,
        homepage=homepage,
        provides=provides,
        docs_url=docs_url,
        auth=auth,
        branding=branding,
        requires=requires,
        source_path=source,
    )


def load_manifest(path: Path | str) -> CofounderManifest:
    """Load + validate from disk. Path is the ``cofounder.yaml`` file."""
    import yaml

    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise ManifestError(f"manifest not found: {p}")
    try:
        body = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ManifestError(f"YAML parse error in {p}: {exc}") from exc
    return parse_manifest(body, source=p)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _required_str(raw: dict[str, Any], key: str) -> str:
    val = raw.get(key)
    if not isinstance(val, str) or not val.strip():
        raise ManifestError(f"missing or empty required string field {key!r}")
    return val.strip()


def _parse_provides(raw: Any) -> ProvidesSpec:
    if raw is None:
        raise ManifestError(
            "'provides' is required — declare what skills/tools the "
            "partner contributes"
        )
    if not isinstance(raw, dict):
        raise ManifestError(
            f"'provides' must be a mapping, got {type(raw).__name__}"
        )
    skills_raw = raw.get("skills") or []
    if not isinstance(skills_raw, list) or not all(
        isinstance(s, str) for s in skills_raw
    ):
        raise ManifestError("'provides.skills' must be a list of strings")
    if not skills_raw:
        raise ManifestError(
            "'provides.skills' must list at least one skill — a "
            "manifest with nothing to register is rejected so partners "
            "don't ship empty noise"
        )
    return ProvidesSpec(skills=tuple(skills_raw))


def _parse_auth(raw: Any) -> AuthSpec:
    if not isinstance(raw, dict):
        raise ManifestError(
            f"'auth' must be a mapping, got {type(raw).__name__}"
        )
    kind = raw.get("kind")
    if kind not in _VALID_AUTH_KINDS:
        raise ManifestError(
            f"'auth.kind' must be one of {_VALID_AUTH_KINDS}; got {kind!r}"
        )
    return AuthSpec(
        kind=kind,
        api_key_env=raw.get("api_key_env"),
        setup_command=raw.get("setup_command"),
        signup_url=raw.get("signup_url"),
        oauth_authorize_url=raw.get("oauth_authorize_url"),
    )


def _parse_branding(raw: Any) -> BrandingSpec:
    if not isinstance(raw, dict):
        raise ManifestError(
            f"'branding' must be a mapping, got {type(raw).__name__}"
        )
    color = raw.get("primary_color")
    if color is not None and not _is_valid_hex_color(color):
        raise ManifestError(
            f"'branding.primary_color' must be #RRGGBB hex; got {color!r}"
        )
    logo = raw.get("logo_url")
    if logo is not None and not (
        isinstance(logo, str)
        and (logo.startswith("https://") or logo.startswith("http://"))
    ):
        raise ManifestError(
            "'branding.logo_url' must be an http(s) URL"
        )
    return BrandingSpec(primary_color=color, logo_url=logo)


def _parse_requires(raw: Any) -> RequiresSpec:
    if not isinstance(raw, dict):
        raise ManifestError(
            f"'requires' must be a mapping, got {type(raw).__name__}"
        )
    egress_raw = raw.get("network_egress") or []
    if not isinstance(egress_raw, list) or not all(
        isinstance(s, str) for s in egress_raw
    ):
        raise ManifestError(
            "'requires.network_egress' must be a list of hostnames"
        )
    return RequiresSpec(
        korpha_version=raw.get("korpha_version"),
        network_egress=tuple(egress_raw),
    )


def _is_valid_hex_color(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    if len(value) != 7 or not value.startswith("#"):
        return False
    try:
        int(value[1:], 16)
        return True
    except ValueError:
        return False
