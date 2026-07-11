import os

import discord
from discord.ext import commands

from .storage import load_json, save_json_atomic

MANAGEMENT_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "management.json")
COGS_DIR = os.path.dirname(__file__)

# Files in cogs/ that aren't behavioral cogs themselves and shouldn't show up in
# discovery/toggle lists.
_NON_COG_FILES = {"__init__.py", "storage.py"}


def _discover_cogs() -> dict[str, str]:
    """Map short cog name -> extension name (e.g. "moderation" -> "cogs.moderation")
    by scanning the cogs/ directory. Recomputed on demand so it always reflects
    what's actually on disk."""
    cogs = {}
    for filename in sorted(os.listdir(COGS_DIR)):
        if not filename.endswith(".py") or filename in _NON_COG_FILES:
            continue
        name = filename[:-3]
        cogs[name] = f"cogs.{name}"
    return cogs


def _to_extension(name: str) -> str:
    """Normalize user input ("moderation" or "cogs.moderation") to an extension name."""
    name = name.strip()
    return name if name.startswith("cogs.") else f"cogs.{name}"


def _to_short_name(name: str) -> str:
    """Normalize user input ("moderation" or "cogs.moderation") to a short cog name."""
    return name.strip().removeprefix("cogs.")


def _default_config() -> dict:
    return {"global": {"disabled_extensions": [], "presence": None}, "guilds": {}}


def cog_enabled(bot, guild_id: int, cog_key: str) -> bool:
    """Whether a per-guild-toggleable cog's behavior is enabled for a guild.
    Fails open (True) if the management cog isn't loaded, so removing/unloading
    management never silently disables everything else."""
    mgmt = bot.get_cog("Management")
    if mgmt is None:
        return True
    return mgmt.is_cog_enabled(guild_id, cog_key)


def globally_disabled_extensions() -> set[str]:
    """Standalone reader of global.disabled_extensions, for bot.py to consult
    before the management cog (or any cog) is loaded."""
    data = load_json(MANAGEMENT_FILE)
    return set(data.get("global", {}).get("disabled_extensions", []))


