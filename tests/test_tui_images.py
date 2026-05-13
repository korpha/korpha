"""Tests for the TUI image-paste pipeline.

Covers:
  - Magic-byte sniffing of PNG / JPEG / GIF / unknown
  - Save round-trip (correct extension, correct path, bytes match)
  - Inline-render protocol detection from env vars
  - Kitty + iTerm2 escape-sequence shape (smoke-level — full
    terminal handshake testing requires actual terminals)
  - Clipboard capture path with all three backends mocked

We can't actually exercise the system clipboard in CI, so the
capture tests stub asyncio.create_subprocess_exec to inject the
binary's response.
"""
from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from korpha.tui.images import (
    ClipboardImage,
    capture_clipboard_image,
    inline_render_escape,
    inline_render_protocol,
    inline_render_supported,
    iterm2_inline_escape,
    kitty_inline_escape,
    save_image,
)


# Magic bytes for each format
PNG_HEADER = b"\x89PNG\r\n\x1a\n"
JPEG_HEADER = b"\xff\xd8\xff\xe0"
GIF_HEADER = b"GIF89a"


# ---- Magic-byte sniffing ----


def test_sniff_png_extension() -> None:
    img = ClipboardImage(data=PNG_HEADER + b"\x00" * 32, mime="image/png")
    assert img.extension == "png"


def test_sniff_jpeg_extension() -> None:
    img = ClipboardImage(data=JPEG_HEADER + b"\x00" * 32, mime="image/jpeg")
    assert img.extension == "jpg"


def test_sniff_gif_extension() -> None:
    img = ClipboardImage(data=GIF_HEADER + b"\x00" * 16, mime="image/gif")
    assert img.extension == "gif"


def test_unknown_mime_falls_back_to_bin() -> None:
    img = ClipboardImage(data=b"random", mime="application/octet-stream")
    assert img.extension == "bin"


# ---- save_image ----


def test_save_image_writes_file_and_returns_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    image = ClipboardImage(
        data=PNG_HEADER + b"fake-png-payload",
        mime="image/png",
    )
    path = save_image(image)
    assert path.exists()
    assert path.suffix == ".png"
    assert path.read_bytes() == PNG_HEADER + b"fake-png-payload"
    assert path.parent == tmp_path / "images"


def test_save_image_creates_dir_if_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    images_dir = tmp_path / "images"
    assert not images_dir.exists()
    save_image(ClipboardImage(data=PNG_HEADER, mime="image/png"))
    assert images_dir.is_dir()


