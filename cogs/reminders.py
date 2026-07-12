import logging
import time
from datetime import timedelta

import discord
from discord.ext import commands, tasks

from .management import cog_enabled, common_error_reply, reply_ephemeral_aware
from .moderation import parse_duration
from .storage import data_path, load_json, save_json_atomic

logger = logging.getLogger(__name__)

REMINDERS_FILE = data_path("reminders.json")
POLL_INTERVAL_SECONDS = 30
MAX_REMINDERS_PER_USER = 10
MAX_DURATION = timedelta(days=90)
MAX_TEXT_LENGTH = 500
LATE_THRESHOLD_SECONDS = 120


class Reminders(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.data = load_json(REMINDERS_FILE)
        # Deliberate deviation from the per-guild-keyed convention documented in
        # CLAUDE.md: reminders are user-scoped timed events consumed by one global
        # delivery loop, not per-guild config — nesting under guild id would force
        # per-guild scans on every tick and break the guild-less DM fallback.
        # user_id/channel_id are stored as JSON numbers (they're values here, not
        # dict keys, so the str-key convention doesn't apply to them); small counter
        # ids are safe as slash `int` options (no snowflake 2^53 issue).
        self.data.setdefault("next_id", 1)
        self.data.setdefault("reminders", [])
        if not self._deliver.is_running():
            self._deliver.start()

    def cog_unload(self):
        self._deliver.cancel()

    def _save(self):
        save_json_atomic(REMINDERS_FILE, self.data)

    async def cog_check(self, ctx):
        return ctx.guild is None or cog_enabled(self.bot, ctx.guild.id, "reminders")

    @staticmethod
    async def _reply(ctx, *args, **kwargs):
        """ctx.reply, but ephemeral (visible only to the invoker) when the
        command was invoked via / rather than the text prefix."""
        return await reply_ephemeral_aware(ctx, *args, **kwargs)

    async def cog_command_error(self, ctx, error):
        if await common_error_reply(ctx, error, reply=lambda *a, **k: self._reply(ctx, *a, **k)):
            return
        raise error

    # --- Delivery loop -----------------------------------------------------------

    async def _fire(self, reminder: dict):
        """Deliver one reminder. Never raises — a failed channel send falls back
        to a DM, and a failed DM is logged and dropped."""
        content = f"⏰ <@{reminder['user_id']}> Reminder: {reminder['text']}"
        late = time.time() - reminder["fire_at"]
        if late > LATE_THRESHOLD_SECONDS:
            content += " *(sorry this is late — I was offline when it was due)*"

        channel = self.bot.get_channel(reminder["channel_id"])
        if channel is not None:
            try:
                await channel.send(content)
                return
            except (discord.Forbidden, discord.HTTPException):
                pass

        user = self.bot.get_user(reminder["user_id"])
        if user is None:
            try:
                user = await self.bot.fetch_user(reminder["user_id"])
            except discord.HTTPException:
                user = None
        if user is not None:
            try:
                await user.send(content)
                return
            except (discord.Forbidden, discord.HTTPException):
                pass

        logger.warning(
            "Couldn't deliver reminder #%s to user %s — dropped.", reminder["id"], reminder["user_id"]
        )

    @tasks.loop(seconds=POLL_INTERVAL_SECONDS)
    async def _deliver(self):
        now = time.time()
        due = [r for r in self.data["reminders"] if r["fire_at"] <= now]
        if not due:
            return
        for reminder in due:
            await self._fire(reminder)
        # Remove by id set rather than re-filtering on time — remindme can append
        # new reminders to self.data["reminders"] during the awaits above.
        due_ids = {r["id"] for r in due}
        self.data["reminders"] = [r for r in self.data["reminders"] if r["id"] not in due_ids]
        self._save()

    @_deliver.before_loop
    async def _before_deliver(self):
        await self.bot.wait_until_ready()

    # --- Commands ------------------------------------------------------------------

    @commands.hybrid_command(name="remindme", description="Set a reminder for yourself.")
    async def remindme(self, ctx, duration: str, *, text: str):
        """Set a reminder — e.g. `.remindme 2h walk the dog` (units: s/m/h/d)."""
        try:
            delta = parse_duration(duration)
        except commands.BadArgument as e:
            # parse_duration is called directly (not as a converter), so a raised
            # BadArgument would otherwise arrive wrapped in CommandInvokeError.
            await self._reply(ctx, str(e))
            return

        if delta > MAX_DURATION:
            await self._reply(ctx, f"Reminders can't be set more than {MAX_DURATION.days} days out.")
            return
        if len(text) > MAX_TEXT_LENGTH:
            await self._reply(ctx, f"Reminder text can't exceed {MAX_TEXT_LENGTH} characters.")
            return

        user_reminders = [r for r in self.data["reminders"] if r["user_id"] == ctx.author.id]
        if len(user_reminders) >= MAX_REMINDERS_PER_USER:
            await self._reply(
                ctx,
                f"You already have {MAX_REMINDERS_PER_USER} pending reminders — "
                f"cancel one with `forget` first.",
            )
            return

        fire_at = time.time() + delta.total_seconds()
        reminder = {
            "id": self.data["next_id"],
            "user_id": ctx.author.id,
            "channel_id": ctx.channel.id,
            "fire_at": fire_at,
            "text": text,
        }
        self.data["reminders"].append(reminder)
        self.data["next_id"] += 1
        self._save()

        await self._reply(ctx, f"⏰ Reminder **#{reminder['id']}** set — I'll remind you <t:{int(fire_at)}:R>.")

    @commands.hybrid_command(name="reminders", description="List your pending reminders.")
    async def list_reminders(self, ctx):
        """List your pending reminders."""
        user_reminders = sorted(
            (r for r in self.data["reminders"] if r["user_id"] == ctx.author.id),
            key=lambda r: r["fire_at"],
        )
        if not user_reminders:
            await self._reply(ctx, "You have no pending reminders.")
            return

        lines = [
            f"**#{r['id']}** — <t:{int(r['fire_at'])}:R> — {r['text'][:80]}" for r in user_reminders
        ]
        embed = discord.Embed(title="Your Reminders", description="\n".join(lines), color=discord.Color.blurple())
        await self._reply(ctx, embed=embed)

    @commands.hybrid_command(name="forget", description="Cancel a pending reminder by ID.")
    async def forget(self, ctx, reminder_id: int):
        """Cancel a pending reminder by ID."""
        for i, r in enumerate(self.data["reminders"]):
            if r["id"] == reminder_id and r["user_id"] == ctx.author.id:
                del self.data["reminders"][i]
                self._save()
                await self._reply(ctx, f"🗑️ Cancelled reminder #{reminder_id}.")
                return
        await self._reply(ctx, "You don't have a reminder with that ID.")


async def setup(bot):
    await bot.add_cog(Reminders(bot))
