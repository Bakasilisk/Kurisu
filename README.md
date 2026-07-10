# Kurisutina Discord Bot

A simple, lightweight Discord bot written in Python using `discord.py`.

## Features

**Triggers**
- Replies whenever anyone says "kurisutina" (case-insensitive): _Hör auf mich_ **_Kurisutina_** _zu nennen!_
- Replies whenever anyone says "horny" (case-insensitive): "`@user` ist Horny!"

**Moderation** (prefix `!`): `kick`, `ban`, `unban`, `timeout`/`mute`, `untimeout`/`unmute`,
`warn`, `warnings`/`warnlist`, `clearwarnings`, `purge`/`clear`, `slowmode`, `lock`, `unlock`.
Each requires the relevant Discord permission (e.g. `Kick Members`, `Ban Members`,
`Moderate Members`, `Manage Messages`, `Manage Channels`) on both the caller and the bot,
and enforces role-hierarchy checks so members can't act on others with an equal or higher role.
Warnings are persisted to `warnings.json` in the project root.

**Leveling** — members earn 15-25 XP per message (60s cooldown to prevent spam farming),
with an announcement on level-up. Commands: `rank`/`level [member]` to view level/XP/server
rank, `leaderboard`/`lb`/`top [count]` for the server's top members, and `resetxp <member>`
(requires `Moderate Members`) to clear a member's progress. XP is persisted to `xp.json`.

The bot loads each cog independently at startup — if one fails to load, the failure is
logged and the rest of the bot still starts.

## Setup Instructions

1. **Get a Discord Bot Token:**
   - Go to the [Discord Developer Portal](https://discord.com/developers/applications).
   - Create a New Application and navigate to the **Bot** tab.
   - Click **Add Bot** and then **Reset Token** to copy your bot's token.
   - Under **Privileged Gateway Intents**, make sure to enable the **Message Content Intent** (required for the bot to read messages).

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
     - `Kick Members`, `Ban Members`, `Moderate Members`, `Manage Messages`, `Manage Channels`
       (needed for the moderation commands)
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