def test_save_image_filenames_are_unique(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two consecutive saves should not collide. The timestamp goes
    down to microseconds so even rapid pastes get unique names."""
    monkeypatch.setenv("KORPHA_DATA_DIR", str(tmp_path))
    p1 = save_image(ClipboardImage(data=PNG_HEADER + b"a", mime="image/png"))
    p2 = save_image(ClipboardImage(data=PNG_HEADER + b"b", mime="image/png"))
    assert p1 != p2


# ---- Inline render protocol detection ----


def test_inline_render_protocol_detects_kitty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KITTY_WINDOW_ID", "1")
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    assert inline_render_protocol() == "kitty"


def test_inline_render_protocol_detects_kitty_via_term(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KITTY_WINDOW_ID", raising=False)
    monkeypatch.setenv("TERM", "xterm-kitty")
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    assert inline_render_protocol() == "kitty"


def test_inline_render_protocol_detects_iterm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KITTY_WINDOW_ID", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    assert inline_render_protocol() == "iterm2"


def test_inline_render_protocol_detects_wezterm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KITTY_WINDOW_ID", raising=False)
    monkeypatch.setenv("TERM_PROGRAM", "WezTerm")
    assert inline_render_protocol() == "iterm2"


def test_inline_render_protocol_returns_none_for_plain_terminals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KITTY_WINDOW_ID", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    assert inline_render_protocol() is None


def test_inline_render_supported_kitty_only_png(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KITTY_WINDOW_ID", "1")
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    png = ClipboardImage(data=PNG_HEADER, mime="image/png")
    jpg = ClipboardImage(data=JPEG_HEADER, mime="image/jpeg")
    assert inline_render_supported(png) is True
    assert inline_render_supported(jpg) is False


def test_inline_render_supported_iterm_accepts_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KITTY_WINDOW_ID", raising=False)
    monkeypatch.setenv("TERM_PROGRAM", "iTerm.app")
    for mime in ("image/png", "image/jpeg", "image/gif"):
        img = ClipboardImage(data=b"\x00", mime=mime)
        assert inline_render_supported(img) is True


# ---- Escape sequences (shape, not full terminal handshake) ----


def test_kitty_escape_starts_with_apc_payload() -> None:
    image = ClipboardImage(data=PNG_HEADER + b"x" * 100, mime="image/png")
    seq = kitty_inline_escape(image)
    assert seq.startswith("\x1b_G")
    assert "f=100" in seq
    # Payload is base64 of the bytes; should be present
    encoded = base64.b64encode(image.data).decode()
    # The full payload is split into 4 KB chunks; the start of
    # the first chunk should match the start of the encoded data.
    assert encoded[:50] in seq


def test_kitty_escape_returns_empty_for_jpeg() -> None:
    image = ClipboardImage(data=JPEG_HEADER, mime="image/jpeg")
    assert kitty_inline_escape(image) == ""


def test_iterm2_escape_starts_with_osc_1337() -> None:
    image = ClipboardImage(data=PNG_HEADER + b"x" * 50, mime="image/png")
    seq = iterm2_inline_escape(image)
    assert seq.startswith("\x1b]1337;File=")
    assert seq.endswith("\x07")


def test_inline_render_escape_dispatches_kitty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KITTY_WINDOW_ID", "1")
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    image = ClipboardImage(data=PNG_HEADER + b"x" * 32, mime="image/png")
    seq = inline_render_escape(image)
    assert seq.startswith("\x1b_G")


def test_inline_render_escape_returns_empty_when_unsupported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KITTY_WINDOW_ID", raising=False)
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    monkeypatch.setenv("TERM", "xterm")
    image = ClipboardImage(data=PNG_HEADER, mime="image/png")
    assert inline_render_escape(image) == ""


# ---- Clipboard capture (with subprocess mocked) ----


@pytest.mark.asyncio
async def test_capture_clipboard_image_returns_none_when_no_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If no clipboard binary is on PATH the capture short-circuits
    cleanly (no subprocess spawn, returns None)."""
    import shutil
    monkeypatch.setattr(shutil, "which", lambda _: None)
    result = await capture_clipboard_image()
    assert result is None


@pytest.mark.asyncio
async def test_capture_clipboard_image_handles_xclip_png(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """xclip returns PNG bytes → captured + sniffed."""
    import shutil

    def which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name == "xclip" else None

    monkeypatch.setattr(shutil, "which", which)

    class _FakeProc:
        returncode = 0
        async def communicate(self) -> tuple[bytes, bytes]:
            return PNG_HEADER + b"png-payload", b""

    async def _fake_exec(*_, **__) -> _FakeProc:
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    image = await capture_clipboard_image()
    assert image is not None
    assert image.mime == "image/png"
    assert b"png-payload" in image.data


@pytest.mark.asyncio
async def test_capture_clipboard_image_skips_non_image_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pbpaste returning text shouldn't be misinterpreted as an
    image. We sniff + reject."""
    import shutil

    def which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name == "pbpaste" else None

    monkeypatch.setattr(shutil, "which", which)

    class _FakeProc:
        returncode = 0
        async def communicate(self) -> tuple[bytes, bytes]:
            return b"hello clipboard text", b""

    async def _fake_exec(*_, **__) -> _FakeProc:
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    image = await capture_clipboard_image()
    assert image is None


@pytest.mark.asyncio
async def test_capture_clipboard_image_handles_subprocess_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """xclip exits non-zero → fall through to next candidate (or
    return None if no more)."""
    import shutil

    def which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name == "xclip" else None

    monkeypatch.setattr(shutil, "which", which)

    class _FailedProc:
        returncode = 1
        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b"xclip: clipboard empty"

    async def _fake_exec(*_, **__) -> _FailedProc:
        return _FailedProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

    image = await capture_clipboard_image()
    assert image is None
