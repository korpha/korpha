# Multi-session recovery — restore the dev orchestra after reset

This project runs four coordinated Claude Code sessions to dogfood
Korpha: **main** (orchestrator + code), **marketro** (drives the live
Marketro install on `127.0.0.1:9000`), **andrew** (drives the Andrew
install on the Ubuntu mini-PC at `192.168.0.52:8765`), and **bugs**
(fixes defects surfaced by the drivers). They coordinate via file-
based inboxes at `~/.claude/inboxes/*.md` and a `relay-send` script.

After a computer reboot, a Claude Code restart, or any unplanned
session loss, here's how to bring the orchestra back up.

---

## What survives a reset (no action needed)

These live on disk and persist across reboots:

- `~/.claude/inboxes/{main,marketro,andrew,bugs}.md` — message history
- `~/.claude/bin/relay-send` — sender script (PATH is wired in `.bashrc`)
- `~/.ssh/id_ed25519_korpha_andrew` + `~/.ssh/config` (alias `andrew`)
- `~/.korpha/` — Marketro's data dir (providers.yaml, sqlite, etc.)
- `/home/code4/marketro/` — Marketro install directory
- Git worktrees: `aigenteur_agent`, `aigenteur_agent-marketro`,
  `aigenteur_agent-andrew`, `aigenteur_agent-bugs`
- `CLAUDE.md` (relay protocol description) committed in main repo
- Memory notes under
  `~/.claude/projects/-home-code4-aigenteur-agent/memory/`
- Marketro server on `:9000` — auto-restarts via... actually no, see
  troubleshooting below. The Marketro server PID 246818 we started
  manually does NOT have systemd. After a reboot you need to bring it
  back up by hand (or write a systemd unit — task to add).
- Andrew install on the mini-PC — IS under systemd user unit
  `korpha.service` with Linger=yes; comes back automatically across
  reboots of the mini-PC. The dev box reboot doesn't touch it.

## What does NOT survive (needs rebootstrap)

- The four Claude Code session windows themselves
- The `tail -F` Monitors armed in each session
- Whatever in-memory context each session had (this is what the
  bootstrap snippets re-establish from disk)

---

## Step-by-step recovery procedure

### 0. Verify infrastructure first (one minute)

Before spawning any Claude Code sessions, sanity-check the moving
pieces from a regular terminal:

```bash
# Marketro live install — check if the :9000 server is alive
curl -s --max-time 3 -w "\n" http://127.0.0.1:9000/healthz || echo "Marketro :9000 DOWN — restart needed (see troubleshooting)"

# Andrew install on the mini-PC — check reachability
curl -s --max-time 3 -w "\n" http://192.168.0.52:8765/healthz || echo "Andrew dashboard unreachable — check mini-PC + LAN"

# SSH to Andrew works key-only
ssh -o BatchMode=yes andrew 'echo "andrew ssh OK"' || echo "Andrew SSH broken — regenerate key + re-install"

# Inboxes intact
ls -la ~/.claude/inboxes/

# Relay script
which relay-send && echo "PATH OK" || echo "PATH missing — source ~/.bashrc"
```

If everything reports green, proceed to step 1. If Marketro :9000 is
down, see "Restart Marketro" in Troubleshooting.

### 1. Spawn the four Claude Code sessions

Open **four separate Claude Code windows**, one in each worktree:

| Session | Worktree path | Branch |
|---|---|---|
| main | `/home/code4/aigenteur_agent` | `main` |
| marketro | `/home/code4/aigenteur_agent-marketro` | `marketro-driver` |
| andrew | `/home/code4/aigenteur_agent-andrew` | `andrew-driver` |
| bugs | `/home/code4/aigenteur_agent-bugs` | `bugs-fixer` |

### 2. Paste the bootstrap snippet into each

**Bootstrap snippets are below in this doc.** Copy the one matching
each session, paste as the FIRST prompt in that session.

Order doesn't matter strictly, but **start with main** so it's ready
to receive "online" acks from the drivers as they come up.

### 3. Round-trip verification

From the main session, send a ping to each worker:

```
~/.claude/bin/relay-send marketro "post-recovery wire test" "Confirm online."
~/.claude/bin/relay-send andrew "post-recovery wire test" "Confirm online."
~/.claude/bin/relay-send bugs "post-recovery wire test" "Confirm online."
```

Each should reply within seconds. Main's Monitor should show the
acks as task-notifications. If any session doesn't respond → its
Monitor isn't armed or its bootstrap didn't complete; restart that
one.

---

