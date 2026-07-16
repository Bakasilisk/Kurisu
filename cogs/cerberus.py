import asyncio
import hashlib
import logging
import re
import time
import typing
from collections import deque
from datetime import timedelta
from typing import NamedTuple

import discord
from discord.ext import commands, tasks

from .management import cog_enabled
from .moderation import restore_overwrite, snapshot_overwrite
from .storage import backfill_defaults, data_path, load_json, save_json_atomic

logger = logging.getLogger(__name__)

CERBERUS_FILE = data_path("cerberus.json")

URL_RE = re.compile(r"https?://\S+")

# Pattern A: sleeper raid/scam accounts bursting the same content across many
# channels while pinging a high-membership role.
PATTERN_A_CHANNEL_THRESHOLD = 4
PATTERN_A_WINDOW_SECONDS = 20

# A role counts as "high-value" (worth watching for Pattern A) once its member
# count crosses this percentage of the guild, with a floor so small servers
# don't treat every role as high-value.
HIGH_VALUE_ROLE_PERCENT = 0.30
HIGH_VALUE_ROLE_FLOOR = 25
HIGH_VALUE_ROLE_CACHE_TTL_SECONDS = 300

# Pattern B: plain flood spam, independent of channel spread or role mentions.
PATTERN_B_FAST_COUNT = 5
PATTERN_B_FAST_WINDOW_SECONDS = 5
PATTERN_B_SLOW_COUNT = 10
PATTERN_B_SLOW_WINDOW_SECONDS = 30

MEMBER_ACTIVITY_MAX_AGE_SECONDS = max(PATTERN_A_WINDOW_SECONDS, PATTERN_B_SLOW_WINDOW_SECONDS)
PRUNE_INTERVAL_SECONDS = 60

CERBERUS_TIMEOUT_DURATION_SECONDS = 60 * 60  # 1h; mods can adjust via the existing !timeout/!untimeout

# Guild-wide duplicate-content wave: catches coordinated raids that spread load
# thin across many accounts, each individually staying under Pattern A/B.
DUP_CONTENT_MIN_ACCOUNTS = 3
DUP_CONTENT_WINDOW_SECONDS = 60

# Raid wave: multiple accounts independently tripping Pattern A close together
# is treated as a coordinated attack, not isolated incidents.
RAID_WAVE_MIN_ACCOUNTS = 2
RAID_WAVE_WINDOW_SECONDS = 60

# A repeat wave trigger within this window (of a prior one) signals an ongoing
# raid rather than a one-off, consumed by the lockdown mechanism (later step).
LOCKDOWN_REPEAT_WINDOW_SECONDS = 60 * 60

# How long a member stays "already actioned" before a continuing burst is
# allowed to re-trigger a response (re-alert, retry a failed timeout, etc.).
ACTION_REARM_SECONDS = 5 * 60

LOCKDOWN_MAX_DURATION_SECONDS = 15 * 60


class MessageEvent(NamedTuple):
    timestamp: float
    channel_id: int
    content_hash: str | None
    mentions_high_value_role: bool
    message: discord.Message


class HashEvent(NamedTuple):
    timestamp: float
    author_id: int
    message: discord.Message


class PatternATrip(NamedTuple):
    timestamp: float
    member_id: int


class ActionResult(NamedTuple):
    """The outcome of _respond_to_member's enforcement actions, as opposed to
    the detection event itself — bundled so _send_alert's call site can't
    silently swap two same-typed positional args."""
    timeout_ok: bool
    timeout_error: Exception | None
    deleted: int


def _content_hash(message: discord.Message) -> str | None:
    """A normalized content fingerprint for cross-message duplicate detection.
    Prefers URLs/attachment identity over raw text, since raid bots often wrap
    the same scam link/image in slightly different filler text."""
    urls = sorted(URL_RE.findall(message.content))
    attachments = sorted(f"{a.filename}:{a.size}" for a in message.attachments)
    if urls or attachments:
        payload = "|".join(urls + attachments)
    else:
        normalized = re.sub(r"\s+", " ", message.content.strip().lower())
        if not normalized:
            return None
        payload = normalized
    return hashlib.sha256(payload.encode()).hexdigest()


