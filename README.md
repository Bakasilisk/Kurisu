# Kurisutina Discord Bot

A simple, lightweight Discord bot written in Python using `discord.py`.

## Features
- Listens to chat messages and triggers whenever anyone says "kurisutina" (case-insensitive).
- Replies with: _Hör auf mich_ **_Kurisutina_** _zu nennen!_ (cursive, with her name in bold).

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
