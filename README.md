# Kurisutina Discord Bot

A simple, lightweight Discord bot written in Python using `discord.py`. Requires **Python 3.10+**
(the code uses `X | None` union type hints).

## Features

**Triggers**
- Replies whenever anyone says "kurisutina" (case-insensitive): _Hör auf mich_ **_Kurisutina_** _zu nennen!_
- Replies whenever anyone says "horny" (case-insensitive): "`@user` ist Horny!"

**Moderation** (prefix `!`): `kick`, `ban`, `unban`, `timeout`/`mute`, `untimeout`/`unmute`,
`warn`, `warnings`/`warnlist`, `clearwarnings`, `purge`/`clear`, `slowmode`, `lock`, `unlock`.
Each requires the relevant Discord permission (e.g. `Kick Members`, `Ban Members`,
`Moderate Members`, `Manage Messages`, `Manage Channels`) on both the caller and the bot,
and enforces role-hierarchy checks so members can't act on others with an equal or higher role.
`lock`/`unlock` snapshot each channel's exact pre-lock permission state (persisted to
`channel_locks.json`) so unlocking always restores what was there before, rather than
blindly resetting it. Warnings are persisted to `warnings.json` in the project root.
Every action above is optionally logged as an embed to a mod-log channel — configure it
with `modlog set #channel`, check it with `modlog`, and turn it off with `modlog disable`
(all three require `Manage Server`). The configured channel is persisted to `mod_log.json`.

**Leveling** — members earn 15-25 XP per message (60s cooldown to prevent spam farming),
with an announcement on level-up. Commands: `rank`/`level [member]` to view level/XP/server
rank, `leaderboard`/`lb`/`top [count]` for the server's top members, and `resetxp <member>`
(requires `Moderate Members`) to clear a member's progress. XP is persisted to `xp.json`.

**Watchdog** — automated detection of raid/spam behavior, reacting faster than a human mod
can (or when none are online). Ships **in shadow mode by default**: it detects and alerts,
but takes no action until a mod runs `!watchdog mode active`. Detects:
- **Pattern A (sleeper raid/scam bursts):** an account posting in 4+ distinct channels within
  20 seconds while mentioning a role above a member-count threshold (auto-detected as
  "high-value" — no manual role list to maintain; `@everyone`/`@here` always counts).
- **Pattern B (flooding):** 5+ messages in 5 seconds, or 10+ in 30 seconds, from one account.
- **Duplicate-content wave:** the same link/image/text posted by 3+ distinct accounts within
  60 seconds, even when each account individually stays under its own threshold — catches
  raids that spread load thin across many accounts.

Response (in active mode) is always **timeout, then delete, then alert** — never an outright
ban, to bound the damage of a false positive. A failed timeout (e.g. the account's role sits
above the bot's) produces a loud, distinct alert rather than failing silently. If 2+ accounts
independently trip Pattern A, or 3+ post duplicate content, within 60 seconds of each other,
watchdog also triggers a temporary **lockdown**: `@everyone` loses send permission in every
text channel (any configured protected role(s) keep it), auto-lifting after 15 minutes unless
it's a repeat trigger within the last hour, in which case it stays locked until a mod runs
`!watchdog unlock`. Every channel's exact pre-lockdown permission state is snapshotted first
and restored exactly on lift.

Commands (all require `Manage Server`): `watchdog`/`watchdog status`, `watchdog mode
<shadow|active>`, `watchdog setlog #channel`, `watchdog exempt add/remove/list <role|member>`,
`watchdog protectedrole add/remove/list <role>`, `watchdog unlock`. Config (including active
lockdown state, so a restart mid-lockdown resumes correctly) is persisted to `watchdog.json`.

Known v1 limitations: webhook messages are safely ignored rather than acted upon (a webhook
can't be timed out); detection thresholds are fixed constants, not yet per-server tunable.

The bot loads each cog independently at startup — if one fails to load, the failure is
logged and the rest of the bot still starts.

All persisted data files (`warnings.json`, `channel_locks.json`, `mod_log.json`, `xp.json`,
`watchdog.json`) are created automatically on first use — no manual setup needed, and
they're already `.gitignore`d.

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

3. **Invite the Bot to your Server:**
   - In the Developer Portal, go to **OAuth2** -> **URL Generator**.
   - Under **Scopes**, select `bot`.
   - Under **Bot Permissions**, select:
     - `Read Messages/View Channels`
     - `Send Messages`
     - `Read Message History`
     - `Kick Members`, `Ban Members`, `Moderate Members`, `Manage Messages`, `Manage Channels`,
       `Manage Roles` (needed for the moderation commands, `lock`/`unlock`, and watchdog's
       lockdown mechanism)
   - Copy the generated URL and open it in your browser to invite the bot to your server.

4. **Run the Bot:**
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
