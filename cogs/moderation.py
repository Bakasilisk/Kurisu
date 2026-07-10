import os
import re
from datetime import timedelta

import discord
from discord.ext import commands

from .storage import load_json, save_json_atomic

WARNINGS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "warnings.json")
LOCKS_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "channel_locks.json")

DURATION_RE = re.compile(r"^(\d+)([smhd])$")
DURATION_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def parse_duration(text: str) -> timedelta:
    match = DURATION_RE.match(text.strip().lower())
    if not match:
        raise commands.BadArgument(
            "Invalid duration. Use a number followed by s/m/h/d, e.g. `10m`, `2h`, `1d`."
        )
    amount, unit = match.groups()
    return timedelta(**{DURATION_UNITS[unit]: int(amount)})


def snapshot_overwrite(channel, target) -> dict | None:
    """Capture a channel's permission overwrite for a role/member as a JSON-safe dict,
    or None if no explicit overwrite currently exists for that target."""
    if target not in channel.overwrites:
        return None
    allow, deny = channel.overwrites_for(target).pair()
    return {"allow": allow.value, "deny": deny.value}


async def restore_overwrite(channel, target, snapshot: dict | None, *, reason: str):
    """Restore a channel's permission overwrite for a role/member to a previously captured
    snapshot (or clear it entirely if there was none)."""
    if snapshot is None:
        await channel.set_permissions(target, overwrite=None, reason=reason)
    else:
        overwrite = discord.PermissionOverwrite.from_pair(
            discord.Permissions(snapshot["allow"]), discord.Permissions(snapshot["deny"])
        )
        await channel.set_permissions(target, overwrite=overwrite, reason=reason)