class Management(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config = load_json(MANAGEMENT_FILE)
        self._ensure_schema()

    def _ensure_schema(self):
        defaults = _default_config()
        self.config.setdefault("global", {})
        for key, value in defaults["global"].items():
            self.config["global"].setdefault(key, value)
        self.config.setdefault("guilds", {})

    def _save(self):
        save_json_atomic(MANAGEMENT_FILE, self.config)

    def _guild_conf(self, guild_id: int) -> dict:
        return self.config["guilds"].setdefault(str(guild_id), {"disabled_cogs": []})

    def is_cog_enabled(self, guild_id: int, cog_key: str) -> bool:
        guild_conf = self.config["guilds"].get(str(guild_id))
        if not guild_conf:
            return True
        return cog_key not in guild_conf.get("disabled_cogs", [])

    def _toggleable_names(self) -> set[str]:
        return {name for name in _discover_cogs() if name != "management"}

    async def _apply_presence(self):
        text = self.config["global"]["presence"]
        if text:
            await self.bot.change_presence(activity=discord.Game(name=text))

    @commands.Cog.listener()
    async def on_ready(self):
        await self._apply_presence()

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.CommandInvokeError):
            original = error.original
            if isinstance(original, commands.ExtensionNotFound):
                await ctx.reply("I couldn't find a cog by that name.")
                return
            elif isinstance(original, commands.ExtensionAlreadyLoaded):
                await ctx.reply("That cog is already loaded.")
                return
            elif isinstance(original, commands.ExtensionNotLoaded):
                await ctx.reply("That cog isn't loaded.")
                return
            elif isinstance(original, commands.ExtensionError):
                await ctx.reply(f"Couldn't do that: {original}")
                return
        if isinstance(error, commands.NotOwner):
            await ctx.reply("Only the bot owner can do that.")
        elif isinstance(error, commands.MissingPermissions):
            await ctx.reply("You don't have permission to do that.")
        elif isinstance(error, commands.BotMissingPermissions):
            await ctx.reply("I don't have permission to do that.")
        elif isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
            await ctx.reply(str(error) or "Invalid or missing argument.")
        else:
            raise error

    # --- Owner-only: cog control -------------------------------------------------

    @commands.group(name="cog", invoke_without_command=True)
    @commands.is_owner()
    async def cog_group(self, ctx):
        """Manage loaded cogs."""
        await ctx.reply("Use `.cog list|load|unload|reload <name>`.")

    @cog_group.command(name="list")
    @commands.is_owner()
    async def cog_list(self, ctx):
        """List every known cog and its loaded/disabled state."""
        disabled = set(self.config["global"]["disabled_extensions"])
        lines = []
        for short, ext in sorted(_discover_cogs().items()):
            if ext in disabled:
                status = "🔴 disabled"
            elif ext in self.bot.extensions:
                status = "🟢 loaded"
            else:
                status = "⚪ unloaded"
            lines.append(f"`{short}` — {status}")
        await ctx.reply("\n".join(lines) or "No cogs found.")

    @cog_group.command(name="load")
    @commands.is_owner()
    async def cog_load_cmd(self, ctx, name: str):
        """Load a cog by name."""
        ext = _to_extension(name)
        await self.bot.load_extension(ext)
        disabled = self.config["global"]["disabled_extensions"]
        if ext in disabled:
            disabled.remove(ext)
            self._save()
        await ctx.reply(f"✅ Loaded `{_to_short_name(name)}`.")

    @cog_group.command(name="unload")
    @commands.is_owner()
    async def cog_unload_cmd(self, ctx, name: str):
        """Unload a cog by name."""
        if _to_short_name(name) == "management":
            await ctx.reply("Refusing to unload the management cog — that would lock you out.")
            return
        ext = _to_extension(name)
        await self.bot.unload_extension(ext)
        disabled = self.config["global"]["disabled_extensions"]
        if ext not in disabled:
            disabled.append(ext)
            self._save()
        await ctx.reply(f"✅ Unloaded `{_to_short_name(name)}`.")

    @cog_group.command(name="reload")
    @commands.is_owner()
    async def cog_reload_cmd(self, ctx, name: str):
        """Reload a cog by name."""
        ext = _to_extension(name)
        await self.bot.reload_extension(ext)
        await ctx.reply(f"🔁 Reloaded `{_to_short_name(name)}`.")

    @commands.command(name="reloadall")
    @commands.is_owner()
    async def reloadall(self, ctx):
        """Reload every currently-loaded extension."""
        failures = []
        for ext in list(self.bot.extensions.keys()):
            try:
                await self.bot.reload_extension(ext)
            except commands.ExtensionError as e:
                failures.append(f"`{ext}`: {e}")
        if failures:
            await ctx.reply("Reloaded with errors:\n" + "\n".join(failures))
        else:
            await ctx.reply(f"🔁 Reloaded {len(self.bot.extensions)} extension(s).")

    @commands.command(name="sync")
    @commands.is_owner()
    async def sync(self, ctx):
        """Re-sync slash commands with Discord."""
        synced = await self.bot.tree.sync()
        await ctx.reply(f"🔄 Synced {len(synced)} slash command(s).")

    @commands.command(name="guilds")
    @commands.is_owner()
    async def guilds_cmd(self, ctx):
        """List every guild the bot is in."""
        lines = [
            f"{guild.name} (`{guild.id}`) — {guild.member_count} members"
            for guild in self.bot.guilds
        ]
        await ctx.reply("\n".join(lines) or "Not in any guilds.")

    @commands.command(name="leave")
    @commands.is_owner()
    async def leave(self, ctx, guild_id: int):
        """Leave a guild by ID (no accidental current-guild leave)."""
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            await ctx.reply("I'm not in a guild with that ID.")
            return
        name = guild.name
        await guild.leave()
        await ctx.reply(f"👋 Left **{name}** (`{guild_id}`).")

    @commands.command(name="presence")
    @commands.is_owner()
    async def presence(self, ctx, *, text: str):
        """Set the bot's "Playing ..." status, persisted across restarts."""
        self.config["global"]["presence"] = text
        self._save()
        await self._apply_presence()
        await ctx.reply(f"🎮 Presence set to: Playing {text}")

    @commands.command(name="shutdown")
    @commands.is_owner()
    async def shutdown(self, ctx):
        """Shut the bot down."""
        await ctx.reply("🛑 Shutting down.")
        await self.bot.close()

    # --- Server-admin: per-guild feature toggles ----------------------------------

    @commands.group(name="feature", invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def feature(self, ctx):
        """Manage which cogs' behavior is enabled in this server."""
        await ctx.reply("Use `.feature list|enable|disable <name>`.")

    @feature.command(name="list")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def feature_list(self, ctx):
        """List each toggleable cog's enabled/disabled state in this server."""
        guild_conf = self.config["guilds"].get(str(ctx.guild.id), {"disabled_cogs": []})
        disabled = set(guild_conf.get("disabled_cogs", []))
        lines = [
            f"`{name}` — {'🔴 disabled' if name in disabled else '🟢 enabled'}"
            for name in sorted(self._toggleable_names())
        ]
        await ctx.reply("\n".join(lines) or "No toggleable features found.")

    @feature.command(name="enable")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def feature_enable(self, ctx, name: str):
        """Enable a cog's behavior in this server."""
        if name not in self._toggleable_names():
            await ctx.reply(f"`{name}` isn't a toggleable feature.")
            return
        guild_conf = self._guild_conf(ctx.guild.id)
        if name in guild_conf["disabled_cogs"]:
            guild_conf["disabled_cogs"].remove(name)
            self._save()
        await ctx.reply(f"✅ `{name}` is now enabled in this server.")

    @feature.command(name="disable")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def feature_disable(self, ctx, name: str):
        """Disable a cog's behavior in this server."""
        if name not in self._toggleable_names():
            await ctx.reply(f"`{name}` isn't a toggleable feature.")
            return
        guild_conf = self._guild_conf(ctx.guild.id)
        if name not in guild_conf["disabled_cogs"]:
            guild_conf["disabled_cogs"].append(name)
            self._save()
        await ctx.reply(f"🚫 `{name}` is now disabled in this server.")


async def setup(bot):
    await bot.add_cog(Management(bot))
