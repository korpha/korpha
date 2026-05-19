"""Persistent Chromium profile store for per-platform-per-unit sessions.

Layout::

    ~/.korpha/browser-profiles/
      _meta.json                          # timestamps + per-account notes
      x/
        <business_unit_id>/               # one X account per business line
      linkedin/
        <business_unit_id>/
      youtube/
        <business_unit_id>/
      facebook/
        <business_unit_id>/
      instagram/
        <business_unit_id>/
      threads/
        <business_unit_id>/

Each leaf is a real Chromium ``user_data_dir``. The first time Mike
opens "Login to X for KDP Activity Books", the agent launches a
headed Chromium pointed at x.com, Mike logs in to the KDP brand
account, the session lands in
``browser-profiles/x/<kdp-unit-id>/``, and subsequent posts to that
business line reuse it. A different business line ("Evergreen
T-shirts & Mugs") gets its own X profile at
``browser-profiles/x/<evergreen-unit-id>/`` with its own brand voice.

Why scope by business unit:
  * Each business line typically has its own brand handle / account
    on each platform — "@KDPActivityBooks" vs "@EvergreenTees".
  * Brand voice differs across lines (KDP educational vs Evergreen
    lifestyle). Posting from the right account is a load-bearing
    correctness concern.
  * Cross-install dogfood (Marketro vs Andrew) is handled at the
    install-level (different ``$KORPHA_DATA_DIR``). Each install
    owns its own ``browser-profiles/`` tree.

Identity choices:
  * Filesystem keys use the business unit UUID (not slug) for
    collision safety — a unit slug is only unique among siblings.
  * The CLI / UI resolves user-friendly slugs to UUIDs before
    calling into this module; this module is identity-agnostic.
  * Platform ids stay short lowercase slugs (``x``, ``linkedin``,
    etc.) — those ARE globally unique and Mike-readable.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


# Canonical platform metadata. Add a row here to teach the rest of
# the system about a new social network. Keeps the CLI menu, the
# dashboard tile list, and the docs in sync — there is exactly one
# source of truth.
@dataclass(frozen=True)
class PlatformSpec:
    slug: str
    """Stable id used in URLs, filesystem paths, and config. Never
    rename — old profiles wouldn't find their dir."""

    label: str
    """Human-readable name shown in UI + CLI prompts."""

    home_url: str
    """Page to open during the login wizard. Picked so the
    'logged in?' check is unambiguous — usually the platform's
    settings or profile page rather than the marketing homepage."""

    compose_url: str
    """Page to open when posting. The action loop navigates here
    before driving the compose dialog."""

    requires_visual_fallback: bool
    """Whether this platform's compose flow is known to defeat the
    accessibility-tree action loop (shadow DOMs, contenteditable
    surfaces). The post() workflow respects this hint and sets
    ``BrowserTask.visual_fallback`` accordingly."""


PLATFORMS: tuple[PlatformSpec, ...] = (
    PlatformSpec(
        slug="x",
        label="X (Twitter)",
        home_url="https://x.com/home",
        compose_url="https://x.com/compose/post",
        requires_visual_fallback=False,
    ),
    PlatformSpec(
        slug="linkedin",
        label="LinkedIn",
        home_url="https://www.linkedin.com/feed/",
        compose_url="https://www.linkedin.com/feed/?shareActive=true",
        requires_visual_fallback=True,
    ),
    PlatformSpec(
        slug="youtube",
        label="YouTube (Community + Shorts)",
        home_url="https://www.youtube.com/feed/you",
        compose_url="https://studio.youtube.com/channel/UC/community",
        requires_visual_fallback=True,
    ),
    PlatformSpec(
        slug="facebook",
        label="Facebook",
        home_url="https://www.facebook.com/me",
        compose_url="https://www.facebook.com/",
        requires_visual_fallback=True,
    ),
    PlatformSpec(
        slug="instagram",
        label="Instagram",
        home_url="https://www.instagram.com/",
        compose_url="https://www.instagram.com/",
        requires_visual_fallback=True,
    ),
    PlatformSpec(
        slug="threads",
        label="Threads",
        home_url="https://www.threads.net/",
        compose_url="https://www.threads.net/",
        requires_visual_fallback=True,
    ),
)


