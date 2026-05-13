"""Interactive setup CLI — ``korpha setup ...``.

Drives off the ``provider_profile_registry`` and channel
``platform_registry`` so plugin-supplied entries appear in the
picker automatically alongside built-ins. The Mike-non-technical
rule says: every setup knob needs an interactive UI/CLI path. This
module is that path for the new plugin contracts shipped this
session.

Three subcommands:

  - ``korpha setup providers`` — pick a provider profile, walk
    its ``setup_fields`` (env_var, description, setup_url, secret),
    write to ``~/.korpha/providers.yaml``.
  - ``korpha setup channels`` — pick a channel adapter, surface
    its ``required_env``, write to ``~/.korpha/channels.yaml``.
  - ``korpha setup plugins`` — list / enable / disable plugins
    via the ``KORPHA_PLUGINS_ENABLED`` allow-list (rendered to
    the same yaml so users can rerun without env vars).

Why a config file rather than env vars: env vars die when the shell
exits. Mike installs once + expects it to work tomorrow. The config
file lives in ``~/.korpha/``, gets chmod 600, and the inference
loader reads it on every server start.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import typer
import yaml

from korpha.audit.model import InferenceTier
from korpha.channels.registry import (
    PlatformEntry,
    platform_registry,
)
from korpha.inference.provider_profile import (
    ProviderProfile,
    SetupField,
    provider_profile_registry,
)
from korpha.inference.registry import AuthType


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _config_dir() -> Path:
    """Where setup writes config. Override via ``KORPHA_DATA_DIR``
    so tests can use tmp_path."""
    base = os.getenv("KORPHA_DATA_DIR")
    return Path(base) if base else Path.home() / ".korpha"


def _write_yaml(path: Path, body: dict[str, Any]) -> None:
    """Atomic-ish YAML write + tight perms. The dir is shared by
    other config files; we don't lock it, just chmod the file."""
    import contextlib

    path.parent.mkdir(parents=True, exist_ok=True)
    yaml_text = yaml.safe_dump(body, sort_keys=False, default_flow_style=False)
    path.write_text(yaml_text, encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def _read_yaml(path: Path) -> dict[str, Any]:
    """Read a YAML config file, returning {} for missing or empty files.
    Raises with a clear message on malformed contents — Mike sees the
    error rather than silent zeroing."""
    if not path.exists():
        return {}
    body = yaml.safe_load(path.read_text(encoding="utf-8"))
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise typer.BadParameter(
            f"{path}: top level must be a YAML mapping, got "
            f"{type(body).__name__}"
        )
    return body


def _bold(text: str) -> str:
    return typer.style(text, bold=True)


def _dim(text: str) -> str:
    return typer.style(text, dim=True)


def _green(text: str) -> str:
    return typer.style(text, fg=typer.colors.GREEN)


def _yellow(text: str) -> str:
    return typer.style(text, fg=typer.colors.YELLOW)


def _red(text: str) -> str:
    return typer.style(text, fg=typer.colors.RED)


def _walk_setup_fields(
    fields: list[SetupField],
    *,
    title: str,
    setup_url: str | None = None,
    install_hint: str | None = None,
    existing: dict[str, str] | None = None,
) -> dict[str, str] | None:
    """Walk a list of SetupField, prompt for each, return the
    collected ``{env_var: value}`` dict.

    Returns ``None`` if Mike cancels at any prompt. Re-uses values
    from ``existing`` so re-running setup doesn't ask Mike to retype
    the keys he already entered.

    Secret fields hide the input + don't echo. Optional fields can
    be skipped with empty input (default empty string).
    """
    if title:
        typer.echo()
        typer.echo(_bold(title))
    if setup_url:
        typer.echo(_dim(f"  Get credentials at: {setup_url}"))
    if install_hint:
        typer.echo(_dim(f"  Note: {install_hint}"))
    typer.echo()

    if not fields:
        typer.echo(_dim("  (no credentials needed)"))
        return {}

    out: dict[str, str] = {}
    for f in fields:
        existing_value = (existing or {}).get(f.env_var, "")
        # Show a placeholder hint when there's already a value (don't
        # echo it back — it's a secret).
        existing_hint = (
            f"  [press Enter to keep existing value]"
            if existing_value else ""
        )
        prompt_label = f"  {f.env_var}"
        if f.optional:
            prompt_label += _dim(" (optional)")
        if f.description:
            typer.echo(f"  {f.env_var} — {f.description}")
            if f.setup_url and f.setup_url != setup_url:
                typer.echo(_dim(f"      → {f.setup_url}"))
        try:
            value = typer.prompt(
                prompt_label + existing_hint,
                hide_input=f.secret,
                default="" if not existing_value else "[keep]",
                show_default=False,
            )
        except typer.Abort:
            typer.echo()
            typer.echo(_dim("  cancelled."))
            return None

        if value == "[keep]" and existing_value:
            value = existing_value
        value = value.strip()
        if not value and not f.optional:
            typer.echo(_red(f"  {f.env_var} is required."))
            return None
        if value:
            out[f.env_var] = value
    return out


# ---------------------------------------------------------------------------
# `korpha setup providers`
# ---------------------------------------------------------------------------


_PROVIDERS_FILE_NAME = "providers.yaml"


def _providers_path() -> Path:
    return _config_dir() / _PROVIDERS_FILE_NAME


def _existing_provider_envs(profile_name: str) -> dict[str, str]:
    """Return env vars already saved for this provider, so re-running
    setup pre-fills them. ``providers.yaml`` is shared with the
    existing wizard; we pull from the same file."""
    body = _read_yaml(_providers_path())
    providers = body.get("providers") or []
    if not isinstance(providers, list):
        return {}
    for entry in providers:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("preset") or "") == profile_name:
            envs = entry.get("setup_envs") or {}
            return envs if isinstance(envs, dict) else {}
    return {}


