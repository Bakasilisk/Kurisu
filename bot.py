import asyncio
import logging
import os

import discord
from discord.ext import commands

logger = logging.getLogger("kurisu")

# Simple loader for .env files without requiring external dependencies
if os.path.exists(".env"):
    with open(".env") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                try:
                    key, val = line.split("=", 1)
                    os.environ[key.strip()] = val.strip()
                except ValueError:
                    pass

# Get the token from environment variables
TOKEN = os.environ.get("DISCORD_TOKEN")

# Setup bot intents
intents = discord.Intents.default()
intents.message_content = True
# Privileged intent, required for accurate Role.members counts (watchdog's high-value-role
# detection) — must also be enabled under Privileged Gateway Intents in the Developer Portal.
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

INITIAL_EXTENSIONS = [
    "cogs.triggers",
    "cogs.moderation",
    "cogs.leveling",
    "cogs.watchdog",
]


@bot.event
async def on_ready():
    logger.info("Logged in as %s (ID: %s)", bot.user.name, bot.user.id)


async def main():
    async with bot:
        for extension in INITIAL_EXTENSIONS:
            try:
                await bot.load_extension(extension)
            except commands.ExtensionError:
                logger.exception("Failed to load extension %s", extension)
        await bot.start(TOKEN)


if __name__ == "__main__":
    discord.utils.setup_logging(root=True)
    if not TOKEN:
        logger.error("DISCORD_TOKEN environment variable is not set.")
        logger.error("Please create a .env file containing: DISCORD_TOKEN=your_token_here")
    else:
        asyncio.run(main())