_PLATFORM_BY_SLUG: dict[str, PlatformSpec] = {p.slug: p for p in PLATFORMS}


def get_platform(slug: str) -> PlatformSpec:
    """Look up a platform by slug, raising KeyError with a helpful
    message listing known slugs when missing."""
    try:
        return _PLATFORM_BY_SLUG[slug]
    except KeyError as exc:
        known = ", ".join(p.slug for p in PLATFORMS)
        raise KeyError(
            f"unknown platform slug {slug!r}. known: {known}"
        ) from exc


# ---------------------------------------------------------------------------
# Profile store on disk
# ---------------------------------------------------------------------------


@dataclass
class ProfileMeta:
    """One (platform, business unit) session's metadata.

    The key in :meth:`ProfileStore.load_meta` is a ``(slug, unit_id)``
    tuple. ``slug`` + ``unit_id`` are also stored on the row itself
    so the JSON file is self-describing.
    """

    slug: str
    unit_id: str
    last_login_at: float | None = None
    """Unix epoch when the login wizard last finished. None = never
    logged in (profile dir doesn't exist or is empty)."""

    last_post_at: float | None = None
    """When the agent most recently used this profile to post. UI
    surfaces this so Mike can see "last posted from KDP-Activity-Books
    to X 4 hours ago" at a glance."""

    notes: str = ""
    """Free-form annotation Mike can edit. E.g., "uses business
    account, not personal"."""


_META_FILENAME = "_meta.json"


def _meta_key(slug: str, unit_id: str) -> str:
    """Compose the dict / JSON key for a (platform, unit) pair.

    Kept as a function so callers don't sprinkle the separator —
    if we ever need to escape ``::`` in an id, the change lands here.
    """
    return f"{slug}::{unit_id}"


