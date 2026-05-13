"""Vision tier tests — registry, message serialization, wizard auto-attach.

Vision = analyzing images. Image generation is a separate concern (see
korpha/imagery/). These tests pin: model-capability detection, the
multimodal message shape over OpenAI-compat, and the wizard's
auto-attach-vision behavior.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from korpha.audit.model import InferenceTier
from korpha.cli import app
from korpha.inference.providers.openai_compat import _message_to_openai
from korpha.inference.types import ImageRef, Message, Role
from korpha.inference.vision import (
    DEFAULT_VISION_MODEL,
    model_supports_vision,
)

# ---------------------------------------------------------------------------
# Vision-tier enum + capability registry
# ---------------------------------------------------------------------------


def test_vision_is_a_real_inference_tier() -> None:
    """The eval / pool routing gates on this enum value existing."""
    assert InferenceTier.VISION.value == "vision"


def test_known_open_weights_vision_models_detected() -> None:
    """Smoke set covering the open-weights vision models we care about."""
    samples = [
        "kimi-k2.6",
        "qwen3-vl-7b",
        "qwen2.5-vl-72b-instruct",
        "meta-llama/Llama-3.2-Vision-11B-Instruct",
        "meta-llama/Llama-3.3-Vision",
        "glm-4v-9b",
        "pixtral-12b-2409",
        "internvl2-26b",
        "llava-v1.6-34b",
        "molmo-7B-D",
        "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
        "deepseek-vl-7b-chat",
        "phi-3.5-vision-instruct",
    ]
    for m in samples:
        assert model_supports_vision(m), f"{m!r} should be detected as vision-capable"


def test_text_only_models_not_marked_vision() -> None:
    """Common text-only models must NOT be flagged."""
    samples = [
        "deepseek-v4-pro",
        "deepseek-v4-flash",
        "llama-3.1-70b-instruct",
        "qwen-2.5-32b",
        "mistral-small-4-119b",
        "kimi-k2-instruct",  # text-only Kimi K2 (the K2 base, not K2.6)
    ]
    for m in samples:
        assert not model_supports_vision(m), (
            f"{m!r} is text-only; should NOT be detected as vision"
        )


def test_closed_models_detected_for_user_who_already_has_keys() -> None:
    """We don't recommend closed models, but we DO detect them so the
    wizard doesn't push a separate vision setup on someone with a
    GPT-4o or Claude Sonnet key already configured."""
    assert model_supports_vision("gpt-4o-mini")
    assert model_supports_vision("claude-sonnet-4-6")
    assert model_supports_vision("gemini-2.0-pro")


def test_default_vision_model_points_at_open_weights_nemotron() -> None:
    """Per memory feedback_open_weights_only — default suggestion must
    be open-weights, not GPT-4o or anything closed."""
    assert "nemotron" in DEFAULT_VISION_MODEL.lower()
    assert "nvidia" in DEFAULT_VISION_MODEL.lower()


def test_empty_or_unknown_model_id_safely_returns_false() -> None:
    assert not model_supports_vision("")
    assert not model_supports_vision("definitely-not-real-llm-name")


# ---------------------------------------------------------------------------
# Multimodal message serialization
# ---------------------------------------------------------------------------


def test_text_only_message_serializes_as_string_content() -> None:
    """Backward compat: text-only messages keep their string content
    field — providers that don't speak multimodal still work."""
    m = Message(role=Role.USER, content="hello")
    payload = _message_to_openai(m)
    assert payload == {"role": "user", "content": "hello"}


def test_message_with_url_image_serializes_as_parts_array() -> None:
    """The OpenAI multimodal shape every major open-weights vision
    model speaks: content is an array, image entries are
    {type: image_url, image_url: {url}}."""
    m = Message(
        role=Role.USER,
        content="What's in this screenshot?",
        images=(ImageRef(url="https://example.com/screenshot.png"),),
    )
    payload = _message_to_openai(m)
    assert payload["role"] == "user"
    assert isinstance(payload["content"], list)
    assert payload["content"][0] == {
        "type": "text",
        "text": "What's in this screenshot?",
    }
    assert payload["content"][1] == {
        "type": "image_url",
        "image_url": {"url": "https://example.com/screenshot.png"},
    }


