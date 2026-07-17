import asyncio
import hmac
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Literal

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

Period = Literal["week", "month", "year", "all"]
_PERIOD_DAYS = {"week": 7, "month": 30, "year": 365}
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


def _day_str(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _period_start(period: str) -> str | None:
    if period not in _PERIOD_DAYS:
        return None
    return _day_str(datetime.now(timezone.utc) - timedelta(days=_PERIOD_DAYS[period]))


def _accepted_keys(raw: str) -> set[str]:
    return {k.strip() for k in raw.split(",") if k.strip()}


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
        r.add_get("/api/guilds", self._handle_guilds)
        r.add_get("/api/guilds/{gid}/overview", self._handle_overview)
        r.add_get("/api/guilds/{gid}/top", self._handle_top)
        r.add_get("/api/guilds/{gid}/channels", self._handle_channels)
        r.add_get("/api/guilds/{gid}/activity", self._handle_activity)
        r.add_get("/api/guilds/{gid}/voice", self._handle_voice)
        r.add_get("/api/guilds/{gid}/growth", self._handle_growth)
        r.add_get("/api/guilds/{gid}/members/{uid}", self._handle_member)
        r.add_get("/api/guilds/{gid}/quietest", self._handle_quietest)
        r.add_get("/api/guilds/{gid}/leveling", self._handle_leveling)
        r.add_get("/api/guilds/{gid}/economy", self._handle_economy)
        r.add_get("/api/guilds/{gid}/warnings", self._handle_warnings)
        r.add_get("/api/guilds/{gid}/security", self._handle_security)
        r.add_get("/api/guilds/{gid}/palantir", self._handle_palantir)
        r.add_get("/api/guilds/{gid}/verification", self._handle_verification)
        r.add_get("/api/guilds/{gid}/moderation", self._handle_moderation)

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

    # --- Auth middleware ----------------------------------------------------

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        key = request.headers.get("X-API-Key")
        if not key or not any(hmac.compare_digest(key, k) for k in self._keys):
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

    @staticmethod
    def _period_param(request: web.Request, default: str) -> str:
        period = request.query.get("period", default)
        return period if period in ("week", "month", "year", "all") else default

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
        if self._owner_id is None:
            try:
                info = await self.bot.application_info()
                self._owner_id = info.owner.id
            except Exception:
                self._owner_id = getattr(self.bot, "owner_id", None)
        return web.json_response({
            "owner_id": str(self._owner_id) if self._owner_id else None,
            "guild_count": len(self.bot.guilds),
        })

    async def _handle_guilds(self, request: web.Request):
        return web.json_response([self._guild_json(g) for g in self.bot.guilds])

    async def _handle_overview(self, request: web.Request):
        guild, err = self._guild_or_error(request)
        if err:
            return err

        totals = await self._fetch(
            "SELECT COALESCE(SUM(count),0), COALESCE(SUM(words),0), MIN(day) FROM messages WHERE guild_id = ?",
            (guild.id,),
        )
        total_count, total_words, first_day = totals[0]

        now = datetime.now(timezone.utc)
        trend_start = _day_str(now - timedelta(days=TREND_PERIOD_DAYS))
        prior_start = _day_str(now - timedelta(days=2 * TREND_PERIOD_DAYS))
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

        reaction_total = await self._fetch(
            "SELECT COALESCE(SUM(given),0) FROM reactions WHERE guild_id = ?", (guild.id,)
        )
        voice_total = await self._fetch(
            "SELECT COALESCE(SUM(seconds),0) FROM voice WHERE guild_id = ?", (guild.id,)
        )

        return web.json_response({
            "guild": self._guild_json(guild),
            "total_messages": total_count,
            "total_words": total_words,
            "first_day": first_day,
            "active_members_30d": active_members,
            "avg_day": avg_day,
            "reactions": reaction_total[0][0],
            "voice_seconds": voice_total[0][0],
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
        row = await self._fetch(sql, tuple(params))
        joins, leaves = row[0]

        msg_sql = "SELECT COALESCE(SUM(count),0) FROM messages WHERE guild_id = ?"
        msg_params = [guild.id]
        if start_day:
            msg_sql += " AND day >= ?"
            msg_params.append(start_day)
        msg_row = await self._fetch(msg_sql, tuple(msg_params))

        return web.json_response({
            "period": period, "joins": joins, "leaves": leaves,
            "net": joins - leaves, "messages": msg_row[0][0],
        })

    async def _handle_member(self, request: web.Request):
        guild, err = self._guild_or_error(request)
        if err:
            return err
        try:
            uid = int(request.match_info["uid"])
        except ValueError:
            return web.json_response({"error": "invalid user id"}, status=400)

        msg_row = await self._fetch(
            "SELECT COALESCE(SUM(count),0), COALESCE(SUM(words),0), COUNT(DISTINCT day), MIN(day) "
            "FROM messages WHERE guild_id = ? AND user_id = ?",
            (guild.id, uid),
        )
        total_count, total_words, active_days, first_day = msg_row[0]

        server_row = await self._fetch(
            "SELECT COALESCE(SUM(count),0) FROM messages WHERE guild_id = ?", (guild.id,)
        )
        server_total = server_row[0][0]

        rank_rows = await self._fetch(
            "SELECT user_id, SUM(count) as c FROM messages WHERE guild_id = ? GROUP BY user_id", (guild.id,)
        )
        rank = rank_of(rank_rows, key=lambda kv: kv[1], target_id=uid)

        reaction_row = await self._fetch(
            "SELECT COALESCE(SUM(given),0), COALESCE(SUM(received),0) FROM reactions "
            "WHERE guild_id = ? AND user_id = ?",
            (guild.id, uid),
        )
        given, received = reaction_row[0]

        voice_row = await self._fetch(
            "SELECT COALESCE(SUM(seconds),0) FROM voice WHERE guild_id = ? AND user_id = ?",
            (guild.id, uid),
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
        bits = guild_bank.get(str(uid), {}).get("balance", 0)
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
            "voice_seconds": voice_row[0][0],
            "reactions_given": given,
            "reactions_received": received,
            "top_channels": [{"channel": self._channel_json(guild, cid), "count": c} for cid, c in channel_rows],
            "leveling": {"xp": xp, "level": level_from_xp(xp), "rank": lvl_rank},
            "economy": {"bits": bits, "rank": econ_rank},
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
            ((int(uid), entry.get("balance", 0)) for uid, entry in guild_bank.items()),
            key=lambda kv: kv[1],
            reverse=True,
        )
        if limit is not None:
            rows = rows[:limit]
        entries = [{"user": self._user_json(guild, uid), "bits": bal} for uid, bal in rows]
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


async def setup(bot):
    await bot.add_cog(WebAPI(bot))
