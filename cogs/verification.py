import discord
from discord.ext import commands

from .management import bot_outranks, cog_enabled, common_error_reply
from .storage import backfill_defaults, data_path, load_json, save_json_atomic

VERIFICATION_FILE = data_path("verification.json")
WELCOME_TEXT = "Herzlich Willkommen auf dem Magic Society Server, {user}!"
WELCOME_EMOJI_IDS = (859152939750391818, 858417437523705896)


def _default_guild_config() -> dict:
    return {"granter_role_id": None, "target_role_id": None, "welcome_channel_id": None}


class Verification(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config = load_json(VERIFICATION_FILE)

    def _save_config(self):
        save_json_atomic(VERIFICATION_FILE, self.config)

    def _guild_conf(self, guild_id: int) -> dict:
        entry = self.config.setdefault(str(guild_id), {})
        return backfill_defaults(entry, _default_guild_config())

    async def _send_welcome(self, guild, member):
        channel_id = self.config.get(str(guild.id), {}).get("welcome_channel_id")
        if not channel_id:
            return
        channel = guild.get_channel(channel_id)
        if channel is None:
            return
        emojis = " ".join(
            str(e) for e in (self.bot.get_emoji(i) for i in WELCOME_EMOJI_IDS)
            if e is not None
        )
        content = WELCOME_TEXT.format(user=member.mention)
        if emojis:
            content = f"{content} {emojis}"
        try:
            await channel.send(
                content,
                allowed_mentions=discord.AllowedMentions(
                    users=True, roles=False, everyone=False
                ),
            )
        except discord.Forbidden:
            pass

    async def cog_check(self, ctx):
        return ctx.guild is None or cog_enabled(self.bot, ctx.guild.id, "verification")

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.RoleNotFound):
            await ctx.reply("I couldn't find that role.")
        elif isinstance(error, commands.MemberNotFound):
            await ctx.reply("I couldn't find that member.")
        elif isinstance(error, commands.ChannelNotFound):
            await ctx.reply("I couldn't find that channel.")
        elif await common_error_reply(ctx, error):
            return
        else:
            raise error

    @commands.group(invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def verification(self, ctx):
        """Show the current verification configuration."""
        guild_conf = self._guild_conf(ctx.guild.id)
        granter = (
            ctx.guild.get_role(guild_conf["granter_role_id"])
            if guild_conf["granter_role_id"]
            else None
        )
        target = (
            ctx.guild.get_role(guild_conf["target_role_id"])
            if guild_conf["target_role_id"]
            else None
        )
        channel = (
            ctx.guild.get_channel(guild_conf["welcome_channel_id"])
            if guild_conf["welcome_channel_id"]
            else None
        )
        await ctx.reply(
            f"Granter role: {granter.mention if granter else 'Not set'}\n"
            f"Role granted: {target.mention if target else 'Not set'}\n"
            f"Welcome channel: {channel.mention if channel else 'Not set'}\n"
            f"Members with the granter role can run `.verify @member`.",
            allowed_mentions=discord.AllowedMentions(roles=False),
        )

    @verification.command(name="granter")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def verification_granter(self, ctx, role: discord.Role):
        """Set the role someone must hold to use .verify."""
        guild_conf = self._guild_conf(ctx.guild.id)
        guild_conf["granter_role_id"] = role.id
        self._save_config()
        await ctx.reply(
            f"✅ {role.mention} can now use `.verify`.",
            allowed_mentions=discord.AllowedMentions(roles=False),
        )

    @verification.command(name="target")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def verification_target(self, ctx, role: discord.Role):
        """Set the role .verify assigns to its target."""
        guild_conf = self._guild_conf(ctx.guild.id)
        guild_conf["target_role_id"] = role.id
        self._save_config()
        await ctx.reply(
            f"✅ `.verify` now grants {role.mention}.",
            allowed_mentions=discord.AllowedMentions(roles=False),
        )

    @verification.group(name="welcome", invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def verification_welcome(self, ctx):
        """Show the channel welcomes are posted to."""
        guild_conf = self._guild_conf(ctx.guild.id)
        channel = (
            ctx.guild.get_channel(guild_conf["welcome_channel_id"])
            if guild_conf["welcome_channel_id"] else None
        )
        await ctx.reply(
            f"Welcome messages are sent to {channel.mention}." if channel
            else "No welcome channel is set. Use `.verification welcome set #channel`."
        )

    @verification_welcome.command(name="set")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def verification_welcome_set(self, ctx, channel: discord.TextChannel):
        """Set the channel newly-verified members are welcomed in."""
        guild_conf = self._guild_conf(ctx.guild.id)
        guild_conf["welcome_channel_id"] = channel.id
        self._save_config()
        await ctx.reply(f"✅ Welcome messages will be sent to {channel.mention}.")

    @verification_welcome.command(name="disable")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def verification_welcome_disable(self, ctx):
        """Stop welcoming newly-verified members."""
        guild_conf = self._guild_conf(ctx.guild.id)
        had_one = guild_conf["welcome_channel_id"] is not None
        guild_conf["welcome_channel_id"] = None
        self._save_config()
        await ctx.reply(
            "✅ Welcome messages disabled." if had_one
            else "Welcome messages were not enabled."
        )

    @commands.command()
    @commands.guild_only()
    @commands.bot_has_permissions(manage_roles=True)
    async def verify(self, ctx, member: discord.Member):
        """Give a member the configured role, if you hold the configured granter role."""
        guild_conf = self._guild_conf(ctx.guild.id)
        granter_role_id = guild_conf["granter_role_id"]
        target_role_id = guild_conf["target_role_id"]

        if not granter_role_id or not target_role_id:
            await ctx.reply("Verification isn't configured yet — ask a mod to run `.verification`.")
            return
        if not any(role.id == granter_role_id for role in ctx.author.roles):
            await ctx.reply("You don't have permission to do that.")
            return

        target_role = ctx.guild.get_role(target_role_id)
        if target_role is None:
            await ctx.reply(
                "The configured role no longer exists — ask a mod to set a new one with "
                "`.verification target`."
            )
            return
        if target_role in member.roles:
            await ctx.reply(
                f"{member.mention} already has {target_role.mention}.",
                allowed_mentions=discord.AllowedMentions(roles=False),
            )
            return
        if not bot_outranks(ctx.guild, target_role):
            await ctx.reply("I can't assign a role that's equal to or higher than my own top role.")
            return

        await member.add_roles(target_role, reason=f"Granted by {ctx.author} via .verify")
        await self._send_welcome(ctx.guild, member)
        await ctx.reply(
            f"✅ Gave {member.mention} the {target_role.mention} role.",
            allowed_mentions=discord.AllowedMentions(roles=False),
        )


async def setup(bot):
    await bot.add_cog(Verification(bot))