## Bootstrap snippet — main session

Paste this as the first prompt in the main worktree's session:

```
You are the main session for the Korpha/AIgenteur project.

You are the orchestrator + code editor. Drivers (marketro, andrew) report bugs/observations to you; bugs session can absorb parallel code fixes when you're heads-down. You spawn / coordinate / make design calls.

Bootstrap (do these BEFORE doing real work):

1. Read CLAUDE.md in this worktree — it has the relay protocol + role descriptions for all four sessions.
2. Skim the auto-memory: cat ~/.claude/projects/-home-code4-aigenteur-agent/memory/MEMORY.md
3. Read docs/MULTI_SESSION_RECOVERY.md (this doc) to confirm you understand the post-reset playbook.
4. Arm your inbox listener via the Monitor tool:

   Monitor:
     command: tail -F ~/.claude/inboxes/main.md 2>/dev/null
     persistent: true
     timeout_ms: 3600000
     description: "main inbox relay"

5. Self-test: ~/.claude/bin/relay-send main "post-recovery self-test" "main bootstrap complete" — expect a task-notification within a second.
6. Probe state:
   - Local repo: git log --oneline -5
   - Marketro live: curl http://127.0.0.1:9000/healthz
   - Andrew remote: curl http://192.168.0.52:8765/healthz
7. Ping each worker for ack: relay-send marketro / andrew / bugs with subject "post-recovery wire test".

After all three workers ack, you're in normal operating mode.
```

## Bootstrap snippet — marketro session

```
You are the marketro session for the Korpha/AIgenteur project.

You drive the live Marketro install at http://127.0.0.1:9000 via Playwright. You do NOT edit code. You file bug repros to the bugs session via relay-send. Code mutations go through main (design) or bugs (defect fixes).

Memory rules that apply specifically to you (from ~/.claude/projects/-home-code4-aigenteur-agent/memory/):
- feedback_always_drive_ui_not_curl.md
- feedback_verify_ui_yourself.md
- feedback_no_live_data_test_fixtures.md
- feedback_user_is_mike_for_marketro.md
- reference_relay.md

Bootstrap:

1. Read CLAUDE.md in this worktree.
2. Skim ~/.claude/projects/-home-code4-aigenteur-agent/memory/MEMORY.md and read the rules listed above.
3. Arm your inbox listener:

   Monitor:
     command: tail -F ~/.claude/inboxes/marketro.md 2>/dev/null
     persistent: true
     timeout_ms: 3600000
     description: "marketro inbox relay"

4. Self-test: ~/.claude/bin/relay-send marketro "smoke" "self-test"
5. Health probe (read-only): curl -s -o /dev/null -w "Marketro :9000 → HTTP %{http_code}\n" http://127.0.0.1:9000/healthz
6. Tell main you're online:
   ~/.claude/bin/relay-send main "marketro online" "Driver session armed in worktree aigenteur_agent-marketro. Dashboard :9000 is <healthy/down>. Ready for tasks."

Then wait for task-notifications.
```

## Bootstrap snippet — andrew session

```
You are the andrew session for the Korpha/AIgenteur project.

You drive the Andrew install at http://192.168.0.52:8765 via Playwright (LAN reach). You do NOT edit code. You file bug repros to bugs. Andrew Darius is positioned as an independent coach/operator using Korpha — NEVER publicly the Korpha author (see feedback_andrew_not_korpha_author_publicly.md).

Memory rules that apply specifically to you:
- feedback_andrew_not_korpha_author_publicly.md (HARD rule — no I-built-this leakage in any Andrew content)
- project_andrew_identity.md (Andrew Darius, andrew@andrewdarius.com, andrewdarius.com domain)
- feedback_always_drive_ui_not_curl.md
- feedback_verify_ui_yourself.md
- reference_relay.md

Bootstrap:

1. Read CLAUDE.md in this worktree.
2. Skim ~/.claude/projects/-home-code4-aigenteur-agent/memory/MEMORY.md and read the rules listed above.
3. Arm your inbox listener:

   Monitor:
     command: tail -F ~/.claude/inboxes/andrew.md 2>/dev/null
     persistent: true
     timeout_ms: 3600000
     description: "andrew inbox relay"

4. Self-test: ~/.claude/bin/relay-send andrew "smoke" "self-test"
5. Health probe over LAN: curl -s -o /dev/null -w "Andrew :8765 → HTTP %{http_code}\n" http://192.168.0.52:8765/healthz
6. Tell main you're online:
   ~/.claude/bin/relay-send main "andrew online" "Driver session armed in worktree aigenteur_agent-andrew. Dashboard at 192.168.0.52:8765 is <healthy/down>. Ready for tasks."

Then wait for task-notifications.
```

