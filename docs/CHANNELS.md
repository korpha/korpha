# Channels — talk to your cofounder from Telegram, Discord, or email

**Audience**: anyone who wants Korpha outside the dashboard — on
their phone, in their team Discord, in their email inbox.

The dashboard at `localhost:8765` is the canonical surface, but
every channel adapter lets the CEO + Director conversations happen
on whatever platform you live on. Approvals route there; replies
come back; one persistent CEO voice across all of them.

---

## Available channels

| Channel | What it gives you | What you need |
| --- | --- | --- |
| **Telegram** | DM CEO from your phone; approval buttons inline | Telegram bot token (free, from @BotFather) |
| **Discord** | One bot, optionally per-C-suite channels (#ceo, #cto, ...) | Discord application + bot token |
| **Email outbound** | Daily / weekly digest of pending approvals + KPIs | Resend API key + verified sending domain |
| **Email inbound** | Reply to digest = approval/discussion goes back into the system | IMAP creds OR a Resend inbound webhook (planned) |

---

## Telegram

**Best for**: phone-first solopreneurs who want to approve cold
emails between meetings.

### Setup

1. **Get a bot token**:
   - Telegram → message @BotFather
   - `/newbot`
   - pick a name (e.g. "Korpha Bot") and username (must end in `bot`)
   - Copy the token it returns (looks like `123456789:ABCdefGHI...`)

2. **Add to Korpha**:
   ```bash
   echo 'TELEGRAM_BOT_TOKEN=123456789:ABC...' >> ~/.korpha/.env
   ```

3. **(optional) Restrict to your chat ID**:
   - Message your bot once (anything)
   - Visit `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser
   - Find your chat `id` (a number) and set:
   ```bash
   echo 'TELEGRAM_ALLOWED_CHAT_IDS=12345678' >> ~/.korpha/.env
   ```
   Without this, anyone who finds your bot's username can DM the CEO.

4. **Run the channel**:
   ```bash
   korpha channel-run telegram
   ```
   Leave it running (use `tmux` / `systemd` / nohup for persistence).
   The CEO now answers DMs to your bot.

### How it feels

- DM the bot — gets piped to CEO same as the dashboard chat
- Approvals appear inline with `Approve / Deny / Discuss` buttons
- Voice messages auto-transcribe (if you have a STT provider)
- Bot replies stream tokens as the model generates

### Troubleshooting

- **Bot doesn't reply** → confirm `korpha channel-run telegram`
  is running. Check the logs for `401 Unauthorized` (bad token) or
  `409 Conflict` (another instance polling the same bot — only one
  should be live).
- **Other people can DM your bot** → you didn't set
  `TELEGRAM_ALLOWED_CHAT_IDS`. Add it.

---

## Discord

**Best for**: small teams where the cofounder needs to talk to more
than one human.

### Setup

1. **Create a Discord app**:
   - https://discord.com/developers/applications → "New Application"
   - Bot tab → "Reset Token" → copy the token
   - "Privileged Gateway Intents" → enable **MESSAGE CONTENT INTENT**

2. **Invite to your server**:
   - OAuth2 → URL Generator → scopes: `bot`, `applications.commands`
   - Permissions: `Send Messages`, `Read Message History`, `Embed Links`
   - Open the generated URL in your browser, authorize for your server

3. **Add to Korpha**:
   ```bash
   echo 'DISCORD_BOT_TOKEN=...' >> ~/.korpha/.env
   ```

4. **(optional) Per-C-suite channels**: create channels named `#ceo`,
   `#cto`, `#cmo`, `#coo`. Messages in `#cmo` route to the CMO
   Director directly, etc. If you only have one channel, all
   conversations go to the CEO and they delegate from there.

5. **Run**:
   ```bash
   korpha channel-run discord
   ```

### How it feels

- @mention the bot in any channel → routed to the appropriate role
- Slash commands: `/approve <id>`, `/deny <id>`, `/skills`,
  `/blockers`
- Threads — long discussions create a thread per topic; CEO follows up
- Approvals appear as embeds with reaction-button approve/deny

---

## Email outbound (digest)

**Best for**: anyone who wants Korpha to surface "here's what
needs your attention today" without you opening the dashboard.

### Setup

1. **Resend account**:
   - https://resend.com → sign up
   - Add + verify a domain (DKIM / SPF / DMARC records)
   - Generate an API key

2. **Add to Korpha**:
   ```bash
   echo 'RESEND_API_KEY=re_...' >> ~/.korpha/.env
   echo 'RESEND_FROM_EMAIL=cofounder@yourdomain.com' >> ~/.korpha/.env
   echo 'RESEND_FROM_NAME=Korpha Cofounder' >> ~/.korpha/.env
   ```

3. **Test send**:
   ```bash
   korpha email-test --to you@example.com
   ```

4. **Schedule the digest**:
   - Default cadence: daily at 8am local time + weekly on Monday
   - Configure in `~/.korpha/routines.yaml` (see
     [`ROUTINES.md`](ROUTINES.md))

### What's in the digest

- Pending approvals count + top 3 with one-line summaries
- Yesterday's spend + week-to-date
- Top 1-2 KPI movements
- Chief of Staff blocker digest (deduped)
- One-click links back to the dashboard

### Manual digest send

```bash
korpha email-digest --to you@example.com
```

---

## Email inbound (reply parsing)

**Status**: planned. The infrastructure is there (channel framework
+ skill router), but the IMAP fetch + Resend inbound-webhook adapter
hasn't shipped yet. Tracking in NEXT_STEPS Phase 2.

For now: digest replies come back to your inbox, you click the
"open in dashboard" link, act there.

---

## Channel + approval interaction

When CEO produces a side-effect skill (cold email, payment link,
code change) it ALWAYS produces an `Approval` first. That approval
then surfaces on:

- The dashboard `/app/approvals` page (always)
- The currently-live channels you're connected to (if Telegram /
  Discord are running)
- Your next email digest (if email outbound is configured)

You can approve from any of them. First-to-decide wins; the others
show the action as already-decided when refreshed.

---

## Reference

- Channel adapter framework: [`korpha/channels/`](../korpha/channels/)
- Telegram adapter: [`korpha/channels/telegram.py`](../korpha/channels/telegram.py)
- Discord adapter: [`korpha/channels/discord.py`](../korpha/channels/discord.py)
- Email notifier: [`korpha/notifications/`](../korpha/notifications/)
- Run any channel: `korpha channel-run <telegram|discord>`
- Test email: `korpha email-test --to <addr>`
- Send digest: `korpha email-digest --to <addr>`
