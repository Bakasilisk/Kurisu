import asyncio
import os
import random
import time

import discord
from discord.ext import commands, tasks

from .management import cog_enabled
from .storage import load_json, save_json_atomic

XP_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "xp.json")

XP_MIN = 15
XP_MAX = 25
XP_COOLDOWN_SECONDS = 60
XP_FLUSH_INTERVAL_SECONDS = 30


def total_xp_for_level(level: int) -> int:
    """Cumulative XP required to reach the given level from 0."""
    return 25 * level * (level + 1)


def level_from_xp(xp: int) -> int:
    """The level corresponding to a total XP amount."""
    level = 0
    while total_xp_for_level(level + 1) <= xp:
        level += 1
    return level


class Leveling(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.xp = load_json(XP_FILE)
        self._cooldowns = {}
        self._dirty = False
        self._xp_lock = asyncio.Lock()
        self._flush_xp.start()

    def cog_unload(self):
        self._flush_xp.cancel()
        # cog_unload can't be a coroutine, so it can't await self._xp_lock.
        # If a flush is already in flight on the thread pool, that write
        # already carries the latest data, so skip ours rather than race it.
        if self._dirty and not self._xp_lock.locked():
            self._dirty = False
            save_json_atomic(XP_FILE, self._snapshot())

    def _snapshot(self) -> dict:
        """A shallow copy safe to hand to a background thread for serialization."""
        return {guild_id: dict(members) for guild_id, members in self.xp.items()}

    @tasks.loop(seconds=XP_FLUSH_INTERVAL_SECONDS)
    async def _flush_xp(self):
        async with self._xp_lock:
            if self._dirty:
                self._dirty = False
                await asyncio.to_thread(save_json_atomic, XP_FILE, self._snapshot())

    @_flush_xp.before_loop
    async def _before_flush_xp(self):
        await self.bot.wait_until_ready()

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("You don't have permission to do that.")
        elif isinstance(error, commands.MemberNotFound):
            await ctx.reply("I couldn't find that member.")
        elif isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
            await ctx.reply(str(error) or "Invalid or missing argument.")
        else:
            raise error

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild:
            return
        if not cog_enabled(self.bot, message.guild.id, "leveling"):
            return

        key = (message.guild.id, message.author.id)
        now = time.monotonic()
        last = self._cooldowns.get(key)
        if last is not None and now - last < XP_COOLDOWN_SECONDS:
            return
        self._cooldowns[key] = now

        guild_xp = self.xp.setdefault(str(message.guild.id), {})
        user_id = str(message.author.id)
        old_xp = guild_xp.get(user_id, 0)
        old_level = level_from_xp(old_xp)

        new_xp = old_xp + random.randint(XP_MIN, XP_MAX)
        guild_xp[user_id] = new_xp
        self._dirty = True

        new_level = level_from_xp(new_xp)
        if new_level > old_level:
            await message.channel.send(
                f"🎉 {message.author.mention} leveled up to **level {new_level}**!"
            )

    @commands.command(aliases=["level"])
    @commands.guild_only()
    async def rank(self, ctx, member: discord.Member = None):
        """Show your (or another member's) level and XP."""
        member = member or ctx.author
        guild_xp = self.xp.get(str(ctx.guild.id), {})
        member_id = str(member.id)
        total_xp = guild_xp.get(member_id, 0)

        current_level = level_from_xp(total_xp)
        level_floor = total_xp_for_level(current_level)
        level_ceiling = total_xp_for_level(current_level + 1)

        position = None
        if member_id in guild_xp:
            sorted_members = sorted(guild_xp.items(), key=lambda kv: kv[1], reverse=True)
            position = next(
                i for i, (uid, _) in enumerate(sorted_members, start=1) if uid == member_id
            )

        embed = discord.Embed(title=f"{member.display_name}'s Rank", color=discord.Color.gold())
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Level", value=str(current_level))
        embed.add_field(name="XP", value=f"{total_xp - level_floor}/{level_ceiling - level_floor}")
        embed.add_field(name="Total XP", value=str(total_xp))
        embed.add_field(name="Server Rank", value=f"#{position}" if position else "Unranked")
        await ctx.reply(embed=embed)

    @commands.command(name="leaderboard", aliases=["lb", "top"])
    @commands.guild_only()
    async def leaderboard(self, ctx, top: int = 10):
        """Show the server's XP leaderboard."""
        top = max(1, min(top, 25))
        guild_xp = self.xp.get(str(ctx.guild.id), {})
        if not guild_xp:
            await ctx.reply("Nobody has earned any XP yet.")
            return

        sorted_members = sorted(guild_xp.items(), key=lambda kv: kv[1], reverse=True)[:top]
        lines = []
        for i, (user_id, xp_amount) in enumerate(sorted_members, start=1):
            member = ctx.guild.get_member(int(user_id))
            name = member.mention if member else f"<@{user_id}>"
            lines.append(f"**#{i}** {name} — Level {level_from_xp(xp_amount)} ({xp_amount} XP)")

        embed = discord.Embed(
            title=f"🏆 {ctx.guild.name} Leaderboard",
            description="\n".join(lines),
            color=discord.Color.gold(),
        )
        await ctx.reply(embed=embed)

    @commands.command()
    @commands.has_permissions(moderate_members=True)
    @commands.guild_only()
    async def resetxp(self, ctx, member: discord.Member):
        """Reset a member's XP and level."""
        guild_xp = self.xp.get(str(ctx.guild.id), {})
        had_xp = guild_xp.pop(str(member.id), None) is not None
        async with self._xp_lock:
            self._dirty = False
            await asyncio.to_thread(save_json_atomic, XP_FILE, self._snapshot())
        if had_xp:
            await ctx.reply(f"🧹 Reset XP for {member.mention}")
        else:
            await ctx.reply(f"{member.mention} has no XP to reset.")


async def setup(bot):
    await bot.add_cog(Leveling(bot))