def _prune_deque(dq: deque, max_age: float, now: float) -> None:
    while dq and now - dq[0].timestamp > max_age:
        dq.popleft()


def _pattern_a_window(events, now: float) -> list:
    return [e for e in events if now - e.timestamp <= PATTERN_A_WINDOW_SECONDS]


def _check_pattern_a(events, now: float) -> bool:
    window = _pattern_a_window(events, now)
    if len({e.channel_id for e in window}) < PATTERN_A_CHANNEL_THRESHOLD:
        return False
    return any(e.mentions_high_value_role for e in window)


def _check_pattern_b(events, now: float) -> bool:
    fast_count = sum(1 for e in events if now - e.timestamp <= PATTERN_B_FAST_WINDOW_SECONDS)
    if fast_count >= PATTERN_B_FAST_COUNT:
        return True
    slow_count = sum(1 for e in events if now - e.timestamp <= PATTERN_B_SLOW_WINDOW_SECONDS)
    return slow_count >= PATTERN_B_SLOW_COUNT


def _describe_role_pinged(window) -> str | None:
    """A human-readable description of the high-value role mention that made a
    Pattern A window qualify, for the alert embed."""
    for event in window:
        if not event.mentions_high_value_role:
            continue
        if event.message.mention_everyone:
            return "@everyone/@here"
        if event.message.role_mentions:
            return ", ".join(role.mention for role in event.message.role_mentions)
    return None


def _is_exempt(guild_conf: dict, member: discord.Member) -> bool:
    if member.bot:
        return True
    perms = member.guild_permissions
    if perms.manage_messages or perms.administrator:
        return True
    if member.id == member.guild.owner_id:
        return True
    if member.id in guild_conf["exempt_user_ids"]:
        return True
    return any(role.id in guild_conf["exempt_role_ids"] for role in member.roles)


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


