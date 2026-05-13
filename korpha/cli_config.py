"""Interactive provider-setup wizard for `korpha config`.

Designed for non-technical Founders. Walks through:

    1. Pick a provider (numbered list of presets + "custom" option)
    2. (custom only) Enter base_url + name
    3. Enter API key
    4. Enter model names per tier
    5. Optional: concurrency limit, spend cap

Writes the resulting entry to ``~/.korpha/providers.yaml``. The same
function is used by ``korpha init`` when no provider is configured
yet, so first-run + later "add another" share the same UX.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import typer

from korpha.inference.config_writer import append_provider_entry
from korpha.inference.providers.openai_compat import (
    PROVIDER_PRESETS,
    SUBSCRIPTION_PRESETS,
)

# What to suggest as workhorse / pro models per known preset. Used as
# placeholder/default in the prompts so Mike doesn't have to know what
# model names look like for, say, OpenRouter.
_TIER_SUGGESTIONS: dict[str, dict[str, str]] = {
    # Subscription providers: ONLY set the pro tier. Workhorse stays
    # unset — quotas (Plus $20, Pro $20, Max $100+) blow through fast
    # if you route every routine call through them. Wizard follows up
    # with a "want to add a cheap workhorse provider?" prompt.
    "codex-cli": {"pro": "codex-default"},
    "claude-code-cli": {"pro": "sonnet"},
    "openai": {"workhorse": "gpt-4o-mini", "pro": "gpt-4o"},
    "anthropic": {"workhorse": "claude-haiku-4-5-20251001", "pro": "claude-sonnet-4-6"},
    "deepseek": {"workhorse": "deepseek-chat", "pro": "deepseek-reasoner"},
    "openrouter": {"workhorse": "openai/gpt-4o-mini", "pro": "anthropic/claude-sonnet-4-6"},
    "ollama-cloud": {"workhorse": "deepseek-v4-flash:cloud", "pro": "deepseek-v4-pro:cloud"},
    "opencode-go": {"workhorse": "deepseek-v4-flash", "pro": "deepseek-v4-pro"},
    "opencode-zen": {"workhorse": "claude-haiku-4-5", "pro": "claude-sonnet-4-6"},
    "groq": {"workhorse": "llama-3.1-8b-instant", "pro": "llama-3.3-70b-versatile"},
    "together": {"workhorse": "meta-llama/Llama-3-8B-Instruct-Turbo", "pro": "meta-llama/Llama-3-70B-Instruct-Turbo"},
    "cerebras": {"workhorse": "llama3.1-8b", "pro": "llama-3.3-70b"},
    "local-ollama": {"workhorse": "llama3.1:8b", "pro": "llama3.1:70b"},
}

# Provider-specific human hints for where to get an API key.
_KEY_HINT: dict[str, str] = {
    "openai": "https://platform.openai.com/api-keys",
    "anthropic": "https://console.anthropic.com/settings/keys",
    "deepseek": "https://platform.deepseek.com/api_keys",
    "openrouter": "https://openrouter.ai/keys",
    "ollama-cloud": "https://ollama.com/settings/keys",
    "groq": "https://console.groq.com/keys",
    "together": "https://api.together.ai/settings/api-keys",
    "cerebras": "https://cloud.cerebras.ai/?tab=apiKeys",
}


def run_provider_wizard(
    *,
    on_done_message: str = (
        "Provider added. You can run `korpha providers` any time to "
        "see what's configured."
    ),
) -> Path | None:
    """Walk through the wizard, write the entry, return the resolved
    config path. Returns None if the user aborts at any prompt."""
    typer.echo()
    typer.echo(typer.style("Add an LLM provider", bold=True))
    typer.echo(
        typer.style(
            "Korpha uses two tiers — Pro (brain work: plan, score, "
            "decide) and Workhorse (bulk drip: dispatch, format, draft). "
            "You pick a provider for each. Pick whatever you already "
            "have access to.",
            dim=True,
        )
    )
    typer.echo()
    typer.echo(
        typer.style(
            "Pick a provider from the menu below for each tier. The "
            "wizard runs once per provider — typically: a strong one "
            "for Pro, then a cheaper one for Workhorse.",
            dim=True,
        )
    )
    typer.echo(
        typer.style(
            "If you already have a ChatGPT Plus or Claude Pro/Max "
            "subscription, you can route Pro through it (codex-cli / "
            "claude-code-cli) — quotas fill fast, still pair with an "
            "API-key workhorse.",
            dim=True,
        )
    )
    typer.echo()

    all_options = [*list(PROVIDER_PRESETS), *SUBSCRIPTION_PRESETS, "custom"]
    presets_ordered = _suggest_order(all_options)
    _menu_hints = {
        "codex-cli": "(use your ChatGPT subscription, no API key)",
        "claude-code-cli": "(use your Claude Pro/Max subscription, no API key)",
        "custom": "(your own OpenAI-compat endpoint)",
    }
    for idx, name in enumerate(presets_ordered, 1):
        hint = _menu_hints.get(name, "")
        typer.echo(f"  {idx:>2}. {name}  {typer.style(hint, dim=True)}")
    typer.echo()
    pick = typer.prompt(
        "Pick a number (or type 'q' to cancel)", default="1", show_default=False
    ).strip().lower()
    if pick in ("q", "quit", "cancel", ""):
        typer.echo(typer.style("  cancelled.", dim=True))
        return None
    try:
        choice_idx = int(pick) - 1
        if choice_idx < 0 or choice_idx >= len(presets_ordered):
            raise ValueError
    except ValueError:
        typer.echo(typer.style(f"  '{pick}' isn't a number from the list.", fg="red"))
        return None
    preset = presets_ordered[choice_idx]

    entry: dict[str, Any] = {"preset": preset}

    # Custom endpoint: collect base_url + name
    if preset == "custom":
        typer.echo()
        typer.echo(
            typer.style(
                "Custom endpoint — anything that speaks the OpenAI Chat "
                "Completions protocol.",
                dim=True,
            )
        )
        base_url = typer.prompt("  Base URL (e.g. https://api.example.com/v1)").strip()
        if not (base_url.startswith("http://") or base_url.startswith("https://")):
            typer.echo(typer.style("  base_url must start with http:// or https://", fg="red"))
            return None
        entry["base_url"] = base_url.rstrip("/")
        name = typer.prompt(
            "  Short name for this endpoint (e.g. home-vllm)"
        ).strip()
        if not name:
            typer.echo(typer.style("  name is required.", fg="red"))
            return None
        entry["name"] = name

    # Label — defaults to the preset name; user can override
    typer.echo()
    default_label = str(entry.get("name") if preset == "custom" else preset)
    label_input = typer.prompt(
        "Label for this account (used in logs)", default=default_label
    ).strip()
    entry["label"] = label_input or default_label

    # API key — write inline. We chmod 600 the file. We could prefer
    # api_key_env but that requires Mike to also know how to set env
    # vars, which defeats the whole point.
    # SUBSCRIPTION_PRESETS skip this entirely; auth is via the CLI's
    # own OAuth login (e.g. `codex login`).
    if preset in SUBSCRIPTION_PRESETS:
        typer.echo()
        import shutil as _shutil

        sub_info = {
            "codex-cli": (
                "codex",
                "npm install -g @openai/codex",
                "Then run `codex login` (opens a browser for ChatGPT OAuth).",
                "ChatGPT",
            ),
            "claude-code-cli": (
                "claude",
                "curl -fsSL https://claude.ai/install.sh | bash",
                "Then run `claude` once — first launch handles the login flow.",
                "Claude Pro / Max",
            ),
        }
        binary, install_cmd, login_hint, sub_label = sub_info[preset]
        if _shutil.which(binary) is None:
            typer.echo(typer.style(
                f"  {binary} isn't on PATH yet. Install with:", fg="yellow"
            ))
            typer.echo(typer.style(f"    {install_cmd}", dim=True))
            typer.echo(typer.style(f"  {login_hint}", dim=True))
            typer.echo(typer.style(
                f"  Continuing setup — you can install {binary} any time "
                "before first use.",
                dim=True,
            ))
        else:
            typer.echo(typer.style(
                f"  {binary} is on PATH. Make sure your {sub_label} "
                "subscription is logged in.",
                dim=True,
            ))
        typer.echo()
        # Honest framing — quotas WILL fill if you route everything here.
        typer.echo(typer.style(
            "  We'll wire this for the Pro tier only.",
            dim=True,
        ))
        typer.echo(typer.style(
            "  Heads up: subscription quotas (Plus/Pro $20, Max $100+) "
            "fill fast if you also send routine tasks here. After this "
            "we'll offer to pair with a cheap API for the workhorse "
            "tier — most users want that. You can also skip the "
            "subscription path entirely and use API keys for both tiers.",
            dim=True,
        ))
    else:
        typer.echo()
        if preset in _KEY_HINT:
            typer.echo(typer.style(f"  Get a key here: {_KEY_HINT[preset]}", dim=True))
        api_key = typer.prompt("API key", hide_input=True).strip()
        if not api_key:
            typer.echo(typer.style("  Empty API key — aborting.", fg="red"))
            return None
        entry["api_key"] = api_key

    # Model names per tier
    typer.echo()
    typer.echo(typer.style("Model names", bold=True))
    suggestions = _TIER_SUGGESTIONS.get(preset, {})
    if preset in SUBSCRIPTION_PRESETS:
        # Pro only — see "subscription quotas fill fast" note above.
        typer.echo(
            typer.style(
                "Setting Pro tier only. Workhorse stays unset — pair with "
                "a cheap API after this.",
                dim=True,
            )
        )
        workhorse = ""
    else:
        typer.echo(
            typer.style(
                "Korpha uses two tiers: 'workhorse' for cheap routine "
                "work, 'pro' for harder thinking.",
                dim=True,
            )
        )
        workhorse = typer.prompt(
            "  Workhorse model",
            default=suggestions.get("workhorse", ""),
            show_default=bool(suggestions.get("workhorse")),
        ).strip()
    pro = typer.prompt(
        "  Pro model",
        default=suggestions.get("pro", ""),
        show_default=bool(suggestions.get("pro")),
    ).strip()
    tiers: dict[str, str] = {}
    if workhorse:
        tiers["workhorse"] = workhorse
    if pro:
        tiers["pro"] = pro
    if not tiers:
        typer.echo(typer.style("  At least one tier must have a model.", fg="red"))
        return None
    entry["tiers"] = tiers

    # Optional spend cap
    typer.echo()
    cap = typer.prompt(
        "Daily spend cap in USD (Enter to skip)", default="", show_default=False
    ).strip()
    if cap:
        try:
            cap_val = float(cap)
            if cap_val > 0:
                entry["spend_cap_usd"] = cap_val
        except ValueError:
            typer.echo(
                typer.style(
                    f"  '{cap}' isn't a number — skipping cap.", fg="yellow"
                )
            )

    path = append_provider_entry(entry)
    typer.echo()
    typer.echo(typer.style(f"✓ Wrote to {path}", fg="green", bold=True))

    # Vision tier: if the Pro model we just configured already supports
    # vision, auto-add it to this entry's tiers. Otherwise tell the
    # Founder + suggest the open-weights default. This keeps screenshot
    # review / browser-loop visual checks working without an extra
    # wizard pass when the user's Pro provider already handles vision.
    _maybe_attach_vision_tier(entry, path, pro_model=pro)

    # If we just wrote a subscription preset, the workhorse tier is
    # unset on this provider — offer to chain a cheap API for it now,
    # while Mike's still in the setup mindset.
    if preset in SUBSCRIPTION_PRESETS:
        typer.echo()
        typer.echo(typer.style(
            "Pro tier set ✓. Now add a cheap API for the workhorse tier "
            "(routine dispatch, formatting, drafts).",
            dim=True,
        ))
        if typer.confirm(
            "Add a workhorse provider now? (recommended)", default=True
        ):
            run_provider_wizard(
                on_done_message=(
                    "Both tiers configured. Run `korpha server` to launch."
                ),
            )
            return path
    typer.echo(typer.style(f"  {on_done_message}", dim=True))
    return path


def _maybe_attach_vision_tier(
    entry: dict[str, Any],
    path: Path,
    *,
    pro_model: str,
) -> None:
    """If the just-written entry's Pro model supports vision, attach
    vision: <pro_model> to its tiers. Otherwise print the suggestion
    so Mike knows to add a vision provider via a follow-up wizard pass.
    """
    from korpha.inference.vision import (
        DEFAULT_VISION_MODEL,
        DEFAULT_VISION_PROVIDER_HINT,
        model_supports_vision,
    )

    typer.echo()
    if pro_model and model_supports_vision(pro_model):
        # Re-write the entry to include vision tier — same model.
        try:
            import yaml as _yaml

            existing = _yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            providers = existing.get("providers") or []
            if providers:
                last = providers[-1]
                tiers = last.setdefault("tiers", {})
                if "vision" not in tiers:
                    tiers["vision"] = pro_model
                    path.write_text(
                        _yaml.safe_dump(
                            existing, sort_keys=False, default_flow_style=False
                        ),
                        encoding="utf-8",
                    )
            typer.echo(typer.style(
                f"  ✓ Vision tier auto-set to {pro_model!r} — your Pro "
                "model already supports image input.",
                fg="green",
            ))
        except Exception:
            # Best-effort; the user can fix by hand if it fails.
            pass
        return

    # Pro model doesn't support vision (or no Pro model set, e.g. for a
    # workhorse-only provider). Suggest the default.
    typer.echo(typer.style(
        "  · Vision tier not set. Korpha uses Vision for browser "
        "screenshots, design review, and landing-page QA.",
        dim=True,
    ))
    typer.echo(typer.style(f"  {DEFAULT_VISION_PROVIDER_HINT}", dim=True))
    typer.echo(typer.style(
        f"  Add it via another `korpha config` pass — pick "
        f"openrouter and set `tiers.vision: {DEFAULT_VISION_MODEL}`.",
        dim=True,
    ))


def _suggest_order(names: list[str]) -> list[str]:
    """Put the most likely picks first — Mike scans top-to-bottom."""
    priority = [
        "openai",
        "anthropic",
        "openrouter",
        "deepseek",
        "groq",
        "ollama-cloud",
        "local-ollama",
        "opencode-go",
        "opencode-zen",
        "together",
        "cerebras",
    ]
    seen = set()
    ordered: list[str] = []
    for n in priority:
        if n in names and n not in seen:
            ordered.append(n)
            seen.add(n)
    for n in names:
        if n not in seen:
            ordered.append(n)
            seen.add(n)
    return ordered


# ---------------------------------------------------------------------------
# Image-provider wizard (separate from inference; lives under
# `image_providers:` in providers.yaml).
# ---------------------------------------------------------------------------

_IMAGE_PRESET_HINTS: dict[str, str] = {
    "replicate": "(pay-per-image, FLUX/SDXL/Recraft/Stable Diffusion 3.5)",
    "fal": "(pay-per-image, fast FLUX dev/schnell, Recraft, Ideogram)",
    "local-sd": "(your own GPU via A1111/ComfyUI/Forge — $0 marginal)",
    "codex-cli": "(uses your ChatGPT subscription, gpt-image-2 — best model today)",
}

_IMAGE_DEFAULT_MODEL: dict[str, str] = {
    "replicate": "black-forest-labs/flux-1.1-pro",
    "fal": "fal-ai/flux/dev",
    "local-sd": "",  # user picks via WebUI itself unless they want to pin
    "codex-cli": "",  # codex internal
}

_IMAGE_KEY_HINT: dict[str, tuple[str, str]] = {
    # (where to get the key, env var name we'll write into the YAML)
    "replicate": ("https://replicate.com/account/api-tokens", "REPLICATE_API_TOKEN"),
    "fal": ("https://fal.ai/dashboard/keys", "FAL_KEY"),
}


def run_image_provider_wizard() -> Path | None:
    """Walk through adding one image-gen provider entry to
    ``image_providers:`` in providers.yaml. Returns the resolved path
    or None if the user aborts."""
    from korpha.imagery.service import IMAGE_PRESETS

    typer.echo()
    typer.echo(typer.style("Add an image-gen provider", bold=True))
    typer.echo(
        typer.style(
            "Image generation is separate from your text/inference "
            "provider. Pick one below — you can run as many as you want "
            "(first one wins, others fall back).",
            dim=True,
        )
    )
    typer.echo()

    presets = list(IMAGE_PRESETS)
    for idx, name in enumerate(presets, 1):
        hint = _IMAGE_PRESET_HINTS.get(name, "")
        typer.echo(f"  {idx:>2}. {name}  {typer.style(hint, dim=True)}")
    typer.echo()
    pick = typer.prompt(
        "Pick a number (or 'q' to cancel)", default="1", show_default=False
    ).strip().lower()
    if pick in ("q", "quit", "cancel", ""):
        typer.echo(typer.style("  cancelled.", dim=True))
        return None
    try:
        choice_idx = int(pick) - 1
        if choice_idx < 0 or choice_idx >= len(presets):
            raise ValueError
    except ValueError:
        typer.echo(typer.style(f"  '{pick}' isn't a number from the list.", fg="red"))
        return None
    preset = presets[choice_idx]

    entry: dict[str, Any] = {"preset": preset}

    if preset == "local-sd":
        typer.echo()
        typer.echo(typer.style(
            "Local SD WebUI (A1111-compatible). Make sure it's running with "
            "--api flag.",
            dim=True,
        ))
        base_url = typer.prompt(
            "  Base URL", default="http://localhost:7860"
        ).strip().rstrip("/")
        entry["base_url"] = base_url
        model = typer.prompt(
            "  Default checkpoint name (Enter to use whatever WebUI has loaded)",
            default="",
            show_default=False,
        ).strip()
        if model:
            entry["default_model"] = model
    elif preset in _IMAGE_KEY_HINT:
        url, _env_name = _IMAGE_KEY_HINT[preset]
        typer.echo()
        typer.echo(typer.style(f"  Get a key here: {url}", dim=True))
        api_key = typer.prompt("  API key", hide_input=True).strip()
        if not api_key:
            typer.echo(typer.style("  Empty API key — aborting.", fg="red"))
            return None
        entry["api_key"] = api_key
        default_model = _IMAGE_DEFAULT_MODEL.get(preset, "")
        model = typer.prompt(
            "  Default model",
            default=default_model,
            show_default=bool(default_model),
        ).strip()
        if model:
            entry["default_model"] = model
    elif preset == "codex-cli":
        typer.echo()
        import shutil as _shutil

        if _shutil.which("codex") is None:
            typer.echo(typer.style(
                "  codex isn't on PATH yet. Install with:",
                fg="yellow",
            ))
            typer.echo(typer.style(
                "    npm install -g @openai/codex", dim=True
            ))
            typer.echo(typer.style(
                "  Then run `codex login` to attach your ChatGPT subscription.",
                dim=True,
            ))
        else:
            typer.echo(typer.style(
                "  codex is on PATH. Make sure it's logged in.",
                dim=True,
            ))

    path = _append_image_provider_entry(entry)
    typer.echo()
    typer.echo(typer.style(f"✓ Wrote to {path}", fg="green", bold=True))
    typer.echo(typer.style(
        "  Run `korpha skill run imagery.generate_image "
        "--arg prompt='a red circle on white'` to test it.",
        dim=True,
    ))
    return path


def _append_image_provider_entry(entry: dict[str, Any]) -> Path:
    """Append to the ``image_providers:`` list (separate from
    ``providers:``) in providers.yaml. Creates the file + section if
    missing."""
    import os as _os

    import yaml as _yaml

    from korpha.inference.config import config_path

    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        existing = _yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if not isinstance(existing, dict):
            raise ValueError(f"{p}: top level must be a mapping")
        image_providers = existing.get("image_providers")
        if image_providers is None:
            existing["image_providers"] = [entry]
        elif isinstance(image_providers, list):
            image_providers.append(entry)
        else:
            raise ValueError(f"{p}: image_providers must be a list")
        body = existing
    else:
        body = {"image_providers": [entry]}

    yaml_text = _yaml.safe_dump(body, sort_keys=False, default_flow_style=False)
    p.write_text(yaml_text, encoding="utf-8")
    import contextlib

    with contextlib.suppress(OSError):
        _os.chmod(p, 0o600)
    return p


# ---------------------------------------------------------------------------
# RankMyAnswer wizard — third optional integration alongside inference
# providers and image providers. Lives under `integrations:` in
# providers.yaml.
# ---------------------------------------------------------------------------


def run_rankmyanswer_wizard() -> Path | None:
    """Walk the Founder through adding a RankMyAnswer.com API key.
    Optional integration — the cofounder uses it for GEO + SEO audits
    and JSON-LD schema generation. Skip if not interested."""
    typer.echo()
    typer.echo(typer.style("Add RankMyAnswer.com (GEO + SEO)", bold=True))
    typer.echo(
        typer.style(
            "Add your RankMyAnswer.com API key so Korpha can work on "
            "getting eyeballs to your product or service (GEO + SEO).",
            dim=True,
        )
    )
    typer.echo(
        typer.style(
            "GEO = ChatGPT / Perplexity / Claude / Gemini citations. "
            "SEO = Google. Both matter — RankMyAnswer scores both surfaces "
            "on every audit.",
            dim=True,
        )
    )
    typer.echo()
    typer.echo(typer.style(
        "  Get a key here: https://rankmyanswer.com",
        dim=True,
    ))
    api_key = typer.prompt("API key (or 'q' to skip)", hide_input=True).strip()
    if not api_key or api_key.lower() == "q":
        typer.echo(typer.style(
            "  Skipped. Run `korpha config-rankmyanswer-add` later.",
            dim=True,
        ))
        return None

    base_url = typer.prompt(
        "Base URL",
        default="https://api.rankmyanswer.com/v1",
    ).strip().rstrip("/")

    entry: dict[str, Any] = {
        "kind": "rank_my_answer",
        "api_key": api_key,
        "base_url": base_url,
    }
    path = _append_integration_entry(entry)
    typer.echo()
    typer.echo(typer.style(f"✓ Wrote to {path}", fg="green", bold=True))
    typer.echo(typer.style(
        "  Test it: `korpha skill run geo_seo.balance` to confirm "
        "the key works.",
        dim=True,
    ))
    return path


def _append_integration_entry(entry: dict[str, Any]) -> Path:
    """Append to ``integrations:`` (top-level list in providers.yaml).
    Same shape as image_providers — a flat list keyed by ``kind:``."""
    import os as _os

    import yaml as _yaml

    from korpha.inference.config import config_path

    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        existing = _yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if not isinstance(existing, dict):
            raise ValueError(f"{p}: top level must be a mapping")
        integrations = existing.get("integrations")
        if integrations is None:
            existing["integrations"] = [entry]
        elif isinstance(integrations, list):
            integrations.append(entry)
        else:
            raise ValueError(f"{p}: integrations must be a list")
        body = existing
    else:
        body = {"integrations": [entry]}

    yaml_text = _yaml.safe_dump(body, sort_keys=False, default_flow_style=False)
    p.write_text(yaml_text, encoding="utf-8")
    import contextlib

    with contextlib.suppress(OSError):
        _os.chmod(p, 0o600)
    return p


__all__ = [
    "run_image_provider_wizard",
    "run_provider_wizard",
    "run_rankmyanswer_wizard",
]
