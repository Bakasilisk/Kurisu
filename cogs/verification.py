import os

import discord
from discord.ext import commands

from .storage import load_json, save_json_atomic

VERIFICATION_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "verification.json")


def _default_guild_config() -> dict:
    return {"granter_role_id": None, "target_role_id": None}


class Verification(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config = load_json(VERIFICATION_FILE)

    def _save_config(self):
        save_json_atomic(VERIFICATION_FILE, self.config)

    def _guild_conf(self, guild_id: int) -> dict:
        return self.config.setdefault(str(guild_id), _default_guild_config())

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("You don't have permission to do that.")
        elif isinstance(error, commands.BotMissingPermissions):
            await ctx.reply("I don't have permission to do that.")
        elif isinstance(error, commands.RoleNotFound):
            await ctx.reply("I couldn't find that role.")
        elif isinstance(error, commands.MemberNotFound):
            await ctx.reply("I couldn't find that member.")
        elif isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
            await ctx.reply(str(error) or "Invalid or missing argument.")
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
        await ctx.reply(
            f"Granter role: {granter.mention if granter else 'Not set'}\n"
            f"Role granted: {target.mention if target else 'Not set'}\n"
            f"Members with the granter role can run `.verify @member`."
        )

    @verification.command(name="granter")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def verification_granter(self, ctx, role: discord.Role):
        """Set the role someone must hold to use .verify."""
        guild_conf = self._guild_conf(ctx.guild.id)
        guild_conf["granter_role_id"] = role.id
        self._save_config()
        await ctx.reply(f"✅ {role.mention} can now use `.verify`.")

    @verification.command(name="target")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def verification_target(self, ctx, role: discord.Role):
        """Set the role .verify assigns to its target."""
        guild_conf = self._guild_conf(ctx.guild.id)
        guild_conf["target_role_id"] = role.id
        self._save_config()
        await ctx.reply(f"✅ `.verify` now grants {role.mention}.")

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
            await ctx.reply(f"{member.mention} already has {target_role.mention}.")
            return
        if target_role >= ctx.guild.me.top_role:
            await ctx.reply("I can't assign a role that's equal to or higher than my own top role.")
            return

        await member.add_roles(target_role, reason=f"Granted by {ctx.author} via .verify")
        await ctx.reply(f"✅ Gave {member.mention} the {target_role.mention} role.")


async def setup(bot):
    await bot.add_cog(Verification(bot))
