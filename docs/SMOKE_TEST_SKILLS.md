# Smoke-test the ~/.claude/skills/ infrastructure

Repeatable methodology to verify every Claude Code skill installed
under `~/.claude/skills/` is functional end-to-end. Run after:

- Installing or updating any skill bundle
- A fresh dev-box bootstrap
- Adding a new mesh-* skill from the Vidoyo GPU Mesh
- Any change to `~/.bashrc` env-var loading (e.g. `GPU_MESH_API_KEY`)

Target wall time: **~10 min** (most skills run in seconds; LTX + WAN
+ codex-image take 60-90s each and run in parallel).

---

## Prerequisites

```bash
# Key file for Vidoyo mesh skills
ls ~/.claude/vidoyo-mesh.key       # must be readable, mode 600
echo "$GPU_MESH_API_KEY" | head -c 1 # must be non-empty (from key file via ~/.bashrc)

# Python deps
python3 -c "import pptx, num2words"  # webinar-builder prereqs

# System tools
which ffmpeg node npm                # required for video pipelines + browse CLI
```

If `GPU_MESH_API_KEY` is empty, run:
```bash
source ~/.bashrc
```

If `~/.bashrc` doesn't auto-load it, add the bridge:
```bash
cat >> ~/.bashrc <<'EOF'
if [ -r ~/.claude/vidoyo-mesh.key ]; then
    export GPU_MESH_API_KEY="$(cat ~/.claude/vidoyo-mesh.key)"
fi
EOF
```

---

## The 10 tests, in order

```bash
mkdir -p /tmp/skill-smoke && cd /tmp/skill-smoke
```

### 1. mesh-kokoro — fast TTS

```bash
python3 ~/.claude/skills/mesh-kokoro/tts.py \
    --out kokoro.wav --voice af_sarah \
    "Hello from Operator Console smoke test."
ls -la kokoro.wav   # expect ~200KB+, several seconds
```
Expect: `MESH_OK kokoro -> kokoro.wav (N bytes, Ns @ 24000Hz, voice=af_sarah)`

### 2. mesh-whisper — direct transcription (depends on test 1's output)

```bash
python3 ~/.claude/skills/mesh-whisper/transcribe.py \
    --file kokoro.wav --json mesh-whisper-out.json
python3 -c "import json; d=json.load(open('mesh-whisper-out.json')); \
    print(d['text']); print('words[]?', any('words' in s for s in d['segments']))"
```
Expect: transcribed text matches input; `words[]? True` (word-level
timestamps required for spokesperson-vsl-build).

### 3. whisper-gpu shim — proves shim → mesh-whisper bridge works

```bash
bash ~/.claude/skills/whisper-gpu/scripts/transcribe.sh \
    --input kokoro.wav --output shim-out.json
```
Expect: stderr shows `[whisper-gpu] backend=mesh (via mesh-whisper shim)`;
JSON output identical to test 2.

### 4. mesh-omnivoice — advanced TTS (random voice mode)

```bash
python3 ~/.claude/skills/mesh-omnivoice/omnivoice.py \
    auto --out omnivoice.wav "Smoke testing OmniVoice."
```
Expect: `MESH_OK omnivoice/auto -> omnivoice.wav (N bytes, Ns)`

### 5. mesh-rembg — background cutout — ⚠️ CDN matters

```bash
python3 ~/.claude/skills/mesh-rembg/rembg.py \
    --out rembg.png "https://picsum.photos/seed/smoketest/640/480"
file rembg.png   # expect "PNG image data, 640 x 480, 8-bit/color RGBA"
```
**Gotcha**: Wikipedia commons URLs return 403 to the mesh server's
user-agent. **Use bot-friendly CDNs**:
- ✅ `picsum.photos` (random photos)
- ✅ `images.unsplash.com`
- ✅ Raw GitHub URLs
- ❌ `upload.wikimedia.org` (blocks non-browser clients)

### 6. mesh-ltx — text-to-video

```bash
python3 ~/.claude/skills/mesh-ltx/ltx.py generate \
    --out ltx.mp4 --duration 4 --resolution 720p --aspect 16:9 \
    "calm ocean wave at sunset, slow motion, golden hour" &
LTX_PID=$!
```
Run in background (60-90s wall). Output: ~800KB MP4.

### 7. mesh-wan — image-to-video (depends on test 5's output)

```bash
python3 ~/.claude/skills/mesh-wan/wan.py generate \
    --image rembg.png --out wan.mp4 --num-frames 81 \
    "slow zoom in, cinematic" &
WAN_PID=$!
```
Run in background (90-120s wall). Output: ~2-3MB MP4.