class Moderation(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.warnings = self._load_warnings()
        self.locks = load_json(LOCKS_FILE)

    def _load_warnings(self) -> dict:
        return load_json(WARNINGS_FILE)

    def _save_warnings(self):
        save_json_atomic(WARNINGS_FILE, self.warnings)

    def _save_locks(self):
        save_json_atomic(LOCKS_FILE, self.locks)

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.reply("You don't have permission to do that.")
        elif isinstance(error, commands.BotMissingPermissions):
            await ctx.reply("I don't have permission to do that.")
        elif isinstance(error, commands.MemberNotFound):
            await ctx.reply("I couldn't find that member.")
        elif isinstance(error, commands.UserNotFound):
            await ctx.reply("I couldn't find that user.")
        elif isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
            await ctx.reply(str(error) or "Invalid or missing argument.")
        else:
            raise error

    @commands.command()
    @commands.has_permissions(kick_members=True)
    @commands.bot_has_permissions(kick_members=True)
    @commands.guild_only()
    async def kick(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        """Kick a member from the server."""
        if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            await ctx.reply("You can't kick someone with an equal or higher role than you.")
            return
        await member.kick(reason=f"{ctx.author}: {reason}")
        await ctx.reply(f"👢 Kicked {member.mention} — {reason}")

    @commands.command()
    @commands.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    @commands.guild_only()
    async def ban(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        """Ban a member from the server."""
        if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            await ctx.reply("You can't ban someone with an equal or higher role than you.")
            return
        await member.ban(reason=f"{ctx.author}: {reason}", delete_message_days=0)
        await ctx.reply(f"🔨 Banned {member.mention} — {reason}")

    @commands.command()
    @commands.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    @commands.guild_only()
    async def unban(self, ctx, user: discord.User, *, reason: str = "No reason provided"):
        """Unban a user by ID or exact username."""
        await ctx.guild.unban(user, reason=f"{ctx.author}: {reason}")
        await ctx.reply(f"✅ Unbanned {user.mention} — {reason}")

    @commands.command(aliases=["mute"])
    @commands.has_permissions(moderate_members=True)
    @commands.bot_has_permissions(moderate_members=True)
    @commands.guild_only()
    async def timeout(
        self, ctx, member: discord.Member, duration: str, *, reason: str = "No reason provided"
    ):
        """Time out a member for a given duration (e.g. 10m, 2h, 1d)."""
        if member.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            await ctx.reply("You can't time out someone with an equal or higher role than you.")
            return
        delta = parse_duration(duration)
        if delta > timedelta(days=28):
            await ctx.reply("Timeouts can't exceed 28 days.")
            return
        await member.timeout(delta, reason=f"{ctx.author}: {reason}")
        await ctx.reply(f"🔇 Timed out {member.mention} for {duration} — {reason}")

    @commands.command(aliases=["unmute"])
    @commands.has_permissions(moderate_members=True)
    @commands.bot_has_permissions(moderate_members=True)
    @commands.guild_only()
    async def untimeout(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        """Remove an active timeout from a member."""
        await member.timeout(None, reason=f"{ctx.author}: {reason}")
        await ctx.reply(f"🔊 Removed timeout from {member.mention}")

    @commands.command()
    @commands.has_permissions(moderate_members=True)
    @commands.guild_only()
    async def warn(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        """Warn a member and record it."""
        guild_warnings = self.warnings.setdefault(str(ctx.guild.id), {})
        member_warnings = guild_warnings.setdefault(str(member.id), [])
        member_warnings.append(
            {
                "reason": reason,
                "moderator_id": ctx.author.id,
                "timestamp": discord.utils.utcnow().isoformat(),
            }
        )
        self._save_warnings()

        try:
            await member.send(f"You were warned in **{ctx.guild.name}**: {reason}")
        except discord.Forbidden:
            pass

        await ctx.reply(
            f"⚠️ Warned {member.mention} — {reason} (total: {len(member_warnings)})"
        )

    @commands.command(name="warnings", aliases=["warnlist"])
    @commands.has_permissions(moderate_members=True)
    @commands.guild_only()
    async def warnings_(self, ctx, member: discord.Member):
        """List a member's warnings."""
        member_warnings = self.warnings.get(str(ctx.guild.id), {}).get(str(member.id), [])
        if not member_warnings:
            await ctx.reply(f"{member.mention} has no warnings.")
            return

        embed = discord.Embed(title=f"Warnings for {member}", color=discord.Color.orange())
        for i, warning in enumerate(member_warnings, start=1):
            moderator = ctx.guild.get_member(warning["moderator_id"])
            embed.add_field(
                name=f"#{i} — {warning['timestamp'][:10]}",
                value=f"By {moderator.mention if moderator else warning['moderator_id']}: "
                f"{warning['reason']}",
                inline=False,
            )
        await ctx.reply(embed=embed)

    @warnings_.error
    async def warnings_error(self, ctx, error):
        await self.cog_command_error(ctx, error)

    @commands.command()
    @commands.has_permissions(moderate_members=True)
    @commands.guild_only()
    async def clearwarnings(self, ctx, member: discord.Member):
        """Clear all warnings for a member."""
        guild_warnings = self.warnings.get(str(ctx.guild.id), {})
        count = len(guild_warnings.pop(str(member.id), []))
        self._save_warnings()
        await ctx.reply(f"🧹 Cleared {count} warning(s) for {member.mention}")

    @commands.command(aliases=["clear"])
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    @commands.guild_only()
    async def purge(self, ctx, amount: int, member: discord.Member = None):
        """Bulk-delete messages in the current channel, optionally filtered by member."""
        if not 1 <= amount <= 100:
            await ctx.reply("Please choose an amount between 1 and 100.")
            return

        def check(msg):
            return member is None or msg.author == member

        deleted = await ctx.channel.purge(limit=amount + 1, check=check)
        confirmation = await ctx.send(f"🧹 Deleted {len(deleted) - 1} message(s).")
        await confirmation.delete(delay=5)

    @commands.command()
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    @commands.guild_only()
    async def slowmode(self, ctx, seconds: int):
        """Set the slowmode delay (in seconds) for the current channel. Use 0 to disable."""
        if not 0 <= seconds <= 21600:
            await ctx.reply("Slowmode must be between 0 and 21600 seconds (6 hours).")
            return
        await ctx.channel.edit(slowmode_delay=seconds)
        if seconds == 0:
            await ctx.reply("🐇 Slowmode disabled.")
        else:
            await ctx.reply(f"🐌 Slowmode set to {seconds} second(s).")

    @commands.command()
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_roles=True)
    @commands.guild_only()
    async def lock(self, ctx, *, reason: str = "No reason provided"):
        """Prevent @everyone from sending messages in the current channel."""
        channel_id = str(ctx.channel.id)
        if channel_id not in self.locks:
            # Only capture on the first lock, so a second !lock on an already-locked
            # channel doesn't clobber the true pre-lock snapshot.
            self.locks[channel_id] = snapshot_overwrite(ctx.channel, ctx.guild.default_role)
            self._save_locks()

        overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = False
        await ctx.channel.set_permissions(
            ctx.guild.default_role, overwrite=overwrite, reason=f"{ctx.author}: {reason}"
        )
        await ctx.reply(f"🔒 Channel locked — {reason}")

    @commands.command()
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_roles=True)
    @commands.guild_only()
    async def unlock(self, ctx, *, reason: str = "No reason provided"):
        """Allow @everyone to send messages in the current channel again."""
        channel_id = str(ctx.channel.id)
        # None if never locked via !lock (e.g. state lost to a restart) — falls back
        # to clearing the overwrite entirely, the old (safe) behavior.
        snapshot = self.locks.get(channel_id)
        # Only pop/persist once the restore actually succeeds — if it raises, the
        # snapshot must survive so a retry (or a later !unlock) can still recover it.
        await restore_overwrite(
            ctx.channel, ctx.guild.default_role, snapshot, reason=f"{ctx.author}: {reason}"
        )
        self.locks.pop(channel_id, None)
        self._save_locks()
        await ctx.reply(f"🔓 Channel unlocked — {reason}")


async def setup(bot):
    await bot.add_cog(Moderation(bot))
