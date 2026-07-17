import asyncio
import copy
import random
import time
from datetime import datetime, time as dt_time, timezone

import discord
from discord.ext import commands, tasks

from .management import cog_enabled, common_error_reply, has_permissions_or_owner, rank_of, require_outranks
from .storage import data_path, load_json, save_json_atomic

XP_FILE = data_path("xp.json")
MESSAGES_FILE = data_path("messages.json")

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
        self.messages = load_json(MESSAGES_FILE)
        self._cooldowns = {}
        self._dirty = False
        self._messages_dirty = False
        self._xp_lock = asyncio.Lock()
        self._last_monthly_reset = None
        if not self._flush_xp.is_running():
            self._flush_xp.start()
        if not self._monthly_reset.is_running():
            self._monthly_reset.start()

    def cog_unload(self):
        self._flush_xp.cancel()
        self._monthly_reset.cancel()
        # cog_unload can't be a coroutine, so it can't await self._xp_lock.
        # If a flush is already in flight on the thread pool, that write
        # already carries the latest data, so skip ours rather than race it.
        if not self._xp_lock.locked():
            if self._dirty:
                self._dirty = False
                save_json_atomic(XP_FILE, self._snapshot())
            if self._messages_dirty:
                self._messages_dirty = False
                save_json_atomic(MESSAGES_FILE, self._messages_snapshot())

    def _snapshot(self) -> dict:
        """A deep copy safe to hand to a background thread for serialization."""
        return copy.deepcopy(self.xp)

    def _messages_snapshot(self) -> dict:
        """A deep copy of messages for background serialization."""
        return copy.deepcopy(self.messages)

    def _today_str(self) -> str:
        """Return today's date in YYYY-MM-DD format (UTC)."""
        return datetime.now(timezone.utc).date().isoformat()

    async def _save_with_dirty_flag(self, dirty_attr: str, file_path: str, data: dict) -> None:
        """Save data to file and reset dirty flag on success, or set it on failure for retry."""
        try:
            await asyncio.to_thread(save_json_atomic, file_path, data)
            setattr(self, dirty_attr, False)
        except Exception:
            setattr(self, dirty_attr, True)
            raise

    @tasks.loop(seconds=XP_FLUSH_INTERVAL_SECONDS)
    async def _flush_xp(self):
        async with self._xp_lock:
            if self._dirty:
                await self._save_with_dirty_flag("_dirty", XP_FILE, self._snapshot())
            if self._messages_dirty:
                await self._save_with_dirty_flag("_messages_dirty", MESSAGES_FILE, self._messages_snapshot())

    @_flush_xp.before_loop
    async def _before_flush_xp(self):
        await self.bot.wait_until_ready()

    @tasks.loop(time=dt_time(0, 0, tzinfo=timezone.utc))
    async def _monthly_reset(self):
        today = datetime.now(timezone.utc).date()
        if today.day == 1 and self._last_monthly_reset != today:
            self._last_monthly_reset = today
            async with self._xp_lock:
                self.messages.clear()
                await self._save_with_dirty_flag("_messages_dirty", MESSAGES_FILE, {})

    @_monthly_reset.before_loop
    async def _before_monthly_reset(self):
        await self.bot.wait_until_ready()

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.MemberNotFound):
            await ctx.reply("I couldn't find that member.")
        elif isinstance(error, commands.CheckAnyFailure):
            # A CheckFailure sibling, not a MissingPermissions subclass — common_error_reply
            # doesn't recognize it and would otherwise silently swallow it as a bare CheckFailure.
            await ctx.reply("You don't have permission to do that.")
        elif await common_error_reply(ctx, error):
            return
        else:
            raise error

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not message.guild:
            return
        if not cog_enabled(self.bot, message.guild.id, "leveling"):
            return

        ctx = await self.bot.get_context(message)
        if ctx.command is not None:
            return

        user_id = str(message.author.id)
        today = self._today_str()

        async with self._xp_lock:
            guild_messages = self.messages.setdefault(str(message.guild.id), {})
            user_messages = guild_messages.get(user_id, {})
            if user_messages.get("date") != today:
                user_messages = {"date": today, "count": 0}
            user_messages["count"] += 1
            guild_messages[user_id] = user_messages
            self._messages_dirty = True

        key = (message.guild.id, message.author.id)
        now = time.monotonic()
        last = self._cooldowns.get(key)
        if last is not None and now - last < XP_COOLDOWN_SECONDS:
            return
        self._cooldowns[key] = now

        async with self._xp_lock:
            guild_xp = self.xp.setdefault(str(message.guild.id), {})
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

    def _messages_today_unsafe(self, guild_id: int, user_id: int) -> int:
        """Get the message count for today, or 0 if not found or stale. Must be called under _xp_lock."""
        guild_messages = self.messages.get(str(guild_id), {})
        user_messages = guild_messages.get(str(user_id), {})
        if user_messages.get("date") == self._today_str():
            return user_messages.get("count", 0)
        return 0

    @commands.command(aliases=["level"])
    @commands.guild_only()
    async def rank(self, ctx, member: discord.Member = None):
        """Show your (or another member's) level and XP."""
        member = member or ctx.author
        member_id = str(member.id)

        async with self._xp_lock:
            guild_xp = self.xp.get(str(ctx.guild.id), {})
            total_xp = guild_xp.get(member_id, 0)
            xp_items = list(guild_xp.items()) if member_id in guild_xp else []
            messages_today = self._messages_today_unsafe(ctx.guild.id, member.id)

        current_level = level_from_xp(total_xp)
        level_floor = total_xp_for_level(current_level)
        level_ceiling = total_xp_for_level(current_level + 1)

        position = rank_of(xp_items, key=lambda kv: kv[1], target_id=member_id)

        embed = discord.Embed(title=f"{member.display_name}'s Rank", color=discord.Color.gold())
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Level", value=str(current_level))
        embed.add_field(name="XP", value=f"{total_xp - level_floor}/{level_ceiling - level_floor}")
        embed.add_field(name="Total XP", value=str(total_xp))
        embed.add_field(name="Messages Today", value=str(messages_today))
        embed.add_field(name="Server Rank", value=f"#{position}" if position else "Unranked")
        await ctx.reply(embed=embed)

    @commands.command(name="leaderboard", aliases=["lb", "top"])
    @commands.guild_only()
    async def leaderboard(self, ctx, top: int = 9):
        """Show the server's XP leaderboard as a 3x3 grid."""
        top = max(1, min(top, 9))

        async with self._xp_lock:
            guild_xp = self.xp.get(str(ctx.guild.id), {})
            if not guild_xp:
                await ctx.reply("Nobody has earned any XP yet.")
                return
            xp_items = list(guild_xp.items())

        sorted_members = sorted(xp_items, key=lambda kv: kv[1], reverse=True)[:top]

        embed = discord.Embed(
            title=f"🏆 {ctx.guild.name} Leaderboard",
            color=discord.Color.gold(),
        )
        for i, (user_id, xp_amount) in enumerate(sorted_members, start=1):
            member = ctx.guild.get_member(int(user_id))
            name = member.display_name if member else f"Unknown ({user_id})"
            messages_today = self._messages_today_unsafe(ctx.guild.id, user_id)
            embed.add_field(
                name=f"#{i} **{name}**",
                value=f"Level {level_from_xp(xp_amount)} | {xp_amount} XP | {messages_today} msgs today",
                inline=True,
            )
        await ctx.reply(embed=embed)

    @commands.command()
    @has_permissions_or_owner(moderate_members=True)
    @commands.guild_only()
    async def resetxp(self, ctx, member: discord.Member):
        """Reset a member's XP and level."""
        if not await require_outranks(self.bot, ctx, member, "reset XP for"):
            return
        async with self._xp_lock:
            guild_xp = self.xp.get(str(ctx.guild.id), {})
            had_xp = guild_xp.pop(str(member.id), None) is not None
            await self._save_with_dirty_flag("_dirty", XP_FILE, self._snapshot())
        if had_xp:
            await ctx.reply(f"🧹 Reset XP for {member.mention}")
        else:
            await ctx.reply(f"{member.mention} has no XP to reset.")

    @commands.command(name="setxp")
    @has_permissions_or_owner(moderate_members=True)
    @commands.guild_only()
    async def setxp(self, ctx, member: discord.Member, amount: int):
        """Set a member's total XP."""
        if not await require_outranks(self.bot, ctx, member, "set XP for"):
            return
        if amount < 0:
            await ctx.reply("Amount can't be negative.")
            return
        async with self._xp_lock:
            guild_xp = self.xp.setdefault(str(ctx.guild.id), {})
            guild_xp[str(member.id)] = amount
            await self._save_with_dirty_flag("_dirty", XP_FILE, self._snapshot())
        await ctx.reply(f"✅ Set {member.mention}'s XP to **{amount}** (Level {level_from_xp(amount)}).")


async def setup(bot):
    await bot.add_cog(Leveling(bot))
