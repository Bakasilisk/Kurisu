import asyncio
import hmac
import json
import logging
import math
import os
import platform
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import discord
from aiohttp import web
from discord.ext import commands

from .management import rank_of
from .storage import data_path, load_json

logger = logging.getLogger(__name__)

# Read-only infra cog: a separate sqlite connection (mode=ro, WAL lets it read
# concurrently with the Stats cog's writer connection) plus the bot's live
# Discord cache for name/avatar resolution. Not a per-guild behavioral cog —
# no cog_enabled/.feature toggle, matches management/help's "infra" carve-out.
STATS_DB = os.environ.get("STATS_DB_PATH") or data_path("stats.db")

# Single in-module source of truth for the period vocabulary (mirrors
# cogs/stats.py's pair by convention, never by import — see below).
_PERIOD_DAYS = {"week": 7, "month": 30, "year": 365}
PERIODS = (*_PERIOD_DAYS, "all")
# Mirrors cogs/stats.py's TREND_PERIOD_DAYS, kept as its own constant rather
# than imported so this cog has zero import-time coupling to cogs/stats.py.
TREND_PERIOD_DAYS = 30


# mirrors cogs/leveling.py's level curve
def total_xp_for_level(level: int) -> int:
    """Cumulative XP required to reach the given level from 0."""
    return 25 * level * (level + 1)


def level_from_xp(xp: int) -> int:
    """The level corresponding to a total XP amount."""
    level = 0
    while total_xp_for_level(level + 1) <= xp:
        level += 1
    return level


# mirrors cogs/management.py's management._discover_cogs (excludes
# __init__.py/storage.py) - kept as its own directory scan rather than
# imported, matching this file's zero-import-coupling philosophy for mirrored
# logic. _toggleable_cog_names narrows it like Management._toggleable_names
# (further excludes management/help). webapi itself IS toggleable per that
# list (toggling it is a no-op since webapi has no cog_enabled gate), so it's
# deliberately not excluded.
_NON_COGS = {"__init__", "storage"}
_NON_TOGGLEABLE = _NON_COGS | {"management", "help"}


def _cog_names(excluded: set[str] = _NON_COGS) -> list[str]:
    names = []
    for filename in os.listdir(os.path.dirname(__file__)):
        if not filename.endswith(".py"):
            continue
        name = filename[:-3]
        if name in excluded:
            continue
        names.append(name)
    return sorted(names)


def _toggleable_cog_names() -> list[str]:
    return _cog_names(_NON_TOGGLEABLE)


# Data files /api/health reports on: stats.db plus its WAL, and the JSON data
# files .gitignore lists. Name/size/mtime only - contents are never read
# (palantir_messages.json is the surveillance cache, see _handle_palantir).
_HEALTH_JSON_FILES = (
    "warnings.json", "channel_locks.json", "mod_log.json",
    "xp.json", "messages.json", "leveling_exclude.json",
    "economy.json", "summary.json", "cerberus.json", "verification.json",
    "management.json", "palantir.json", "palantir_messages.json",
    "reminders.json", "tickets.json",
)


