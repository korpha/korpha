"""Clipboard image capture + terminal inline-render detection.

Mike pastes a screenshot of a competitor's pricing page; the TUI
captures the clipboard image, saves it to ``~/.korpha/images/``,
and surfaces it inline if the terminal supports a graphics protocol.

Terminals that work inline (best → worst):

  - **Kitty**: kitty-graphics protocol (base64 PNG over CSI). We
    detect via ``KITTY_WINDOW_ID`` or ``TERM=xterm-kitty``.
  - **iTerm2 (macOS)**: inline images via ``\033]1337;File=...``.
    Detect via ``TERM_PROGRAM=iTerm.app``.
  - **WezTerm**: also supports iTerm2's protocol. Same env detect.
  - **Sixel** terminals (xterm with sixel, mlterm, foot): CSI/Sixel
    queries — too fiddly to detect reliably; we skip for v1.
  - **Everything else** (most SSH sessions, plain xterm,
    GNOME Terminal, etc.): no inline render. We save the file and
    render a path link the user can open with their host viewer.

Design choices:
  - ``capture_clipboard_image`` returns bytes + a guess at format.
    No PIL dep — we trust the clipboard binary's output is already
    PNG / JPEG. Mime sniff via the magic bytes.
  - ``inline_render_protocol()`` returns the protocol name (or
    None) so the TUI can pick the right escape sequence.
  - All escape-sequence emission happens in this module so the
    main app stays clean.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Clipboard capture
# ---------------------------------------------------------------------------


@dataclass
class ClipboardImage:
    """Captured clipboard image. ``data`` is raw PNG/JPEG bytes;
    ``mime`` is ``"image/png"`` / ``"image/jpeg"`` / ``"image/gif"``
    derived from the magic bytes."""

    data: bytes
    mime: str

    @property
    def extension(self) -> str:
        return {
            "image/png": "png",
            "image/jpeg": "jpg",
            "image/gif": "gif",
        }.get(self.mime, "bin")


def _sniff_mime(data: bytes) -> str:
    """Magic-byte sniff — fast, no PIL dep. Returns
    ``"image/png"`` / ``"image/jpeg"`` / ``"image/gif"`` /
    ``"application/octet-stream"`` for unknown."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "application/octet-stream"


async def capture_clipboard_image() -> ClipboardImage | None:
    """Read the system clipboard's current image, if any.

    Tries (in order): ``pbpaste`` (macOS), ``wl-paste`` (Wayland),
    ``xclip`` (X11), ``xsel`` (X11 fallback). Returns ``None`` if no
    binary is available or clipboard has no image.

    The implementations are all subprocess calls — async-safe via
    ``asyncio.create_subprocess_exec``. Each candidate has its own
    flag set so we can request "give me the image MIME types only".
    """
    candidates: list[tuple[list[str], str | None]] = []

    if shutil.which("pbpaste"):
        # macOS pbpaste reads anything; no image-specific flag, but
        # it returns image data when an image is on the pasteboard.
        # We can't request "image only" cleanly — we read raw +
        # sniff. ``-Prefer png`` exists on newer macOS but not all.
        candidates.append((["pbpaste"], None))
    if shutil.which("wl-paste"):
        # Wayland: list types, then pick image/png if present.
        candidates.append((
            ["wl-paste", "--type", "image/png"], "image/png",
        ))
    if shutil.which("xclip"):
        candidates.append((
            ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
            "image/png",
        ))
    if shutil.which("xsel"):
        # xsel doesn't speak MIME — it's text-only by default. Skip
        # for image use; xsel-paste-image wrapper would be needed.
        pass

    for cmd, expected_mime in candidates:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
        except (TimeoutError, FileNotFoundError, OSError):
            continue
        if proc.returncode != 0 or not stdout:
            continue
        mime = expected_mime or _sniff_mime(stdout)
        if not mime.startswith("image/"):
            # The binary returned non-image bytes (e.g. pbpaste with
            # text on the clipboard). Skip — try the next candidate.
            continue
        return ClipboardImage(data=stdout, mime=mime)

    return None


