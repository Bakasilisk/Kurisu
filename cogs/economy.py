import random
import time

import discord
from discord.ext import commands

from .management import cog_enabled, common_error_reply, has_permissions_or_owner, rank_of, require_outranks
from .storage import data_path, load_json, save_json_atomic

ECONOMY_FILE = data_path("economy.json")

PAYDAY_AMOUNT = 120
PAYDAY_COOLDOWN_SECONDS = 12 * 60 * 60
COINFLIP_MIN_BET = 10
COINFLIP_MAX_BET = 1000


def _format_cooldown(seconds) -> str:
    total_minutes = max(1, int(seconds) // 60)
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


class Economy(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.data = load_json(ECONOMY_FILE)

    def _save(self):
        save_json_atomic(ECONOMY_FILE, self.data)

    def _guild_bank(self, guild_id) -> dict:
        return self.data.setdefault(str(guild_id), {})

    def _account(self, guild_id, user_id) -> dict:
        """Mutating lookup: returns the user's entry in the guild bank, creating
        the guild bank and/or the entry (with default balance/last_payday) if
        missing. Use for commands that go on to modify the returned dict."""
        return self._guild_bank(guild_id).setdefault(str(user_id), {"balance": 0, "last_payday": 0.0})

    def _account_readonly(self, guild_id, user_id) -> dict:
        """Non-mutating lookup: same default entry as `_account`, but never
        creates/persists a guild bank or account entry. Use for read-only
        commands (e.g. `balance`) so looking someone up doesn't start
        persisting an empty account for them."""
        return self.data.get(str(guild_id), {}).get(str(user_id), {"balance": 0, "last_payday": 0.0})

    async def cog_check(self, ctx):
        return ctx.guild is None or cog_enabled(self.bot, ctx.guild.id, "economy")

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

    @commands.command()
    @commands.guild_only()
    async def payday(self, ctx):
        """Collect your payday bits (once every 12 hours)."""
        entry = self._account(ctx.guild.id, ctx.author.id)

        now = time.time()
        remaining = PAYDAY_COOLDOWN_SECONDS - (now - entry["last_payday"])
        if remaining > 0:
            await ctx.reply(
                f"⏳ You've already collected your payday. Try again in {_format_cooldown(remaining)}."
            )
            return

        entry["balance"] += PAYDAY_AMOUNT
        entry["last_payday"] = now
        self._save()

        guild_bank = self._guild_bank(ctx.guild.id)
        position = rank_of(guild_bank.items(), lambda kv: kv[1]["balance"], str(ctx.author.id))

        embed = discord.Embed(
            title="💰 Payday",
            description=f"{ctx.author.mention} collected **{PAYDAY_AMOUNT} bits**!",
            color=discord.Color.dark_gold(),
        )
        embed.add_field(name="Balance", value=f"{entry['balance']} bits")
        embed.add_field(name="Server Rank", value=f"#{position}")
        await ctx.reply(embed=embed)

    @commands.command(name="balance", aliases=["bal"])
    @commands.guild_only()
    async def balance(self, ctx, member: discord.Member = None):
        """Show your (or another member's) bits balance."""
        member = member or ctx.author
        entry = self._account_readonly(ctx.guild.id, member.id)
        await ctx.reply(f"{member.mention} has **{entry['balance']} bits**.")

    @commands.command(name="richest")
    @commands.guild_only()
    async def richest(self, ctx, top: int = 10):
        """Show the server's bits leaderboard."""
        top = max(1, min(top, 25))
        guild_bank = self.data.get(str(ctx.guild.id), {})
        if not guild_bank:
            await ctx.reply("Nobody has any bits yet.")
            return

        sorted_members = sorted(guild_bank.items(), key=lambda kv: kv[1]["balance"], reverse=True)[:top]
        lines = []
        for i, (user_id, entry) in enumerate(sorted_members, start=1):
            member = ctx.guild.get_member(int(user_id))
            name = member.mention if member else f"<@{user_id}>"
            lines.append(f"**#{i}** {name} — {entry['balance']} bits")

        embed = discord.Embed(
            title=f"🏆 {ctx.guild.name} Bits Leaderboard",
            description="\n".join(lines),
            color=discord.Color.dark_gold(),
        )
        await ctx.reply(embed=embed)

    @commands.command(name="setbits")
    @has_permissions_or_owner(moderate_members=True)
    @commands.guild_only()
    async def setbits(self, ctx, member: discord.Member, amount: int):
        """Set a member's bits balance."""
        if not await require_outranks(self.bot, ctx, member, "set bits for"):
            return
        if amount < 0:
            await ctx.reply("Amount can't be negative.")
            return
        entry = self._account(ctx.guild.id, member.id)
        entry["balance"] = amount
        self._save()
        await ctx.reply(f"✅ Set {member.mention}'s balance to **{amount} bits**.")

    @commands.command(name="give")
    @commands.guild_only()
    async def give(self, ctx, member: discord.Member, amount: int):
        """Give some of your bits to another member."""
        if amount <= 0:
            await ctx.reply("Amount must be positive.")
            return
        if member.id == ctx.author.id:
            await ctx.reply("You can't give bits to yourself.")
            return
        if member.bot:
            await ctx.reply("You can't give bits to a bot.")
            return

        sender = self._account(ctx.guild.id, ctx.author.id)
        if sender["balance"] < amount:
            await ctx.reply(f"You don't have enough bits (you have {sender['balance']}).")
            return

        receiver = self._account(ctx.guild.id, member.id)
        sender["balance"] -= amount
        receiver["balance"] += amount
        self._save()
        await ctx.reply(f"✅ {ctx.author.mention} gave **{amount} bits** to {member.mention}.")

    @commands.command(name="coinflip", aliases=["cf"])
    @commands.guild_only()
    async def coinflip(self, ctx, amount: int):
        """Bet bits on a coin flip."""
        if amount < COINFLIP_MIN_BET:
            await ctx.reply(f"Minimum bet is {COINFLIP_MIN_BET} bits.")
            return
        if amount > COINFLIP_MAX_BET:
            await ctx.reply(f"Maximum bet is {COINFLIP_MAX_BET} bits.")
            return

        entry = self._account(ctx.guild.id, ctx.author.id)
        if entry["balance"] < amount:
            await ctx.reply(f"You don't have enough bits (you have {entry['balance']}).")
            return

        if random.random() < 0.5:
            entry["balance"] += amount
            reply = f"🪙 Heads! You won **{amount} bits**. New balance: {entry['balance']}."
        else:
            entry["balance"] -= amount
            reply = f"🪙 Tails! You lost **{amount} bits**. New balance: {entry['balance']}."
        self._save()
        await ctx.reply(reply)


async def setup(bot):
    await bot.add_cog(Economy(bot))