## Bootstrap snippet — bugs session

```
You are the bugs session for the Korpha/AIgenteur project.

You fix bugs reported by the driver sessions (marketro, andrew). Workflow: receive repro → diagnose → fix → add/update test → commit to main branch from this worktree → push → ping reporting driver to re-verify. You do NOT design new features — escalate feature requests to main.

Quality bar: every fix gets a test (or a documented reason it can't), pytest green before commit, commit messages explain the fix not just the symptom.

Memory rules that apply specifically to you:
- feedback_always_fix_bugs_found_via_ui.md (don't defer — fix in-session)
- feedback_no_history_checkout_to_bisect.md (use git show / git diff, never git checkout <sha>)
- reference_relay.md

Bootstrap:

1. Read CLAUDE.md in this worktree.
2. Confirm env: uv run pytest tests/test_updater.py tests/test_migrate.py tests/test_social.py -q
3. Arm your inbox listener:

   Monitor:
     command: tail -F ~/.claude/inboxes/bugs.md 2>/dev/null
     persistent: true
     timeout_ms: 3600000
     description: "bugs inbox relay"

4. Self-test: ~/.claude/bin/relay-send bugs "smoke" "self-test"
5. Tell main you're online:
   ~/.claude/bin/relay-send main "bugs online" "Bug-fixer session armed in worktree aigenteur_agent-bugs on branch bugs-fixer. Test suite <pass/fail count>. Ready for repros."

Then wait for repros.
```

---

## Troubleshooting

### Marketro :9000 is down after reboot

The Marketro server isn't (yet) under systemd on this dev box —
it's a manually-spawned process at PID 246818 (or whatever it was)
that vanishes on reboot. Until we wire a systemd unit (TODO: task
to add), restart manually:

```bash
cd /home/code4/marketro
setsid nohup /home/code4/marketro/.venv/bin/korpha server --port 9000 </dev/null > /tmp/marketro-restart.log 2>&1 &
disown
sleep 5
curl -s http://127.0.0.1:9000/healthz
```

### Andrew dashboard unreachable

Different failure modes:

- **mini-PC powered off** — turn it on; systemd will start
  `korpha.service` automatically (Linger=yes).
- **LAN cable / wifi issue** — ping 192.168.0.52; if no response,
  check the network from the mini-PC side.
- **korpha.service crashed** — `ssh andrew 'systemctl --user
  status korpha; journalctl --user -u korpha -n 50 --no-pager'`.
  Restart with `ssh andrew 'systemctl --user restart korpha'`.

### `relay-send: command not found`

Either the PATH isn't loaded in this shell, or `.bashrc` got reset.

```bash
ls -la ~/.claude/bin/relay-send  # confirm script exists + executable
echo "$PATH" | grep -q "$HOME/.claude/bin" && echo "PATH OK" || \
    echo 'Need: export PATH="$HOME/.claude/bin:$PATH"'
```

If the script is missing entirely, restore from this repo's history
— it's not committed in the repo proper (it lives under `~/.claude/`)
but was originally generated by the relay-setup procedure documented
in `CLAUDE.md`. See `git log` for context; the script body is in
the conversation history of the session that built it.

### A worker's Monitor isn't firing

Means: `relay-send foo "..."` writes to `~/.claude/inboxes/foo.md`
but the foo session doesn't see a task-notification.

Causes:
- The Monitor wasn't armed during bootstrap (most common — the
  worker session skipped step 3 in its bootstrap).
- The Monitor's `persistent: true` was missing, and it timed out.
- The `tail -F` process died (rare).

Fix: in the worker session, re-arm the Monitor using the snippet
from this doc.

### Worktrees diverged from main

Drivers don't edit code, so their worktrees should stay aligned with
main via:

```bash
cd /home/code4/aigenteur_agent-<role>
git fetch origin main
git rebase origin/main
```

The bugs worktree may have its own commits on `bugs-fixer` that
need to be pushed to main; coordinate via relay before rebasing.

---

## Related references

- `CLAUDE.md` (project root) — the relay protocol description loaded
  into every fresh session
- `~/.claude/projects/-home-code4-aigenteur-agent/memory/reference_relay.md`
  — the memory note pointing at this whole system
- `~/.claude/projects/-home-code4-aigenteur-agent/memory/project_andrew_identity.md`
  — Andrew install location + SSH alias details