def test_message_with_b64_image_wraps_as_data_url() -> None:
    """b64_png attachments get wrapped as data: URLs at send time so
    callers don't have to know the encoding scheme."""
    m = Message(
        role=Role.USER,
        content="check this",
        images=(ImageRef(b64_png="iVBORw0KGgoAAAANSUhEUgAA"),),
    )
    payload = _message_to_openai(m)
    img = payload["content"][1]
    assert img["image_url"]["url"].startswith("data:image/png;base64,")
    assert img["image_url"]["url"].endswith("iVBORw0KGgoAAAANSUhEUgAA")


def test_message_with_detail_hint_passes_it_through() -> None:
    """OpenAI's detail: low|high|auto hint. Most open models ignore;
    harmless to pass."""
    m = Message(
        role=Role.USER,
        content="x",
        images=(ImageRef(url="https://x/y.png", detail="high"),),
    )
    payload = _message_to_openai(m)
    assert payload["content"][1]["image_url"]["detail"] == "high"


def test_message_with_only_image_no_text() -> None:
    """A turn that's just an image with no text. The text part is
    omitted from the parts array."""
    m = Message(
        role=Role.USER, content="",
        images=(ImageRef(url="https://x/y.png"),),
    )
    payload = _message_to_openai(m)
    parts = payload["content"]
    assert len(parts) == 1
    assert parts[0]["type"] == "image_url"


# ---------------------------------------------------------------------------
# Wizard auto-attach
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    target = tmp_path / "providers.yaml"
    monkeypatch.setenv("KORPHA_PROVIDERS_FILE", str(target))
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    return target


def test_wizard_auto_attaches_vision_when_pro_supports_it(
    isolated_config: Path,
) -> None:
    """When the user picks a Pro model with built-in vision (Kimi K2.6
    etc.), the wizard writes vision: <same model> to the entry without
    asking the user a second time."""
    from korpha.cli_config import _suggest_order
    from korpha.inference.providers.openai_compat import (
        PROVIDER_PRESETS,
        SUBSCRIPTION_PRESETS,
    )

    ordered = _suggest_order([*PROVIDER_PRESETS, *SUBSCRIPTION_PRESETS, "custom"])
    openrouter_idx = ordered.index("openrouter") + 1

    runner = CliRunner()
    answers = "\n".join([
        str(openrouter_idx),
        "kimi-pro",                           # label
        "sk-test",                            # api key
        "openai/gpt-4o-mini",                 # workhorse (text-only — we'll override pro)
        "moonshotai/kimi-k2.6",               # pro = vision-capable
        "",                                   # skip cap
    ]) + "\n"
    result = runner.invoke(app, ["config"], input=answers)
    assert result.exit_code == 0, result.stdout
    assert "Vision tier auto-set to" in result.stdout
    assert "kimi-k2.6" in result.stdout

    body = yaml.safe_load(isolated_config.read_text())
    entry = body["providers"][0]
    assert entry["tiers"]["vision"] == "moonshotai/kimi-k2.6"


def test_wizard_suggests_default_when_pro_lacks_vision(
    isolated_config: Path,
) -> None:
    """When the Pro model is text-only, the wizard does NOT silently
    add a wrong vision tier — instead it prints the Nemotron suggestion."""
    from korpha.cli_config import _suggest_order
    from korpha.inference.providers.openai_compat import (
        PROVIDER_PRESETS,
        SUBSCRIPTION_PRESETS,
    )

    ordered = _suggest_order([*PROVIDER_PRESETS, *SUBSCRIPTION_PRESETS, "custom"])
    deepseek_idx = ordered.index("deepseek") + 1

    runner = CliRunner()
    answers = "\n".join([
        str(deepseek_idx),
        "ds",                                 # label
        "sk-test",                            # api key
        "deepseek-chat",                      # workhorse
        "deepseek-reasoner",                  # pro — text-only
        "",                                   # skip cap
    ]) + "\n"
    result = runner.invoke(app, ["config"], input=answers)
    assert result.exit_code == 0, result.stdout

    # No silent attach
    body = yaml.safe_load(isolated_config.read_text())
    assert "vision" not in body["providers"][0]["tiers"]
    # But the Nemotron suggestion should be on screen
    assert "Nemotron 3 Nano Omni" in result.stdout
    assert DEFAULT_VISION_MODEL in result.stdout