def _save_provider_setup(
    profile: ProviderProfile, envs: dict[str, str],
) -> Path:
    """Append (or replace) the provider entry in providers.yaml.

    The schema mirrors the wizard's existing format — ``preset`` is
    the profile name, ``setup_envs`` carries the env-var → value
    mapping, ``tiers`` defaults to the profile's tier_capabilities.
    """
    body = _read_yaml(_providers_path())
    providers = body.get("providers")
    if providers is None:
        providers = []
        body["providers"] = providers
    if not isinstance(providers, list):
        raise typer.BadParameter(
            f"{_providers_path()}: 'providers' must be a list"
        )

    tiers: dict[str, str] = {}
    for tier, cap in profile.tier_capabilities.items():
        tiers[tier.value if isinstance(tier, InferenceTier) else str(tier)] = (
            cap.default_model
        )

    new_entry: dict[str, Any] = {
        "preset": profile.name,
        "label": f"{profile.name}-primary",
        "setup_envs": envs,
        "tiers": tiers,
        "concurrency_limit": 4,
    }

    # Replace existing entry with the same preset name; otherwise
    # append. This is what makes "re-run setup to update the key"
    # work without manual edits.
    for i, entry in enumerate(providers):
        if isinstance(entry, dict) and str(entry.get("preset") or "") == profile.name:
            providers[i] = new_entry
            break
    else:
        providers.append(new_entry)

    _write_yaml(_providers_path(), body)
    return _providers_path()


def list_providers() -> None:
    """``korpha setup providers`` (no args) — show the catalog
    with status (configured / needs setup / deps missing)."""
    profiles = sorted(
        provider_profile_registry.all_profiles(),
        key=lambda p: (p.source != "builtin", p.name),
    )
    if not profiles:
        typer.echo(_yellow("No provider profiles registered."))
        return

    body = _read_yaml(_providers_path())
    configured_names: set[str] = set()
    if isinstance(body.get("providers"), list):
        for entry in body["providers"]:
            if isinstance(entry, dict) and entry.get("preset"):
                configured_names.add(str(entry["preset"]))

    typer.echo()
    typer.echo(_bold("Inference providers"))
    typer.echo(_dim(
        "Run `korpha setup providers <name>` to configure one."
    ))
    typer.echo()
    for p in profiles:
        if p.name in configured_names:
            status = _green("✓ configured")
        elif not p.check_fn():
            status = _yellow("⚠ deps missing")
        else:
            status = _dim("· ready to configure")
        tag = _dim(f"({p.source})")
        typer.echo(f"  {p.emoji} {p.name:20} {p.label:42} {status} {tag}")
    typer.echo()


