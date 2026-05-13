"""Codex CLI image-gen provider.

Subscription-paid via ChatGPT Plus / Pro / Max. Currently the best
image model on the market (gpt-image-2 routed via Codex). Stays as one
of four options — never the only one.

Mechanism: ask Codex to render an image; Codex's built-in imagegen skill
saves the file to ``~/.codex/generated_images/<thread_id>/ig_*.png``;
we glob the most recent file from that thread's dir.
"""
from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from korpha.imagery.provider import (
    ImageGenProvider,
    ImageGenRequest,
    ImageGenResult,
)

_GENERATED_IMAGES_DIR = Path.home() / ".codex" / "generated_images"
_DEFAULT_TIMEOUT = 240.0


@dataclass
class CodexCLIImageProvider(ImageGenProvider):
    name: str = "codex-cli-image"
    binary: str = "codex"
    """Override for tests."""

    async def generate(self, request: ImageGenRequest) -> ImageGenResult:
        if shutil.which(self.binary) is None:
            return ImageGenResult(
                success=False,
                image_paths=[],
                error=(
                    f"{self.binary!r} not on PATH. Install Codex "
                    "(npm install -g @openai/codex) and run `codex login`, "
                    "or pick a different image-gen provider via "
                    "`korpha config`."
                ),
            )

        prompt = request.prompt
        if request.style_hint:
            prompt = f"{prompt}\nStyle: {request.style_hint}"
        if request.negative_prompt:
            prompt = f"{prompt}\nAvoid: {request.negative_prompt}"
        prompt = (
            f"Generate an image: {prompt}\n"
            f"Size: {request.width}x{request.height}.\n"
            "Reply with the file name only."
        )

        argv = [
            self.binary, "exec", "--json", "--skip-git-repo-check",
            "-s", "read-only", "-",
        ]
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(prompt.encode("utf-8")),
                timeout=_DEFAULT_TIMEOUT,
            )
        except TimeoutError:
            proc.kill()
            return ImageGenResult(
                success=False, image_paths=[],
                error=f"Codex timed out after {_DEFAULT_TIMEOUT}s",
            )

        thread_id = ""
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "thread.started":
                thread_id = str(event.get("thread_id") or "")
                break

        if not thread_id:
            return ImageGenResult(
                success=False, image_paths=[],
                error=(
                    "Codex didn't emit a thread.started event. "
                    + stderr.decode("utf-8", errors="replace")[:300]
                ),
            )

        thread_dir = _GENERATED_IMAGES_DIR / thread_id
        candidates = (
            sorted(
                thread_dir.glob("ig_*.png"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if thread_dir.exists()
            else []
        )
        if not candidates:
            return ImageGenResult(
                success=False, image_paths=[],
                error=f"Codex completed but no PNG was written to {thread_dir}",
            )

        wanted = candidates[: max(1, request.num_images)]
        final_paths = list(wanted)
        if request.save_to is not None:
            dest = Path(request.save_to).expanduser()
            if dest.suffix:
                # Single-file destination (only the first image goes there).
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(wanted[0], dest)
                final_paths = [dest]
            else:
                # Directory — copy all results in.
                dest.mkdir(parents=True, exist_ok=True)
                final_paths = []
                for src in wanted:
                    target = dest / src.name
                    shutil.copy2(src, target)
                    final_paths.append(target)

        return ImageGenResult(
            success=True,
            image_paths=final_paths,
            model_used="codex/gpt-image-2",
            cost_usd=0.0,  # subscription-paid
            raw={"thread_id": thread_id},
        )


__all__ = ["CodexCLIImageProvider"]
