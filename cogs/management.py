import os

import discord
from discord import app_commands
from discord.ext import commands

from .storage import data_path, load_json, save_json_atomic

MANAGEMENT_FILE = data_path("management.json")
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
    return {"global": {"disabled_extensions": [], "presence": None}}


def _toggle_membership(lst: list, item, present: bool) -> None:
    """Ensure `item`'s membership in `lst` matches `present` (add if missing and
    should be present, remove if there and shouldn't be)."""
    if present:
        if item not in lst:
            lst.append(item)
    else:
        if item in lst:
            lst.remove(item)


def cog_enabled(bot, guild_id: int, cog_key: str) -> bool:
    """Whether a per-guild-toggleable cog's behavior is enabled for a guild.
    Fails open (True) if the management cog isn't loaded, so removing/unloading
    management never silently disables everything else."""
    mgmt = bot.get_cog("Management")
    if mgmt is None:
        return True
    return mgmt.is_cog_enabled(guild_id, cog_key)


def has_permissions_or_owner(**perms):
    """A command check that passes if the invoker has all `perms` in the guild,
    or is the bot owner. Owner-bypass lives here since it's the management cog's
    concern (see is_owner()); behavioral cogs consume this rather than open-coding
    the check_any(has_permissions, is_owner) pair."""
    return commands.check_any(commands.has_permissions(**perms), commands.is_owner())


async def actor_outranks(bot, ctx, member) -> bool:
    """Whether the invoker may act on `member` by role hierarchy. The guild owner
    and the bot owner both bypass the check."""
    return (
        member.top_role < ctx.author.top_role
        or ctx.author == ctx.guild.owner
        or await bot.is_owner(ctx.author)
    )


async def require_outranks(bot, ctx, member, action: str, *, reply=None) -> bool:
    """Guard for commands that act on a member: replies and returns False if the
    invoker doesn't outrank `member` by role hierarchy (see actor_outranks),
    otherwise returns True. `reply` defaults to ctx.reply; pass a cog's own
    ephemeral-aware reply helper for hybrid commands."""
    if reply is None:
        reply = ctx.reply
    if not await actor_outranks(bot, ctx, member):
        await reply(f"You can't {action} someone with an equal or higher role than you.")
        return False
    return True


def bot_outranks(guild, role_or_member) -> bool:
    """Whether the bot's top role is strictly higher than `role_or_member`'s
    (or, if given a discord.Member, that member's top role). Callers checking
    "can the bot act on this target" use `not bot_outranks(...)` to error out."""
    top_role = role_or_member.top_role if isinstance(role_or_member, discord.Member) else role_or_member
    return guild.me.top_role > top_role


def rank_of(items, key, target_id) -> int | None:
    """1-based rank of `target_id` within `items` (an iterable of (id, value) pairs)
    after sorting by `key` descending, or None if `target_id` isn't present. `key`
    is applied the same way as in sorted() — to each (id, value) pair."""
    sorted_items = sorted(items, key=key, reverse=True)
    for i, (item_id, _) in enumerate(sorted_items, start=1):
        if item_id == target_id:
            return i
    return None


async def reply_ephemeral_aware(ctx, *args, **kwargs):
    """ctx.reply, but ephemeral (visible only to the invoker) when the command was
    invoked via / rather than the text prefix."""
    kwargs.setdefault("ephemeral", ctx.interaction is not None)
    return await ctx.reply(*args, **kwargs)