def setup_provider(name: str) -> None:
    """``korpha setup providers <name>`` — interactive credential
    walkthrough for one provider."""
    profile = provider_profile_registry.get(name)
    if profile is None:
        typer.echo(_red(f"No provider profile named {name!r}."))
        typer.echo(_dim("Run `korpha setup providers` to see the catalog."))
        raise typer.Exit(2)

    if not profile.check_fn():
        typer.echo(_yellow(
            f"Provider {profile.label!r} requires runtime deps that aren't "
            f"installed."
        ))
        if profile.install_hint:
            typer.echo(_dim(f"  Install: {profile.install_hint}"))
        typer.echo(_dim(
            "  Re-run setup once those are available."
        ))
        raise typer.Exit(2)

    if profile.auth_type == AuthType.SUBSCRIPTION_CLI:
        typer.echo()
        typer.echo(_bold(f"Set up {profile.label}"))
        typer.echo(_dim(f"  {profile.description}"))
        typer.echo()
        typer.echo(
            _dim(f"  Auth uses the CLI's own login flow — no API key.")
        )
        if profile.install_hint:
            typer.echo(_dim(f"  Install / login: {profile.install_hint}"))
        # Save a marker entry so `korpha setup providers` shows
        # ✓ configured and the runtime knows to use this provider.
        path = _save_provider_setup(profile, envs={})
        typer.echo()
        typer.echo(_green(f"  Saved to {path}"))
        return

    existing = _existing_provider_envs(profile.name)
    envs = _walk_setup_fields(
        profile.setup_fields,
        title=f"Set up {profile.label}",
        setup_url=profile.setup_url,
        install_hint=profile.install_hint or None,
        existing=existing,
    )
    if envs is None:
        raise typer.Exit(1)

    path = _save_provider_setup(profile, envs)
    typer.echo()
    typer.echo(_green(f"  Saved {profile.label} → {path}"))
    if profile.tier_capabilities:
        tiers = ", ".join(t.value for t in profile.tier_capabilities)
        typer.echo(_dim(f"  Serves tiers: {tiers}"))


# ---------------------------------------------------------------------------
# `korpha setup channels`
# ---------------------------------------------------------------------------


_CHANNELS_FILE_NAME = "channels.yaml"


def _channels_path() -> Path:
    return _config_dir() / _CHANNELS_FILE_NAME


def _existing_channel_envs(name: str) -> dict[str, str]:
    body = _read_yaml(_channels_path())
    channels = body.get("channels") or []
    if not isinstance(channels, list):
        return {}
    for entry in channels:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("name") or "") == name:
            envs = entry.get("setup_envs") or {}
            return envs if isinstance(envs, dict) else {}
    return {}


def _save_channel_setup(
    entry: PlatformEntry, envs: dict[str, str],
) -> Path:
    body = _read_yaml(_channels_path())
    channels = body.get("channels")
    if channels is None:
        channels = []
        body["channels"] = channels
    if not isinstance(channels, list):
        raise typer.BadParameter(
            f"{_channels_path()}: 'channels' must be a list"
        )

    new_entry: dict[str, Any] = {
        "name": entry.name,
        "label": entry.label,
        "setup_envs": envs,
        "source": entry.source,
        "plugin_name": entry.plugin_name,
    }

    for i, existing in enumerate(channels):
        if (
            isinstance(existing, dict)
            and str(existing.get("name") or "") == entry.name
        ):
            channels[i] = new_entry
            break
    else:
        channels.append(new_entry)

    _write_yaml(_channels_path(), body)
    return _channels_path()


def list_channels() -> None:
    entries = sorted(
        platform_registry.all_entries(),
        key=lambda e: (e.source != "builtin", e.name),
    )
    if not entries:
        typer.echo(_yellow("No channel adapters registered."))
        return

    body = _read_yaml(_channels_path())
    configured_names: set[str] = set()
    if isinstance(body.get("channels"), list):
        for c in body["channels"]:
            if isinstance(c, dict) and c.get("name"):
                configured_names.add(str(c["name"]))

    typer.echo()
    typer.echo(_bold("Channel adapters"))
    typer.echo(_dim(
        "Run `korpha setup channels <name>` to configure one."
    ))
    typer.echo()
    for e in entries:
        if e.name in configured_names:
            status = _green("✓ configured")
        elif not e.check_fn():
            status = _yellow("⚠ deps missing")
        else:
            status = _dim("· ready to configure")
        tag = _dim(f"({e.source})")
        typer.echo(f"  {e.emoji} {e.name:14} {e.label:30} {status} {tag}")
    typer.echo()