@dataclass
class ProfileStore:
    """Owns the on-disk layout for browser profiles.

    Stateless beyond the ``root`` path — every call hits the
    filesystem. That's fine; profile mutations are infrequent (login
    once per platform-per-unit, then read-only). Tests pass a
    tmp_path so we don't touch the real ``~/.korpha``.
    """

    root: Path
    """Base directory. Default consumers pass
    ``$KORPHA_DATA_DIR/browser-profiles/``."""

    def ensure_root(self) -> None:
        """Create the root directory if absent. Idempotent."""
        self.root.mkdir(parents=True, exist_ok=True)

    def profile_dir(self, slug: str, unit_id: str) -> Path:
        """Path the Chromium user_data_dir lives at for
        ``(slug, unit_id)``.

        Does NOT create the dir — the login wizard creates it;
        ``profile_exists`` does not.
        """
        get_platform(slug)  # validate slug; raises if unknown
        if not unit_id:
            raise ValueError("unit_id is required (pass a BusinessUnit id)")
        return self.root / slug / unit_id

    def profile_exists(self, slug: str, unit_id: str) -> bool:
        """True when the profile dir has any contents (= Mike has
        opened the wizard at least once for this platform + unit)."""
        d = self.profile_dir(slug, unit_id)
        if not d.is_dir():
            return False
        try:
            return any(d.iterdir())
        except OSError:
            return False

    def list_loggedin_units(self, slug: str) -> list[str]:
        """All unit ids that have a non-empty profile for ``slug``.

        Used by the dashboard to show "X is logged in for units: KDP,
        Evergreen" without having to enumerate every BusinessUnit row.
        """
        get_platform(slug)
        platform_dir = self.root / slug
        if not platform_dir.is_dir():
            return []
        out: list[str] = []
        try:
            for child in platform_dir.iterdir():
                if child.is_dir() and any(child.iterdir()):
                    out.append(child.name)
        except OSError:
            return out
        return sorted(out)

    def load_meta(self) -> dict[tuple[str, str], ProfileMeta]:
        """Read the meta file. Keys are ``(slug, unit_id)`` tuples.

        Returns an empty dict when no metadata has ever been written;
        callers should treat missing keys as never-logged-in rather
        than seeding entries for every possible (platform, unit)
        combination (that grows quickly with multiple business lines).
        """
        meta_path = self.root / _META_FILENAME
        if not meta_path.is_file():
            return {}
        try:
            with open(meta_path, encoding="utf-8") as f:
                existing = json.load(f) or {}
        except (OSError, json.JSONDecodeError):
            return {}
        out: dict[tuple[str, str], ProfileMeta] = {}
        for key, row in existing.items():
            slug = row.get("slug")
            unit_id = row.get("unit_id")
            if not slug or not unit_id:
                continue
            out[(slug, unit_id)] = ProfileMeta(
                slug=slug,
                unit_id=unit_id,
                last_login_at=row.get("last_login_at"),
                last_post_at=row.get("last_post_at"),
                notes=row.get("notes", ""),
            )
        return out

    def get_meta(self, slug: str, unit_id: str) -> ProfileMeta:
        """Convenience: return the row for ``(slug, unit_id)`` or a
        fresh empty row if missing. Never raises on missing key."""
        return self.load_meta().get(
            (slug, unit_id),
            ProfileMeta(slug=slug, unit_id=unit_id),
        )

    def save_meta(self, meta: dict[tuple[str, str], ProfileMeta]) -> None:
        """Write the meta file atomically (write-temp + rename).

        Keys are tuples in-memory but serialized as ``slug::unit_id``
        strings since JSON doesn't allow non-string keys.
        """
        self.ensure_root()
        body = {
            _meta_key(slug, unit_id): asdict(m)
            for (slug, unit_id), m in meta.items()
        }
        meta_path = self.root / _META_FILENAME
        tmp = meta_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(body, f, indent=2, sort_keys=True)
        os.replace(tmp, meta_path)

    def mark_login(
        self, slug: str, unit_id: str, *, when: float | None = None,
    ) -> None:
        """Stamp the login timestamp on ``(slug, unit_id)`` and persist.

        Creates the row if it didn't exist yet — login is typically
        the FIRST event a (platform, unit) row sees."""
        get_platform(slug)
        if not unit_id:
            raise ValueError("unit_id is required")
        meta = self.load_meta()
        row = meta.get(
            (slug, unit_id),
            ProfileMeta(slug=slug, unit_id=unit_id),
        )
        row.last_login_at = when if when is not None else time.time()
        meta[(slug, unit_id)] = row
        self.save_meta(meta)

    def mark_post(
        self, slug: str, unit_id: str, *, when: float | None = None,
    ) -> None:
        """Stamp the last-post timestamp on ``(slug, unit_id)`` and
        persist."""
        get_platform(slug)
        if not unit_id:
            raise ValueError("unit_id is required")
        meta = self.load_meta()
        row = meta.get(
            (slug, unit_id),
            ProfileMeta(slug=slug, unit_id=unit_id),
        )
        row.last_post_at = when if when is not None else time.time()
        meta[(slug, unit_id)] = row
        self.save_meta(meta)


def default_profile_store() -> ProfileStore:
    """Convenience: returns a store rooted at the standard
    ``$KORPHA_DATA_DIR/browser-profiles/`` (or ``~/.korpha/...``).
    Used by the CLI + dashboard handlers."""
    base_str = os.environ.get("KORPHA_DATA_DIR")
    base = Path(base_str) if base_str else (Path.home() / ".korpha")
    return ProfileStore(root=base / "browser-profiles")


__all__ = [
    "PLATFORMS",
    "PlatformSpec",
    "ProfileMeta",
    "ProfileStore",
    "default_profile_store",
    "get_platform",
]
