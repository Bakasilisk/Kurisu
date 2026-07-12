import asyncio
import io
import logging
import os
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Literal

import discord
from discord.ext import commands, tasks
from PIL import Image, ImageDraw, ImageFont

from .management import cog_enabled, has_permissions_or_owner, rank_of, reply_ephemeral_aware
from .storage import data_path

logger = logging.getLogger(__name__)

# First non-JSON cog: raw time-series + aggregation strains the "whole file in
# memory" JSON pattern the rest of the repo uses, so this one owns a small
# SQLite database instead (stdlib sqlite3, no new dependency). All DB logic
# stays inside this module — a helper file in cogs/ would be picked up by
# management._discover_cogs() as a phantom toggleable cog.
STATS_DB = os.environ.get("STATS_DB_PATH") or data_path("stats.db")
FONT_PATH = data_path(os.path.join("assets", "fonts", "DejaVuSans.ttf"))

# Config: module constants, no live config command — matches palantir's
# deferred-config convention (tune later if real usage justifies it).
RETENTION_DAYS = 0  # 0 = keep everything; >0 enables the nightly sweep
FLUSH_INTERVAL_SECONDS = 30
TREND_PERIOD_DAYS = 30
TOP_N = 5
TOP_N_MAX = 25
BACKFILL_DEFAULT_DAYS = 0  # 0 = entire server history (all readable channels)
BACKFILL_CHANNEL_SLEEP = 1.0  # seconds between channels during backfill

BAR_WIDTH = 20
SPARK_GLYPHS = "▁▂▃▄▅▆▇█"

Period = Literal["week", "month", "year", "all"]


# --- Presentation helpers (module-level, no self needed) --------------------


def _bar(fraction: float, width: int = BAR_WIDTH) -> str:
    fraction = max(0.0, min(1.0, fraction))
    filled = round(fraction * width)
    return "█" * filled + "░" * (width - filled)


def _sparkline(values: list[float]) -> str:
    if not values:
        return ""
    vmax = max(values) or 1
    return "".join(
        SPARK_GLYPHS[min(len(SPARK_GLYPHS) - 1, int((v / vmax) * (len(SPARK_GLYPHS) - 1)))]
        for v in values
    )


def _format_distribution(entries: list[tuple[str, int]], limit: int, unit=str) -> str:
    """Bar-chart lines for the top `limit` entries (label, count) sorted desc,
    plus a "…and N others X%" remainder line so percentages always sum to
    ~100% even though only the top slice is rendered. `entries` must contain
    every item (not just the ones being displayed) for the total to be right."""
    total = sum(count for _, count in entries)
    if not entries or total == 0:
        return "No data yet."
    lines = []
    shown = 0
    for label, count in entries[:limit]:
        pct = count / total * 100
        shown += count
        lines.append(f"{_bar(count / total)} {label} — {unit(count)} ({pct:.1f}%)")
    remainder_n = len(entries) - min(limit, len(entries))
    remainder_count = total - shown
    if remainder_n > 0:
        pct = remainder_count / total * 100
        lines.append(f"…and {remainder_n} others — {unit(remainder_count)} ({pct:.1f}%)")
    return "\n".join(lines)


def _render_heatmap(grid: list[list[int]], weekday_labels: list[str]) -> io.BytesIO:
    """Render a 7 (weekday) x 24 (hour) heatmap PNG, cells color-scaled
    light-to-dark by count, axis labels drawn with the bundled DejaVu Sans
    font (never PIL's default font — see captions.py's FONT_PATH note).
    Raises on any failure; the caller falls back to a unicode-bar summary."""
    cell = 22
    margin_left = 34
    margin_top = 26
    margin_bottom = 8
    margin_right = 8
    width = margin_left + 24 * cell + margin_right
    height = margin_top + 7 * cell + margin_bottom

    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(FONT_PATH, size=11)

    vmax = max((v for row in grid for v in row), default=0) or 1
    low = (233, 238, 252)  # near-white blue: zero activity
    high = (25, 55, 150)  # deep blue: peak activity

    for weekday in range(7):
        for hour in range(24):
            count = grid[weekday][hour]
            t = count / vmax
            color = tuple(int(low[i] + (high[i] - low[i]) * t) for i in range(3))
            x0 = margin_left + hour * cell
            y0 = margin_top + weekday * cell
            draw.rectangle([x0, y0, x0 + cell - 1, y0 + cell - 1], fill=color, outline="#cccccc")

    for weekday, label in enumerate(weekday_labels):
        y = margin_top + weekday * cell + cell / 2
        draw.text((3, y), label, font=font, fill="black", anchor="lm")

    for hour in range(0, 24, 3):
        x = margin_left + hour * cell + cell / 2
        draw.text((x, margin_top - 4), str(hour), font=font, fill="black", anchor="mb")

    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    buffer.seek(0)
    return buffer