async def common_error_reply(ctx, error, reply=None) -> bool:
    """Handle the error branches duplicated across every cog's cog_command_error.
    Returns True if handled (a reply was sent, or intentionally suppressed for
    CheckFailure), False if the caller should handle/raise it itself. `reply`
    defaults to ctx.reply; pass e.g. a lambda routing through a cog's own
    ephemeral-aware reply helper instead."""
    if reply is None:
        reply = ctx.reply
    if isinstance(error, commands.MissingPermissions):
        await reply("You don't have permission to do that.")
        return True
    elif isinstance(error, commands.BotMissingPermissions):
        await reply("I don't have permission to do that.")
        return True
    elif isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
        await reply(str(error) or "Invalid or missing argument.")
        return True
    elif isinstance(error, commands.CheckFailure):
        return True
    return False


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
        return {name for name in _discover_cogs() if name not in ("management", "help")}

    def _matching_extensions(self, current: str, *, loaded: bool | None = None) -> list[app_commands.Choice[str]]:
        """Cog short names matching `current`, optionally filtered to those that
        are (or aren't) currently loaded — so e.g. `load`'s autocomplete only
        offers cogs that can actually be loaded."""
        current_lower = current.lower()
        choices = []
        for name, ext in sorted(_discover_cogs().items()):
            if loaded is not None and (ext in self.bot.extensions) != loaded:
                continue
            if current_lower in name.lower():
                choices.append(app_commands.Choice(name=name, value=name))
        return choices[:25]

    async def _unloaded_autocomplete(self, interaction: discord.Interaction, current: str):
        return self._matching_extensions(current, loaded=False)

    async def _loaded_autocomplete(self, interaction: discord.Interaction, current: str):
        return self._matching_extensions(current, loaded=True)

    def _matching_toggleable(
        self, current: str, guild_id: int | None, *, enabled: bool | None = None
    ) -> list[app_commands.Choice[str]]:
        """Toggleable cog names matching `current`, optionally filtered to those
        that are (or aren't) currently enabled in this guild."""
        current_lower = current.lower()
        disabled = set()
        if guild_id is not None:
            guild_conf = self.config["guilds"].get(str(guild_id))
            if guild_conf:
                disabled = set(guild_conf.get("disabled_cogs", []))
        choices = []
        for name in sorted(self._toggleable_names()):
            if enabled is not None and (name not in disabled) != enabled:
                continue
            if current_lower in name.lower():
                choices.append(app_commands.Choice(name=name, value=name))
        return choices[:25]

    async def _disabled_feature_autocomplete(self, interaction: discord.Interaction, current: str):
        return self._matching_toggleable(current, interaction.guild_id, enabled=False)

    async def _enabled_feature_autocomplete(self, interaction: discord.Interaction, current: str):
        return self._matching_toggleable(current, interaction.guild_id, enabled=True)

    @staticmethod
    async def _reply(ctx, *args, **kwargs):
        """ctx.reply, but ephemeral (visible only to the invoker) when the command
        was invoked via / rather than the text prefix — mirrors Moderation._reply."""
        return await reply_ephemeral_aware(ctx, *args, **kwargs)

    @staticmethod
    def _embed(description: str, *, title: str | None = None) -> discord.Embed:
        embed = discord.Embed(description=description, color=discord.Color.teal())
        if title:
            embed.title = title
        return embed

    async def _apply_presence(self):
        # change_presence needs a live gateway; calling it before the client is
        # ready (e.g. cog_load during startup) raises AttributeError on the missing
        # websocket. Skip here and let on_ready re-apply once connected.
        if not self.bot.is_ready():
            return
        text = self.config["global"]["presence"]
        activity = discord.Game(name=text) if text else None
        try:
            await self.bot.change_presence(activity=activity)
        except discord.HTTPException:
            pass

    async def cog_load(self):
        # Re-apply persisted presence whenever the cog loads (including a bare
        # `.cog reload management`), not only on a full startup's on_ready.
        await self._apply_presence()

    @commands.Cog.listener()
    async def on_ready(self):
        await self._apply_presence()

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.CommandInvokeError):
            original = error.original
            if isinstance(original, commands.ExtensionNotFound):
                await self._reply(ctx, embed=self._embed("I couldn't find a cog by that name."))
                return
            elif isinstance(original, commands.ExtensionAlreadyLoaded):
                await self._reply(ctx, embed=self._embed("That cog is already loaded."))
                return
            elif isinstance(original, commands.ExtensionNotLoaded):
                await self._reply(ctx, embed=self._embed("That cog isn't loaded."))
                return
            elif isinstance(original, commands.ExtensionError):
                await self._reply(ctx, embed=self._embed(f"Couldn't do that: {original}"))
                return
        if isinstance(error, commands.NotOwner):
            await self._reply(ctx, embed=self._embed("Only the bot owner can do that."))
        elif isinstance(error, commands.MissingPermissions):
            await self._reply(ctx, embed=self._embed("You don't have permission to do that."))
        elif isinstance(error, commands.BotMissingPermissions):
            await self._reply(ctx, embed=self._embed("I don't have permission to do that."))
        elif isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
            await self._reply(ctx, embed=self._embed(str(error) or "Invalid or missing argument."))
        else:
            raise error

    # --- Owner-only: cog control -------------------------------------------------

    @commands.hybrid_group(
        name="cog", invoke_without_command=True, fallback="help",
        description="Manage loaded cogs (owner only).",
    )
    @app_commands.default_permissions(administrator=True)
    @commands.is_owner()
    async def manage_cogs(self, ctx):
        """Manage loaded cogs."""
        await self._reply(ctx, embed=self._embed("Use `.cog list|load|unload|reload <name>` (or `/cog …`)."))

    @manage_cogs.command(name="list", description="List every known cog and its state.")
    @commands.is_owner()
    async def list_cogs(self, ctx):
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
        await self._reply(ctx, embed=self._embed("\n".join(lines) or "No cogs found.", title="Cogs"))

    @manage_cogs.command(name="load", description="Load a cog by name.")
    @commands.is_owner()
    @app_commands.autocomplete(name=_unloaded_autocomplete)
    async def load_cog(self, ctx, name: str):
        """Load a cog by name."""
        ext = _to_extension(name)
        await self.bot.load_extension(ext)
        _toggle_membership(self.config["global"]["disabled_extensions"], ext, present=False)
        self._save()
        await self._reply(ctx, embed=self._embed(f"✅ Loaded `{_to_short_name(name)}`."))

    @manage_cogs.command(name="unload", description="Unload a cog by name.")
    @commands.is_owner()
    @app_commands.autocomplete(name=_loaded_autocomplete)
    async def unload_cog(self, ctx, name: str):
        """Unload a cog by name."""
        if _to_short_name(name) == "management":
            await self._reply(
                ctx, embed=self._embed("Refusing to unload the management cog — that would lock you out.")
            )
            return
        ext = _to_extension(name)
        await self.bot.unload_extension(ext)
        _toggle_membership(self.config["global"]["disabled_extensions"], ext, present=True)
        self._save()
        await self._reply(ctx, embed=self._embed(f"✅ Unloaded `{_to_short_name(name)}`."))

    @manage_cogs.command(name="reload", description="Reload a cog by name.")
    @commands.is_owner()
    @app_commands.autocomplete(name=_loaded_autocomplete)
    async def reload_cog(self, ctx, name: str):
        """Reload a cog by name."""
        ext = _to_extension(name)
        await self.bot.reload_extension(ext)
        await self._reply(ctx, embed=self._embed(f"🔁 Reloaded `{_to_short_name(name)}`."))

    @commands.hybrid_command(name="reloadall", description="Reload every loaded cog.")
    @app_commands.default_permissions(administrator=True)
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
            await self._reply(ctx, embed=self._embed("Reloaded with errors:\n" + "\n".join(failures)))
        else:
            await self._reply(ctx, embed=self._embed(f"🔁 Reloaded {len(self.bot.extensions)} extension(s)."))

    @commands.hybrid_command(name="sync", description="Re-sync slash commands with Discord.")
    @app_commands.default_permissions(administrator=True)
    @commands.is_owner()
    async def sync(self, ctx):
        """Re-sync slash commands with Discord."""
        synced = await self.bot.tree.sync()
        await self._reply(ctx, embed=self._embed(f"🔄 Synced {len(synced)} slash command(s)."))

    @commands.hybrid_command(name="guilds", description="List the servers the bot is in.")
    @app_commands.default_permissions(administrator=True)
    @commands.is_owner()
    async def guilds_cmd(self, ctx):
        """List every guild the bot is in."""
        lines = [
            f"{guild.name} (`{guild.id}`) — {guild.member_count} members"
            for guild in self.bot.guilds
        ]
        await self._reply(ctx, embed=self._embed("\n".join(lines) or "Not in any guilds.", title="Guilds"))

    @commands.hybrid_command(name="leave", description="Leave this server, or another by ID.")
    @app_commands.default_permissions(administrator=True)
    @commands.is_owner()
    async def leave(self, ctx, guild_id: str = None):
        """Leave a guild — the current one if no ID is given, otherwise the one with the
        given ID. ID is a string because guild snowflakes exceed the slash int range."""
        if guild_id is None:
            guild = ctx.guild
            if guild is None:
                await self._reply(
                    ctx, embed=self._embed("There's no current server here — give a guild ID to leave.")
                )
                return
        else:
            try:
                gid = int(guild_id)
            except ValueError:
                await self._reply(ctx, embed=self._embed("That doesn't look like a valid guild ID."))
                return
            guild = self.bot.get_guild(gid)
            if guild is None:
                await self._reply(ctx, embed=self._embed("I'm not in a guild with that ID."))
                return
        # Reply before leaving: a prefix reply can't be posted once we've left the channel.
        await self._reply(ctx, embed=self._embed(f"👋 Leaving **{guild.name}** (`{guild.id}`)."))
        await guild.leave()

    @commands.hybrid_command(name="presence", description="Set or clear the bot's status text.")
    @app_commands.default_permissions(administrator=True)
    @commands.is_owner()
    async def presence(self, ctx, *, text: str = None):
        """Set the bot's "Playing ..." status, persisted across restarts.
        Use `.presence` with no text (or `.presence clear`) to revert to the default."""
        if text is None or text.strip().lower() == "clear":
            self.config["global"]["presence"] = None
            self._save()
            await self._apply_presence()
            await self._reply(ctx, embed=self._embed("🎮 Presence cleared."))
            return
        self.config["global"]["presence"] = text
        self._save()
        await self._apply_presence()
        await self._reply(ctx, embed=self._embed(f"🎮 Presence set to: Playing {text}"))

    @commands.hybrid_command(name="shutdown", description="Shut the bot down.")
    @app_commands.default_permissions(administrator=True)
    @commands.is_owner()
    async def shutdown(self, ctx):
        """Shut the bot down."""
        await self._reply(ctx, embed=self._embed("🛑 Shutting down."))
        await self.bot.close()

    # --- Server-admin: per-guild feature toggles ----------------------------------

    @commands.hybrid_group(
        name="feature", invoke_without_command=True, fallback="help",
        description="Enable/disable a cog's behavior in this server.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def feature(self, ctx):
        """Manage which cogs' behavior is enabled in this server."""
        await self._reply(
            ctx, embed=self._embed("Use `.feature list|enable|disable <name>` (or `/feature …`).")
        )

    @feature.command(name="list", description="Show each cog's state in this server.")
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
        await self._reply(
            ctx,
            embed=self._embed("\n".join(lines) or "No toggleable features found.", title="Feature Toggles"),
        )

    @feature.command(name="enable", description="Enable a cog's behavior in this server.")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    @app_commands.autocomplete(name=_disabled_feature_autocomplete)
    async def feature_enable(self, ctx, name: str):
        """Enable a cog's behavior in this server."""
        if name not in self._toggleable_names():
            await self._reply(ctx, embed=self._embed(f"`{name}` isn't a toggleable feature."))
            return
        guild_conf = self._guild_conf(ctx.guild.id)
        _toggle_membership(guild_conf["disabled_cogs"], name, present=False)
        self._save()
        await self._reply(ctx, embed=self._embed(f"✅ `{name}` is now enabled in this server."))

    @feature.command(name="disable", description="Disable a cog's behavior in this server.")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    @app_commands.autocomplete(name=_enabled_feature_autocomplete)
    async def feature_disable(self, ctx, name: str):
        """Disable a cog's behavior in this server."""
        if name not in self._toggleable_names():
            await self._reply(ctx, embed=self._embed(f"`{name}` isn't a toggleable feature."))
            return
        guild_conf = self._guild_conf(ctx.guild.id)
        _toggle_membership(guild_conf["disabled_cogs"], name, present=True)
        self._save()
        await self._reply(ctx, embed=self._embed(f"🚫 `{name}` is now disabled in this server."))


async def setup(bot):
    await bot.add_cog(Management(bot))
