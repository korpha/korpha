# Backup & restore

Mike never loses his business. Korpha ships backup-by-default —
nothing to configure, nothing to remember.

## What gets backed up

| Artifact | Where | Why it matters |
|---|---|---|
| `korpha.db` | data dir | every BusinessUnit, kanban card, memory entry, approval, cost log |
| `secrets/master.key` | data dir | without it the credentials vault is unreadable |
| `secrets/vault.json.enc` | data dir | encrypted API keys (Stripe / Resend / OpenAI / etc.) |
| `providers.yaml` | data dir | inference provider config |
| `skills/` | data dir | agent-authored skills |
| `checkpoints/` | data dir | workspace checkpoints from Codex runs |
| `cron-scripts/` | data dir | scripts agents authored for scheduled tasks |
| `deploys/` | data dir | deployed sites |
| `calendar/` | data dir | generated ICS files |

Logs are skipped (regenerable, dominate disk usage).

## Layer 1 — Local rotating snapshots (built-in, on by default)

Hourly `sqlite3 .backup` of the DB + daily tar.gz of the whole data
dir + GFS retention (keep 24 hourly / 7 daily / 4 weekly / 12 monthly).

**One-shot install:**

```
korpha cron add-backup-snapshot
```

That's it. From now on you have hourly DB snapshots and a daily full
bundle, auto-pruned so the backup dir stays bounded.

**Browse + restore:**

- Dashboard: <http://127.0.0.1:8765/app/backups>
- CLI: `korpha backups list` / `korpha backups restore <name>`

A safety copy of the current DB is auto-saved before any restore so
a botched restore is itself recoverable.

### What Layer 1 protects against
- Accidental delete (you ran `rm -rf` or an agent issued a bad skill call)
- Bad schema migration (a new release destroys data)
- SQLite corruption (power loss, concurrent writer)

### What Layer 1 does NOT protect against
- **Disk death** — laptop dies, all backups die with it
- Theft / fire / ransomware
- House fire

For those, add Layer 2.

## Layer 2 — Off-disk push (opt-in, takes ~2 minutes)

Three options. Pick one — they all give you disk-death protection.

### Option A — Litestream → S3-compatible (recommended)

[Litestream](https://litestream.io/) is a free open-source tool that
continuously streams SQLite WAL changes to S3 / Cloudflare R2 /
Backblaze B2 / MinIO. It gives you **point-in-time restore** (roll
back to any second), zero data loss, and battle-tested
production reliability.

```
# install litestream (one-time)
curl -fsSL https://github.com/benbjohnson/litestream/releases/latest/download/litestream-linux-amd64.tar.gz \
  | sudo tar -C /usr/local/bin -xzf -

# configure (the wizard puts your S3 creds in the encrypted vault)
korpha backups setup-litestream
```

Recommended providers (cheapest first):
- **Cloudflare R2** — $0.015/GB/mo, zero egress fees
- **Backblaze B2** — $0.005/GB/mo
- **AWS S3** — works fine, more expensive

### Option B — Rclone → Dropbox / Google Drive / OneDrive

Reuses storage you already pay for.

```
# install rclone (one-time)
sudo apt install rclone

# authorize the cloud you use
rclone config         # follow prompts to add "dropbox", "gdrive", etc.

# wire it into the backup cron
korpha backups setup-rclone --remote dropbox:korpha-backup
```

The cron will push every snapshot + the daily bundle to that remote.

### Option C — Korpha Cloud Backup (premium tier — future)

When Korpha SaaS ships, the premium tier includes managed off-disk
backup with no setup. Your data stays encrypted with your master key;
even Korpha Cloud staff can't read it.

## Recovery — "my laptop died"

1. Install Korpha on the new machine (`pip install korpha` or
   `git clone` + `pip install -e .`).
2. Run `korpha init` to bootstrap the new data dir.
3. Pull your latest full bundle from off-disk storage:
   - Litestream: `litestream restore -o ~/.korpha/korpha.db s3://bucket/db`
   - Rclone: `rclone copy dropbox:korpha-backup/latest.tar.gz ~/`
4. Extract the bundle on top of the fresh data dir:
   `tar -xzf latest.tar.gz -C ~/.korpha/ --overwrite`
5. `korpha server` — your business resumes from the last backup
   timestamp.

## Recovery — "I just deleted something by accident"

1. Find the snapshot you want: `korpha backups list`
2. Stop the server (Ctrl-C or `pkill -f 'korpha server'`).
3. Restore: `korpha backups restore db-<timestamp>.sqlite`
4. Restart: `korpha server`

The pre-restore DB is kept at `~/.korpha/korpha.db.before-restore.<ts>`
in case the restore itself was a mistake.

## Verifying your backups work

Once a month, do a fire drill:

```
# Take a snapshot
korpha backups snapshot

# In a scratch dir, restore it as if your disk died
mkdir /tmp/restore-test
KORPHA_DATA_DIR=/tmp/restore-test korpha init --email test@x.com --name x --business x
tar -xzf ~/.korpha/backups/full/full-<latest>.tar.gz -C /tmp/restore-test/ --overwrite

# Boot it
KORPHA_DATA_DIR=/tmp/restore-test korpha server --port 9999

# Confirm your business is there at http://127.0.0.1:9999/app/units
# Then nuke the scratch dir.
rm -rf /tmp/restore-test
```

If anything fails, fix it BEFORE you actually need it.
