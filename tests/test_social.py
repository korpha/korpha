"""Tests for the social-posting facade + persistent profile store.

Coverage:
  - Profile store: layout, exists probe, meta load/save, stamp methods
  - Platform catalogue: stable slugs, lookup error message
  - Goal composer renders text + image attachments
  - post_to_platform refuses when not logged in
  - Visual action parser accepts xy/key shapes from playwright_action
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from korpha.browser.profile_store import (
    PLATFORMS,
    ProfileStore,
    get_platform,
)
from korpha.browser.providers.playwright_action import _parse_action
from korpha.social import (
    PostRequest,
    _compose_goal,
    list_platforms,
)


@pytest.fixture
def store(tmp_path: Path) -> ProfileStore:
    s = ProfileStore(root=tmp_path / "profiles")
    s.ensure_root()
    return s


# ---------------------------------------------------------------------------
# Platform catalogue
# ---------------------------------------------------------------------------


def test_platforms_include_all_target_networks() -> None:
    slugs = {p.slug for p in PLATFORMS}
    assert slugs == {"x", "linkedin", "youtube", "facebook", "instagram", "threads"}


def test_list_platforms_matches_module_constant() -> None:
    assert list_platforms() == PLATFORMS


def test_get_platform_unknown_slug_lists_known() -> None:
    with pytest.raises(KeyError) as exc:
        get_platform("mastodon")
    msg = str(exc.value)
    assert "mastodon" in msg
    assert "x" in msg
    assert "linkedin" in msg


def test_platform_known_slug_returns_spec() -> None:
    p = get_platform("x")
    assert p.label == "X (Twitter)"
    assert p.compose_url.startswith("https://")


def test_linkedin_flagged_for_visual_fallback() -> None:
    p = get_platform("linkedin")
    assert p.requires_visual_fallback is True


def test_x_does_not_force_visual_fallback() -> None:
    p = get_platform("x")
    assert p.requires_visual_fallback is False


# ---------------------------------------------------------------------------
# ProfileStore (now keyed by (slug, unit_id))
# ---------------------------------------------------------------------------


KDP_UNIT = "kdp-unit-id-aaaa"
EVERGREEN_UNIT = "evergreen-unit-id-bbbb"


def test_profile_dir_under_root_with_unit(store: ProfileStore) -> None:
    assert (
        store.profile_dir("x", KDP_UNIT)
        == store.root / "x" / KDP_UNIT
    )


def test_profile_dir_requires_unit_id(store: ProfileStore) -> None:
    with pytest.raises(ValueError):
        store.profile_dir("x", "")


def test_profile_exists_false_when_dir_missing(store: ProfileStore) -> None:
    assert store.profile_exists("x", KDP_UNIT) is False


def test_profile_exists_false_when_dir_empty(store: ProfileStore) -> None:
    store.profile_dir("x", KDP_UNIT).mkdir(parents=True)
    assert store.profile_exists("x", KDP_UNIT) is False


def test_profile_exists_true_when_populated(store: ProfileStore) -> None:
    d = store.profile_dir("x", KDP_UNIT)
    d.mkdir(parents=True)
    (d / "Cookies").write_text("fake")
    assert store.profile_exists("x", KDP_UNIT) is True


def test_profile_isolated_between_units(store: ProfileStore) -> None:
    d = store.profile_dir("x", KDP_UNIT)
    d.mkdir(parents=True)
    (d / "Cookies").write_text("kdp")
    assert store.profile_exists("x", KDP_UNIT) is True
    assert store.profile_exists("x", EVERGREEN_UNIT) is False


def test_list_loggedin_units_returns_populated_only(
    store: ProfileStore,
) -> None:
    d1 = store.profile_dir("x", KDP_UNIT)
    d1.mkdir(parents=True)
    (d1 / "Cookies").write_text("k")
    d2 = store.profile_dir("x", EVERGREEN_UNIT)
    d2.mkdir(parents=True)
    # empty dir shouldn't count
    assert store.list_loggedin_units("x") == [KDP_UNIT]


def test_load_meta_empty_by_default(store: ProfileStore) -> None:
    assert store.load_meta() == {}


def test_get_meta_returns_empty_row_for_missing_pair(
    store: ProfileStore,
) -> None:
    row = store.get_meta("x", KDP_UNIT)
    assert row.slug == "x"
    assert row.unit_id == KDP_UNIT
    assert row.last_login_at is None
    assert row.last_post_at is None


def test_mark_login_then_load_round_trip(store: ProfileStore) -> None:
    fixed = 1_700_000_000.0
    store.mark_login("x", KDP_UNIT, when=fixed)
    meta = store.load_meta()
    assert meta[("x", KDP_UNIT)].last_login_at == fixed
    assert ("linkedin", KDP_UNIT) not in meta


def test_mark_post_persists_across_loads(store: ProfileStore) -> None:
    fixed = 1_700_000_500.0
    store.mark_post("linkedin", EVERGREEN_UNIT, when=fixed)
    meta = store.load_meta()
    assert meta[("linkedin", EVERGREEN_UNIT)].last_post_at == fixed


def test_mark_login_does_not_clobber_other_units(store: ProfileStore) -> None:
    store.mark_login("x", KDP_UNIT, when=100.0)
    store.mark_login("x", EVERGREEN_UNIT, when=200.0)
    meta = store.load_meta()
    assert meta[("x", KDP_UNIT)].last_login_at == 100.0
    assert meta[("x", EVERGREEN_UNIT)].last_login_at == 200.0


def test_mark_login_default_now(store: ProfileStore) -> None:
    before = time.time()
    store.mark_login("x", KDP_UNIT)
    after = time.time()
    meta = store.load_meta()
    assert before <= meta[("x", KDP_UNIT)].last_login_at <= after


def test_save_load_meta_preserves_notes(store: ProfileStore) -> None:
    from korpha.browser.profile_store import ProfileMeta
    meta = {
        ("x", KDP_UNIT): ProfileMeta(
            slug="x", unit_id=KDP_UNIT,
            notes="business account, not personal",
        ),
    }
    store.save_meta(meta)
    reloaded = store.load_meta()
    assert reloaded[("x", KDP_UNIT)].notes == "business account, not personal"


def test_mark_login_rejects_unknown_slug(store: ProfileStore) -> None:
    with pytest.raises(KeyError):
        store.mark_login("mastodon", KDP_UNIT)


def test_mark_login_rejects_empty_unit(store: ProfileStore) -> None:
    with pytest.raises(ValueError):
        store.mark_login("x", "")


def test_meta_file_is_human_readable_json(store: ProfileStore) -> None:
    store.mark_login("x", KDP_UNIT, when=42.0)
    body = (store.root / "_meta.json").read_text()
    assert "last_login_at" in body
    assert KDP_UNIT in body
    assert "\"x\"" in body or "x::" in body


# ---------------------------------------------------------------------------
# Goal composer
# ---------------------------------------------------------------------------


def test_compose_goal_includes_text_verbatim() -> None:
    p = get_platform("x")
    req = PostRequest(text="hello world from the agent")
    goal = _compose_goal(p, req)
    assert "hello world from the agent" in goal
    assert p.label in goal
    assert p.compose_url in goal


def test_compose_goal_lists_image_paths_when_present() -> None:
    p = get_platform("x")
    req = PostRequest(text="post with media", image_paths=("/tmp/a.png", "/tmp/b.png"))
    goal = _compose_goal(p, req)
    assert "/tmp/a.png" in goal
    assert "/tmp/b.png" in goal


def test_compose_goal_omits_image_section_when_no_images() -> None:
    p = get_platform("x")
    req = PostRequest(text="text-only")
    goal = _compose_goal(p, req)
    assert "Images to attach" not in goal


# ---------------------------------------------------------------------------
# post_to_platform pre-condition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_to_platform_refuses_without_login(
    store: ProfileStore,
) -> None:
    from korpha.social import post_to_platform

    with pytest.raises(FileNotFoundError) as exc:
        await post_to_platform(
            "x",
            KDP_UNIT,
            PostRequest(text="hi"),
            store=store,
            pool=object(),  # never reached
        )
    msg = str(exc.value)
    assert "korpha social login x" in msg
    assert "X (Twitter)" in msg
    assert KDP_UNIT in msg


# ---------------------------------------------------------------------------
# Action parser accepts visual-step shapes
# ---------------------------------------------------------------------------


def test_parse_click_xy() -> None:
    a = _parse_action({"action": "click_xy", "x": 300, "y": 450})
    assert a.kind == "click_xy"
    assert a.x == 300
    assert a.y == 450


def test_parse_type_at_with_submit() -> None:
    a = _parse_action({
        "action": "type_at", "x": 10, "y": 20,
        "text": "hello", "submit": True,
    })
    assert a.kind == "type_at"
    assert a.text == "hello"
    assert a.submit is True
    assert a.x == 10


def test_parse_key_named_key() -> None:
    a = _parse_action({"action": "key", "text": "Enter"})
    assert a.kind == "key"
    assert a.text == "Enter"


def test_parse_unknown_action_rejected() -> None:
    from korpha.browser.service import BrowserError
    with pytest.raises(BrowserError):
        _parse_action({"action": "swipe_left"})