class Stats(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

        self._db = sqlite3.connect(STATS_DB, check_same_thread=False)
        # Guards every access to self._db — asyncio.to_thread hands DB calls
        # to the default thread pool, and check_same_thread=False lets any of
        # those worker threads use the connection, so this lock is what
        # actually serializes them (same reasoning as the JSON cogs' locks,
        # just for a shared connection instead of an in-memory dict).
        self._db_lock = threading.Lock()
        self._init_schema()

        # In-memory accumulators, drained to SQLite by _flush_stats every
        # FLUSH_INTERVAL_SECONDS (the repo's buffer-then-flush rhythm — see
        # leveling/palantir). Keyed by the same tuples as each table's
        # primary key; values are mutable lists so increments are in place.
        self._msg_acc: dict[tuple[int, str, int, int], list[int]] = defaultdict(lambda: [0, 0, 0])
        self._hourly_acc: dict[tuple[int, str, int, int], int] = defaultdict(int)
        self._reaction_acc: dict[tuple[int, str, int], list[int]] = defaultdict(lambda: [0, 0])
        self._voice_acc: dict[tuple[int, str, int], int] = defaultdict(int)
        self._membership_acc: dict[tuple[int, str], list[int]] = defaultdict(lambda: [0, 0])
        self._acc_lock = asyncio.Lock()
        self._dirty = False

        # (guild_id, user_id) -> monotonic timestamp of the last voice-channel
        # join/move, used to compute elapsed seconds on the next leave/move.
        # Best-effort: an in-flight session is lost across a restart.
        self._voice_sessions: dict[tuple[int, int], float] = {}

        self._backfill_in_progress: set[int] = set()

        if not self._flush_stats.is_running():
            self._flush_stats.start()
        if RETENTION_DAYS > 0 and not self._sweep_stats.is_running():
            self._sweep_stats.start()

    def cog_unload(self):
        self._flush_stats.cancel()
        if self._sweep_stats.is_running():
            self._sweep_stats.cancel()
        # cog_unload can't be a coroutine, so it can't await self._acc_lock.
        # If a flush is already in flight on the thread pool, that write
        # already carries the latest data, so skip ours rather than race it
        # (same reasoning as leveling/palantir's cog_unload).
        if not self._acc_lock.locked() and self._dirty:
            snapshot = self._drain_accumulators_unsafe()
            try:
                self._write_snapshot(*snapshot)
            except Exception:
                logger.exception("Stats: final flush on unload failed")
        self._db.close()

    # --- Schema ---------------------------------------------------------

    def _init_schema(self) -> None:
        with self._db_lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    guild_id INTEGER NOT NULL,
                    day TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    count INTEGER NOT NULL DEFAULT 0,
                    words INTEGER NOT NULL DEFAULT 0,
                    chars INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, day, user_id, channel_id)
                )
                """
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_guild_day ON messages (guild_id, day)"
            )
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS hourly (
                    guild_id INTEGER NOT NULL,
                    day TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    hour INTEGER NOT NULL,
                    count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, day, user_id, hour)
                )
                """
            )
            # Supports the guild-wide hour-of-day / heatmap aggregation, which
            # sums over all users (no user_id filter) — the PK's leading
            # (guild_id, day) prefix covers it, but an explicit index keeps
            # that intent clear and independent of PK column order.
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_hourly_guild_day ON hourly (guild_id, day)"
            )
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS reactions (
                    guild_id INTEGER NOT NULL,
                    day TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    given INTEGER NOT NULL DEFAULT 0,
                    received INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, day, user_id)
                )
                """
            )
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS voice (
                    guild_id INTEGER NOT NULL,
                    day TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    seconds INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, day, user_id)
                )
                """
            )
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS membership (
                    guild_id INTEGER NOT NULL,
                    day TEXT NOT NULL,
                    joins INTEGER NOT NULL DEFAULT 0,
                    leaves INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, day)
                )
                """
            )
            self._db.commit()

    # --- Buffer -> flush --------------------------------------------------

    def _drain_accumulators_unsafe(self):
        """Snapshot and clear every accumulator dict. Caller must already
        hold _acc_lock, or (only at shutdown in cog_unload) be certain
        nothing else can be touching it concurrently."""
        snapshot = (
            dict(self._msg_acc),
            dict(self._hourly_acc),
            dict(self._reaction_acc),
            dict(self._voice_acc),
            dict(self._membership_acc),
        )
        self._msg_acc.clear()
        self._hourly_acc.clear()
        self._reaction_acc.clear()
        self._voice_acc.clear()
        self._membership_acc.clear()
        self._dirty = False
        return snapshot

    def _merge_back(self, snapshot) -> None:
        """Restore a drained-but-failed-to-write snapshot into the live
        accumulators (additively, so it combines correctly with anything
        accumulated in the meantime) — the retry path for a flush failure."""
        msg, hourly, reactions, voice, membership = snapshot
        for key, vals in msg.items():
            cur = self._msg_acc[key]
            cur[0] += vals[0]
            cur[1] += vals[1]
            cur[2] += vals[2]
        for key, count in hourly.items():
            self._hourly_acc[key] += count
        for key, vals in reactions.items():
            cur = self._reaction_acc[key]
            cur[0] += vals[0]
            cur[1] += vals[1]
        for key, seconds in voice.items():
            self._voice_acc[key] += seconds
        for key, vals in membership.items():
            cur = self._membership_acc[key]
            cur[0] += vals[0]
            cur[1] += vals[1]

    def _write_snapshot(self, msg, hourly, reactions, voice, membership) -> None:
        """Blocking: executemany UPSERT each non-empty accumulator into its
        table. Called via asyncio.to_thread from _flush_now, or directly
        (blocking is fine — we're shutting down) from cog_unload."""
        with self._db_lock:
            if msg:
                self._db.executemany(
                    """
                    INSERT INTO messages (guild_id, day, user_id, channel_id, count, words, chars)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(guild_id, day, user_id, channel_id) DO UPDATE SET
                        count = count + excluded.count,
                        words = words + excluded.words,
                        chars = chars + excluded.chars
                    """,
                    [(g, d, u, c, v[0], v[1], v[2]) for (g, d, u, c), v in msg.items()],
                )
            if hourly:
                self._db.executemany(
                    """
                    INSERT INTO hourly (guild_id, day, user_id, hour, count)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(guild_id, day, user_id, hour) DO UPDATE SET count = count + excluded.count
                    """,
                    [(g, d, u, h, c) for (g, d, u, h), c in hourly.items()],
                )
            if reactions:
                self._db.executemany(
                    """
                    INSERT INTO reactions (guild_id, day, user_id, given, received)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(guild_id, day, user_id) DO UPDATE SET
                        given = given + excluded.given,
                        received = received + excluded.received
                    """,
                    [(g, d, u, v[0], v[1]) for (g, d, u), v in reactions.items()],
                )
            if voice:
                self._db.executemany(
                    """
                    INSERT INTO voice (guild_id, day, user_id, seconds)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(guild_id, day, user_id) DO UPDATE SET seconds = seconds + excluded.seconds
                    """,
                    [(g, d, u, s) for (g, d, u), s in voice.items()],
                )
            if membership:
                self._db.executemany(
                    """
                    INSERT INTO membership (guild_id, day, joins, leaves)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(guild_id, day) DO UPDATE SET
                        joins = joins + excluded.joins,
                        leaves = leaves + excluded.leaves
                    """,
                    [(g, d, v[0], v[1]) for (g, d), v in membership.items()],
                )
            self._db.commit()

    async def _flush_now(self) -> None:
        async with self._acc_lock:
            if not self._dirty:
                return
            snapshot = self._drain_accumulators_unsafe()
        try:
            await asyncio.to_thread(self._write_snapshot, *snapshot)
        except Exception:
            logger.exception("Stats: flush to sqlite failed, buffered counts will retry next cycle")
            async with self._acc_lock:
                self._merge_back(snapshot)
                self._dirty = True

    @tasks.loop(seconds=FLUSH_INTERVAL_SECONDS)
    async def _flush_stats(self):
        await self._flush_now()

    @_flush_stats.before_loop
    async def _before_flush_stats(self):
        await self.bot.wait_until_ready()

    def _sweep_old_rows(self, cutoff: str) -> None:
        with self._db_lock:
            for table in ("messages", "hourly", "reactions", "voice", "membership"):
                self._db.execute(f"DELETE FROM {table} WHERE day < ?", (cutoff,))
            self._db.commit()

    @tasks.loop(hours=24)
    async def _sweep_stats(self):
        cutoff = self._day_str(datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS))
        await asyncio.to_thread(self._sweep_old_rows, cutoff)

    @_sweep_stats.before_loop
    async def _before_sweep_stats(self):
        await self.bot.wait_until_ready()

    # --- Query helpers ----------------------------------------------------

    def _query(self, sql: str, params: tuple) -> list[tuple]:
        with self._db_lock:
            return self._db.execute(sql, params).fetchall()

    async def _fetch(self, sql: str, params: tuple = ()) -> list[tuple]:
        # Flush first so a query reflects the last few seconds of buffered
        # activity too, not just what was already on disk before the last
        # periodic flush — otherwise `.stats` right after a burst of
        # messages would look stale for up to FLUSH_INTERVAL_SECONDS.
        await self._flush_now()
        return await asyncio.to_thread(self._query, sql, params)

    @staticmethod
    def _day_str(dt: datetime) -> str:
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")

    @staticmethod
    def _period_start(period: Period) -> str | None:
        if period == "all":
            return None
        days = {"week": 7, "month": 30, "year": 365}[period]
        return Stats._day_str(datetime.now(timezone.utc) - timedelta(days=days))

    @staticmethod
    def _member_label(guild: discord.Guild, user_id: int) -> str:
        member = guild.get_member(user_id)
        return member.mention if member else f"Unknown (`{user_id}`)"

    @staticmethod
    def _format_duration(seconds) -> str:
        seconds = int(seconds or 0)
        hours, remainder = divmod(seconds, 3600)
        minutes = remainder // 60
        return f"{hours}h {minutes}m" if hours else f"{minutes}m"

    # --- Ingest -------------------------------------------------------------

    async def _accumulate_message(
        self, guild_id: int, day: str, hour: int, user_id: int, channel_id: int, *, words: int, chars: int
    ) -> None:
        """Shared by on_message and the backfill task, so both feed the same
        accumulators/flush path (backfill only ever calls this one)."""
        async with self._acc_lock:
            vals = self._msg_acc[(guild_id, day, user_id, channel_id)]
            vals[0] += 1
            vals[1] += words
            vals[2] += chars
            self._hourly_acc[(guild_id, day, user_id, hour)] += 1
            self._dirty = True

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        guild = message.guild
        if guild is None or message.author.bot:
            return
        if not cog_enabled(self.bot, guild.id, "stats"):
            return
        day = self._day_str(message.created_at)
        hour = message.created_at.astimezone(timezone.utc).hour
        await self._accumulate_message(
            guild.id, day, hour, message.author.id, message.channel.id,
            words=len(message.content.split()), chars=len(message.content),
        )

    async def _handle_reaction(self, reaction: discord.Reaction, user, *, delta: int) -> None:
        message = reaction.message
        guild = message.guild
        if guild is None or user.bot:
            return
        if not cog_enabled(self.bot, guild.id, "stats"):
            return
        day = self._day_str(discord.utils.utcnow())
        author = message.author
        async with self._acc_lock:
            self._reaction_acc[(guild.id, day, user.id)][0] += delta
            # A self-reaction hits the same accumulator key for both indices
            # (given[0] and received[1]) via defaultdict's identity-per-key
            # lookup — no special-casing needed for author == user.
            if author is not None and not author.bot:
                self._reaction_acc[(guild.id, day, author.id)][1] += delta
            self._dirty = True

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction: discord.Reaction, user) -> None:
        # Non-raw: needs reaction.message.author from discord.py's connection
        # cache without an extra API call. Limitation: only fires for
        # messages still in that cache (recent ones) — accepted trade-off to
        # avoid per-reaction fetches and rate-limit risk.
        await self._handle_reaction(reaction, user, delta=1)

    @commands.Cog.listener()
    async def on_reaction_remove(self, reaction: discord.Reaction, user) -> None:
        await self._handle_reaction(reaction, user, delta=-1)

    async def _accumulate_voice_seconds(self, guild_id: int, user_id: int, elapsed: float) -> None:
        if elapsed <= 0:
            return
        day = self._day_str(discord.utils.utcnow())
        async with self._acc_lock:
            self._voice_acc[(guild_id, day, user_id)] += int(elapsed)
            self._dirty = True

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        if before.channel == after.channel or member.bot:
            return
        guild = member.guild
        if not cog_enabled(self.bot, guild.id, "stats"):
            return
        key = (guild.id, member.id)
        now = time.monotonic()
        if before.channel is None and after.channel is not None:
            self._voice_sessions[key] = now
            return
        # Leaving voice entirely, or moving to a different channel — either
        # way the current session ends here; credited to the end day per the
        # plan. A move immediately opens a fresh session in the new channel.
        start = self._voice_sessions.pop(key, None)
        if start is not None:
            await self._accumulate_voice_seconds(guild.id, member.id, now - start)
        if after.channel is not None:
            self._voice_sessions[key] = now

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return
        guild = member.guild
        if not cog_enabled(self.bot, guild.id, "stats"):
            return
        day = self._day_str(discord.utils.utcnow())
        async with self._acc_lock:
            self._membership_acc[(guild.id, day)][0] += 1
            self._dirty = True

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if member.bot:
            return
        guild = member.guild
        if not cog_enabled(self.bot, guild.id, "stats"):
            return
        day = self._day_str(discord.utils.utcnow())
        async with self._acc_lock:
            self._membership_acc[(guild.id, day)][1] += 1
            self._dirty = True

    # --- Commands -----------------------------------------------------------

    @staticmethod
    async def _reply(ctx, *args, **kwargs):
        """ctx.reply, but ephemeral (visible only to the invoker) when the
        command was invoked via / rather than the text prefix."""
        return await reply_ephemeral_aware(ctx, *args, **kwargs)

    async def _require_admin(self, ctx, action: str) -> bool:
        """Inline Manage-Server-or-owner gate for `stats user` on another
        member — mirrors has_permissions_or_owner's check but applied
        conditionally (only when member != ctx.author), so it can't be a
        plain decorator. `quietest`/`backfill` gate unconditionally, so they
        use the has_permissions_or_owner decorator directly instead."""
        if ctx.author.guild_permissions.manage_guild or await self.bot.is_owner(ctx.author):
            return True
        await self._reply(ctx, f"You need Manage Server to {action}.")
        return False

    async def cog_check(self, ctx):
        if ctx.guild is None or await self.bot.is_owner(ctx.author):
            return True
        return cog_enabled(self.bot, ctx.guild.id, "stats")

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.MemberNotFound):
            await self._reply(ctx, "I couldn't find that member.")
        elif isinstance(error, commands.CheckAnyFailure):
            # A CheckFailure sibling, not a MissingPermissions subclass — raised by
            # has_permissions_or_owner on stats_quietest/stats_backfill.
            await self._reply(ctx, "You don't have permission to do that.")
        elif isinstance(error, commands.CheckFailure):
            return
        elif isinstance(error, commands.MissingPermissions):
            await self._reply(ctx, "You don't have permission to do that.")
        elif isinstance(error, commands.BotMissingPermissions):
            await self._reply(ctx, "I don't have permission to do that.")
        elif isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
            await self._reply(ctx, str(error) or "Invalid or missing argument.")
        else:
            raise error

    async def _show_server(self, ctx) -> None:
        guild = ctx.guild
        totals = await self._fetch(
            "SELECT COALESCE(SUM(count),0), COALESCE(SUM(words),0), MIN(day) FROM messages WHERE guild_id = ?",
            (guild.id,),
        )
        total_count, total_words, first_day = totals[0]

        trend_start = self._day_str(datetime.now(timezone.utc) - timedelta(days=TREND_PERIOD_DAYS))
        prior_start = self._day_str(datetime.now(timezone.utc) - timedelta(days=2 * TREND_PERIOD_DAYS))
        recent = await self._fetch(
            "SELECT COALESCE(SUM(count),0), COUNT(DISTINCT user_id) FROM messages "
            "WHERE guild_id = ? AND day >= ?",
            (guild.id, trend_start),
        )
        recent_count, active_members = recent[0]
        prior = await self._fetch(
            "SELECT COALESCE(SUM(count),0) FROM messages WHERE guild_id = ? AND day >= ? AND day < ?",
            (guild.id, prior_start, trend_start),
        )
        prior_count = prior[0][0]

        if prior_count:
            trend_pct = (recent_count - prior_count) / prior_count * 100
            arrow = "▲" if trend_pct >= 0 else "▼"
            trend_text = f"{arrow} {abs(trend_pct):.1f}% vs prior {TREND_PERIOD_DAYS}d"
        elif recent_count:
            trend_text = "▲ new activity (no prior-period data yet)"
        else:
            trend_text = "No recent activity"

        if first_day:
            first_date = datetime.strptime(first_day, "%Y-%m-%d").date()
            elapsed_days = max(1, (datetime.now(timezone.utc).date() - first_date).days + 1)
        else:
            elapsed_days = 1
        avg_day = total_count / elapsed_days

        reaction_total = await self._fetch(
            "SELECT COALESCE(SUM(given),0) FROM reactions WHERE guild_id = ?", (guild.id,)
        )
        voice_total = await self._fetch(
            "SELECT COALESCE(SUM(seconds),0) FROM voice WHERE guild_id = ?", (guild.id,)
        )
        top_rows = await self._fetch(
            "SELECT user_id, SUM(count) as c FROM messages WHERE guild_id = ? GROUP BY user_id ORDER BY c DESC",
            (guild.id,),
        )
        entries = [(self._member_label(guild, uid), c) for uid, c in top_rows]

        embed = discord.Embed(title=f"📊 {guild.name} — Stats", color=discord.Color.blurple())
        embed.add_field(name="Total messages", value=f"{total_count:,}", inline=True)
        embed.add_field(name=f"Active members ({TREND_PERIOD_DAYS}d)", value=str(active_members), inline=True)
        embed.add_field(name="Trend", value=trend_text, inline=True)
        embed.add_field(
            name="Averages",
            value=f"{avg_day:.1f}/day · {avg_day * 7:.0f}/week · {avg_day * 30:.0f}/month · {avg_day * 365:.0f}/year",
            inline=False,
        )
        embed.add_field(
            name="Words / Reactions / Voice",
            value=(
                f"{total_words:,} words · {reaction_total[0][0]:,} reactions · "
                f"{self._format_duration(voice_total[0][0])}"
            ),
            inline=False,
        )
        embed.add_field(
            name=f"Top {min(TOP_N, len(entries))} posters", value=_format_distribution(entries, TOP_N), inline=False
        )
        await self._reply(ctx, embed=embed)

    @commands.hybrid_group(
        invoke_without_command=True, fallback="server",
        description="Show server-wide message statistics.",
    )
    @commands.guild_only()
    async def stats(self, ctx):
        """Show server-wide message statistics."""
        await self._show_server(ctx)

    # with_app_command=False: the group's fallback="server" above already
    # registers the slash-side `/stats server`; this only adds the
    # prefix-side `.stats server` (typing the subcommand name explicitly),
    # mirroring palantir's status/fallback pattern.
    @stats.command(name="server", with_app_command=False)
    @commands.guild_only()
    async def stats_server(self, ctx):
        """Show server-wide message statistics."""
        await self._show_server(ctx)

    @stats.command(name="user", description="Show one member's message statistics.")
    @commands.guild_only()
    async def stats_user(self, ctx, member: discord.Member = None, n: int = TOP_N):
        """Show one member's stats — your own by default, or another member's
        (requires Manage Server). `n` controls how many of their top channels
        are shown."""
        member = member or ctx.author
        if member.id != ctx.author.id and not await self._require_admin(ctx, "view another member's stats"):
            return
        n = max(1, min(n, TOP_N_MAX))
        guild = ctx.guild

        msg_row = await self._fetch(
            "SELECT COALESCE(SUM(count),0), COALESCE(SUM(words),0), COUNT(DISTINCT day), MIN(day) "
            "FROM messages WHERE guild_id = ? AND user_id = ?",
            (guild.id, member.id),
        )
        total_count, total_words, active_days, first_day = msg_row[0]

        server_row = await self._fetch("SELECT COALESCE(SUM(count),0) FROM messages WHERE guild_id = ?", (guild.id,))
        server_total = server_row[0][0]

        rank_rows = await self._fetch(
            "SELECT user_id, SUM(count) as c FROM messages WHERE guild_id = ? GROUP BY user_id", (guild.id,)
        )
        rank = rank_of(rank_rows, key=lambda kv: kv[1], target_id=member.id)

        reaction_row = await self._fetch(
            "SELECT COALESCE(SUM(given),0), COALESCE(SUM(received),0) FROM reactions "
            "WHERE guild_id = ? AND user_id = ?",
            (guild.id, member.id),
        )
        given, received = reaction_row[0]

        voice_row = await self._fetch(
            "SELECT COALESCE(SUM(seconds),0) FROM voice WHERE guild_id = ? AND user_id = ?",
            (guild.id, member.id),
        )
        voice_seconds = voice_row[0][0]

        # Busiest hour: this member's own hourly rows, summed per hour-of-day.
        busiest_hour_row = await self._fetch(
            "SELECT hour, SUM(count) as c FROM hourly WHERE guild_id = ? AND user_id = ? "
            "GROUP BY hour ORDER BY c DESC LIMIT 1",
            (guild.id, member.id),
        )
        busiest_hour = f"{busiest_hour_row[0][0]:02d}:00 UTC" if busiest_hour_row else "—"

        channel_rows = await self._fetch(
            "SELECT channel_id, SUM(count) as c FROM messages WHERE guild_id = ? AND user_id = ? "
            "GROUP BY channel_id ORDER BY c DESC",
            (guild.id, member.id),
        )
        channel_entries = [(f"<#{cid}>", c) for cid, c in channel_rows]

        if first_day:
            first_date = datetime.strptime(first_day, "%Y-%m-%d").date()
            elapsed_days = max(1, (datetime.now(timezone.utc).date() - first_date).days + 1)
        else:
            elapsed_days = 1
        avg_day = total_count / elapsed_days
        pct_of_server = (total_count / server_total * 100) if server_total else 0.0
        words_per_msg = (total_words / total_count) if total_count else 0.0

        embed = discord.Embed(title=f"📊 {member.display_name}'s Stats", color=discord.Color.blurple())
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="Total messages", value=f"{total_count:,}", inline=True)
        embed.add_field(name="Server rank", value=f"#{rank}" if rank else "Unranked", inline=True)
        embed.add_field(name="% of server", value=f"{pct_of_server:.1f}%", inline=True)
        embed.add_field(
            name="Averages", value=f"{avg_day:.1f}/day · {avg_day * 7:.0f}/week · {avg_day * 30:.0f}/month",
            inline=False,
        )
        embed.add_field(name="Active days", value=str(active_days), inline=True)
        embed.add_field(name="Busiest hour", value=busiest_hour, inline=True)
        embed.add_field(name="Words/message", value=f"{words_per_msg:.1f}", inline=True)
        embed.add_field(name="Voice time", value=self._format_duration(voice_seconds), inline=True)
        embed.add_field(name="Reactions given/received", value=f"{given:,} / {received:,}", inline=False)
        if channel_entries:
            embed.add_field(
                name=f"Top {min(n, len(channel_entries))} channels",
                value=_format_distribution(channel_entries, n),
                inline=False,
            )
        await self._reply(ctx, embed=embed)

    @stats.command(name="top", description="Show the top message posters.")
    @commands.guild_only()
    async def stats_top(self, ctx, period: Period = "all", n: int = TOP_N):
        """Show the top-n message posters, with % share (period: week/month/year/all)."""
        n = max(1, min(n, TOP_N_MAX))
        start_day = self._period_start(period)
        sql = "SELECT user_id, SUM(count) as c FROM messages WHERE guild_id = ?"
        params = [ctx.guild.id]
        if start_day:
            sql += " AND day >= ?"
            params.append(start_day)
        sql += " GROUP BY user_id ORDER BY c DESC"
        rows = await self._fetch(sql, tuple(params))
        entries = [(self._member_label(ctx.guild, uid), c) for uid, c in rows]
        embed = discord.Embed(title=f"🏆 Top Posters — {period}", color=discord.Color.gold())
        embed.description = _format_distribution(entries, n)
        await self._reply(ctx, embed=embed)

    @stats.command(name="channels", description="Show the busiest channels.")
    @commands.guild_only()
    async def stats_channels(self, ctx, period: Period = "all", n: int = TOP_N):
        """Show the busiest channels by message count (period: week/month/year/all)."""
        n = max(1, min(n, TOP_N_MAX))
        start_day = self._period_start(period)
        sql = "SELECT channel_id, SUM(count) as c FROM messages WHERE guild_id = ?"
        params = [ctx.guild.id]
        if start_day:
            sql += " AND day >= ?"
            params.append(start_day)
        sql += " GROUP BY channel_id ORDER BY c DESC"
        rows = await self._fetch(sql, tuple(params))
        entries = [(f"<#{cid}>", c) for cid, c in rows]
        embed = discord.Embed(title=f"📊 Busiest Channels — {period}", color=discord.Color.blurple())
        embed.description = _format_distribution(entries, n)
        await self._reply(ctx, embed=embed)

    @stats.command(name="activity", description="Show hour/weekday activity patterns.")
    @commands.guild_only()
    async def stats_activity(self, ctx, period: Period = "month"):
        """Show peak hour/weekday activity, plus an hour x weekday heatmap image."""
        start_day = self._period_start(period)
        # Guild-wide: SUM across all users (no user_id filter), grouped by the
        # (day, hour) pair — weekday is derived from `day` below.
        sql = "SELECT day, hour, SUM(count) FROM hourly WHERE guild_id = ?"
        params = [ctx.guild.id]
        if start_day:
            sql += " AND day >= ?"
            params.append(start_day)
        sql += " GROUP BY day, hour"
        rows = await self._fetch(sql, tuple(params))

        grid = [[0] * 24 for _ in range(7)]
        for day, hour, count in rows:
            try:
                weekday = datetime.strptime(day, "%Y-%m-%d").weekday()
            except ValueError:
                continue
            grid[weekday][hour] += count

        hour_totals = [sum(grid[wd][hr] for wd in range(7)) for hr in range(24)]
        weekday_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        weekday_totals = [sum(grid[wd]) for wd in range(7)]
        total = sum(hour_totals)

        if total == 0:
            await self._reply(ctx, "No message activity recorded yet for this period.")
            return

        peak_hour = max(range(24), key=lambda h: hour_totals[h])
        peak_weekday = max(range(7), key=lambda w: weekday_totals[w])
        summary = (
            f"**Peak hour:** {peak_hour:02d}:00 UTC ({hour_totals[peak_hour]:,} messages)\n"
            f"**Peak day:** {weekday_labels[peak_weekday]} ({weekday_totals[peak_weekday]:,} messages)\n"
            f"**Hourly pattern (00-23 UTC):** `{_sparkline(hour_totals)}`"
        )

        try:
            buffer = await asyncio.to_thread(_render_heatmap, grid, weekday_labels)
            file = discord.File(buffer, filename="activity_heatmap.png")
            embed = discord.Embed(
                title=f"📊 Activity — {period}", description=summary, color=discord.Color.blurple()
            )
            embed.set_image(url="attachment://activity_heatmap.png")
            await self._reply(ctx, embed=embed, file=file)
        except Exception:
            # Best-effort image: never fail the whole reply over a render problem.
            logger.exception("Stats: heatmap render failed, falling back to text summary")
            embed = discord.Embed(
                title=f"📊 Activity — {period} (image unavailable)", description=summary,
                color=discord.Color.blurple(),
            )
            await self._reply(ctx, embed=embed)

    @stats.command(name="voice", description="Show the top members by voice time.")
    @commands.guild_only()
    async def stats_voice(self, ctx, period: Period = "all", n: int = TOP_N):
        """Show the top-n members by voice time (period: week/month/year/all)."""
        n = max(1, min(n, TOP_N_MAX))
        start_day = self._period_start(period)
        sql = "SELECT user_id, SUM(seconds) as s FROM voice WHERE guild_id = ?"
        params = [ctx.guild.id]
        if start_day:
            sql += " AND day >= ?"
            params.append(start_day)
        sql += " GROUP BY user_id ORDER BY s DESC"
        rows = await self._fetch(sql, tuple(params))
        entries = [(self._member_label(ctx.guild, uid), s) for uid, s in rows]
        embed = discord.Embed(title=f"🔊 Voice Time — {period}", color=discord.Color.teal())
        embed.description = _format_distribution(entries, n, unit=self._format_duration)
        await self._reply(ctx, embed=embed)

    @stats.command(name="growth", description="Show member joins/leaves and net growth.")
    @commands.guild_only()
    async def stats_growth(self, ctx, period: Period = "month"):
        """Show joins/leaves/net member growth alongside message activity for
        the same period (period: week/month/year/all)."""
        start_day = self._period_start(period)
        sql = "SELECT COALESCE(SUM(joins),0), COALESCE(SUM(leaves),0) FROM membership WHERE guild_id = ?"
        params = [ctx.guild.id]
        if start_day:
            sql += " AND day >= ?"
            params.append(start_day)
        row = await self._fetch(sql, tuple(params))
        joins, leaves = row[0]

        msg_sql = "SELECT COALESCE(SUM(count),0) FROM messages WHERE guild_id = ?"
        msg_params = [ctx.guild.id]
        if start_day:
            msg_sql += " AND day >= ?"
            msg_params.append(start_day)
        msg_row = await self._fetch(msg_sql, tuple(msg_params))
        total_messages = msg_row[0][0]

        net = joins - leaves
        embed = discord.Embed(
            title=f"📈 Growth — {period}", color=discord.Color.green() if net >= 0 else discord.Color.red()
        )
        embed.add_field(name="Joins", value=f"+{joins:,}", inline=True)
        embed.add_field(name="Leaves", value=f"-{leaves:,}", inline=True)
        embed.add_field(name="Net", value=f"{net:+,}", inline=True)
        embed.add_field(
            name="Messages in period",
            value=f"{total_messages:,} — shown alongside growth, not a statistical correlation",
            inline=False,
        )
        await self._reply(ctx, embed=embed)

    @stats.command(name="quietest", description="Show the least-active members (Manage Server only).")
    @has_permissions_or_owner(manage_guild=True)
    @commands.guild_only()
    async def stats_quietest(self, ctx, n: int = TOP_N):
        """Show the n least-active members over the last TREND_PERIOD_DAYS
        (requires Manage Server)."""
        n = max(1, min(n, TOP_N_MAX))
        start_day = self._day_str(datetime.now(timezone.utc) - timedelta(days=TREND_PERIOD_DAYS))
        rows = await self._fetch(
            "SELECT user_id, SUM(count) as c FROM messages WHERE guild_id = ? AND day >= ? GROUP BY user_id",
            (ctx.guild.id, start_day),
        )
        counts = {uid: c for uid, c in rows}
        members = [m for m in ctx.guild.members if not m.bot]
        quietest = sorted(members, key=lambda m: counts.get(m.id, 0))[:n]
        lines = [f"{m.mention} — {counts.get(m.id, 0)} messages" for m in quietest]
        embed = discord.Embed(
            title=f"🤫 Quietest Members (last {TREND_PERIOD_DAYS}d)",
            description="\n".join(lines) or "No members found.",
            color=discord.Color.dark_grey(),
        )
        await self._reply(ctx, embed=embed)

    @stats.command(
        name="backfill",
        description="Seed historical stats from channel logs (Manage Server only). No days = entire history.",
    )
    @has_permissions_or_owner(manage_guild=True)
    @commands.guild_only()
    async def stats_backfill(self, ctx, days: int = BACKFILL_DEFAULT_DAYS):
        """Seed message/word/char/hourly history from every readable text
        channel's message log. No `days` = the entire server history (can
        take a while on a large server, but discord.py auto-throttles
        pagination so it can't outrun the rate limit). Only seeds
        messages/words/chars/hourly — reactions/voice can't be recovered
        retroactively. One-time seeding: re-running double-counts prior days."""
        guild = ctx.guild
        if guild.id in self._backfill_in_progress:
            await self._reply(ctx, "A backfill is already running for this server — wait for it to finish.")
            return
        self._backfill_in_progress.add(guild.id)

        days = max(0, days)
        if days > 0:
            after = datetime.now(timezone.utc) - timedelta(days=days)
            scope = f"the last {days} day(s)"
        else:
            after = None
            scope = "the entire server history"

        await self._reply(
            ctx,
            f"⏳ Backfill started for {scope}. This scans every readable channel's message "
            "history and can take a while on a large server — I'll post a summary here when done.",
        )
        asyncio.ensure_future(self._run_backfill(guild, ctx.channel, after))

    async def _run_backfill(self, guild: discord.Guild, report_channel, after: datetime | None) -> None:
        started = time.monotonic()
        today = self._day_str(datetime.now(timezone.utc))
        channels_scanned = 0
        channels_skipped = 0
        messages_seen = 0
        try:
            for channel in guild.text_channels:
                perms = channel.permissions_for(guild.me)
                if not perms.view_channel or not perms.read_message_history:
                    channels_skipped += 1
                    continue
                try:
                    async for message in channel.history(limit=None, after=after):
                        if message.author.bot:
                            continue
                        day = self._day_str(message.created_at)
                        if day == today:
                            continue  # today is already covered by live ingest
                        hour = message.created_at.astimezone(timezone.utc).hour
                        await self._accumulate_message(
                            guild.id, day, hour, message.author.id, channel.id,
                            words=len(message.content.split()), chars=len(message.content),
                        )
                        messages_seen += 1
                except discord.Forbidden:
                    channels_skipped += 1
                    continue
                channels_scanned += 1
                await asyncio.sleep(BACKFILL_CHANNEL_SLEEP)

            await self._flush_now()
            elapsed = time.monotonic() - started
            await report_channel.send(
                f"✅ Backfill complete: scanned {channels_scanned} channel(s) "
                f"({channels_skipped} skipped), recorded {messages_seen:,} message(s) in {elapsed:.0f}s."
            )
        except Exception:
            logger.exception("Stats: backfill failed for guild %s", guild.id)
            try:
                await report_channel.send("⚠️ Backfill hit an unexpected error and stopped early — check the logs.")
            except discord.HTTPException:
                pass
        finally:
            self._backfill_in_progress.discard(guild.id)


async def setup(bot):
    await bot.add_cog(Stats(bot))