def setup_channel(name: str) -> None:
    entry = platform_registry.get(name)
    if entry is None:
        typer.echo(_red(f"No channel adapter named {name!r}."))
        typer.echo(_dim("Run `korpha setup channels` for the catalog."))
        raise typer.Exit(2)

    if not entry.check_fn():
        typer.echo(_yellow(
            f"Channel {entry.label!r} requires runtime deps that aren't "
            f"installed."
        ))
        if entry.install_hint:
            typer.echo(_dim(f"  Install: {entry.install_hint}"))
        raise typer.Exit(2)

    # PlatformEntry stores required_env as a flat list of names; promote
    # to SetupField shape on the fly so the same walker works.
    fields = [
        SetupField(
            env_var=name,
            description=f"Required for {entry.label}",
            secret=True,
        )
        for name in entry.required_env
    ]
    existing = _existing_channel_envs(entry.name)
    envs = _walk_setup_fields(
        fields,
        title=f"Set up {entry.label} channel",
        install_hint=entry.install_hint or None,
        existing=existing,
    )
    if envs is None:
        raise typer.Exit(1)

    path = _save_channel_setup(entry, envs)
    typer.echo()
    typer.echo(_green(f"  Saved {entry.label} → {path}"))
    if entry.platform_hint:
        typer.echo(_dim(f"  Platform hint: {entry.platform_hint}"))


# ---------------------------------------------------------------------------
# `korpha setup plugins`
# ---------------------------------------------------------------------------


_PLUGINS_FILE_NAME = "plugins.yaml"


def _plugins_path() -> Path:
    return _config_dir() / _PLUGINS_FILE_NAME


def list_plugins_status() -> None:
    """Show enabled allow-list + disabled deny-list. Source can be
    either env var (read by the loader at runtime) or the config
    file (written by setup)."""
    from korpha.plugins.loader import (
        disabled_set_from_env,
        discover_all_plugins,
        enabled_set_from_env,
    )

    body = _read_yaml(_plugins_path())
    file_enabled = set(body.get("enabled") or [])
    file_disabled = set(body.get("disabled") or [])

    env_enabled = enabled_set_from_env()
    env_disabled = disabled_set_from_env()

    typer.echo()
    typer.echo(_bold("Plugin status"))
    typer.echo()

    if env_enabled or env_disabled:
        typer.echo(_dim("  Env-var policy (overrides config file):"))
        if env_enabled:
            typer.echo(_dim(
                f"    KORPHA_PLUGINS_ENABLED={','.join(sorted(env_enabled))}"
            ))
        if env_disabled:
            typer.echo(_dim(
                f"    KORPHA_PLUGINS_DISABLED={','.join(sorted(env_disabled))}"
            ))
        typer.echo()

    typer.echo(_dim(f"  Config file: {_plugins_path()}"))
    typer.echo(
        f"    enabled:  {sorted(file_enabled) or _dim('(none)')}"
    )
    typer.echo(
        f"    disabled: {sorted(file_disabled) or _dim('(none)')}"
    )
    typer.echo()

    discovered = discover_all_plugins(include_entry_points=True)
    if not discovered:
        typer.echo(_dim(
            "  No plugins discovered. Drop a plugin under "
            "~/.korpha/plugins/ or pip-install one declaring the "
            "`korpha.plugins` entry-point group."
        ))
        return

    typer.echo(_bold("  Discovered plugins:"))
    for m in discovered:
        if m.name in file_enabled or "*" in file_enabled:
            status = _green("✓ enabled")
        elif m.name in file_disabled:
            status = _red("✗ disabled")
        else:
            status = _dim("· not enabled")
        typer.echo(
            f"    {m.name:30} v{m.version:8} {status}  {m.description[:50]}"
        )
    typer.echo()


def enable_plugin(name: str) -> None:
    body = _read_yaml(_plugins_path())
    enabled = list(body.get("enabled") or [])
    if name not in enabled:
        enabled.append(name)
    body["enabled"] = enabled
    # If it was disabled, drop it from the deny-list — enable wins.
    disabled = [d for d in (body.get("disabled") or []) if d != name]
    if disabled or "disabled" in body:
        body["disabled"] = disabled
    _write_yaml(_plugins_path(), body)
    typer.echo(_green(f"  Enabled plugin {name!r}. Restart the agent to load it."))


def disable_plugin(name: str) -> None:
    body = _read_yaml(_plugins_path())
    disabled = list(body.get("disabled") or [])
    if name not in disabled:
        disabled.append(name)
    body["disabled"] = disabled
    enabled = [e for e in (body.get("enabled") or []) if e != name]
    if enabled or "enabled" in body:
        body["enabled"] = enabled
    _write_yaml(_plugins_path(), body)
    typer.echo(_yellow(f"  Disabled plugin {name!r}. Restart the agent to drop it."))


__all__ = [
    "_existing_channel_envs",
    "_existing_provider_envs",
    "_save_channel_setup",
    "_save_provider_setup",
    "disable_plugin",
    "enable_plugin",
    "list_channels",
    "list_plugins_status",
    "list_providers",
    "setup_channel",
    "setup_provider",
]
