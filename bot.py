import asyncio
import logging
import logging.handlers
import os

import discord
from discord.ext import commands

from cogs.management import globally_disabled_extensions

logger = logging.getLogger("kurisu")

LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "kurisu.log")


def setup_logging():
    """Log to the console (as before) and additionally to a rotating logfile,
    so errors can be reviewed without needing to capture the terminal output."""
    discord.utils.setup_logging(root=True)

    os.makedirs(LOG_DIR, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(
        logging.Formatter("[{asctime}] [{levelname:<8}] {name}: {message}", "%Y-%m-%d %H:%M:%S", style="{")
    )
    logging.getLogger().addHandler(file_handler)


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

bot = commands.Bot(command_prefix=".", intents=intents, help_command=None)

INITIAL_EXTENSIONS = [
    "cogs.management",
    "cogs.help",
    "cogs.triggers",
    "cogs.moderation",
    "cogs.leveling",
    "cogs.economy",
    "cogs.watchdog",
    "cogs.verification",
    "cogs.palantir",
    "cogs.captions",
]

_synced = False


@bot.event
async def on_ready():
    global _synced
    logger.info("Logged in as %s (ID: %s)", bot.user.name, bot.user.id)
    if not _synced:
        try:
            synced = await bot.tree.sync()
            logger.info("Synced %d slash command(s).", len(synced))
        except discord.HTTPException:
            logger.exception("Failed to sync slash commands.")
        _synced = True


async def main():
    async with bot:
        disabled = globally_disabled_extensions()
        for extension in INITIAL_EXTENSIONS:
            if extension in disabled and extension != "cogs.management":
                logger.info("Skipping globally-disabled extension %s", extension)
                continue
            try:
                await bot.load_extension(extension)
            except commands.ExtensionError:
                logger.exception("Failed to load extension %s", extension)
        try:
            await bot.start(TOKEN)
        except discord.PrivilegedIntentsRequired:
            logger.exception(
                "A privileged intent (e.g. Server Members or Message Content) is enabled in "
                "code but not in the Developer Portal. Enable it under Privileged Gateway "
                "Intents for this application, then restart the bot."
            )


if __name__ == "__main__":
    setup_logging()
    if not TOKEN:
        logger.error("DISCORD_TOKEN environment variable is not set.")
        logger.error("Please create a .env file containing: DISCORD_TOKEN=your_token_here")
    else:
        asyncio.run(main())