def _day_str(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _period_start(period: str) -> str | None:
    if period not in _PERIOD_DAYS:
        return None
    return _day_str(datetime.now(timezone.utc) - timedelta(days=_PERIOD_DAYS[period]))


# Time-series bucketing for /channels/{cid} and /members/{uid}/series:
# week/month -> one bucket per day, year/all -> one per week. A week bucket's
# key is the Monday of that week; strftime('%w') is Sunday=0, so (w + 6) % 7
# is days since Monday. The only %w in this file - weekday splits go through
# Python's datetime.weekday() (Monday=0) instead, as in _handle_activity.
_WEEK_BUCKET_SQL = "date(day, '-' || ((strftime('%w', day) + 6) % 7) || ' days')"


def _bucket_for(period: str) -> str:
    return "day" if period in ("week", "month") else "week"


def _bucket_sql(bucket: str) -> str:
    return "day" if bucket == "day" else _WEEK_BUCKET_SQL


def _series_start(period: str, bucket: str) -> str | None:
    """First bucket key of the window: the period start for day buckets, the
    Monday on or before it for week buckets, None for period=all."""
    start = _period_start(period)
    if start is None or bucket == "day":
        return start
    d = datetime.strptime(start, "%Y-%m-%d").date()
    return (d - timedelta(days=d.weekday())).isoformat()


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _accepted_keys(raw: str) -> set[bytes]:
    # Bytes, not str: hmac.compare_digest raises TypeError on a str holding
    # non-ASCII, which would turn a non-ASCII X-API-Key into an unhandled 500
    # instead of a 401. Encoded the same way as the header in _auth_middleware.
    return {k.strip().encode("utf-8", "surrogateescape") for k in raw.split(",") if k.strip()}


class WebAPI(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._keys = _accepted_keys(os.environ.get("WEBAPI_KEY", ""))
        self._host = os.environ.get("WEBAPI_HOST", "127.0.0.1")
        self._port = int(os.environ.get("WEBAPI_PORT", "8080"))
        self._owner_id: int | None = None
        self._db_lock = threading.Lock()
        self._db = self._connect_db()
        self._runner: web.AppRunner | None = None

        self.app = web.Application(middlewares=[self._auth_middleware])
        r = self.app.router
        r.add_get("/api/meta", self._handle_meta)
        r.add_get("/api/health", self._handle_health)
        r.add_get("/api/guilds", self._handle_guilds)
        r.add_get("/api/guilds/{gid}/overview", self._handle_overview)
        r.add_get("/api/guilds/{gid}/top", self._handle_top)
        r.add_get("/api/guilds/{gid}/channels", self._handle_channels)
        r.add_get("/api/guilds/{gid}/channels/{cid}", self._handle_channel_detail)
        r.add_get("/api/guilds/{gid}/activity", self._handle_activity)
        r.add_get("/api/guilds/{gid}/voice", self._handle_voice)
        r.add_get("/api/guilds/{gid}/growth", self._handle_growth)
        r.add_get("/api/guilds/{gid}/members/{uid}", self._handle_member)
        r.add_get("/api/guilds/{gid}/members/{uid}/series", self._handle_member_series)
        r.add_get("/api/guilds/{gid}/quietest", self._handle_quietest)
        r.add_get("/api/guilds/{gid}/leveling", self._handle_leveling)
        r.add_get("/api/guilds/{gid}/economy", self._handle_economy)
        r.add_get("/api/guilds/{gid}/warnings", self._handle_warnings)
        r.add_get("/api/guilds/{gid}/security", self._handle_security)
        r.add_get("/api/guilds/{gid}/palantir", self._handle_palantir)
        r.add_get("/api/guilds/{gid}/verification", self._handle_verification)
        r.add_get("/api/guilds/{gid}/moderation", self._handle_moderation)
        r.add_get("/api/guilds/{gid}/features", self._handle_features)
        r.add_post("/api/guilds/{gid}/export", self._handle_export_start)
        r.add_get("/api/guilds/{gid}/export", self._handle_export_status)
        r.add_get("/api/guilds/{gid}/export/download", self._handle_export_download)
        r.add_get("/api/users/{uid}/reminders", self._handle_user_reminders)

    # --- Lifecycle ------------------------------------------------------

    async def cog_load(self):
        if not self._keys:
            logger.warning(
                "webapi: WEBAPI_KEY is not set - the web API will not start. "
                "Set WEBAPI_KEY in .env (comma-separated for multiple accepted keys) to enable it."
            )
            return
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        logger.info("webapi: listening on %s:%s", self._host, self._port)

    async def cog_unload(self):
        if self._runner is not None:
            await self._runner.cleanup()
        if self._db is not None:
            self._db.close()

    # --- DB ---------------------------------------------------------------

    def _connect_db(self) -> sqlite3.Connection | None:
        try:
            return sqlite3.connect(f"file:{STATS_DB}?mode=ro", uri=True, check_same_thread=False)
        except sqlite3.OperationalError:
            # stats.db doesn't exist yet (e.g. no messages recorded anywhere
            # yet) - retried lazily on the next query via _get_db().
            logger.info("webapi: stats.db not found yet at %s - will retry lazily", STATS_DB)
            return None

    def _get_db(self) -> sqlite3.Connection | None:
        with self._db_lock:
            if self._db is None:
                self._db = self._connect_db()
            return self._db

    def _query(self, sql: str, params: tuple) -> list[tuple]:
        db = self._get_db()
        if db is None:
            return []
        with self._db_lock:
            try:
                return db.execute(sql, params).fetchall()
            except sqlite3.Error:
                logger.exception("webapi: query failed: %s", sql)
                return []

    async def _fetch(self, sql: str, params: tuple = ()) -> list[tuple]:
        return await asyncio.to_thread(self._query, sql, params)

    async def _fetch_one(self, sql: str, params: tuple, default: tuple) -> tuple:
        # Aggregate COALESCE(...) queries always return exactly one row once
        # they run - but _query returns [] outright when stats.db doesn't
        # exist yet (or a query errors), so callers that assume rows[0]
        # exists need this fallback to the same zero-row COALESCE would give.
        rows = await self._fetch(sql, params)
        return rows[0] if rows else default

    # --- Auth middleware ----------------------------------------------------

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        key = request.headers.get("X-API-Key")
        # surrogateescape: aiohttp decodes header bytes that aren't valid UTF-8
        # into lone surrogates, which a plain .encode() would raise on.
        key_bytes = key.encode("utf-8", "surrogateescape") if key else b""
        if not key_bytes or not any(hmac.compare_digest(key_bytes, k) for k in self._keys):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except Exception:
            logger.exception("webapi: unhandled error handling %s", request.path)
            return web.json_response({"error": "internal error"}, status=500)

    # --- Resolution helpers -------------------------------------------------

    def _user_json(self, guild, user_id: int) -> dict:
        member = guild.get_member(user_id)
        if member is None:
            return {"id": str(user_id), "name": "Unknown", "avatar": None}
        return {"id": str(user_id), "name": member.display_name, "avatar": str(member.display_avatar.url)}

    def _channel_json(self, guild, channel_id: int) -> dict:
        channel = guild.get_channel(channel_id)
        if channel is None:
            return {"id": str(channel_id), "name": "unknown-channel"}
        return {"id": str(channel_id), "name": channel.name}

    def _role_json(self, guild, role_id: int) -> dict:
        role = guild.get_role(role_id)
        if role is None:
            return {"id": str(role_id), "name": "unknown-role"}
        return {"id": str(role_id), "name": role.name}

    def _cog_json(self, filename: str) -> dict:
        """Per-request read of a behavioral cog's JSON data file (small files,
        no caching needed) - keeps this cog decoupled from importing them."""
        return load_json(data_path(filename))

    @staticmethod
    def _guild_json(guild) -> dict:
        return {"id": str(guild.id), "name": guild.name, "icon": str(guild.icon.url) if guild.icon else None}

    def _guild_or_error(self, request: web.Request):
        try:
            gid = int(request.match_info["gid"])
        except ValueError:
            return None, web.json_response({"error": "invalid guild id"}, status=400)
        guild = self.bot.get_guild(gid)
        if guild is None:
            return None, web.json_response({"error": "unknown guild"}, status=404)
        return guild, None

    async def _resolve_owner_id(self) -> int | None:
        """Lazily resolve and cache the bot application's owner id. Shared by
        /api/meta and the export endpoint's live permission check."""
        if self._owner_id is None:
            try:
                info = await self.bot.application_info()
                self._owner_id = info.owner.id
            except Exception:
                self._owner_id = getattr(self.bot, "owner_id", None)
        return self._owner_id

    async def _export_permitted(self, guild, uid: int) -> bool:
        """Live permission check shared by export start and download - the
        API's most sensitive endpoints (the download returns message content).
        A web session caches guild rights for days, so the bot re-verifies on
        every call rather than trusting the caller. Bot owner, or a live Manage
        Server holder (covers Administrator and the guild owner via discord.py's
        own guild_permissions resolution)."""
        owner_id = await self._resolve_owner_id()
        member = guild.get_member(uid)
        return uid == owner_id or (member is not None and member.guild_permissions.manage_guild)

    def _export_cog(self):
        # Runtime coupling via get_cog, never an import - the same
        # bot.get_cog("Palantir")-with-None-guard idiom other cogs use to reach
        # Palantir.log_event. Keeps this file at zero import-time coupling to
        # other cogs, including the Export cog reached into here.
        cog = self.bot.get_cog("Export")
        if cog is None:
            return None, web.json_response({"error": "export unavailable"}, status=503)
        return cog, None

    @staticmethod
    def _period_param(request: web.Request, default: str) -> str:
        # Lowercased to match stats' now-case-insensitive period arg; falls
        # back to `default` silently on a miss, same as before.
        period = request.query.get("period", default).lower()
        return period if period in PERIODS else default

    @staticmethod
    def _limit_param(request: web.Request) -> int | None:
        """Optional positive `limit` query param; None (= full list) when
        absent or invalid, mirroring _period_param's silent fallback."""
        try:
            limit = int(request.query.get("limit", ""))
        except ValueError:
            return None
        return limit if limit > 0 else None

    # --- Endpoints -----------------------------------------------------------

    async def _handle_meta(self, request: web.Request):
        owner_id = await self._resolve_owner_id()
        return web.json_response({
            "owner_id": str(owner_id) if owner_id else None,
            "guild_count": len(self.bot.guilds),
        })

    async def _handle_guilds(self, request: web.Request):
        return web.json_response([self._guild_json(g) for g in self.bot.guilds])

    async def _handle_health(self, request: web.Request):
        # Owner tier (see API.md): bot-wide, not guild-scoped. Like the self
        # tier, enforcement is on the consumer. started_at/failed_extensions
        # are set by bot.py's main(), so both are absent until the bot has
        # been fully restarted on a bot.py that sets them.
        started_at = getattr(self.bot, "started_at", None)
        if started_at is not None:
            uptime = int((datetime.now(timezone.utc) - started_at).total_seconds())
        else:
            uptime = None
        # latency is nan before the first heartbeat and inf while disconnected.
        latency = getattr(self.bot, "latency", None)
        latency_ms = round(latency * 1000, 1) if latency is not None and math.isfinite(latency) else None

        # Same state order as `.cog list`, plus `failed` from bot.py's load loop.
        global_disabled = set(self._cog_json("management.json").get("global", {}).get("disabled_extensions", []))
        failed = getattr(self.bot, "failed_extensions", {})
        cogs = []
        for name in _cog_names():
            ext = f"cogs.{name}"
            error = None
            if ext in global_disabled:
                state = "disabled"
            elif ext in self.bot.extensions:
                state = "loaded"
            elif ext in failed:
                state = "failed"
                error = failed[ext]
            else:
                state = "unloaded"
            cogs.append({"name": name, "state": state, "error": error})

        # stat() only - never open these (see _HEALTH_JSON_FILES).
        paths = [STATS_DB, f"{STATS_DB}-wal"] + [data_path(f) for f in _HEALTH_JSON_FILES]
        data_files = []
        for path in paths:
            try:
                st = os.stat(path)
            except OSError:
                continue
            data_files.append({
                "name": os.path.basename(path),
                "bytes": st.st_size,
                "modified": _iso_utc(datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)),
            })

        return web.json_response({
            "started_at": _iso_utc(started_at) if started_at is not None else None,
            "uptime_seconds": uptime,
            "latency_ms": latency_ms,
            "guild_count": len(self.bot.guilds),
            "member_count": sum(g.member_count or 0 for g in self.bot.guilds),
            "versions": {"python": platform.python_version(), "discordpy": discord.__version__},
            "cogs": cogs,
            "data_files": data_files,
        })

    async def _handle_overview(self, request: web.Request):
        guild, err = self._guild_or_error(request)
        if err:
            return err

        total_count, total_words, first_day = await self._fetch_one(
            "SELECT COALESCE(SUM(count),0), COALESCE(SUM(words),0), MIN(day) FROM messages WHERE guild_id = ?",
            (guild.id,),
            default=(0, 0, None),
        )

        now = datetime.now(timezone.utc)
        trend_start = _day_str(now - timedelta(days=TREND_PERIOD_DAYS))
        prior_start = _day_str(now - timedelta(days=2 * TREND_PERIOD_DAYS))
        recent_count, active_members = await self._fetch_one(
            "SELECT COALESCE(SUM(count),0), COUNT(DISTINCT user_id) FROM messages "
            "WHERE guild_id = ? AND day >= ?",
            (guild.id, trend_start),
            default=(0, 0),
        )
        prior_count, = await self._fetch_one(
            "SELECT COALESCE(SUM(count),0) FROM messages WHERE guild_id = ? AND day >= ? AND day < ?",
            (guild.id, prior_start, trend_start),
            default=(0,),
        )

        if prior_count:
            pct = (recent_count - prior_count) / prior_count * 100
            text = f"{'up' if pct >= 0 else 'down'} {abs(pct):.1f}% vs prior {TREND_PERIOD_DAYS}d"
        elif recent_count:
            pct = None
            text = "new activity (no prior-period data yet)"
        else:
            pct = None
            text = "No recent activity"

        if first_day:
            first_date = datetime.strptime(first_day, "%Y-%m-%d").date()
            elapsed_days = max(1, (now.date() - first_date).days + 1)
        else:
            elapsed_days = 1
        avg_day = total_count / elapsed_days

        reaction_total, = await self._fetch_one(
            "SELECT COALESCE(SUM(given),0) FROM reactions WHERE guild_id = ?", (guild.id,), default=(0,)
        )
        voice_total, = await self._fetch_one(
            "SELECT COALESCE(SUM(seconds),0) FROM voice WHERE guild_id = ?", (guild.id,), default=(0,)
        )

        return web.json_response({
            "guild": self._guild_json(guild),
            "total_messages": total_count,
            "total_words": total_words,
            "first_day": first_day,
            "active_members_30d": active_members,
            "avg_day": avg_day,
            "reactions": reaction_total,
            "voice_seconds": voice_total,
            "trend": {"recent": recent_count, "prior": prior_count, "pct": pct, "text": text},
        })

    async def _handle_top(self, request: web.Request):
        guild, err = self._guild_or_error(request)
        if err:
            return err
        period = self._period_param(request, "all")
        start_day = _period_start(period)
        limit = self._limit_param(request)
        sql = "SELECT user_id, SUM(count) as c FROM messages WHERE guild_id = ?"
        params = [guild.id]
        if start_day:
            sql += " AND day >= ?"
            params.append(start_day)
        sql += " GROUP BY user_id ORDER BY c DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = await self._fetch(sql, tuple(params))
        entries = [{"user": self._user_json(guild, uid), "count": c} for uid, c in rows]
        return web.json_response({"period": period, "entries": entries})

    async def _handle_channels(self, request: web.Request):
        guild, err = self._guild_or_error(request)
        if err:
            return err
        period = self._period_param(request, "all")
        start_day = _period_start(period)
        limit = self._limit_param(request)
        sql = "SELECT channel_id, SUM(count) as c FROM messages WHERE guild_id = ?"
        params = [guild.id]
        if start_day:
            sql += " AND day >= ?"
            params.append(start_day)
        sql += " GROUP BY channel_id ORDER BY c DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = await self._fetch(sql, tuple(params))
        entries = [{"channel": self._channel_json(guild, cid), "count": c} for cid, c in rows]
        return web.json_response({"period": period, "entries": entries})

    async def _handle_channel_detail(self, request: web.Request):
        guild, err = self._guild_or_error(request)
        if err:
            return err
        try:
            cid = int(request.match_info["cid"])
        except ValueError:
            return web.json_response({"error": "invalid channel id"}, status=400)
        # A deleted channel with recorded history still answers (name falls
        # back to unknown-channel); only a channel that is neither cached nor
        # in stats.db for this guild is a 404.
        if guild.get_channel(cid) is None:
            seen = await self._fetch(
                "SELECT 1 FROM messages WHERE guild_id = ? AND channel_id = ? LIMIT 1", (guild.id, cid)
            )
            if not seen:
                return web.json_response({"error": "unknown channel"}, status=404)

        period = self._period_param(request, "month")
        start_day = _period_start(period)
        bucket = _bucket_for(period)
        # The one ranked list whose fallback is 10, not the full list.
        limit = self._limit_param(request) or 10

        where = " WHERE guild_id = ? AND channel_id = ?"
        params = [guild.id, cid]
        if start_day:
            where += " AND day >= ?"
            params.append(start_day)

        messages, words, chars, posters = await self._fetch_one(
            "SELECT COALESCE(SUM(count),0), COALESCE(SUM(words),0), COALESCE(SUM(chars),0), "
            "COUNT(DISTINCT user_id) FROM messages" + where,
            tuple(params),
            default=(0, 0, 0, 0),
        )

        guild_sql = "SELECT COALESCE(SUM(count),0) FROM messages WHERE guild_id = ?"
        guild_params = [guild.id]
        if start_day:
            guild_sql += " AND day >= ?"
            guild_params.append(start_day)
        guild_total, = await self._fetch_one(guild_sql, tuple(guild_params), default=(0,))
        share = messages / guild_total if guild_total else 0.0

        # _handle_overview's recent-vs-prior window, generalised from its fixed
        # 30 days to the requested period's length. No trend for period=all.
        trend = None
        if period in _PERIOD_DAYS:
            n = _PERIOD_DAYS[period]
            now = datetime.now(timezone.utc)
            recent_start = _day_str(now - timedelta(days=n))
            prior_start = _day_str(now - timedelta(days=2 * n))
            recent, = await self._fetch_one(
                "SELECT COALESCE(SUM(count),0) FROM messages WHERE guild_id = ? AND channel_id = ? AND day >= ?",
                (guild.id, cid, recent_start),
                default=(0,),
            )
            prior, = await self._fetch_one(
                "SELECT COALESCE(SUM(count),0) FROM messages "
                "WHERE guild_id = ? AND channel_id = ? AND day >= ? AND day < ?",
                (guild.id, cid, prior_start, recent_start),
                default=(0,),
            )
            pct = round((recent - prior) / prior * 100, 1) if prior else None
            trend = {"recent": recent, "prior": prior, "pct": pct}

        key = _bucket_sql(bucket)
        series_rows = await self._fetch(
            f"SELECT {key} AS b, SUM(count) FROM messages" + where + " GROUP BY b ORDER BY b",
            tuple(params),
        )

        top_rows = await self._fetch(
            "SELECT user_id, SUM(count) as c FROM messages" + where + " GROUP BY user_id ORDER BY c DESC LIMIT ?",
            (*params, limit),
        )

        day_rows = await self._fetch(
            "SELECT day, SUM(count) FROM messages" + where + " GROUP BY day", tuple(params)
        )
        weekdays = [0] * 7
        for day, count in day_rows:
            try:
                weekday = datetime.strptime(day, "%Y-%m-%d").weekday()
            except ValueError:
                continue
            weekdays[weekday] += count

        return web.json_response({
            "channel": self._channel_json(guild, cid),
            "period": period,
            "bucket": bucket,
            "start": _series_start(period, bucket),
            "totals": {"messages": messages, "words": words, "chars": chars, "posters": posters},
            "share": share,
            "trend": trend,
            "series": [{"day": b, "count": c} for b, c in series_rows],
            "top": [{"user": self._user_json(guild, uid), "count": c} for uid, c in top_rows],
            "weekdays": weekdays,
        })

    async def _handle_activity(self, request: web.Request):
        guild, err = self._guild_or_error(request)
        if err:
            return err
        period = self._period_param(request, "month")
        start_day = _period_start(period)
        sql = "SELECT day, hour, SUM(count) FROM hourly WHERE guild_id = ?"
        params = [guild.id]
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

        weekday_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        hour_totals = [sum(grid[wd][hr] for wd in range(7)) for hr in range(24)]
        weekday_totals = [sum(grid[wd]) for wd in range(7)]
        total = sum(hour_totals)
        peak_hour = max(range(24), key=lambda h: hour_totals[h]) if total else 0
        peak_weekday = max(range(7), key=lambda w: weekday_totals[w]) if total else 0

        return web.json_response({
            "period": period,
            "grid": grid,
            "weekday_labels": weekday_labels,
            "hour_totals": hour_totals,
            "weekday_totals": weekday_totals,
            "peak_hour": peak_hour,
            "peak_weekday": peak_weekday,
            "total": total,
        })

    async def _handle_voice(self, request: web.Request):
        guild, err = self._guild_or_error(request)
        if err:
            return err
        period = self._period_param(request, "all")
        start_day = _period_start(period)
        limit = self._limit_param(request)
        sql = "SELECT user_id, SUM(seconds) as s FROM voice WHERE guild_id = ?"
        params = [guild.id]
        if start_day:
            sql += " AND day >= ?"
            params.append(start_day)
        sql += " GROUP BY user_id ORDER BY s DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = await self._fetch(sql, tuple(params))
        entries = [{"user": self._user_json(guild, uid), "seconds": s} for uid, s in rows]
        return web.json_response({"period": period, "entries": entries})

    async def _handle_growth(self, request: web.Request):
        guild, err = self._guild_or_error(request)
        if err:
            return err
        period = self._period_param(request, "month")
        start_day = _period_start(period)

        sql = "SELECT COALESCE(SUM(joins),0), COALESCE(SUM(leaves),0) FROM membership WHERE guild_id = ?"
        params = [guild.id]
        if start_day:
            sql += " AND day >= ?"
            params.append(start_day)
        joins, leaves = await self._fetch_one(sql, tuple(params), default=(0, 0))

        msg_sql = "SELECT COALESCE(SUM(count),0) FROM messages WHERE guild_id = ?"
        msg_params = [guild.id]
        if start_day:
            msg_sql += " AND day >= ?"
            msg_params.append(start_day)
        messages, = await self._fetch_one(msg_sql, tuple(msg_params), default=(0,))

        return web.json_response({
            "period": period, "joins": joins, "leaves": leaves,
            "net": joins - leaves, "messages": messages,
        })

    async def _handle_member(self, request: web.Request):
        guild, err = self._guild_or_error(request)
        if err:
            return err
        try:
            uid = int(request.match_info["uid"])
        except ValueError:
            return web.json_response({"error": "invalid user id"}, status=400)

        total_count, total_words, active_days, first_day = await self._fetch_one(
            "SELECT COALESCE(SUM(count),0), COALESCE(SUM(words),0), COUNT(DISTINCT day), MIN(day) "
            "FROM messages WHERE guild_id = ? AND user_id = ?",
            (guild.id, uid),
            default=(0, 0, 0, None),
        )

        server_total, = await self._fetch_one(
            "SELECT COALESCE(SUM(count),0) FROM messages WHERE guild_id = ?", (guild.id,), default=(0,)
        )

        rank_rows = await self._fetch(
            "SELECT user_id, SUM(count) as c FROM messages WHERE guild_id = ? GROUP BY user_id", (guild.id,)
        )
        rank = rank_of(rank_rows, key=lambda kv: kv[1], target_id=uid)

        given, received = await self._fetch_one(
            "SELECT COALESCE(SUM(given),0), COALESCE(SUM(received),0) FROM reactions "
            "WHERE guild_id = ? AND user_id = ?",
            (guild.id, uid),
            default=(0, 0),
        )

        voice_seconds, = await self._fetch_one(
            "SELECT COALESCE(SUM(seconds),0) FROM voice WHERE guild_id = ? AND user_id = ?",
            (guild.id, uid),
            default=(0,),
        )

        busiest_hour_row = await self._fetch(
            "SELECT hour, SUM(count) as c FROM hourly WHERE guild_id = ? AND user_id = ? "
            "GROUP BY hour ORDER BY c DESC LIMIT 1",
            (guild.id, uid),
        )
        busiest_hour = busiest_hour_row[0][0] if busiest_hour_row else None

        channel_rows = await self._fetch(
            "SELECT channel_id, SUM(count) as c FROM messages WHERE guild_id = ? AND user_id = ? "
            "GROUP BY channel_id ORDER BY c DESC",
            (guild.id, uid),
        )

        pct_of_server = (total_count / server_total * 100) if server_total else 0.0
        words_per_msg = (total_words / total_count) if total_count else 0.0

        # Harmless (member-readable) enrichment only - leveling and economy.
        # Warnings are spicy/mod-tier and deliberately excluded here; they
        # live solely on the /warnings endpoint.
        guild_xp = self._cog_json("xp.json").get(str(guild.id), {})
        xp = guild_xp.get(str(uid), 0)
        lvl_rank = rank_of(((int(k), v) for k, v in guild_xp.items()), key=lambda kv: kv[1], target_id=uid)

        guild_bank = self._cog_json("economy.json").get(str(guild.id), {})
        econ_entry = guild_bank.get(str(uid), {})
        bits = econ_entry.get("balance", 0)
        streak = econ_entry.get("streak", 0)
        econ_rank = rank_of(
            ((int(k), e.get("balance", 0)) for k, e in guild_bank.items()), key=lambda kv: kv[1], target_id=uid
        )

        return web.json_response({
            "user": self._user_json(guild, uid),
            "total_messages": total_count,
            "total_words": total_words,
            "active_days": active_days,
            "first_day": first_day,
            "server_rank": rank,
            "pct_of_server": pct_of_server,
            "words_per_msg": words_per_msg,
            "busiest_hour": busiest_hour,
            "voice_seconds": voice_seconds,
            "reactions_given": given,
            "reactions_received": received,
            "top_channels": [{"channel": self._channel_json(guild, cid), "count": c} for cid, c in channel_rows],
            "leveling": {"xp": xp, "level": level_from_xp(xp), "rank": lvl_rank},
            "economy": {"bits": bits, "rank": econ_rank, "streak": streak},
        })

    async def _handle_member_series(self, request: web.Request):
        guild, err = self._guild_or_error(request)
        if err:
            return err
        try:
            uid = int(request.match_info["uid"])
        except ValueError:
            return web.json_response({"error": "invalid user id"}, status=400)
        period = self._period_param(request, "year")
        start_day = _period_start(period)
        bucket = _bucket_for(period)
        key = _bucket_sql(bucket)

        where = " WHERE guild_id = ? AND user_id = ?"
        params = [guild.id, uid]
        if start_day:
            where += " AND day >= ?"
            params.append(start_day)
        msg_rows = await self._fetch(
            f"SELECT {key} AS b, SUM(count) FROM messages" + where + " GROUP BY b", tuple(params)
        )
        voice_rows = await self._fetch(
            f"SELECT {key} AS b, SUM(seconds) FROM voice" + where + " GROUP BY b", tuple(params)
        )

        merged: dict[str, dict] = {}
        for b, count in msg_rows:
            merged.setdefault(b, {"day": b, "messages": 0, "voice_seconds": 0})["messages"] = count
        for b, seconds in voice_rows:
            merged.setdefault(b, {"day": b, "messages": 0, "voice_seconds": 0})["voice_seconds"] = seconds

        return web.json_response({
            "period": period,
            "bucket": bucket,
            "start": _series_start(period, bucket),
            "series": [merged[b] for b in sorted(merged)],
        })

    async def _handle_quietest(self, request: web.Request):
        guild, err = self._guild_or_error(request)
        if err:
            return err
        start_day = _day_str(datetime.now(timezone.utc) - timedelta(days=TREND_PERIOD_DAYS))
        rows = await self._fetch(
            "SELECT user_id, SUM(count) as c FROM messages WHERE guild_id = ? AND day >= ? GROUP BY user_id",
            (guild.id, start_day),
        )
        counts = {uid: c for uid, c in rows}
        members = [m for m in guild.members if not m.bot]
        quietest = sorted(members, key=lambda m: counts.get(m.id, 0))
        limit = self._limit_param(request)
        if limit is not None:
            quietest = quietest[:limit]
        entries = [
            {
                "user": {"id": str(m.id), "name": m.display_name, "avatar": str(m.display_avatar.url)},
                "count": counts.get(m.id, 0),
            }
            for m in quietest
        ]
        return web.json_response({"entries": entries})

    async def _handle_leveling(self, request: web.Request):
        guild, err = self._guild_or_error(request)
        if err:
            return err
        limit = self._limit_param(request)
        guild_xp = self._cog_json("xp.json").get(str(guild.id), {})
        rows = sorted(((int(uid), xp) for uid, xp in guild_xp.items()), key=lambda kv: kv[1], reverse=True)
        if limit is not None:
            rows = rows[:limit]
        entries = [
            {"user": self._user_json(guild, uid), "xp": xp, "level": level_from_xp(xp)} for uid, xp in rows
        ]
        return web.json_response({"entries": entries})

    async def _handle_economy(self, request: web.Request):
        guild, err = self._guild_or_error(request)
        if err:
            return err
        limit = self._limit_param(request)
        guild_bank = self._cog_json("economy.json").get(str(guild.id), {})
        rows = sorted(
            ((int(uid), entry) for uid, entry in guild_bank.items()),
            key=lambda kv: kv[1].get("balance", 0),
            reverse=True,
        )
        if limit is not None:
            rows = rows[:limit]
        entries = [
            {
                "user": self._user_json(guild, uid),
                "bits": entry.get("balance", 0),
                "streak": entry.get("streak", 0),
            }
            for uid, entry in rows
        ]
        return web.json_response({"entries": entries})

    async def _handle_warnings(self, request: web.Request):
        guild, err = self._guild_or_error(request)
        if err:
            return err
        limit = self._limit_param(request)
        guild_warns = self._cog_json("warnings.json").get(str(guild.id), {})
        rows = sorted(
            ((int(uid), warn_list) for uid, warn_list in guild_warns.items()),
            key=lambda kv: len(kv[1]),
            reverse=True,
        )
        if limit is not None:
            rows = rows[:limit]
        entries = [
            {
                "user": self._user_json(guild, uid),
                "count": len(warn_list),
                "warnings": [
                    {
                        "reason": w.get("reason"),
                        "moderator": self._user_json(guild, w["moderator_id"]) if w.get("moderator_id") is not None else None,
                        "timestamp": w.get("timestamp"),
                    }
                    for w in warn_list
                ],
            }
            for uid, warn_list in rows
        ]
        return web.json_response({"entries": entries})

    async def _handle_security(self, request: web.Request):
        guild, err = self._guild_or_error(request)
        if err:
            return err
        conf = self._cog_json("cerberus.json").get(str(guild.id), {})
        ld = conf.get("lockdown", {}) or {}
        active = bool(ld.get("active"))
        expires_at = ld.get("expires_at")
        now = datetime.now(timezone.utc).timestamp()
        remaining = max(0, int(expires_at - now)) if (active and expires_at is not None) else 0
        stay_locked = active and expires_at is None
        log_channel_id = conf.get("log_channel_id")
        return web.json_response({
            "mode": conf.get("mode", "shadow"),
            "log_channel": self._channel_json(guild, log_channel_id) if log_channel_id is not None else None,
            "exempt_roles": len(conf.get("exempt_role_ids", [])),
            "exempt_users": len(conf.get("exempt_user_ids", [])),
            "protected_roles": len(conf.get("protected_role_ids", [])),
            "lockdown": {
                "active": active,
                "started_at": ld.get("started_at"),
                "expires_at": expires_at,
                "remaining_seconds": remaining,
                "stay_locked": stay_locked,
            },
        })

    async def _handle_palantir(self, request: web.Request):
        # CONFIG + CACHE-SIZE ONLY - the surveillance boundary. palantir_messages.json
        # holds cached message content/author ids/attachment urls/edit pre-images; this
        # endpoint reads that file solely to take len() of the guild's dict and must
        # never surface a cached entry, content string, author, or attachment url.
        guild, err = self._guild_or_error(request)
        if err:
            return err
        conf = self._cog_json("palantir.json").get(str(guild.id), {})
        cached = len(self._cog_json("palantir_messages.json").get(str(guild.id), {}))
        log_channel_id = conf.get("log_channel_id")
        return web.json_response({
            "log_channel": self._channel_json(guild, log_channel_id) if log_channel_id is not None else None,
            "enabled": bool(conf.get("enabled", True)),
            "archive_attachments": bool(conf.get("archive_attachments", False)),
            "muted_categories": conf.get("disabled_categories", []),
            "cached_messages": cached,
        })

    async def _handle_verification(self, request: web.Request):
        guild, err = self._guild_or_error(request)
        if err:
            return err
        conf = self._cog_json("verification.json").get(str(guild.id), {})
        gr = conf.get("granter_role_id")
        tr = conf.get("target_role_id")
        wc = conf.get("welcome_channel_id")
        return web.json_response({
            "granter_role": self._role_json(guild, gr) if gr is not None else None,
            "target_role": self._role_json(guild, tr) if tr is not None else None,
            "welcome_channel": self._channel_json(guild, wc) if wc is not None else None,
            # verification.py treats welcome_channel_id is not None as "enabled"
            # (see Verification.verification_welcome_disable, which clears it to
            # disable welcomes) - mirrored here rather than a separate flag.
            "welcome_enabled": wc is not None,
        })

    async def _handle_moderation(self, request: web.Request):
        guild, err = self._guild_or_error(request)
        if err:
            return err
        log_channel_id = self._cog_json("mod_log.json").get(str(guild.id))
        # channel_locks.json's values are permission-overwrite restoration
        # snapshots - same privacy boundary as /security's lockdown snapshots.
        # Only the keys (locked channel ids) are ever read here; a snapshot
        # value itself must never be touched.
        lock_keys = self._cog_json("channel_locks.json").keys()
        locked_channels = []
        for key in lock_keys:
            try:
                cid = int(key)
            except ValueError:
                continue
            if guild.get_channel(cid) is not None:
                locked_channels.append(self._channel_json(guild, cid))
        locked_channels.sort(key=lambda c: c["name"])
        return web.json_response({
            "mod_log_channel": self._channel_json(guild, log_channel_id) if log_channel_id is not None else None,
            "locked_channels": locked_channels,
        })

    async def _handle_features(self, request: web.Request):
        guild, err = self._guild_or_error(request)
        if err:
            return err
        conf = self._cog_json("management.json")
        disabled = set(conf.get("guilds", {}).get(str(guild.id), {}).get("disabled_cogs", []))
        global_disabled = set(conf.get("global", {}).get("disabled_extensions", []))
        cogs = [
            {
                "name": name,
                "enabled": name not in disabled,
                "globally_disabled": f"cogs.{name}" in global_disabled,
            }
            for name in _toggleable_cog_names()
        ]
        return web.json_response({"cogs": cogs})

    async def _handle_export_start(self, request: web.Request):
        guild, err = self._guild_or_error(request)
        if err:
            return err
        cog, err = self._export_cog()
        if err:
            return err

        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            return web.json_response({"error": "invalid json"}, status=400)
        try:
            uid = int(body.get("requested_by") if isinstance(body, dict) else None)
        except (TypeError, ValueError):
            return web.json_response({"error": "invalid requested_by"}, status=400)

        # Live permission check, see _export_permitted.
        if not await self._export_permitted(guild, uid):
            return web.json_response({"error": "not permitted"}, status=403)

        # `weeks` is optional and silently clamped by the cog (see Export._clamp_weeks) —
        # unlike `requested_by` above, an invalid/missing value is not a 400 here. No
        # isinstance(body, dict) check needed on this .get(): the uid parse above only
        # succeeds when body is a dict (int(None) always raises otherwise), so by this point
        # body is already known to be one. No range check either — the bound lives in exactly
        # one place, Export.start_export, mirroring _period_param/_limit_param's silent
        # query-param fallback (this is the body-param equivalent).
        status, job = cog.start_export(guild, uid, "web", weeks=body.get("weeks"))
        logger.info("webapi: export start | guild=%s requested_by=%s status=%s", guild.id, uid, status)
        if status == "started":
            return web.json_response(job, status=202)
        if status == "running":
            return web.json_response({"error": "already running", "job": job}, status=409)
        if status == "disabled":
            return web.json_response({"error": "export disabled"}, status=403)
        # Contract only defines the three states above; fail closed rather than
        # silently 200-ing on an unexpected one.
        return web.json_response({"error": "internal error"}, status=500)

    async def _handle_export_status(self, request: web.Request):
        guild, err = self._guild_or_error(request)
        if err:
            return err
        cog, err = self._export_cog()
        if err:
            return err
        job = cog.job_status(guild.id)
        return web.json_response(job if job is not None else {"state": "idle"})

    async def _handle_export_download(self, request: web.Request):
        guild, err = self._guild_or_error(request)
        if err:
            return err
        # Mandatory, so a client that omits it can't skip the live check. It
        # checks the downloader's *current* rights, not that they started the
        # job - any current manager (or the bot owner) may fetch a finished export.
        try:
            uid = int(request.query["requested_by"])
        except (KeyError, ValueError):
            return web.json_response({"error": "invalid requested_by"}, status=400)
        if not await self._export_permitted(guild, uid):
            return web.json_response({"error": "not permitted"}, status=403)
        cog, err = self._export_cog()
        if err:
            return err
        result = cog.job_csv(guild.id)
        if result is None:
            return web.json_response({"error": "no export"}, status=404)
        data, filename = result
        job = cog.job_status(guild.id) or {}
        logger.info(
            "webapi: export downloaded | guild=%s requested_by=%s job_requested_by=%s bytes=%s",
            guild.id, uid, job.get("requested_by"), len(data),
        )
        # The only endpoint in this cog that returns message content - every
        # other endpoint is aggregate stats or config (mirrors the /palantir
        # surveillance boundary's equivalent rule on the read side).
        return web.Response(
            body=data,
            content_type="text/csv",
            charset="utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    async def _handle_user_reminders(self, request: web.Request):
        # First non-guild-scoped endpoint (self-tier, see API.md) - takes a
        # bare {uid}, no {gid}/_guild_or_error. Channel resolution therefore
        # goes through the bot's global self.bot.get_channel rather than
        # _channel_json (which is guild.get_channel-scoped).
        try:
            uid = int(request.match_info["uid"])
        except ValueError:
            return web.json_response({"error": "invalid user id"}, status=400)

        all_reminders = self._cog_json("reminders.json").get("reminders", [])
        user_reminders = sorted(
            (r for r in all_reminders if r["user_id"] == uid), key=lambda r: r["fire_at"]
        )

        entries = []
        for r in user_reminders:
            channel = self.bot.get_channel(r["channel_id"])
            if channel is None:
                channel_json = {"id": str(r["channel_id"]), "name": "unknown-channel"}
                guild_json = None
            else:
                name = getattr(channel, "name", None) or "DM"
                g = getattr(channel, "guild", None)
                channel_json = {"id": str(r["channel_id"]), "name": name}
                guild_json = self._guild_json(g) if g is not None else None
            entries.append({
                "id": r["id"],
                "fire_at": datetime.fromtimestamp(r["fire_at"], tz=timezone.utc).isoformat(),
                "text": r["text"],
                "channel": channel_json,
                "guild": guild_json,
            })
        return web.json_response({"reminders": entries})


async def setup(bot):
    await bot.add_cog(WebAPI(bot))
