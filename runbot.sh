#!/usr/bin/env bash
# Sets up (venv + dependencies + .env) and runs the bot in one step.
# See README.md's Setup Instructions for the manual, one-time Discord
# Developer Portal steps (bot token, privileged intents, invite URL) —
# this script only covers the repeatable local setup + launch.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

if [ ! -f .env ]; then
    cp .env.example .env
    echo "Created .env from .env.example — add your DISCORD_TOKEN to it, then run this script again."
    exit 1
fi

if [ ! -d .venv ]; then
    python3 -m venv .venv
fi

source .venv/bin/activate
pip install -q -r requirements.txt

exec python bot.py
