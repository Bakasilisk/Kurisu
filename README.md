# Kurisutina Discord Bot

An all-in-one, self-hosted Discord bot built on `discord.py`: moderation, automated raid/spam
defense, full server surveillance logging, leveling, and a bits economy. Zero infrastructure —
no database, no external services; everything persists to JSON files next to the code.
Command prefix is `.`.

- [Features](#features):
  [Triggers](#triggers) ·
  [Moderation](#moderation) ·
  [Leveling](#leveling) ·
  [Economy](#economy) ·
  [Verification](#verification) ·
  [Watchdog](#watchdog) ·
  [Palantir](#palantir) ·
  [Captions](#captions) ·
  [AI Detect](#ai-detect) ·
  [Trace Anime](#trace-anime) ·
  [Help](#help) ·
  [Management](#management) ·
  [Logging & data files](#logging--data-files)
- [Setup Instructions](#setup-instructions)
- [Running as a Background Service (systemd)](#running-as-a-background-service-systemd)

## Features

Moderation, palantir, management, help, captions, aidetect, and trace commands are also available
as `/` slash commands with autocomplete descriptions; slash invocations reply ephemerally (visible
only to the invoker) while `.` invocations reply publicly — except captions, aidetect, and trace,
whose results always reply publicly regardless of invocation method. The other cogs are
prefix-only.

### Triggers

Auto-replies on every message:

- "kurisutina" anywhere in a message (case-insensitive): _Hör auf mich_ **_Kurisutina_** _zu nennen!_
- "horny" (case-insensitive): "`@user` ist Horny!"

| Command | Does |
|---|---|
| `nuko` | Posts a chain of nuko emotes with a random-length middle section |
| `füße` | Mentions a specific (hardcoded) user |

### Moderation

| Command | Does | Requires |
|---|---|---|
| `kick <member> [reason]` | Kick a member | Kick Members |
| `ban <member> [reason]` | Ban a member | Ban Members |
| `unban <user> [reason]` | Unban by ID or exact username | Ban Members |
| `timeout` / `mute <member> <duration> [reason]` | Time out (e.g. `10m`, `2h`, `1d`) | Moderate Members |
| `untimeout` / `unmute <member> [reason]` | Remove an active timeout | Moderate Members |
| `warn <member> [reason]` | Warn a member and record it | Moderate Members |
| `warnings` / `warnlist <member>` | List a member's warnings | Moderate Members |
| `clearwarnings <member>` | Clear a member's warnings | Moderate Members |
| `purge` / `clear <amount> [member]` | Bulk-delete messages | Manage Messages |
| `slowmode <seconds>` | Set the channel's slowmode | Manage Channels |
| `lock` / `unlock [reason]` | Block/restore @everyone sending in the channel | Manage Channels |
| `modlog` / `modlog set #channel` / `modlog disable` | Show/set/disable the mod-log channel | Manage Server |

- Each command requires its permission on both the caller and the bot, and enforces
  role-hierarchy checks so members can't act on others with an equal or higher role.
- Every action is recorded to `logs/kurisu.log` and to the mod-log channel if one is
  configured — so ephemeral slash actions are still traceable.
- `lock`/`unlock` snapshot each channel's exact pre-lock permission state (persisted, so it
  survives restarts) and restore what was there before, rather than blindly resetting it.

### Leveling

Members earn 15-25 XP per message (60s cooldown to prevent spam farming), with an
announcement on level-up. Per-member message counts reset monthly.

| Command | Does | Requires |
|---|---|---|
| `rank` / `level [member]` | Level, XP, server rank, and messages sent today | — |
| `leaderboard` / `lb` / `top [count]` | The server's top members | — |
| `resetxp <member>` | Clear a member's progress | Moderate Members |
| `setxp <member> <amount>` | Set a member's XP directly | Moderate Members |

### Economy

A simple bits currency, tracked per server.

| Command | Does | Requires |
|---|---|---|
| `payday` | Collect 120 bits, once every 12 hours; shows the new balance and server rank, or the time remaining if already claimed | — |
| `balance` / `bal [member]` | Check bits without claiming | — |
| `richest [count]` | The server's top bit holders | — |
| `give <member> <amount>` | Transfer bits to another member | — |
| `coinflip` / `cf <amount>` | Bet bits on a coin flip (10-1000 bits) | — |
| `setbits <member> <amount>` | Correct a member's balance | Moderate Members |

### Verification

Role-gated verification: members holding the configured "granter" role can hand out the
configured role with `verify` — no moderator permission needed. The bot needs
`Manage Roles`, with its top role above the granted role.

| Command | Does | Requires |
|---|---|---|
| `verify <member>` | Give the member the configured role | the granter role |
| `verification` | Show the current configuration | Manage Server |
| `verification granter <role>` | Set the role allowed to use `verify` | Manage Server |
| `verification target <role>` | Set the role `verify` assigns | Manage Server |

### Watchdog

Automated detection of raid/spam behavior, reacting faster than a human mod can (or when
none are online). Ships **in shadow mode by default**: it detects and alerts, but takes no
action until a mod runs `.watchdog mode active`. Detects:

- **Pattern A (sleeper raid/scam bursts):** an account posting in 4+ distinct channels within
  20 seconds while mentioning a role above a member-count threshold (auto-detected as
  "high-value" — no manual role list to maintain; `@everyone`/`@here` always counts).
- **Pattern B (flooding):** 5+ messages in 5 seconds, or 10+ in 30 seconds, from one account.
- **Duplicate-content wave:** the same link/image/text posted by 3+ distinct accounts within
  60 seconds, even when each account individually stays under its own threshold — catches
  raids that spread load thin across many accounts.

Response (in active mode) is always **timeout, then delete, then alert** — never an outright
ban, to bound the damage of a false positive. A failed timeout (e.g. the account's role sits
above the bot's) produces a loud, distinct alert rather than failing silently.

If 2+ accounts independently trip Pattern A, or 3+ post duplicate content, within 60 seconds
of each other, watchdog also triggers a temporary **lockdown**: `@everyone` loses send
permission in every text channel (any configured protected role(s) keep it), auto-lifting
after 15 minutes unless it's a repeat trigger within the last hour, in which case it stays
locked until a mod runs `.watchdog unlock`. Every channel's exact pre-lockdown permission
state is snapshotted first and restored exactly on lift; lockdown state is persisted, so a
restart mid-lockdown resumes correctly.

All watchdog commands require `Manage Server`:

| Command | Does |
|---|---|
| `watchdog` / `watchdog status` | Show the current configuration and status |
| `watchdog mode <shadow\|active>` | Switch between alert-only and enforcing |
| `watchdog setlog #channel` | Set the alert channel |
| `watchdog exempt add/remove/list <role\|member>` | Exempt roles/members from all checks |
| `watchdog protectedrole add/remove/list <role>` | Roles that keep send permission during lockdown |
| `watchdog unlock` | Lift an active lockdown |

Known v1 limitations: webhook messages are safely ignored rather than acted upon (a webhook
can't be timed out); detection thresholds are fixed constants, not yet per-server tunable.

### Palantir

Total surveillance logging: every join/leave, message edit/delete, role/nickname change,
mod action, channel/role/server-structure change, voice move, and invite create/delete is
streamed as an embed to a configured log channel, split into independently mutable
categories:

| Category | Covers |
|---|---|
| `members` | Joins (with account age and the invite used), leaves, nickname changes |
| `messages` | Edits, deletes, and bulk deletes |
| `roles` | Role create/delete/edit; a member's roles changing |
| `voice` | Voice channel join/leave/move (mute/deafen ignored) |
| `modactions` | Ban/kick/timeout and moderator role grants, attributed via audit log |
| `invites` | Invite created/deleted |
| `server` | Channel create/delete/edit, emoji changes, server settings |

- Message edits/deletes show the pre-change content from palantir's own disk-backed cache,
  capped at 20,000 messages and 14 days per server, oldest evicted/expired automatically —
  not a config option.
- Ban/kick/timeout/role-grant actions are attributed to the responsible moderator by name
  via the audit log (requires the *View Audit Log* permission); ban/unban still log
  (unattributed) without it. A deleted message likewise names the moderator who removed it
  when a mod deletes another member's message (best-effort via the audit log).
- Attachment archiving is a runtime toggle, default off: when on, attached files are
  downloaded to `palantir_attachments/` on post and re-uploaded on delete so they survive
  Discord's CDN URL expiry, instead of a possibly-expired URL.

All palantir commands require `Manage Server`:

| Command | Does |
|---|---|
| `palantir` / `palantir status` | Show the current configuration |
| `palantir setchannel #channel` | Set the log channel |
| `palantir disable` | Turn logging off |
| `palantir mute/unmute <category>` | Suppress/resume one category |
| `palantir archive <on\|off>` | Toggle attachment archiving |

### Captions

Overlays text onto a fixed base image at pre-defined regions — one command per image, so
adding a new image later is a new command, not a change to an existing one.

| Command | Does |
|---|---|
| `makima <text1>` | Caption the Makima image with text |
| `denji <text1>` | Caption the Denji image with text |
| `nanachi <text1> <text2>` | Caption the Nanachi image (left bubble, right bubble) |

A command with a single text field takes the rest of the message as that field, so multi-word
text needs no quoting with either `.` or `/` (e.g. `makima`, `denji`). A command with more than
one text field (e.g. `nanachi`) needs quotes around multi-word text with `.` (`.nanachi "Serves
you right." "Your ambition ends here."`) since there's no other way to tell where one field ends
and the next begins; `/` never needs quoting since each field is its own option. Text is
auto-wrapped and shrunk to fit its region; a template with a missing/unreadable base image
replies with an error instead of crashing.

Each image's blank text area is defined in code as a `Region(box=(x1, y1, x2, y2))` in
`cogs/captions.py` — **two pixel points**, not four independent values: `(x1, y1)` is the
region's top-left corner and `(x2, y2)` is its bottom-right corner, both measured from the
image's own top-left origin `(0, 0)`. Adding a new image means getting these corners (one
`Region` per bubble) and adding a new `Template` + thin command function alongside
`MAKIMA`/`makima`.

To prep a base image that still has dialogue text in its speech bubble(s), use
`scripts/detect_bubble.py`:

```bash
python scripts/detect_bubble.py list <input.png>
python scripts/detect_bubble.py erase <input.png> <output.png> <index> [index ...]
```

`list` ranks bright connected regions by area — the actual bubble(s) stand out but so can other
bright clutter (e.g. a page's own margin, eye highlights), so check the printed box against the
image before picking indices. `erase` erases the dialogue text inside the chosen bubble(s) (any
dark ink fully enclosed by that bubble's own pixels, leaving its outline intact) and prints one
`Region(box=...)` per index in the order given — pass multiple indices for a multi-bubble image
like `nanachi`. Review the output image before wiring it into a `Template`.

### AI Detect

`.aicheck` / `/aicheck [url]` estimates whether an image is AI-generated, via the Sightengine
`genai` model — attach an image, pass a URL, or reply to an image message. Right-click a message
→ **Apps** → **Check if AI** does the same without a command. Results (a percentage + verdict)
always post publicly. Requires `SIGHTENGINE_API_USER` / `SIGHTENGINE_API_SECRET` in `.env` (a
free account at [sightengine.com](https://sightengine.com)); without them, the command replies
that detection isn't configured instead of scoring. It's a probabilistic estimate, not proof.

### Trace Anime

`.trace` / `/trace [url]` reverse-searches a screenshot against trace.moe's scene index — attach
an image, pass a URL, or reply to an image message. Right-click a message → **Apps** → **Trace
anime** does the same without a command. Results (always posted publicly) show the anime title,
episode, timestamp, similarity, a scene thumbnail, a muted preview clip when small enough to
upload, and up to two runner-up matches; a match below 87% similarity is flagged as low
confidence. Adult titles have their thumbnail/clip hidden outside age-restricted channels. Works
anonymously; an optional `TRACE_MOE_API_KEY` in `.env` raises trace.moe's rate limit.

### Help

`.help` / `/help` lists every command you can currently use, grouped by cog, with a one-line
description each.

### Management

Bot administration from Discord. Owner-only (bot owner account):

| Command | Does |
|---|---|
| `cog list/load/unload/reload <name>` | Manage cogs at runtime |
| `reloadall` | Reload every loaded cog |
| `sync` | Re-sync slash commands with Discord |
| `guilds` | List the servers the bot is in |
| `leave [guild_id]` | Leave a server (the current one when no ID is given) |
| `presence [text]` | Set the bot's status text (no text clears it) |
| `shutdown` | Shut the bot down cleanly |

Server admins (`Manage Server`):

| Command | Does |
|---|---|
| `feature list/enable/disable <name>` | Soft-disable a cog's behavior in their own guild only |

The bot loads each cog independently at startup — if one fails to load, the failure is
logged and the rest of the bot still starts. Extensions unloaded via `.cog unload` stay
unloaded across restarts, and `management` itself can't be unloaded.

### Logging & data files

In addition to the console, everything is written to a rotating logfile at `logs/kurisu.log`
(5 MB per file, 3 backups kept), so errors can be reviewed without needing to capture the
terminal output. This covers uncaught errors from commands and events too, since discord.py
routes those through the same logging system.

All data files live in the project root and are created automatically on first use — no
manual setup needed:

| File | Holds |
|---|---|
| `warnings.json`, `channel_locks.json`, `mod_log.json` | Warnings, pre-lock permission snapshots, mod-log channel |
| `xp.json`, `messages.json` | XP, monthly message counts |
| `economy.json` | Bits balances |
| `verification.json` | Verification role configuration |
| `watchdog.json` | Watchdog config, including active lockdown state |
| `palantir.json`, `palantir_messages.json` | Palantir config, message cache |
| `management.json` | Unloaded extensions, per-guild feature toggles |

Palantir additionally stores archived attachment bytes under `palantir_attachments/` when
archiving is turned on.

## Setup Instructions

1. **Get a Discord Bot Token:**
   - Go to the [Discord Developer Portal](https://discord.com/developers/applications).
   - Create a New Application and navigate to the **Bot** tab.
   - Click **Add Bot** and then **Reset Token** to copy your bot's token.
   - Under **Privileged Gateway Intents**, make sure to enable the **Message Content Intent**
     (required for the bot to read messages) and the **Server Members Intent** (required for
     watchdog's high-value-role detection).

2. **Configure your Token:**
   - Copy `.env.example` to `.env`:
     ```bash
     cp .env.example .env
     ```
   - Open `.env` and replace `your_bot_token_here` with your actual Discord Bot Token.
   - Optionally, set `SIGHTENGINE_API_USER` / `SIGHTENGINE_API_SECRET` (free account at
     [sightengine.com](https://sightengine.com)) to enable the [AI Detect](#ai-detect) `.aicheck`
     command; leave them blank to skip it.
   - Optionally, set `TRACE_MOE_API_KEY` to raise the rate limit for [Trace Anime](#trace-anime)'s
     `.trace` command; it works anonymously without one.

3. **Invite the Bot to your Server:**
   - In the Developer Portal, go to **OAuth2** -> **URL Generator**.
   - Under **Scopes**, select `bot` and `applications.commands` (the latter is required for
     the `/` slash commands to register).
   - Under **Bot Permissions**, select:
     - `Read Messages/View Channels`
     - `Send Messages`
     - `Read Message History`
     - `Kick Members`, `Ban Members`, `Moderate Members`, `Manage Messages`, `Manage Channels`,
       `Manage Roles` (needed for the moderation commands, `lock`/`unlock`, verification's
       role grants, and watchdog's lockdown mechanism)
     - `View Audit Log` (needed for palantir to attribute ban/kick/timeout/role-grant actions
       to the responsible moderator by name)
   - Copy the generated URL and open it in your browser to invite the bot to your server.

4. **Run the Bot:**
   - Requires **Python 3.10+**.
   - Set up and activate the virtual environment:
     ```bash
     python3 -m venv .venv
     source .venv/bin/activate
     pip install -r requirements.txt
     ```
   - Run the bot script:
     ```bash
     python bot.py
     ```
   - Or use the bundled `./runbot.sh`, which does the venv setup, dependency install, and
     launch in one step.

## Running as a Background Service (systemd)

Running `python bot.py` or `./runbot.sh` directly ties the bot to your terminal session —
it stops the moment you disconnect. To keep it running on a server after an SSH disconnect
(and restart it automatically on crash or reboot), use the bundled `kurisu.service` template:

1. **Copy the unit file and fill in the placeholders:**
   ```bash
   sudo cp kurisu.service /etc/systemd/system/kurisu.service
   sudo nano /etc/systemd/system/kurisu.service
   ```
   Replace `youruser` with the user the bot should run as, and both `/path/to/Kurisu`
   placeholders with the absolute path to this repo (e.g. `/home/youruser/Kurisu`).

2. **Enable and start it:**
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now kurisu
   ```
   `enable --now` starts the bot immediately and again on every future boot.

3. **Check status and logs:**
   ```bash
   sudo systemctl status kurisu
   journalctl -u kurisu -f
   ```

4. **Manual control:**
   ```bash
   sudo systemctl restart kurisu
   sudo systemctl stop kurisu
   ```
   `.shutdown` (owner-only, see [Management](#management)) exits the bot cleanly, so systemd
   won't auto-restart it — only a crash triggers the automatic restart.
