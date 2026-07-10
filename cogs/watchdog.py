import os
import time
import typing

import discord
from discord.ext import commands

from .storage import load_json, save_json_atomic

WATCHDOG_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "watchdog.json")


def _default_guild_config() -> dict:
    return {
        "mode": "shadow",  # "shadow" (detect + alert only) | "active" (also take action)
        "log_channel_id": None,
        "exempt_role_ids": [],
        "exempt_user_ids": [],
        "protected_role_ids": [],
        "lockdown": {
            "active": False,
            "started_at": None,
            "expires_at": None,
            "trigger_timestamps": [],
            "channel_overwrites": {},
            "protected_role_overwrites": {},
        },
    }


class Watchdog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config = load_json(WATCHDOG_FILE)

    def _save_config(self):
        save_json_atomic(WATCHDOG_FILE, self.config)

    def _guild_conf(self, guild_id: int) -> dict:
        return self.config.setdefault(str(guild_id), _default_guild_config())

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("You don't have permission to do that.")
        elif isinstance(error, commands.ChannelNotFound):
            await ctx.reply("I couldn't find that channel.")
        elif isinstance(error, commands.RoleNotFound):
            await ctx.reply("I couldn't find that role.")
        elif isinstance(error, commands.MemberNotFound):
            await ctx.reply("I couldn't find that member.")
        elif isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
            await ctx.reply(str(error) or "Invalid or missing argument.")
        else:
            raise error

    async def _status_embed(self, ctx, guild_conf) -> discord.Embed:
        log_channel = (
            ctx.guild.get_channel(guild_conf["log_channel_id"])
            if guild_conf["log_channel_id"]
            else None
        )
        protected_roles = [ctx.guild.get_role(rid) for rid in guild_conf["protected_role_ids"]]
        protected_roles = [r for r in protected_roles if r is not None]
        lockdown = guild_conf["lockdown"]

        embed = discord.Embed(title="🐕 Watchdog Status", color=discord.Color.blue())
        embed.add_field(name="Mode", value=guild_conf["mode"])
        embed.add_field(
            name="Log channel", value=log_channel.mention if log_channel else "Not set"
        )
        embed.add_field(
            name="Protected roles",
            value=", ".join(r.mention for r in protected_roles) if protected_roles else "None",
            inline=False,
        )
        embed.add_field(
            name="Exemptions",
            value=(
                f"{len(guild_conf['exempt_role_ids'])} role(s), "
                f"{len(guild_conf['exempt_user_ids'])} member(s)"
            ),
        )
        if lockdown["active"]:
            remaining = (
                max(0, int(lockdown["expires_at"] - time.time()))
                if lockdown["expires_at"]
                else None
            )
            value = f"🔒 ACTIVE — {remaining}s remaining" if remaining is not None else "🔒 ACTIVE"
        else:
            value = "Not active"
        embed.add_field(name="Lockdown", value=value, inline=False)
        return embed

    async def _exemptions_embed(self, ctx, guild_conf) -> discord.Embed:
        roles = [ctx.guild.get_role(rid) for rid in guild_conf["exempt_role_ids"]]
        roles = [r for r in roles if r is not None]
        members = [ctx.guild.get_member(uid) for uid in guild_conf["exempt_user_ids"]]
        members = [m for m in members if m is not None]

        embed = discord.Embed(title="🐕 Watchdog Exemptions", color=discord.Color.blue())
        embed.add_field(
            name="Roles", value=", ".join(r.mention for r in roles) if roles else "None",
            inline=False,
        )
        embed.add_field(
            name="Members",
            value=", ".join(m.mention for m in members) if members else "None",
            inline=False,
        )
        return embed

    async def _protected_roles_reply(self, ctx, guild_conf) -> str:
        roles = [ctx.guild.get_role(rid) for rid in guild_conf["protected_role_ids"]]
        roles = [r for r in roles if r is not None]
        return "Protected roles: " + (", ".join(r.mention for r in roles) if roles else "None")

    @commands.group(invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def watchdog(self, ctx):
        """Show the current watchdog configuration and status."""
        guild_conf = self._guild_conf(ctx.guild.id)
        await ctx.reply(embed=await self._status_embed(ctx, guild_conf))

    @watchdog.command(name="status")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def watchdog_status(self, ctx):
        """Show the current watchdog configuration and status."""
        guild_conf = self._guild_conf(ctx.guild.id)
        await ctx.reply(embed=await self._status_embed(ctx, guild_conf))

    @watchdog.command(name="mode")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def watchdog_mode(self, ctx, mode: str):
        """Set watchdog's mode: shadow (detect + alert only) or active (also take action)."""
        mode = mode.lower()
        if mode not in ("shadow", "active"):
            await ctx.reply("Mode must be `shadow` or `active`.")
            return
        guild_conf = self._guild_conf(ctx.guild.id)
        guild_conf["mode"] = mode
        self._save_config()
        await ctx.reply(f"🐕 Watchdog mode set to **{mode}**.")

    @watchdog.command(name="setlog")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def watchdog_setlog(self, ctx, channel: discord.TextChannel):
        """Set the channel watchdog alerts are posted to."""
        guild_conf = self._guild_conf(ctx.guild.id)
        guild_conf["log_channel_id"] = channel.id
        self._save_config()
        await ctx.reply(f"🐕 Watchdog alerts will be sent to {channel.mention}.")

    @watchdog.group(name="exempt", invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def watchdog_exempt(self, ctx):
        """Show watchdog's current exemption list."""
        guild_conf = self._guild_conf(ctx.guild.id)
        await ctx.reply(embed=await self._exemptions_embed(ctx, guild_conf))

    @watchdog_exempt.command(name="add")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def watchdog_exempt_add(
        self, ctx, target: typing.Union[discord.Role, discord.Member]
    ):
        """Exempt a role or member from all watchdog checks."""
        guild_conf = self._guild_conf(ctx.guild.id)
        if isinstance(target, discord.Role):
            ids, kind = guild_conf["exempt_role_ids"], "Role"
        else:
            ids, kind = guild_conf["exempt_user_ids"], "Member"
        if target.id not in ids:
            ids.append(target.id)
            self._save_config()
        await ctx.reply(f"✅ {kind} {target.mention} is now exempt from watchdog checks.")

    @watchdog_exempt.command(name="remove")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def watchdog_exempt_remove(
        self, ctx, target: typing.Union[discord.Role, discord.Member]
    ):
        """Remove a role or member's watchdog exemption."""
        guild_conf = self._guild_conf(ctx.guild.id)
        ids = (
            guild_conf["exempt_role_ids"]
            if isinstance(target, discord.Role)
            else guild_conf["exempt_user_ids"]
        )
        if target.id in ids:
            ids.remove(target.id)
            self._save_config()
            await ctx.reply(f"❌ {target.mention} is no longer exempt.")
        else:
            await ctx.reply(f"{target.mention} wasn't exempt.")

    @watchdog_exempt.command(name="list")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def watchdog_exempt_list(self, ctx):
        """List watchdog's current exemptions."""
        guild_conf = self._guild_conf(ctx.guild.id)
        await ctx.reply(embed=await self._exemptions_embed(ctx, guild_conf))

    @watchdog.group(name="protectedrole", invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def watchdog_protectedrole(self, ctx):
        """Show the roles exempt from watchdog lockdowns."""
        guild_conf = self._guild_conf(ctx.guild.id)
        await ctx.reply(await self._protected_roles_reply(ctx, guild_conf))

    @watchdog_protectedrole.command(name="add")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def watchdog_protectedrole_add(self, ctx, role: discord.Role):
        """Exempt a role from watchdog lockdowns (it keeps send permission)."""
        guild_conf = self._guild_conf(ctx.guild.id)
        if role.id not in guild_conf["protected_role_ids"]:
            guild_conf["protected_role_ids"].append(role.id)
            self._save_config()
        await ctx.reply(f"🛡️ {role.mention} is now protected from watchdog lockdowns.")

    @watchdog_protectedrole.command(name="remove")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def watchdog_protectedrole_remove(self, ctx, role: discord.Role):
        """Remove a role's protection from watchdog lockdowns."""
        guild_conf = self._guild_conf(ctx.guild.id)
        if role.id in guild_conf["protected_role_ids"]:
            guild_conf["protected_role_ids"].remove(role.id)
            self._save_config()
            await ctx.reply(f"{role.mention} is no longer protected.")
        else:
            await ctx.reply(f"{role.mention} wasn't protected.")

    @watchdog_protectedrole.command(name="list")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def watchdog_protectedrole_list(self, ctx):
        """List the roles exempt from watchdog lockdowns."""
        guild_conf = self._guild_conf(ctx.guild.id)
        await ctx.reply(await self._protected_roles_reply(ctx, guild_conf))

    @watchdog.command(name="unlock")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def watchdog_unlock(self, ctx):
        """Manually end an active watchdog lockdown."""
        guild_conf = self._guild_conf(ctx.guild.id)
        if not guild_conf["lockdown"]["active"]:
            await ctx.reply("There is no active watchdog lockdown.")
            return
        # Real lockdown-lifting logic lands once the lockdown mechanism itself does.
        await ctx.reply("Lockdown lifting isn't implemented yet.")


async def setup(bot):
    await bot.add_cog(Watchdog(bot))
