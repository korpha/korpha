"""Dashboard theme system tests.

Covers: schema validation (parse_theme), built-ins ship cleanly, user
YAML discovery, active-theme persistence, CSS-var rendering, and the
two HTTP endpoints (GET /api/dashboard/themes + PUT /api/dashboard/theme).
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from korpha.themes import (
    BUILTIN_THEMES,
    DEFAULT_THEME,
    DashboardThemesError,
    discover_user_themes,
    get_active_theme_name,
    list_themes,
    load_theme_by_name,
    parse_theme,
    set_active_theme_name,
)
from korpha.themes.css import render_theme_css_vars
from korpha.themes.types import ThemeValidationError

# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


_MIN_VALID = {
    "name": "test",
    "label": "Test Theme",
    "description": "A test.",
    "palette": {
        "background": "#0c0d10",
        "midground": "#e6e8eb",
        "foreground": {"hex": "#ffffff", "alpha": 0},
    },
}


def test_parse_minimal_valid_theme() -> None:
    t = parse_theme(_MIN_VALID)
    assert t.name == "test"
    assert t.palette.background.hex == "#0c0d10"
    assert t.palette.foreground.alpha == 0.0


def test_missing_required_name() -> None:
    body = dict(_MIN_VALID)
    del body["name"]
    with pytest.raises(ThemeValidationError, match=r"'name'"):
        parse_theme(body)


def test_missing_palette() -> None:
    body = {**_MIN_VALID}
    del body["palette"]
    with pytest.raises(ThemeValidationError, match=r"palette"):
        parse_theme(body)


def test_bad_palette_hex_rejected() -> None:
    body = {**_MIN_VALID, "palette": {**_MIN_VALID["palette"], "background": "blue"}}
    with pytest.raises(ThemeValidationError, match=r"#RRGGBB"):
        parse_theme(body)


def test_invalid_density_rejected() -> None:
    body = {**_MIN_VALID, "layout": {"density": "spaceous"}}
    with pytest.raises(ThemeValidationError, match=r"density"):
        parse_theme(body)


def test_invalid_layout_variant_rejected() -> None:
    body = {**_MIN_VALID, "layout_variant": "haunted-house"}
    with pytest.raises(ThemeValidationError, match=r"layout_variant"):
        parse_theme(body)


def test_camelcase_keys_accepted() -> None:
    """Authors used to Hermes' camelCase shouldn't have to convert."""
    body = {
        **_MIN_VALID,
        "layoutVariant": "tiled",
        "typography": {"baseSize": "16px", "fontUrl": "https://example.com/x.css"},
        "colorOverrides": {"primaryForeground": "#ffffff"},
    }
    t = parse_theme(body)
    assert t.layout_variant == "tiled"
    assert t.typography.base_size == "16px"
    assert t.color_overrides.primary_foreground == "#ffffff"


def test_bad_color_override_hex() -> None:
    body = {**_MIN_VALID, "color_overrides": {"primary": "blue"}}
    with pytest.raises(ThemeValidationError, match=r"#RRGGBB"):
        parse_theme(body)


def test_unknown_color_override_key() -> None:
    body = {**_MIN_VALID, "color_overrides": {"made_up": "#000000"}}
    with pytest.raises(ThemeValidationError, match=r"unknown color_overrides"):
        parse_theme(body)


def test_unknown_component_bucket() -> None:
    body = {**_MIN_VALID, "component_styles": {"floof": {"x": "1"}}}
    with pytest.raises(ThemeValidationError, match=r"unknown component bucket"):
        parse_theme(body)


def test_assets_custom_keys_validated() -> None:
    """Custom asset keys must be alphanumeric/underscore/dash only."""
    body = {**_MIN_VALID, "assets": {"custom": {"hero img!": "https://x.com/x"}}}
    with pytest.raises(ThemeValidationError, match=r"alphanumeric"):
        parse_theme(body)


# ---------------------------------------------------------------------------
# Built-in presets
# ---------------------------------------------------------------------------


def test_all_builtins_validate_against_schema() -> None:
    """Every shipped built-in must round-trip through the validator —
    otherwise authors copying one as a template would inherit broken
    YAML."""
    for theme in BUILTIN_THEMES.values():

        # Re-parse from a flattened dict — confirms the dataclass
        # defaults still satisfy the schema.
        parse_theme({
            "name": theme.name,
            "label": theme.label,
            "description": theme.description,
            "palette": {
                "background": theme.palette.background.hex,
                "midground": theme.palette.midground.hex,
                "foreground": {
                    "hex": theme.palette.foreground.hex,
                    "alpha": theme.palette.foreground.alpha,
                },
                "warm_glow": theme.palette.warm_glow,
                "noise_opacity": theme.palette.noise_opacity,
            },
        })


def test_default_is_canonical_and_first() -> None:
    """The 'default' theme name is what get_active_theme_name() falls
    back to. Must exist."""
    assert "default" in BUILTIN_THEMES
    assert DEFAULT_THEME.name == "default"


def test_four_builtins_ship() -> None:
    expected = {"default", "midnight", "sage", "ember"}
    assert set(BUILTIN_THEMES) == expected


# ---------------------------------------------------------------------------
# Loader (user themes + active persistence)
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    return tmp_path


def _write_user_theme(data_dir: Path, name: str, body: dict) -> Path:
    themes_dir = data_dir / "dashboard-themes"
    themes_dir.mkdir(parents=True, exist_ok=True)
    p = themes_dir / f"{name}.yaml"
    p.write_text(yaml.safe_dump(body))
    return p


def test_discover_user_themes_finds_yaml(isolated_data_dir: Path) -> None:
    body = {**_MIN_VALID, "name": "ocean", "label": "Ocean", "description": "Blue."}
    _write_user_theme(isolated_data_dir, "ocean", body)
    themes = discover_user_themes()
    assert any(t.name == "ocean" for t in themes)


def test_discover_skips_malformed_yaml(isolated_data_dir: Path) -> None:
    """A broken YAML should not take down the whole picker — discover
    silently skips. (Real load_theme_by_name will raise.)"""
    themes_dir = isolated_data_dir / "dashboard-themes"
    themes_dir.mkdir(parents=True)
    (themes_dir / "broken.yaml").write_text("not a: dict\n  - this is\n")
    (themes_dir / "missing-fields.yaml").write_text("name: x\n")
    # discover_user_themes returns whatever was parseable
    out = discover_user_themes()
    # Both are malformed — neither should appear
    assert all(t.name not in ("broken", "missing-fields") for t in out)


def test_filename_stem_is_default_name(isolated_data_dir: Path) -> None:
    """If author drops 'twilight.yaml' without setting name in the file,
    the stem fills in. Lowers the friction for sharing — drop the file
    and go."""
    themes_dir = isolated_data_dir / "dashboard-themes"
    themes_dir.mkdir(parents=True)
    body = {k: v for k, v in _MIN_VALID.items() if k != "name"}
    body["label"] = "Twilight"
    body["description"] = "Purple-blue dusk."
    (themes_dir / "twilight.yaml").write_text(yaml.safe_dump(body))
    themes = discover_user_themes()
    assert any(t.name == "twilight" for t in themes)


def test_list_themes_returns_builtins_and_users(isolated_data_dir: Path) -> None:
    body = {**_MIN_VALID, "name": "ocean"}
    _write_user_theme(isolated_data_dir, "ocean", body)
    entries = list_themes()
    names = {e.name for e in entries}
    # All 4 built-ins
    assert {"default", "midnight", "sage", "ember"}.issubset(names)
    # User theme
    assert "ocean" in names


def test_user_cannot_shadow_builtin(isolated_data_dir: Path) -> None:
    """A user theme named 'default' is dropped from the list — built-ins
    win. Stops a custom theme from accidentally replacing the canonical
    fallback that get_active_theme_name() returns to."""
    body = {**_MIN_VALID, "name": "default", "label": "User Default"}
    _write_user_theme(isolated_data_dir, "default", body)
    entries = list_themes()
    default_entries = [e for e in entries if e.name == "default"]
    assert len(default_entries) == 1
    assert default_entries[0].is_builtin is True


def test_user_themes_ship_full_definition(isolated_data_dir: Path) -> None:
    """May-4 Hermes fix: user themes include their full definition
    inline so the picker can render real palette swatches without
    a second round-trip."""
    body = {**_MIN_VALID, "name": "ocean"}
    _write_user_theme(isolated_data_dir, "ocean", body)
    entries = list_themes()
    ocean = next(e for e in entries if e.name == "ocean")
    assert ocean.is_builtin is False
    assert ocean.definition is not None
    assert ocean.definition.palette.background.hex == "#0c0d10"


def test_builtin_entries_omit_definition() -> None:
    """Built-in entries don't ship `definition` — the dashboard
    already has them statically. Saves bytes on every list call."""
    entries = list_themes()
    for entry in entries:
        if entry.is_builtin:
            assert entry.definition is None


def test_load_theme_by_name_builtin() -> None:
    t = load_theme_by_name("midnight")
    assert t.name == "midnight"


def test_load_theme_by_name_user(isolated_data_dir: Path) -> None:
    body = {**_MIN_VALID, "name": "ocean"}
    _write_user_theme(isolated_data_dir, "ocean", body)
    t = load_theme_by_name("ocean")
    assert t.name == "ocean"


def test_load_theme_by_name_missing(isolated_data_dir: Path) -> None:
    with pytest.raises(DashboardThemesError, match=r"not found"):
        load_theme_by_name("ghost")


def test_load_theme_by_name_malformed_user(isolated_data_dir: Path) -> None:
    """When directly asked for a malformed theme, RAISE (vs the silent
    skip in discover)."""
    themes_dir = isolated_data_dir / "dashboard-themes"
    themes_dir.mkdir(parents=True)
    (themes_dir / "junk.yaml").write_text("name: junk\n")
    with pytest.raises(DashboardThemesError, match=r"malformed"):
        load_theme_by_name("junk")


def test_active_theme_default_when_unset(isolated_data_dir: Path) -> None:
    assert get_active_theme_name() == "default"


def test_active_theme_round_trip(isolated_data_dir: Path) -> None:
    set_active_theme_name("midnight")
    assert get_active_theme_name() == "midnight"


def test_set_active_rejects_unknown(isolated_data_dir: Path) -> None:
    with pytest.raises(DashboardThemesError):
        set_active_theme_name("ghost")


# ---------------------------------------------------------------------------
# CSS-var rendering
# ---------------------------------------------------------------------------


def test_render_theme_includes_core_vars() -> None:
    css = render_theme_css_vars(DEFAULT_THEME)
    # The shell.css reads these — every one MUST be present after render.
    for var in (
        "--bg:", "--bg-elev:", "--text:", "--text-dim:", "--accent:",
        "--green:", "--yellow:", "--red:", "--font-sans:", "--font-mono:",
        "--radius:", "--spacing-mul:",
    ):
        assert var in css, f"missing {var} in rendered CSS"


def test_render_theme_color_overrides_win() -> None:
    sage = BUILTIN_THEMES["sage"]
    css = render_theme_css_vars(sage)
    # sage overrides primary to #1f7a4d
    assert "--primary: #1f7a4d" in css


def test_render_theme_with_font_url_rendered_separately() -> None:
    """font_url is emitted via render_font_link, not the CSS-var block —
    keeps <head> clean (link tag separate from style block)."""
    from korpha.themes.css import render_font_link

    midnight = BUILTIN_THEMES["midnight"]
    link = render_font_link(midnight)
    assert "fonts.googleapis.com" in link
    assert 'rel="stylesheet"' in link


def test_render_theme_custom_css_appended() -> None:
    body = {
        **_MIN_VALID,
        "custom_css": ".my-banner::before { content: '★'; }",
    }
    theme = parse_theme(body)
    css = render_theme_css_vars(theme)
    assert ".my-banner::before" in css
    assert "/* theme:test customCSS */" in css


def test_render_theme_assets_emitted_as_vars() -> None:
    body = {
        **_MIN_VALID,
        "assets": {
            "bg": "https://x.com/bg.png",
            "logo": "linear-gradient(45deg, red, blue)",
            "custom": {"hero_a": "https://x.com/h.png"},
        },
    }
    theme = parse_theme(body)
    css = render_theme_css_vars(theme)
    assert "--theme-asset-bg: url('https://x.com/bg.png')" in css
    # Pre-wrapped CSS expressions pass through untouched
    assert "--theme-asset-logo: linear-gradient(45deg, red, blue)" in css
    assert "--theme-asset-custom-hero_a: url('https://x.com/h.png')" in css


# ---------------------------------------------------------------------------
# HTTP endpoints
# ---------------------------------------------------------------------------


class _ThemeSetBody(__import__("pydantic").BaseModel):
    name: str


def _make_api_app():
    """A fresh FastAPI app with theme endpoints. Standalone — doesn't
    spin the whole dashboard which has heavy DI."""
    from dataclasses import asdict

    from fastapi import FastAPI, HTTPException

    from korpha.themes import (
        DashboardTheme,
        DashboardThemesError,
        get_active_theme_name,
        list_themes,
        set_active_theme_name,
    )

    def _theme_to_dict(theme: DashboardTheme) -> dict:
        return asdict(theme)

    app = FastAPI()

    @app.get("/api/dashboard/themes")
    def get_themes() -> dict:
        entries = list_themes()
        return {
            "active": get_active_theme_name(),
            "themes": [
                {
                    "name": e.name,
                    "label": e.label,
                    "description": e.description,
                    "is_builtin": e.is_builtin,
                    "definition": _theme_to_dict(e.definition) if e.definition else None,
                }
                for e in entries
            ],
        }

    @app.put("/api/dashboard/theme")
    def set_theme(body: _ThemeSetBody) -> dict:
        try:
            set_active_theme_name(body.name)
        except DashboardThemesError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"ok": True, "theme": body.name}

    return app


@pytest.fixture
def api_app(isolated_data_dir: Path):
    return _make_api_app()


def test_get_themes_endpoint(api_app, isolated_data_dir: Path) -> None:
    from fastapi.testclient import TestClient

    body = {**_MIN_VALID, "name": "ocean"}
    _write_user_theme(isolated_data_dir, "ocean", body)
    client = TestClient(api_app)
    resp = client.get("/api/dashboard/themes")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["active"] == "default"
    names = {t["name"] for t in payload["themes"]}
    assert {"default", "midnight", "sage", "ember", "ocean"}.issubset(names)
    # Built-ins ship with definition=None to save bytes
    default_entry = next(t for t in payload["themes"] if t["name"] == "default")
    assert default_entry["definition"] is None
    # User themes ship full definition for swatches
    ocean_entry = next(t for t in payload["themes"] if t["name"] == "ocean")
    assert ocean_entry["definition"] is not None
    assert ocean_entry["definition"]["palette"]["background"]["hex"] == "#0c0d10"


def test_put_theme_endpoint(api_app, isolated_data_dir: Path) -> None:
    from fastapi.testclient import TestClient

    client = TestClient(api_app)
    resp = client.put("/api/dashboard/theme", json={"name": "midnight"})
    assert resp.status_code == 200
    assert resp.json()["theme"] == "midnight"
    assert get_active_theme_name() == "midnight"


def test_put_theme_unknown_404s(api_app, isolated_data_dir: Path) -> None:
    from fastapi.testclient import TestClient

    client = TestClient(api_app)
    resp = client.put("/api/dashboard/theme", json={"name": "ghost"})
    assert resp.status_code == 404