# ---------------------------------------------------------------------------
# Persistent storage
# ---------------------------------------------------------------------------


def _images_dir() -> Path:
    base = os.getenv("KORPHA_DATA_DIR")
    return (Path(base) if base else Path.home() / ".korpha") / "images"


def save_image(image: ClipboardImage) -> Path:
    """Write the clipboard bytes to ``~/.korpha/images/<ts>.<ext>``.
    Returns the absolute path. Caller is responsible for surfacing
    the path to the founder + posting it to the agent as an
    attachment."""
    from datetime import UTC, datetime

    out_dir = _images_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")[:21]
    path = out_dir / f"clipboard-{stamp}.{image.extension}"
    path.write_bytes(image.data)
    return path


# ---------------------------------------------------------------------------
# Inline-render protocol detection + emission
# ---------------------------------------------------------------------------


def inline_render_protocol() -> str | None:
    """Sniff the terminal we're running in. Returns one of:

      ``"kitty"``   — kitty-graphics protocol available
      ``"iterm2"``  — iTerm2-style inline images (also WezTerm)
      ``None``      — fall back to a path link

    Detection is env-var based, no escape-sequence probing. False
    negatives (Kitty without ``KITTY_WINDOW_ID``) just downgrade to
    a path link, which is the safe default.
    """
    if os.getenv("KITTY_WINDOW_ID") or os.getenv("TERM") == "xterm-kitty":
        return "kitty"
    term_program = os.getenv("TERM_PROGRAM", "")
    if term_program in ("iTerm.app", "WezTerm"):
        return "iterm2"
    return None


def kitty_inline_escape(image: ClipboardImage) -> str:
    """Emit the kitty-graphics escape sequence for a single image.

    Action ``T`` = transmit + display in-place. Format ``32`` = PNG,
    ``f=100`` = format=100 means raw PNG; we send the bytes
    base64-encoded in 4 KB chunks per the protocol spec.
    """
    if image.mime != "image/png":
        # Kitty wants PNG; if we got JPEG, falling back to a link is
        # cheaper than re-encoding. Caller checks this case via
        # inline_render_supported(image).
        return ""
    payload = base64.b64encode(image.data).decode("ascii")
    chunk_size = 4096
    out: list[str] = []
    chunks = [
        payload[i : i + chunk_size]
        for i in range(0, len(payload), chunk_size)
    ]
    if not chunks:
        return ""
    for i, chunk in enumerate(chunks):
        more = 1 if i < len(chunks) - 1 else 0
        if i == 0:
            out.append(f"\x1b_Ga=T,f=100,m={more};{chunk}\x1b\\")
        else:
            out.append(f"\x1b_Gm={more};{chunk}\x1b\\")
    return "".join(out)


def iterm2_inline_escape(image: ClipboardImage) -> str:
    """iTerm2 / WezTerm inline image escape. Single OSC 1337 frame
    with the full image base64-encoded. Filename is optional but
    helps when the user does ⌘+click → reveal."""
    payload = base64.b64encode(image.data).decode("ascii")
    return (
        f"\x1b]1337;File=inline=1;preserveAspectRatio=1:"
        f"{payload}\x07"
    )


def inline_render_supported(image: ClipboardImage) -> bool:
    """True if the current terminal can render this specific image
    inline. False = caller should write a path link instead."""
    proto = inline_render_protocol()
    if proto == "kitty":
        return image.mime == "image/png"
    if proto == "iterm2":
        return image.mime in ("image/png", "image/jpeg", "image/gif")
    return False


def inline_render_escape(image: ClipboardImage) -> str:
    """Single dispatch for whichever protocol matched. Returns ``""``
    if no protocol available — caller should fall back to a link."""
    proto = inline_render_protocol()
    if proto == "kitty":
        return kitty_inline_escape(image)
    if proto == "iterm2":
        return iterm2_inline_escape(image)
    return ""


__all__ = [
    "ClipboardImage",
    "capture_clipboard_image",
    "inline_render_escape",
    "inline_render_protocol",
    "inline_render_supported",
    "save_image",
]