class Cerberus(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config = load_json(CERBERUS_FILE)
        self._member_activity: dict[tuple[int, int], deque] = {}
        self._high_value_role_cache: dict[int, tuple[float, set[int]]] = {}
        # Last time each member was actioned (monotonic time), so a single ongoing
        # burst doesn't re-trigger a timeout/delete/alert on every message, but an
        # ongoing raid still periodically re-alerts/retries rather than going
        # silent forever after the first hit (e.g. in shadow mode, or if the
        # timeout attempt failed and the member keeps posting).
        self._actioned_members: dict[tuple[int, int], float] = {}
        self._content_hash_activity: dict[tuple[int, str], deque] = {}
        self._pattern_a_trips: dict[int, deque] = {}
        # Pending auto-lift tasks for active lockdowns, keyed by guild ID. Not
        # cancelled on cog_unload (see cog_unload docstring) — they close over
        # `self` and keep working correctly across a bare extension reload.
        self._lockdown_tasks: dict[int, asyncio.Task] = {}
        # Serializes _start_lockdown/_lift_lockdown per guild. Needed because
        # _start_lockdown has a real await gap (asyncio.gather over all channels)
        # between its "already active?" check and setting active=True — two
        # concurrent triggers could otherwise both pass the check and each
        # snapshot the other's in-progress changes as if they were the original
        # pre-lockdown state, corrupting the restore-on-lift guarantee.
        self._lockdown_locks: dict[int, asyncio.Lock] = {}
        self._prune_activity.start()
        # One-shot: resume any lockdown that was still active when the bot last
        # stopped, rather than leaving it silently permanent or silently lifted.
        self._rehydrate_task = asyncio.ensure_future(self._rehydrate_lockdowns())

    def cog_unload(self):
        self._prune_activity.cancel()
        # Deliberately NOT cancelling self._lockdown_tasks here: an in-flight
        # auto-lift only restores channel permissions and doesn't depend on this
        # cog instance's lifecycle, so letting it run to completion is safer than
        # abandoning an active lockdown on a bare `.reload cerberus`.

    def _save_config(self):
        save_json_atomic(CERBERUS_FILE, self.config)

    def _guild_conf(self, guild_id: int) -> dict:
        # Backfill any keys missing from a config persisted by an earlier schema
        # (e.g. a guild's config saved before "lockdown" or some other field
        # existed, or before some field within "lockdown" existed), so
        # accessing a newer key never raises a bare KeyError. Recurses into
        # "lockdown", but only fills keys that are *absent* — an existing
        # `expires_at: null` on an active lockdown (meaning "stay locked") is
        # a present key and is left untouched, not treated as missing.
        guild_conf = self.config.setdefault(str(guild_id), {})
        return backfill_defaults(guild_conf, _default_guild_config())

    def _high_value_role_ids(self, guild: discord.Guild) -> set[int]:
        now = time.monotonic()
        cached = self._high_value_role_cache.get(guild.id)
        if cached is not None and now - cached[0] < HIGH_VALUE_ROLE_CACHE_TTL_SECONDS:
            return cached[1]
        threshold = max(HIGH_VALUE_ROLE_FLOOR, int(guild.member_count * HIGH_VALUE_ROLE_PERCENT))
        role_ids = {
            role.id for role in guild.roles if role.mentionable and len(role.members) >= threshold
        }
        self._high_value_role_cache[guild.id] = (now, role_ids)
        return role_ids

    async def _handle_webhook_message(self, message: discord.Message) -> None:
        # Webhooks can't be timed out; a full response path (delete + disable the
        # webhook + alert) is deferred to a later step. This must stay crash-proof,
        # since webhook "authors" don't support guild_permissions like real members.
        return

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        if not cog_enabled(self.bot, message.guild.id, "cerberus"):
            return
        if message.webhook_id is not None:
            await self._handle_webhook_message(message)
            return
        if message.author.bot:
            return

        guild_conf = self._guild_conf(message.guild.id)
        if _is_exempt(guild_conf, message.author):
            return

        now = time.monotonic()
        high_value_ids = self._high_value_role_ids(message.guild)
        mentions_high_value_role = message.mention_everyone or any(
            role.id in high_value_ids for role in message.role_mentions
        )
        event = MessageEvent(
            timestamp=now,
            channel_id=message.channel.id,
            content_hash=_content_hash(message),
            mentions_high_value_role=mentions_high_value_role,
            message=message,
        )
        key = (message.guild.id, message.author.id)
        events = self._member_activity.setdefault(key, deque())
        events.append(event)
        _prune_deque(events, MEMBER_ACTIVITY_MAX_AGE_SECONDS, now)

        # Guild-wide duplicate-content tracking runs regardless of this member's
        # own actioned status — it exists specifically to catch coordinated raids
        # that spread load thin across many accounts, each staying under their
        # own individual Pattern A/B threshold.
        if event.content_hash is not None:
            hash_key = (message.guild.id, event.content_hash)
            hash_events = self._content_hash_activity.setdefault(hash_key, deque())
            hash_events.append(HashEvent(timestamp=now, author_id=message.author.id, message=message))
            _prune_deque(hash_events, DUP_CONTENT_WINDOW_SECONDS, now)
            distinct_authors = {e.author_id for e in hash_events}
            if len(distinct_authors) >= DUP_CONTENT_MIN_ACCOUNTS:
                events_by_author = {
                    author_id: [e for e in hash_events if e.author_id == author_id]
                    for author_id in distinct_authors
                }
                await self._trigger_raid_wave(
                    message.guild, distinct_authors,
                    reason="Duplicate content posted by multiple accounts",
                    events_by_author=events_by_author,
                )

        last_actioned = self._actioned_members.get(key)
        if last_actioned is not None and now - last_actioned < ACTION_REARM_SECONDS:
            return  # already handled recently; don't re-trigger on every message

        if _check_pattern_a(events, now):
            window = _pattern_a_window(events, now)
            self._actioned_members[key] = now
            await self._respond_to_member(
                message.guild, message.author, window,
                reason="Pattern A: burst across multiple channels with a high-value role mention",
                role_pinged=_describe_role_pinged(window),
            )

            trips = self._pattern_a_trips.setdefault(message.guild.id, deque())
            trips.append(PatternATrip(timestamp=now, member_id=message.author.id))
            _prune_deque(trips, RAID_WAVE_WINDOW_SECONDS, now)
            distinct_trippers = {t.member_id for t in trips}
            if len(distinct_trippers) >= RAID_WAVE_MIN_ACCOUNTS:
                await self._trigger_raid_wave(
                    message.guild, distinct_trippers,
                    reason="Multiple accounts tripped Pattern A", events_by_author=None,
                )
        elif _check_pattern_b(events, now):
            self._actioned_members[key] = now
            await self._respond_to_member(
                message.guild, message.author, list(events),
                reason="Pattern B: message flooding",
                role_pinged=None,
            )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if not cog_enabled(self.bot, member.guild.id, "cerberus"):
            return
        key = (member.guild.id, member.id)
        self._member_activity.pop(key, None)
        self._actioned_members.pop(key, None)

    def _prune_dict_of_deques(self, mapping: dict, max_age: float, now: float) -> list:
        """Prune every deque in `mapping` to `max_age` and delete any key whose
        deque became empty as a result. Returns the deleted keys, so a caller
        needing extra cleanup tied to a specific mapping (e.g. _member_activity's
        paired _actioned_members entries) can act on them afterward."""
        stale_keys = []
        for key, dq in mapping.items():
            _prune_deque(dq, max_age, now)
            if not dq:
                stale_keys.append(key)
        for key in stale_keys:
            del mapping[key]
        return stale_keys

    @tasks.loop(seconds=PRUNE_INTERVAL_SECONDS)
    async def _prune_activity(self):
        now = time.monotonic()
        stale_keys = self._prune_dict_of_deques(
            self._member_activity, MEMBER_ACTIVITY_MAX_AGE_SECONDS, now
        )
        for key in stale_keys:
            self._actioned_members.pop(key, None)

        self._prune_dict_of_deques(self._content_hash_activity, DUP_CONTENT_WINDOW_SECONDS, now)
        self._prune_dict_of_deques(self._pattern_a_trips, RAID_WAVE_WINDOW_SECONDS, now)

    async def _trigger_raid_wave(self, guild, involved_member_ids, reason, *, events_by_author):
        """Common entry point for both wave triggers (repeated Pattern A trips,
        and the duplicate-content check). Responds to any involved member not
        already actioned, then escalates to a (stubbed) lockdown."""
        now = time.monotonic()
        for member_id in involved_member_ids:
            key = (guild.id, member_id)
            last_actioned = self._actioned_members.get(key)
            if last_actioned is not None and now - last_actioned < ACTION_REARM_SECONDS:
                continue
            member = guild.get_member(member_id)
            if member is None:
                continue
            events = (events_by_author or {}).get(member_id) or list(
                self._member_activity.get(key, ())
            )
            if not events:
                continue
            self._actioned_members[key] = now
            await self._respond_to_member(guild, member, events, reason=reason, role_pinged=None)

        guild_conf = self._guild_conf(guild.id)
        lockdown = guild_conf["lockdown"]
        now_epoch = time.time()
        lockdown["trigger_timestamps"].append(now_epoch)
        lockdown["trigger_timestamps"] = [
            t for t in lockdown["trigger_timestamps"] if now_epoch - t <= LOCKDOWN_REPEAT_WINDOW_SECONDS
        ]
        self._save_config()

        await self._start_lockdown(guild, reason)

    async def _rehydrate_lockdowns(self):
        """Resume any lockdown that was still active when the bot last stopped,
        so a restart mid-raid doesn't silently make it permanent (data says
        active but nothing is scheduled to lift it) or silently lift it early."""
        await self.bot.wait_until_ready()
        now_epoch = time.time()
        for guild_id_str, guild_conf in list(self.config.items()):
            lockdown = guild_conf.get("lockdown", {})
            if not lockdown.get("active"):
                continue
            guild = self.bot.get_guild(int(guild_id_str))
            if guild is None:
                continue
            expires_at = lockdown.get("expires_at")
            if expires_at is None:
                continue  # stay-locked lockdown: holds until a manual `.cerberus unlock`
            remaining = expires_at - now_epoch
            if remaining <= 0:
                await self._lift_lockdown(guild, manual=False)
            else:
                self._lockdown_tasks[guild.id] = asyncio.ensure_future(
                    self._auto_lift_after(guild.id, remaining)
                )

    async def _start_lockdown(self, guild: discord.Guild, reason: str):
        """Deny @everyone send_messages in every text channel, with an explicit
        allow carve-out for any configured protected role(s), snapshotting each
        channel's prior state first so it can be restored exactly on lift.
        In shadow mode only the alert is sent — no permissions are touched."""
        lock = self._lockdown_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            guild_conf = self._guild_conf(guild.id)
            lockdown = guild_conf["lockdown"]
            if lockdown["active"]:
                return  # already locked; don't re-snapshot an already-modified state

            if guild_conf["mode"] == "shadow":
                await self._send_lockdown_alert(guild, reason, stay_locked=False, shadow=True)
                return

            protected_roles = [guild.get_role(rid) for rid in guild_conf["protected_role_ids"]]
            protected_roles = [r for r in protected_roles if r is not None]

            channel_overwrites: dict[str, dict | None] = {}
            protected_role_overwrites: dict[str, dict[str, dict | None]] = {
                str(role.id): {} for role in protected_roles
            }
            semaphore = asyncio.Semaphore(5)  # bound concurrent edits against rate limits

            async def lock_one(channel):
                async with semaphore:
                    channel_overwrites[str(channel.id)] = snapshot_overwrite(
                        channel, guild.default_role
                    )
                    overwrite = channel.overwrites_for(guild.default_role)
                    overwrite.send_messages = False
                    await channel.set_permissions(
                        guild.default_role, overwrite=overwrite,
                        reason=f"Cerberus lockdown: {reason}",
                    )
                    for role in protected_roles:
                        protected_role_overwrites[str(role.id)][str(channel.id)] = (
                            snapshot_overwrite(channel, role)
                        )
                        allow_overwrite = channel.overwrites_for(role)
                        allow_overwrite.send_messages = True
                        await channel.set_permissions(
                            role, overwrite=allow_overwrite,
                            reason=f"Cerberus lockdown: {reason}",
                        )

            # return_exceptions=True is essential here: with the default False,
            # one channel raising would propagate immediately, aborting before
            # anything below is persisted — so channels that HAD already been
            # successfully locked would be stuck with zero record to restore
            # them, and the other still-running lock_one() calls would keep
            # mutating real Discord state in the background, unretrieved.
            # Evaluated once and reused below: Guild.text_channels rebuilds its
            # list fresh on every access (not cached), and channels could be
            # created/deleted/reordered during the gather below — re-evaluating
            # it after the fact could misalign results with the wrong channel.
            channels = list(guild.text_channels)

            results = await asyncio.gather(
                *(lock_one(channel) for channel in channels),
                return_exceptions=True,
            )
            failures = [
                (channel, result)
                for channel, result in zip(channels, results)
                if isinstance(result, BaseException)
            ]
            if failures:
                logger.warning(
                    "Cerberus: failed to lock %d/%d channel(s) in guild %s: %s",
                    len(failures), len(channels), guild.id,
                    ", ".join(f"{c.id} ({e})" for c, e in failures),
                )

            # A repeat trigger within the last hour means this is an ongoing raid,
            # not a one-off — stay locked until a mod manually lifts it instead of
            # auto-lifting into what might still be an active attack. Persisted as
            # expires_at=None so a restart holds the lockdown too instead of
            # rehydrating it into an auto-lifting one.
            stay_locked = len(lockdown["trigger_timestamps"]) > 1

            # Always persist whatever succeeded, even if some channels failed —
            # a partially-applied lockdown must still be fully restorable.
            now_epoch = time.time()
            lockdown["active"] = True
            lockdown["started_at"] = now_epoch
            lockdown["expires_at"] = (
                None if stay_locked else now_epoch + LOCKDOWN_MAX_DURATION_SECONDS
            )
            lockdown["channel_overwrites"] = channel_overwrites
            lockdown["protected_role_overwrites"] = protected_role_overwrites
            self._save_config()

            if not stay_locked:
                self._lockdown_tasks[guild.id] = asyncio.ensure_future(
                    self._auto_lift_after(guild.id, LOCKDOWN_MAX_DURATION_SECONDS)
                )
            await self._send_lockdown_alert(guild, reason, stay_locked, failed_count=len(failures))

    async def _auto_lift_after(self, guild_id: int, delay: float):
        await asyncio.sleep(delay)
        guild = self.bot.get_guild(guild_id)
        if guild is not None:
            await self._lift_lockdown(guild, manual=False)

    async def _lift_lockdown(self, guild: discord.Guild, *, manual: bool, actor=None):
        lock = self._lockdown_locks.setdefault(guild.id, asyncio.Lock())
        async with lock:
            guild_conf = self._guild_conf(guild.id)
            lockdown = guild_conf["lockdown"]
            if not lockdown["active"]:
                return

            for channel_id_str, snapshot in lockdown["channel_overwrites"].items():
                channel = guild.get_channel(int(channel_id_str))
                if channel is not None:
                    await restore_overwrite(
                        channel, guild.default_role, snapshot, reason="Cerberus: lockdown lifted"
                    )
            for role_id_str, per_channel in lockdown["protected_role_overwrites"].items():
                role = guild.get_role(int(role_id_str))
                if role is None:
                    continue
                for channel_id_str, snapshot in per_channel.items():
                    channel = guild.get_channel(int(channel_id_str))
                    if channel is not None:
                        await restore_overwrite(
                            channel, role, snapshot, reason="Cerberus: lockdown lifted"
                        )

            lockdown["active"] = False
            lockdown["started_at"] = None
            lockdown["expires_at"] = None
            lockdown["channel_overwrites"] = {}
            lockdown["protected_role_overwrites"] = {}
            self._save_config()
            self._lockdown_tasks.pop(guild.id, None)
            await self._send_lockdown_lifted_alert(guild, manual=manual, actor=actor)

    async def _send_to_log_channel(self, guild, guild_conf, embed) -> None:
        """Resolve the guild's configured log channel and send `embed` to it,
        silently no-op'ing if none is configured/resolvable, and swallowing a
        Forbidden (missing send perms there) rather than raising."""
        log_channel_id = guild_conf["log_channel_id"]
        log_channel = guild.get_channel(log_channel_id) if log_channel_id else None
        if log_channel is None:
            return
        try:
            await log_channel.send(embed=embed)
        except discord.Forbidden:
            pass

    async def _send_lockdown_alert(self, guild, reason, stay_locked, *, failed_count=0, shadow=False):
        guild_conf = self._guild_conf(guild.id)

        description = reason
        if shadow:
            description += (
                "\n\nIn active mode this would have started a guild-wide lockdown. "
                "No channels were locked."
            )
        elif stay_locked:
            description += (
                "\n\n⚠️ This is a repeat trigger within the last hour — staying locked "
                "until manually lifted with `.cerberus unlock`."
            )
        else:
            minutes = LOCKDOWN_MAX_DURATION_SECONDS // 60
            description += f"\n\nAuto-lifts in {minutes} minutes, or use `.cerberus unlock`."
        if failed_count:
            description += (
                f"\n\n⚠️ Failed to lock {failed_count} channel(s) — check the bot's "
                f"permissions there. Everything that DID lock is fully restorable."
            )

        embed = discord.Embed(
            title=(
                "🔒 Cerberus Lockdown [SHADOW MODE — no action taken]"
                if shadow
                else "🔒 Cerberus Lockdown Started"
            ),
            description=description,
            color=discord.Color.orange() if shadow else discord.Color.dark_red(),
            timestamp=discord.utils.utcnow(),
        )
        await self._send_to_log_channel(guild, guild_conf, embed)

    async def _send_lockdown_lifted_alert(self, guild, *, manual, actor=None):
        guild_conf = self._guild_conf(guild.id)

        description = (
            f"Manually lifted by {actor.mention if actor else 'a moderator'}."
            if manual
            else "Auto-lifted after the lockdown duration elapsed."
        )
        embed = discord.Embed(
            title="🔓 Cerberus Lockdown Lifted", description=description,
            color=discord.Color.green(), timestamp=discord.utils.utcnow(),
        )
        await self._send_to_log_channel(guild, guild_conf, embed)

    async def _respond_to_member(self, guild, member, events, reason, *, role_pinged):
        """Timeout, then delete, then alert — in that order, so the burst is
        stopped before cleanup, and always alert (even in shadow mode) so mods
        can validate detections before trusting the cog with real actions."""
        guild_conf = self._guild_conf(guild.id)
        shadow = guild_conf["mode"] == "shadow"

        timeout_ok, timeout_error = True, None
        if not shadow:
            try:
                await member.timeout(
                    timedelta(seconds=CERBERUS_TIMEOUT_DURATION_SECONDS),
                    reason=f"Cerberus: {reason}",
                )
            except discord.HTTPException as e:
                timeout_ok, timeout_error = False, e

        deleted = 0
        if not shadow:
            for event in events:
                try:
                    await event.message.delete()
                    deleted += 1
                except discord.HTTPException:
                    pass

        action = ActionResult(timeout_ok=timeout_ok, timeout_error=timeout_error, deleted=deleted)
        await self._send_alert(guild, member, events, reason, role_pinged, shadow, action)

    async def _send_alert(
        self, guild, member, events, reason, role_pinged, shadow, action: ActionResult
    ):
        guild_conf = self._guild_conf(guild.id)

        if not action.timeout_ok:
            title = "⚠️ CERBERUS: TIMEOUT FAILED — manual action required"
            color = discord.Color.red()
        elif shadow:
            title = "🐕 Cerberus Detection [SHADOW MODE — no action taken]"
            color = discord.Color.orange()
        else:
            title = "🐕 Cerberus Action Taken"
            color = discord.Color.orange()

        channels_touched = ", ".join(
            f"<#{cid}>" for cid in sorted({e.message.channel.id for e in events})
        )

        embed = discord.Embed(title=title, color=color, timestamp=discord.utils.utcnow())
        embed.add_field(name="Member", value=f"{member.mention} ({member})", inline=False)
        embed.add_field(name="Reason", value=reason, inline=False)
        embed.add_field(name="Channels touched", value=channels_touched or "None", inline=False)
        if role_pinged:
            embed.add_field(name="Role pinged", value=role_pinged, inline=False)
        embed.add_field(name="Messages deleted", value=str(action.deleted))
        if not action.timeout_ok:
            embed.add_field(
                name="Timeout failed",
                value=(
                    f"Could not time out {member.mention} — likely a role-hierarchy issue "
                    f"(the bot's role may sit below the member's top role). Manual action "
                    f"needed.\nError: {action.timeout_error}"
                ),
                inline=False,
            )

        await self._send_to_log_channel(guild, guild_conf, embed)

    @_prune_activity.before_loop
    async def _before_prune_activity(self):
        await self.bot.wait_until_ready()

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

        embed = discord.Embed(title="🐕 Cerberus Status", color=discord.Color.blue())
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
            value = (
                f"🔒 ACTIVE — {remaining}s remaining"
                if remaining is not None
                else "🔒 ACTIVE — until manually unlocked"
            )
        else:
            value = "Not active"
        embed.add_field(name="Lockdown", value=value, inline=False)
        return embed

    async def _exemptions_embed(self, ctx, guild_conf) -> discord.Embed:
        roles = [ctx.guild.get_role(rid) for rid in guild_conf["exempt_role_ids"]]
        roles = [r for r in roles if r is not None]
        members = [ctx.guild.get_member(uid) for uid in guild_conf["exempt_user_ids"]]
        members = [m for m in members if m is not None]

        embed = discord.Embed(title="🐕 Cerberus Exemptions", color=discord.Color.blue())
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
    async def cerberus(self, ctx):
        """Show the current cerberus configuration and status."""
        guild_conf = self._guild_conf(ctx.guild.id)
        await ctx.reply(embed=await self._status_embed(ctx, guild_conf))

    @cerberus.command(name="status")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def cerberus_status(self, ctx):
        """Show the current cerberus configuration and status."""
        guild_conf = self._guild_conf(ctx.guild.id)
        await ctx.reply(embed=await self._status_embed(ctx, guild_conf))

    @cerberus.command(name="mode")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def cerberus_mode(self, ctx, mode: str):
        """Set cerberus's mode: shadow (detect + alert only) or active (also take action)."""
        mode = mode.lower()
        if mode not in ("shadow", "active"):
            await ctx.reply("Mode must be `shadow` or `active`.")
            return
        guild_conf = self._guild_conf(ctx.guild.id)
        guild_conf["mode"] = mode
        self._save_config()
        await ctx.reply(f"🐕 Cerberus mode set to **{mode}**.")

    @cerberus.command(name="setlog")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def cerberus_setlog(self, ctx, channel: discord.TextChannel):
        """Set the channel cerberus alerts are posted to."""
        guild_conf = self._guild_conf(ctx.guild.id)
        guild_conf["log_channel_id"] = channel.id
        self._save_config()
        await ctx.reply(f"🐕 Cerberus alerts will be sent to {channel.mention}.")

    @cerberus.group(name="exempt", invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def cerberus_exempt(self, ctx):
        """Show cerberus's current exemption list."""
        guild_conf = self._guild_conf(ctx.guild.id)
        await ctx.reply(embed=await self._exemptions_embed(ctx, guild_conf))

    @cerberus_exempt.command(name="add")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def cerberus_exempt_add(
        self, ctx, target: typing.Union[discord.Role, discord.Member]
    ):
        """Exempt a role or member from all cerberus checks.

        If a role and a member share the same name, prefer a mention or ID —
        the role is tried first and wins any name collision."""
        guild_conf = self._guild_conf(ctx.guild.id)
        if isinstance(target, discord.Role):
            ids, kind = guild_conf["exempt_role_ids"], "Role"
        else:
            ids, kind = guild_conf["exempt_user_ids"], "Member"
        if target.id not in ids:
            ids.append(target.id)
            self._save_config()
        await ctx.reply(f"✅ {kind} {target.mention} is now exempt from cerberus checks.")

    @cerberus_exempt.command(name="remove")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def cerberus_exempt_remove(
        self, ctx, target: typing.Union[discord.Role, discord.Member]
    ):
        """Remove a role or member's cerberus exemption.

        If a role and a member share the same name, prefer a mention or ID —
        the role is tried first and wins any name collision."""
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

    @cerberus_exempt.command(name="list")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def cerberus_exempt_list(self, ctx):
        """List cerberus's current exemptions."""
        guild_conf = self._guild_conf(ctx.guild.id)
        await ctx.reply(embed=await self._exemptions_embed(ctx, guild_conf))

    @cerberus.group(name="protectedrole", invoke_without_command=True)
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def cerberus_protectedrole(self, ctx):
        """Show the roles exempt from cerberus lockdowns."""
        guild_conf = self._guild_conf(ctx.guild.id)
        await ctx.reply(await self._protected_roles_reply(ctx, guild_conf))

    @cerberus_protectedrole.command(name="add")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def cerberus_protectedrole_add(self, ctx, role: discord.Role):
        """Exempt a role from cerberus lockdowns (it keeps send permission)."""
        guild_conf = self._guild_conf(ctx.guild.id)
        if role.id not in guild_conf["protected_role_ids"]:
            guild_conf["protected_role_ids"].append(role.id)
            self._save_config()
        await ctx.reply(f"🛡️ {role.mention} is now protected from cerberus lockdowns.")

    @cerberus_protectedrole.command(name="remove")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def cerberus_protectedrole_remove(self, ctx, role: discord.Role):
        """Remove a role's protection from cerberus lockdowns."""
        guild_conf = self._guild_conf(ctx.guild.id)
        if role.id in guild_conf["protected_role_ids"]:
            guild_conf["protected_role_ids"].remove(role.id)
            self._save_config()
            await ctx.reply(f"{role.mention} is no longer protected.")
        else:
            await ctx.reply(f"{role.mention} wasn't protected.")

    @cerberus_protectedrole.command(name="list")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def cerberus_protectedrole_list(self, ctx):
        """List the roles exempt from cerberus lockdowns."""
        guild_conf = self._guild_conf(ctx.guild.id)
        await ctx.reply(await self._protected_roles_reply(ctx, guild_conf))

    @cerberus.command(name="unlock")
    @commands.has_permissions(manage_guild=True)
    @commands.guild_only()
    async def cerberus_unlock(self, ctx):
        """Manually end an active cerberus lockdown."""
        guild_conf = self._guild_conf(ctx.guild.id)
        if not guild_conf["lockdown"]["active"]:
            await ctx.reply("There is no active cerberus lockdown.")
            return
        task = self._lockdown_tasks.pop(ctx.guild.id, None)
        if task is not None:
            task.cancel()
        await self._lift_lockdown(ctx.guild, manual=True, actor=ctx.author)
        await ctx.reply("🔓 Lockdown manually lifted.")


async def setup(bot):
    await bot.add_cog(Cerberus(bot))