### 8. webinar-builder — python-pptx round-trip + reference impl syntax

```bash
python3 -c "
from pptx import Presentation
prs = Presentation()
slide = prs.slides.add_slide(prs.slide_layouts[5])
slide.shapes.title.text = 'Smoke Test'
slide.notes_slide.notes_text_frame.text = 'Speaker note voiceover for HeyGen.'
prs.save('webinar-smoke.pptx')
print('python-pptx OK')
"

# Reference impl must parse clean (no syntax errors):
for f in build_webinar_deck split_parts render_audio; do
  python3 -c "import py_compile; py_compile.compile(\
    '$HOME/.claude/skills/webinar-builder/reference/$f.py', \
    doraise=True); print('$f.py: parses')"
done
```
Expect: all four lines succeed. `webinar-smoke.pptx` is ~33KB.

### 9. spokesperson-vsl-build — structural integrity

```bash
ls ~/.claude/skills/spokesperson-vsl-build/
ls ~/.claude/skills/spokesperson-vsl-build/reference/rules/ | wc -l
```
Expect: `SKILL.md`, `reference/` dir present; rules dir has **10** files
(matching the skill's documented locked-rule count).

Full end-to-end test of this skill requires a real webcam MP4 input;
it's deferred to actual use rather than smoke-tested.

### 10. codex-image — generate test PNG — ⚠️ PROMPT PHRASING MATTERS

```bash
cd /home/code4 && \
codex exec --sandbox workspace-write --skip-git-repo-check \
    "Generate an image: a single black coffee mug on a clean white desk, \
top-down view, minimalist composition, soft natural lighting, size 1024x1024. \
Use your built-in image_generation tool. \
Do NOT use the imagegen skill or any python script — use the \
image_generation tool that comes built into your runtime." \
    < /dev/null > /tmp/skill-smoke/codex-image.log 2>&1 &
CODEX_PID=$!

# Wait for codex exec to exit (60-120s typical)
wait $CODEX_PID

# Recover the generated PNG from the session-id-keyed dir:
SID=$(grep -oE 'session id: [0-9a-f-]+' /tmp/skill-smoke/codex-image.log | head -1 | awk '{print $3}')
cp ~/.codex/generated_images/$SID/ig_*.png /tmp/skill-smoke/codex-image.png
file /tmp/skill-smoke/codex-image.png   # expect "PNG image data"
```
**Gotcha**: Codex CLI has TWO things named "imagegen":
1. The **built-in `image_generation` tool** (what we want — uses
   ChatGPT login, no API key)
2. The **`imagegen` skill / python script** (requires `OPENAI_API_KEY`)

Without the explicit "Do NOT use the imagegen skill" clause, Codex
routes to (2) and fails silently because `OPENAI_API_KEY` is unset.
Always include the disambiguation in the prompt.

---

## Wait for backgrounded jobs + collect results

```bash
wait $LTX_PID $WAN_PID
ls -la /tmp/skill-smoke/
```

Expect this inventory (sizes approximate):

| File | Bytes | Notes |
|---|---|---|
| kokoro.wav | ~234K | TTS output |
| mesh-whisper-out.json | ~2K | with `words[]` |
| shim-out.json | ~2K | identical |
| omnivoice.wav | ~96K | random voice |
| rembg.png | ~234K | RGBA cutout |
| ltx.mp4 | ~800K | 4s 720p MP4 |
| wan.mp4 | ~2-3M | 5s MP4 |
| webinar-smoke.pptx | ~33K | with speaker notes |
| codex-image.png | ~1-2M | 1024×1024 PNG |

---

## What it doesn't cover (and why)

- **spokesperson-vsl-build full pipeline**: requires a real webcam MP4
  and produces a multi-section composed VSL. Skip for smoke-test;
  exercise during actual VSL build.
- **webinar-builder full pipeline**: requires writing narration +
  uploading to HeyGen. Smoke-test verifies prereqs + reference-impl
  parses; full build is a per-project task.
- **codex-image edits via `-i` flag**: only text-to-image tested.
  Edit/reference mode (with `-i source.png`) is verified on a real
  edit task, not smoke-tested.

---

## When to re-run

- After any `~/.claude/skills/` bundle install or update
- After `apt upgrade` (system tools may move)
- After Node major upgrade (`browse` CLI install dependency)
- After Python version bump
- When debugging a skill that "used to work"
- Before any session that depends on the production pipeline
  (course/copy/main writing real assets)

Total time ~10 min. Pays itself back the first time a silent skill
regression would have wasted an hour.
