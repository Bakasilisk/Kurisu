import asyncio
import logging
import os
import time
from datetime import timedelta

import discord
from discord.ext import commands, tasks

from .management import cog_enabled, has_permissions_or_owner, reply_ephemeral_aware
from .storage import backfill_defaults, data_path, load_json, save_json_atomic

logger = logging.getLogger(__name__)

PALANTIR_FILE = data_path("palantir.json")
MESSAGES_FILE = data_path("palantir_messages.json")
ATTACHMENTS_DIR = data_path("palantir_attachments")

# Message-content cache bounds (module-level constants, no config command — repo
# convention is to defer tunable knobs until real usage justifies them).
MSG_CACHE_MAX_PER_GUILD = 20_000
MSG_CACHE_MAX_AGE_DAYS = 14
MSG_CACHE_FLUSH_INTERVAL_SECONDS = 30
MSG_CACHE_SWEEP_INTERVAL_SECONDS = 1800

# Discord's default per-file upload cap; attachments larger than this are never
# archived (logged as a URL only, best-effort, may expire).
ARCHIVE_MAX_BYTES = 8 * 1024 * 1024

# How many individual message contents to sample in a bulk-delete embed, to stay
# well under Discord's per-field character limit.
BULK_DELETE_SAMPLE_LIMIT = 10

CATEGORIES = ("members", "messages", "roles", "voice", "modactions", "invites", "server")

CATEGORY_COLORS = {
    "members": discord.Color.blue(),
    "messages": discord.Color.orange(),
    "roles": discord.Color.purple(),
    "voice": discord.Color.teal(),
    "modactions": discord.Color.red(),
    "invites": discord.Color.green(),
    "server": discord.Color.dark_grey(),
}


def _default_guild_config() -> dict:
    return {"log_channel_id": None, "disabled_categories": [], "archive_attachments": False}


class Palantir(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config = load_json(PALANTIR_FILE)
        self.messages = load_json(MESSAGES_FILE)
        self._cache_lock = asyncio.Lock()
        self._messages_dirty = False
        # Per-guild snapshot of {invite_code: uses}, used to detect which invite
        # was consumed by a new join. Primed on cog_load and kept current by the
        # invite_create/invite_delete listeners and each join's own re-fetch.
        self._invite_uses: dict[int, dict[str, int]] = {}
        # Tracks the last-seen `count` of each message_delete audit-log entry so
        # a fresh moderator deletion can be told apart from Discord's aggregation
        # of repeated deletes into one incrementing entry. Keyed by entry id.
        self._audit_delete_counts: dict[int, int] = {}
        if not self._flush_messages.is_running():
            self._flush_messages.start()
        if not self._sweep_messages.is_running():
            self._sweep_messages.start()

    async def cog_load(self):
        # Fire-and-forget, like watchdog's lockdown rehydration — not tracked or
        # cancelled on unload since it only reads guild state, it doesn't hold
        # anything that needs to be torn down.
        asyncio.ensure_future(self._prime_invite_cache())

    def cog_unload(self):
        self._flush_messages.cancel()
        self._sweep_messages.cancel()
        # cog_unload can't be a coroutine, so it can't await self._cache_lock. If
        # a flush is already in flight on the thread pool, that write already
        # carries the latest data, so skip ours rather than race it.
        if not self._cache_lock.locked() and self._messages_dirty:
            self._messages_dirty = False
            save_json_atomic(MESSAGES_FILE, self._messages_snapshot())

    # --- Config -------------------------------------------------------------

    def _save_config(self):
        save_json_atomic(PALANTIR_FILE, self.config)

    def _guild_conf(self, guild_id: int) -> dict:
        guild_conf = self.config.setdefault(str(guild_id), {})
        # Backfill any keys missing from a config persisted by an earlier schema,
        # so accessing a newer key never raises a bare KeyError.
        return backfill_defaults(guild_conf, _default_guild_config())

    def _should_log(self, guild: discord.Guild, category: str) -> bool:
        """Whether an event in `category` should be logged for this guild at
        all — checked both by listeners (to skip unnecessary work like cache
        writes or attachment downloads) and defensively inside _log itself."""
        if not cog_enabled(self.bot, guild.id, "palantir"):
            return False
        guild_conf = self._guild_conf(guild.id)
        return category not in guild_conf["disabled_categories"]

    def _is_log_channel(self, guild: discord.Guild, channel_id: int) -> bool:
        return self._guild_conf(guild.id)["log_channel_id"] == channel_id

    @staticmethod
    def _has_audit_log_access(guild: discord.Guild) -> bool:
        me = guild.me
        return me is not None and me.guild_permissions.view_audit_log

    @staticmethod
    def _has_manage_guild(guild: discord.Guild) -> bool:
        me = guild.me
        return me is not None and me.guild_permissions.manage_guild

    async def _resolve_message_deleter(self, guild: discord.Guild, author_id: int | None, channel_id: int):
        """Best-effort: identify which moderator deleted someone else's message
        via the audit log. Returns None for self-deletions (Discord logs no
        audit entry for those) or when the bot lacks View Audit Log. Discord
        aggregates repeated deletes of the same author in the same channel into
        a single entry with an incrementing `count`, so a bump in that count —
        or a brand-new, just-created entry — is what marks a fresh moderator
        deletion, rather than an unchanged stale entry from an earlier purge."""
        if author_id is None or not self._has_audit_log_access(guild):
            return None
        try:
            entries = [
                entry
                async for entry in guild.audit_logs(
                    limit=5, action=discord.AuditLogAction.message_delete
                )
            ]
        except (discord.Forbidden, discord.HTTPException):
            return None

        match = None
        for entry in entries:
            if entry.target is None or entry.target.id != author_id:
                continue
            extra_channel = getattr(entry.extra, "channel", None)
            if extra_channel is not None and extra_channel.id != channel_id:
                continue
            match = entry
            break
        if match is None:
            return None

        count = getattr(match.extra, "count", 1) or 1
        prev = self._audit_delete_counts.get(match.id)
        if prev is None:
            # Never-seen entry: only trust it if it was just created, otherwise a
            # self-delete would be misattributed to a stale mod-delete entry that
            # happens to be the most recent one for this author/channel.
            is_mod_delete = (discord.utils.utcnow() - match.created_at).total_seconds() < 30
        else:
            is_mod_delete = count > prev
        self._audit_delete_counts[match.id] = count
        # Keep the tracking dict bounded over long uptimes (insertion-ordered).
        if len(self._audit_delete_counts) > 1000:
            self._audit_delete_counts.pop(next(iter(self._audit_delete_counts)), None)
        return match.user if is_mod_delete else None

    async def _log(
        self, guild: discord.Guild, category: str, embed: discord.Embed,
        *, files: list[discord.File] | None = None,
    ) -> None:
        """Shared post helper: resolve the configured channel, guard on
        cog/category state and channel presence, then send — the
        resolve-channel -> guard -> try/except discord.Forbidden shape from
        watchdog._send_alert."""
        if not self._should_log(guild, category):
            return
        guild_conf = self._guild_conf(guild.id)
        channel_id = guild_conf["log_channel_id"]
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            return
        try:
            if files:
                await channel.send(embed=embed, files=files)
            else:
                await channel.send(embed=embed)
        except discord.Forbidden:
            pass

    # --- Message-content cache -----------------------------------------------

    def _messages_snapshot(self) -> dict:
        """A shallow copy safe to hand to a background thread for serialization."""
        return {guild_id: dict(entries) for guild_id, entries in self.messages.items()}

    def _cache_get(self, guild_id: int, message_id: int) -> dict | None:
        return self.messages.get(str(guild_id), {}).get(str(message_id))

    async def _update_cached_content(self, guild_id: int, message_id: int, content: str) -> None:
        """Refresh the stored content of an already-cached message after an edit,
        so a later delete shows the most recent version. No-op if the message was
        never cached (e.g. it predates the cache or fell out of it)."""
        async with self._cache_lock:
            entry = self.messages.get(str(guild_id), {}).get(str(message_id))
            if entry is not None:
                entry["content"] = content
                self._messages_dirty = True

    async def _cache_message(self, message: discord.Message) -> None:
        entry = {
            "author_id": message.author.id,
            "channel_id": message.channel.id,
            "content": message.content,
            "attachment_urls": [a.url for a in message.attachments],
            "created_at": message.created_at.timestamp(),
        }
        async with self._cache_lock:
            guild_cache = self.messages.setdefault(str(message.guild.id), {})
            guild_cache[str(message.id)] = entry
            # Dict insertion order tracks arrival order closely enough (messages
            # are ingested in send order) to use as an O(1) FIFO stand-in for a
            # full created_at scan across up to MSG_CACHE_MAX_PER_GUILD entries.
            while len(guild_cache) > MSG_CACHE_MAX_PER_GUILD:
                oldest_id = next(iter(guild_cache))
                guild_cache.pop(oldest_id, None)
                self._delete_archived_attachments(message.guild.id, int(oldest_id))
            self._messages_dirty = True

    async def _drop_cache_entry(self, guild_id: int, message_id: int) -> None:
        """Drop a message's cache entry (it can't be edited again after being
        deleted) and prune any archived attachment bytes alongside it."""
        async with self._cache_lock:
            guild_cache = self.messages.get(str(guild_id))
            if guild_cache and guild_cache.pop(str(message_id), None) is not None:
                self._messages_dirty = True
        self._delete_archived_attachments(guild_id, message_id)

    @tasks.loop(seconds=MSG_CACHE_FLUSH_INTERVAL_SECONDS)
    async def _flush_messages(self):
        async with self._cache_lock:
            if self._messages_dirty:
                self._messages_dirty = False
                await asyncio.to_thread(save_json_atomic, MESSAGES_FILE, self._messages_snapshot())

    @_flush_messages.before_loop
    async def _before_flush_messages(self):
        await self.bot.wait_until_ready()

    @tasks.loop(seconds=MSG_CACHE_SWEEP_INTERVAL_SECONDS)
    async def _sweep_messages(self):
        cutoff = time.time() - MSG_CACHE_MAX_AGE_DAYS * 86400
        async with self._cache_lock:
            for guild_id_str, guild_cache in list(self.messages.items()):
                stale_ids = [
                    message_id for message_id, entry in guild_cache.items()
                    if entry.get("created_at", 0) < cutoff
                ]
                for message_id in stale_ids:
                    guild_cache.pop(message_id, None)
                    await asyncio.to_thread(
                        self._delete_archived_attachments, int(guild_id_str), int(message_id)
                    )
                if stale_ids:
                    self._messages_dirty = True
                if not guild_cache:
                    del self.messages[guild_id_str]

    @_sweep_messages.before_loop
    async def _before_sweep_messages(self):
        await self.bot.wait_until_ready()

    # --- Attachment archiving -------------------------------------------------

    def _attachment_guild_dir(self, guild_id: int) -> str:
        return os.path.join(ATTACHMENTS_DIR, str(guild_id))

    @staticmethod
    def _write_attachment_file(path: str, data: bytes) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)

    async def _archive_attachments(self, message: discord.Message) -> None:
        guild_conf = self._guild_conf(message.guild.id)
        if not guild_conf["archive_attachments"] or not message.attachments:
            return
        for index, attachment in enumerate(message.attachments):
            if attachment.size > ARCHIVE_MAX_BYTES:
                continue
            try:
                data = await attachment.read()
            except (discord.HTTPException, discord.NotFound):
                continue
            if len(data) > ARCHIVE_MAX_BYTES:
                continue
            filename = os.path.basename(attachment.filename) or f"attachment{index}"
            path = os.path.join(
                self._attachment_guild_dir(message.guild.id), f"{message.id}_{index}_{filename}"
            )
            try:
                await asyncio.to_thread(self._write_attachment_file, path, data)
            except OSError:
                logger.warning("Palantir: failed to archive attachment for message %s", message.id)

    def _load_archived_attachments(self, guild_id: int, message_id: int) -> list[discord.File]:
        guild_dir = self._attachment_guild_dir(guild_id)
        if not os.path.isdir(guild_dir):
            return []
        prefix = f"{message_id}_"
        files = []
        for name in sorted(os.listdir(guild_dir)):
            if not name.startswith(prefix):
                continue
            path = os.path.join(guild_dir, name)
            # Strip the "<message_id>_<index>_" storage-key prefix back off so
            # the re-uploaded file keeps its original filename.
            parts = name.split("_", 2)
            display_name = parts[2] if len(parts) == 3 else name
            try:
                files.append(discord.File(path, filename=display_name))
            except OSError:
                continue
        return files

    def _delete_archived_attachments(self, guild_id: int, message_id: int) -> None:
        guild_dir = self._attachment_guild_dir(guild_id)
        if not os.path.isdir(guild_dir):
            return
        prefix = f"{message_id}_"
        for name in os.listdir(guild_dir):
            if name.startswith(prefix):
                try:
                    os.remove(os.path.join(guild_dir, name))
                except OSError:
                    pass

    # --- Invite tracking --------------------------------------------------

    async def _prime_invite_cache(self) -> None:
        await self.bot.wait_until_ready()
        for guild in self.bot.guilds:
            await self._snapshot_guild_invites(guild)

    async def _snapshot_guild_invites(self, guild: discord.Guild) -> None:
        try:
            invites = await guild.invites()
        except discord.HTTPException:
            return
        self._invite_uses[guild.id] = {invite.code: (invite.uses or 0) for invite in invites}

    async def _detect_used_invite(self, guild: discord.Guild) -> str | None:
        """Best-effort: diff the current invite-uses snapshot against the last
        known one to find which invite's use count went up. Not fully reliable
        (vanity URLs, races between two joins) but good enough for attribution."""
        before = self._invite_uses.get(guild.id, {})
        try:
            invites = await guild.invites()
        except discord.HTTPException:
            return None
        after = {invite.code: (invite.uses or 0) for invite in invites}
        self._invite_uses[guild.id] = after
        used = next(
            (invite for invite in invites if after.get(invite.code, 0) > before.get(invite.code, 0)),
            None,
        )
        if used is None:
            return None
        if used.inviter:
            return f"{used.inviter.mention} ({used.inviter})"
        return f"`{used.code}`"

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite) -> None:
        guild = invite.guild
        if guild is None:
            return
        self._invite_uses.setdefault(guild.id, {})[invite.code] = invite.uses or 0
        if not self._should_log(guild, "invites"):
            return
        embed = discord.Embed(
            title="🔗 Invite Created", color=CATEGORY_COLORS["invites"], timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="Code", value=f"`{invite.code}`", inline=False)
        embed.add_field(
            name="Channel", value=invite.channel.mention if invite.channel else "Unknown", inline=False
        )
        if invite.inviter:
            embed.add_field(
                name="Created By", value=f"{invite.inviter.mention} ({invite.inviter})", inline=False
            )
        embed.add_field(
            name="Max Uses", value=str(invite.max_uses) if invite.max_uses else "Unlimited", inline=False
        )
        embed.add_field(
            name="Expires",
            value=discord.utils.format_dt(invite.expires_at, style="R") if invite.expires_at else "Never",
            inline=False,
        )
        await self._log(guild, "invites", embed)

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite) -> None:
        guild = invite.guild
        if guild is None:
            return
        self._invite_uses.get(guild.id, {}).pop(invite.code, None)
        if not self._should_log(guild, "invites"):
            return
        embed = discord.Embed(
            title="🔗 Invite Deleted", color=CATEGORY_COLORS["invites"], timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="Code", value=f"`{invite.code}`", inline=False)
        embed.add_field(
            name="Channel", value=invite.channel.mention if invite.channel else "Unknown", inline=False
        )
        await self._log(guild, "invites", embed)

    # --- Message listeners --------------------------------------------------

    def _resolve_cached(
        self, guild_id: int, message_id: int, cached_msg: discord.Message | None
    ) -> dict:
        """Best-effort resolution of a message's author/content/attachments:
        Discord's own connection cache (`cached_msg`, may already be None by
        the time a raw event fires) first, falling back to palantir's own
        on-disk cache, falling back to nothing. Returns a superset dict —
        callers pick out whichever fields they need."""
        cached = self._cache_get(guild_id, message_id)
        author = cached_msg.author if cached_msg else None
        content = (cached_msg.content if cached_msg else None) or (
            cached.get("content") if cached else None
        )
        author_id = author.id if author is not None else (cached.get("author_id") if cached else None)
        attachment_urls = (
            [a.url for a in cached_msg.attachments]
            if cached_msg and cached_msg.attachments
            else (cached.get("attachment_urls", []) if cached else [])
        )
        return {
            "content": content,
            "author": author,
            "author_id": author_id,
            "attachment_urls": attachment_urls,
        }

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Cache ingest only — not itself a log event. Stores content and
        attachment URLs for later edit/delete lookups, and archives attachment
        bytes to disk if archiving is on for this guild."""
        guild = message.guild
        if guild is None or message.author.bot:
            return
        if not self._should_log(guild, "messages"):
            return
        if self._is_log_channel(guild, message.channel.id):
            return
        await self._cache_message(message)
        await self._archive_attachments(message)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        """Raw event so edits of messages no longer in discord.py's connection
        cache are still logged, falling back to palantir's own cache for the
        pre-edit content."""
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None or not self._should_log(guild, "messages"):
            return
        if self._is_log_channel(guild, payload.channel_id):
            return

        author_data = payload.data.get("author") or {}
        if author_data.get("bot"):
            return
        after_content = payload.data.get("content")
        if after_content is None:
            # MESSAGE_UPDATE with no content field is an embed-only change (e.g.
            # a link unfurl or a pin flag) — nothing to log.
            return

        cached_msg = payload.cached_message
        resolved = self._resolve_cached(guild.id, payload.message_id, cached_msg)
        before_content = resolved["content"]
        # Refresh the stored content regardless, so a later delete shows the
        # latest version — then skip the embed if nothing actually changed.
        await self._update_cached_content(guild.id, payload.message_id, after_content)
        if (before_content or "") == (after_content or ""):
            return

        author = resolved["author"]
        if author is None and author_data.get("id"):
            author = guild.get_member(int(author_data["id"]))
        if author is not None:
            author_label = f"{author.mention} ({author})"
        elif author_data.get("id"):
            author_label = f"<@{author_data['id']}>"
        else:
            author_label = "Unknown"

        embed = discord.Embed(
            title="✏️ Message Edited", color=CATEGORY_COLORS["messages"], timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="Author", value=author_label, inline=False)
        embed.add_field(name="Channel", value=f"<#{payload.channel_id}>", inline=False)
        embed.add_field(
            name="Before", value=(before_content or "*Unknown (not cached)*")[:1024], inline=False
        )
        embed.add_field(name="After", value=(after_content or "*Empty*")[:1024], inline=False)
        jump = f"https://discord.com/channels/{guild.id}/{payload.channel_id}/{payload.message_id}"
        embed.add_field(name="Jump", value=f"[Link]({jump})", inline=False)
        await self._log(guild, "messages", embed)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        """Raw event so deletions of messages no longer in discord.py's
        connection cache are still logged — from palantir's own cache and its
        archived attachments, which are keyed by message id regardless of cache."""
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None or not self._should_log(guild, "messages"):
            return
        if self._is_log_channel(guild, payload.channel_id):
            # Still drop any cache entry so it doesn't linger forever, but don't
            # log — avoids a feedback loop logging deletions of our own embeds.
            await self._drop_cache_entry(guild.id, payload.message_id)
            return

        cached_msg = payload.cached_message
        resolved = self._resolve_cached(guild.id, payload.message_id, cached_msg)
        author = resolved["author"]
        author_id = resolved["author_id"]
        if author is None and author_id is not None:
            author = guild.get_member(author_id)
        content = resolved["content"]
        attachment_urls = resolved["attachment_urls"]

        guild_conf = self._guild_conf(guild.id)
        files = (
            self._load_archived_attachments(guild.id, payload.message_id)
            if guild_conf["archive_attachments"]
            else []
        )

        embed = discord.Embed(
            title="🗑️ Message Deleted", color=CATEGORY_COLORS["messages"], timestamp=discord.utils.utcnow()
        )
        if author is not None:
            embed.add_field(name="Author", value=f"{author.mention} ({author})", inline=False)
        elif author_id is not None:
            embed.add_field(name="Author", value=f"Unknown (ID: {author_id})", inline=False)
        embed.add_field(name="Channel", value=f"<#{payload.channel_id}>", inline=False)

        deleter = await self._resolve_message_deleter(guild, author_id, payload.channel_id)
        if deleter is not None:
            deleted_by = f"{deleter.mention} ({deleter})"
        elif not self._has_audit_log_access(guild):
            deleted_by = "Unknown (bot lacks View Audit Log)"
        elif author is not None:
            deleted_by = f"{author.mention} (self-deleted)"
        elif author_id is not None:
            deleted_by = f"<@{author_id}> (self-deleted)"
        else:
            deleted_by = "Self-deleted or unknown"
        embed.add_field(name="Deleted by", value=deleted_by, inline=False)

        embed.add_field(name="Content", value=(content or "*No cached content*")[:1024], inline=False)
        if files:
            embed.add_field(name="Attachments", value=f"{len(files)} file(s) re-uploaded below", inline=False)
        elif attachment_urls:
            embed.add_field(name="Attachments", value="\n".join(attachment_urls)[:1024], inline=False)

        await self._log(guild, "messages", embed, files=files or None)
        await self._drop_cache_entry(guild.id, payload.message_id)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        """Raw event so bulk deletes (purges) are logged even for messages no
        longer in discord.py's connection cache, sampling from palantir's cache."""
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None or not self._should_log(guild, "messages"):
            return

        if self._is_log_channel(guild, payload.channel_id):
            for message_id in payload.message_ids:
                await self._drop_cache_entry(guild.id, message_id)
            return

        cached_by_id = {m.id: m for m in payload.cached_messages}
        sample_lines = []
        for message_id in sorted(payload.message_ids)[:BULK_DELETE_SAMPLE_LIMIT]:
            cached_msg = cached_by_id.get(message_id)
            cached = self._cache_get(guild.id, message_id)
            content = (
                (cached_msg.content if cached_msg else None)
                or (cached.get("content") if cached else "")
                or "*(no content)*"
            )
            if cached_msg is not None:
                author_id = cached_msg.author.id
            elif cached:
                author_id = cached.get("author_id")
            else:
                author_id = None
            author_label = f"<@{author_id}>" if author_id else "Unknown"
            sample_lines.append(f"{author_label}: {content}"[:200])

        total = len(payload.message_ids)
        embed = discord.Embed(
            title="🗑️ Bulk Message Delete", color=CATEGORY_COLORS["messages"], timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="Channel", value=f"<#{payload.channel_id}>", inline=False)
        embed.add_field(name="Messages Deleted", value=str(total), inline=False)
        if sample_lines:
            sample_text = "\n".join(sample_lines)
            if total > BULK_DELETE_SAMPLE_LIMIT:
                sample_text += f"\n… and {total - BULK_DELETE_SAMPLE_LIMIT} more"
            embed.add_field(
                name=f"Sample (up to {BULK_DELETE_SAMPLE_LIMIT})", value=sample_text[:1024], inline=False
            )

        await self._log(guild, "messages", embed)
        for message_id in payload.message_ids:
            await self._drop_cache_entry(guild.id, message_id)

    # --- Member listeners ----------------------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        guild = member.guild
        if not self._should_log(guild, "members"):
            return
        inviter = None
        if self._has_manage_guild(guild):
            inviter = await self._detect_used_invite(guild)

        account_age = discord.utils.utcnow() - member.created_at
        embed = discord.Embed(
            title="📥 Member Joined", color=CATEGORY_COLORS["members"], timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="Member", value=f"{member.mention} ({member})", inline=False)
        embed.add_field(
            name="Account Created", value=discord.utils.format_dt(member.created_at, style="R"), inline=False
        )
        if account_age < timedelta(days=7):
            embed.add_field(name="⚠️ New Account", value=f"{account_age.days} day(s) old", inline=False)
        embed.add_field(name="Invited By", value=inviter or "Unknown", inline=False)
        embed.set_thumbnail(url=member.display_avatar.url)
        await self._log(guild, "members", embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        guild = member.guild
        if not self._should_log(guild, "members"):
            return
        embed = discord.Embed(
            title="📤 Member Left", color=CATEGORY_COLORS["members"], timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="Member", value=f"{member.mention} ({member})", inline=False)
        if member.joined_at:
            embed.add_field(
                name="Joined", value=discord.utils.format_dt(member.joined_at, style="R"), inline=False
            )
        roles = [r.mention for r in member.roles if r != guild.default_role]
        embed.add_field(name="Roles", value=", ".join(roles) if roles else "None", inline=False)
        embed.set_thumbnail(url=member.display_avatar.url)
        await self._log(guild, "members", embed)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        """Nickname changes (category `members`) and raw role add/remove
        (category `roles`) — both unattributed. Moderator-attributed role
        grants and timeout changes are handled solely by
        on_audit_log_entry_create (category `modactions`) to avoid a second,
        duplicate embed for the same change; these two categories are
        independently mutable, so this is deliberate overlap, not redundancy."""
        guild = after.guild

        if before.nick != after.nick and self._should_log(guild, "members"):
            embed = discord.Embed(
                title="✏️ Nickname Changed", color=CATEGORY_COLORS["members"], timestamp=discord.utils.utcnow()
            )
            embed.add_field(name="Member", value=f"{after.mention} ({after})", inline=False)
            embed.add_field(name="Before", value=before.nick or "*None*", inline=False)
            embed.add_field(name="After", value=after.nick or "*None*", inline=False)
            await self._log(guild, "members", embed)

        if before.roles != after.roles and self._should_log(guild, "roles"):
            before_ids = {r.id for r in before.roles}
            after_ids = {r.id for r in after.roles}
            added = [r for r in after.roles if r.id not in before_ids]
            removed = [r for r in before.roles if r.id not in after_ids]
            if added or removed:
                embed = discord.Embed(
                    title="🔧 Member Roles Changed", color=CATEGORY_COLORS["roles"],
                    timestamp=discord.utils.utcnow(),
                )
                embed.add_field(name="Member", value=f"{after.mention} ({after})", inline=False)
                if added:
                    embed.add_field(
                        name="Added", value=", ".join(r.mention for r in added)[:1024], inline=False
                    )
                if removed:
                    embed.add_field(
                        name="Removed", value=", ".join(r.mention for r in removed)[:1024], inline=False
                    )
                await self._log(guild, "roles", embed)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User) -> None:
        """Silent fallback only: if the bot holds View Audit Log,
        on_audit_log_entry_create posts the moderator-attributed version
        instead, so this doesn't also fire (would be a duplicate embed)."""
        if not self._should_log(guild, "modactions") or self._has_audit_log_access(guild):
            return
        embed = discord.Embed(
            title="🔨 Member Banned", color=CATEGORY_COLORS["modactions"], timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="User", value=f"{user.mention} ({user})", inline=False)
        embed.add_field(name="Moderator", value="Unknown (bot lacks View Audit Log)", inline=False)
        await self._log(guild, "modactions", embed)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User) -> None:
        """Silent fallback only — see on_member_ban."""
        if not self._should_log(guild, "modactions") or self._has_audit_log_access(guild):
            return
        embed = discord.Embed(
            title="✅ Member Unbanned", color=CATEGORY_COLORS["modactions"], timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="User", value=f"{user.mention} ({user})", inline=False)
        embed.add_field(name="Moderator", value="Unknown (bot lacks View Audit Log)", inline=False)
        await self._log(guild, "modactions", embed)

    # --- Audit-log mod-action attribution -------------------------------------

    @staticmethod
    def _audit_target_label(entry: discord.AuditLogEntry) -> str:
        target = entry.target
        if target is None:
            return "Unknown"
        if hasattr(target, "mention"):
            return f"{target.mention} ({target})"
        if hasattr(target, "id"):
            return f"ID `{target.id}`"
        return str(target)

    def _add_actor_fields(
        self, embed: discord.Embed, entry: discord.AuditLogEntry, *, include_reason: bool = True
    ) -> None:
        """Target + Moderator fields shared by every audit-log modaction embed.
        Also adds the conditional Reason field when it directly follows with no
        category-specific fields in between (the default); pass
        include_reason=False and add Reason manually afterward when other
        fields (Until, Roles Added/Removed, ...) need to come first."""
        embed.add_field(name="Target", value=self._audit_target_label(entry), inline=False)
        moderator = entry.user
        embed.add_field(
            name="Moderator", value=f"{moderator.mention} ({moderator})" if moderator else "Unknown",
            inline=False,
        )
        if include_reason and entry.reason:
            embed.add_field(name="Reason", value=entry.reason[:1024], inline=False)

    async def _log_audit_action(self, guild: discord.Guild, entry: discord.AuditLogEntry, title: str) -> None:
        embed = discord.Embed(title=title, color=CATEGORY_COLORS["modactions"], timestamp=discord.utils.utcnow())
        self._add_actor_fields(embed, entry)
        await self._log(guild, "modactions", embed)

    async def _handle_member_update_audit(self, guild: discord.Guild, entry: discord.AuditLogEntry) -> None:
        """member_update audit entries cover several unrelated profile changes
        (nickname, timeout, ...); only the timeout transition is attributable
        mod-action noise worth a modactions embed here."""
        before = getattr(entry.changes, "before", None)
        after = getattr(entry.changes, "after", None)
        if before is None or after is None:
            return
        before_until = getattr(before, "communication_disabled_until", None)
        after_until = getattr(after, "communication_disabled_until", None)
        if before_until == after_until:
            return

        now = discord.utils.utcnow()
        applied = after_until is not None and after_until > now
        title = "🔇 Member Timed Out" if applied else "🔊 Timeout Removed"
        embed = discord.Embed(title=title, color=CATEGORY_COLORS["modactions"], timestamp=now)
        self._add_actor_fields(embed, entry, include_reason=False)
        if applied:
            embed.add_field(name="Until", value=discord.utils.format_dt(after_until, style="R"), inline=False)
        if entry.reason:
            embed.add_field(name="Reason", value=entry.reason[:1024], inline=False)
        await self._log(guild, "modactions", embed)

    async def _handle_role_update_audit(self, guild: discord.Guild, entry: discord.AuditLogEntry) -> None:
        """member_role_update audit entries store the roles that were removed
        under changes.before.roles and the roles that were added under
        changes.after.roles — not full before/after role sets."""
        before = getattr(entry.changes, "before", None)
        after = getattr(entry.changes, "after", None)
        added = getattr(after, "roles", None) or []
        removed = getattr(before, "roles", None) or []
        if not added and not removed:
            return
        embed = discord.Embed(
            title="🔧 Member Roles Updated (by moderator)", color=CATEGORY_COLORS["modactions"],
            timestamp=discord.utils.utcnow(),
        )
        self._add_actor_fields(embed, entry, include_reason=False)
        if added:
            embed.add_field(name="Roles Added", value=", ".join(f"<@&{r.id}>" for r in added)[:1024], inline=False)
        if removed:
            embed.add_field(
                name="Roles Removed", value=", ".join(f"<@&{r.id}>" for r in removed)[:1024], inline=False
            )
        if entry.reason:
            embed.add_field(name="Reason", value=entry.reason[:1024], inline=False)
        await self._log(guild, "modactions", embed)

    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry) -> None:
        """The sole, always-attributed source for ban/unban/kick/timeout/
        role-grant modactions embeds — requires the bot to hold View Audit
        Log; if it doesn't, Discord simply never emits this event for the
        guild and on_member_ban/on_member_unban's fallback picks up the
        ban/unban half of that gap (kick and timeout have no dedicated
        gateway event to fall back to, so they go unlogged without the
        permission — a known limitation, documented in the README)."""
        guild = entry.guild
        if guild is None or not self._should_log(guild, "modactions"):
            return

        action = entry.action
        if action == discord.AuditLogAction.kick:
            await self._log_audit_action(guild, entry, "👢 Member Kicked")
        elif action == discord.AuditLogAction.ban:
            await self._log_audit_action(guild, entry, "🔨 Member Banned")
        elif action == discord.AuditLogAction.unban:
            await self._log_audit_action(guild, entry, "✅ Member Unbanned")
        elif action == discord.AuditLogAction.member_update:
            await self._handle_member_update_audit(guild, entry)
        elif action == discord.AuditLogAction.member_role_update:
            await self._handle_role_update_audit(guild, entry)

    # --- Guild structure listeners --------------------------------------------

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role) -> None:
        guild = role.guild
        if not self._should_log(guild, "roles"):
            return
        embed = discord.Embed(
            title="✨ Role Created", color=CATEGORY_COLORS["roles"], timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="Role", value=f"{role.mention} (`{role.name}`)", inline=False)
        await self._log(guild, "roles", embed)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        guild = role.guild
        if not self._should_log(guild, "roles"):
            return
        embed = discord.Embed(
            title="🗑️ Role Deleted", color=CATEGORY_COLORS["roles"], timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="Role", value=f"`{role.name}` (`{role.id}`)", inline=False)
        await self._log(guild, "roles", embed)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        guild = after.guild
        if not self._should_log(guild, "roles"):
            return
        changes = []
        if before.name != after.name:
            changes.append(f"Name: `{before.name}` → `{after.name}`")
        if before.permissions != after.permissions:
            changes.append("Permissions changed")
        if before.colour != after.colour:
            changes.append(f"Color: `{before.colour}` → `{after.colour}`")
        if before.hoist != after.hoist:
            changes.append(f"Hoisted: {before.hoist} → {after.hoist}")
        if before.mentionable != after.mentionable:
            changes.append(f"Mentionable: {before.mentionable} → {after.mentionable}")
        if not changes:
            return
        embed = discord.Embed(
            title="🔧 Role Updated", color=CATEGORY_COLORS["roles"], timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="Role", value=after.mention, inline=False)
        embed.add_field(name="Changes", value="\n".join(changes)[:1024], inline=False)
        await self._log(guild, "roles", embed)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel) -> None:
        guild = channel.guild
        if not self._should_log(guild, "server"):
            return
        embed = discord.Embed(
            title="➕ Channel Created", color=CATEGORY_COLORS["server"], timestamp=discord.utils.utcnow()
        )
        mention = channel.mention if hasattr(channel, "mention") else f"`#{channel.name}`"
        embed.add_field(name="Channel", value=f"{mention} (`{channel.type}`)", inline=False)
        await self._log(guild, "server", embed)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel) -> None:
        guild = channel.guild
        if not self._should_log(guild, "server"):
            return
        embed = discord.Embed(
            title="➖ Channel Deleted", color=CATEGORY_COLORS["server"], timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="Channel", value=f"`#{channel.name}` (`{channel.type}`)", inline=False)
        await self._log(guild, "server", embed)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before, after) -> None:
        guild = after.guild
        if not self._should_log(guild, "server"):
            return
        changes = []
        if before.name != after.name:
            changes.append(f"Name: `{before.name}` → `{after.name}`")
        if getattr(before, "topic", None) != getattr(after, "topic", None):
            changes.append("Topic changed")
        if getattr(before, "category", None) != getattr(after, "category", None):
            before_cat = before.category.name if before.category else "None"
            after_cat = after.category.name if after.category else "None"
            changes.append(f"Category: `{before_cat}` → `{after_cat}`")
        if before.overwrites != after.overwrites:
            changes.append("Permission overwrites changed")
        if not changes:
            return
        embed = discord.Embed(
            title="🔧 Channel Updated", color=CATEGORY_COLORS["server"], timestamp=discord.utils.utcnow()
        )
        mention = after.mention if hasattr(after, "mention") else f"`#{after.name}`"
        embed.add_field(name="Channel", value=mention, inline=False)
        embed.add_field(name="Changes", value="\n".join(changes)[:1024], inline=False)
        await self._log(guild, "server", embed)

    @commands.Cog.listener()
    async def on_guild_emojis_update(
        self, guild: discord.Guild, before: "tuple[discord.Emoji, ...]", after: "tuple[discord.Emoji, ...]"
    ) -> None:
        if not self._should_log(guild, "server"):
            return
        before_ids = {e.id for e in before}
        after_ids = {e.id for e in after}
        added = [e for e in after if e.id not in before_ids]
        removed = [e for e in before if e.id not in after_ids]
        if not added and not removed:
            return
        embed = discord.Embed(
            title="😀 Emojis Updated", color=CATEGORY_COLORS["server"], timestamp=discord.utils.utcnow()
        )
        if added:
            embed.add_field(name="Added", value=" ".join(str(e) for e in added)[:1024], inline=False)
        if removed:
            embed.add_field(
                name="Removed", value=", ".join(f"`{e.name}`" for e in removed)[:1024], inline=False
            )
        await self._log(guild, "server", embed)

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild) -> None:
        if not self._should_log(after, "server"):
            return
        changes = []
        if before.name != after.name:
            changes.append(f"Name: `{before.name}` → `{after.name}`")
        if before.icon != after.icon:
            changes.append("Icon changed")
        if before.owner_id != after.owner_id:
            changes.append(f"Owner changed: <@{before.owner_id}> → <@{after.owner_id}>")
        if before.verification_level != after.verification_level:
            changes.append(
                f"Verification level: `{before.verification_level}` → `{after.verification_level}`"
            )
        if not changes:
            return
        embed = discord.Embed(
            title="🔧 Server Updated", color=CATEGORY_COLORS["server"], timestamp=discord.utils.utcnow()
        )
        embed.add_field(name="Changes", value="\n".join(changes)[:1024], inline=False)
        await self._log(after, "server", embed)

    # --- Voice listener --------------------------------------------------

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        # Join/leave/move between channels only — ignore mute/deafen/stream/
        # self-mute-only changes, which leave the channel unchanged.
        if before.channel == after.channel:
            return
        guild = member.guild
        if not self._should_log(guild, "voice"):
            return
        if before.channel is None and after.channel is not None:
            title, description = "🔊 Voice Join", f"{member.mention} joined {after.channel.mention}"
        elif before.channel is not None and after.channel is None:
            title, description = "🔈 Voice Leave", f"{member.mention} left {before.channel.mention}"
        else:
            title, description = (
                "🔀 Voice Move",
                f"{member.mention} moved {before.channel.mention} → {after.channel.mention}",
            )
        embed = discord.Embed(
            title=title, description=description, color=CATEGORY_COLORS["voice"],
            timestamp=discord.utils.utcnow(),
        )
        await self._log(guild, "voice", embed)

    # --- Commands --------------------------------------------------------

    @staticmethod
    async def _reply(ctx, *args, **kwargs):
        """ctx.reply, but ephemeral (visible only to the invoker) when the
        command was invoked via / rather than the text prefix."""
        return await reply_ephemeral_aware(ctx, *args, **kwargs)

    def _status_embed(self, guild: discord.Guild, guild_conf: dict) -> discord.Embed:
        channel = guild.get_channel(guild_conf["log_channel_id"]) if guild_conf["log_channel_id"] else None
        disabled = set(guild_conf["disabled_categories"])
        category_lines = "\n".join(f"{'🔴' if c in disabled else '🟢'} {c}" for c in CATEGORIES)
        embed = discord.Embed(title="🔮 Palantir Status", color=discord.Color.dark_purple())
        embed.add_field(name="Log channel", value=channel.mention if channel else "Not set", inline=False)
        embed.add_field(
            name="Archive attachments", value="On" if guild_conf["archive_attachments"] else "Off",
            inline=False,
        )
        embed.add_field(name="Categories", value=category_lines, inline=False)
        return embed

    async def cog_check(self, ctx):
        if ctx.guild is None or await self.bot.is_owner(ctx.author):
            return True
        return cog_enabled(self.bot, ctx.guild.id, "palantir")

    async def cog_command_error(self, ctx, error):
        if isinstance(error, (commands.MissingPermissions, commands.CheckAnyFailure)):
            await self._reply(ctx, "You don't have permission to do that.")
        elif isinstance(error, commands.ChannelNotFound):
            await self._reply(ctx, "I couldn't find that channel.")
        elif isinstance(error, (commands.BadArgument, commands.MissingRequiredArgument)):
            await self._reply(ctx, str(error) or "Invalid or missing argument.")
        elif isinstance(error, commands.CheckFailure):
            return
        else:
            raise error

    @commands.hybrid_group(
        invoke_without_command=True, fallback="status",
        description="Show the current palantir surveillance-log configuration.",
    )
    @has_permissions_or_owner(manage_guild=True)
    @commands.guild_only()
    async def palantir(self, ctx):
        """Show the current palantir configuration and status."""
        guild_conf = self._guild_conf(ctx.guild.id)
        await self._reply(ctx, embed=self._status_embed(ctx.guild, guild_conf))

    # with_app_command=False: the group's fallback="status" above already
    # registers the slash-side `/palantir status`; this only adds the
    # prefix-side `.palantir status` (typing the subcommand name explicitly),
    # since a second app_command named "status" under the same group would
    # collide with the fallback's.
    @palantir.command(name="status", with_app_command=False)
    @has_permissions_or_owner(manage_guild=True)
    @commands.guild_only()
    async def palantir_status(self, ctx):
        """Show the current palantir configuration and status."""
        guild_conf = self._guild_conf(ctx.guild.id)
        await self._reply(ctx, embed=self._status_embed(ctx.guild, guild_conf))

    @palantir.command(name="setchannel", description="Set the channel palantir logs are posted to.")
    @has_permissions_or_owner(manage_guild=True)
    @commands.guild_only()
    async def palantir_setchannel(self, ctx, channel: discord.TextChannel):
        """Set the channel palantir surveillance logs are posted to."""
        guild_conf = self._guild_conf(ctx.guild.id)
        guild_conf["log_channel_id"] = channel.id
        self._save_config()
        await self._reply(ctx, f"🔮 Palantir logs will be sent to {channel.mention}.")

    @palantir.command(name="disable", description="Stop palantir logging in this server.")
    @has_permissions_or_owner(manage_guild=True)
    @commands.guild_only()
    async def palantir_disable(self, ctx):
        """Stop palantir logging (clears the configured channel)."""
        guild_conf = self._guild_conf(ctx.guild.id)
        guild_conf["log_channel_id"] = None
        self._save_config()
        await self._reply(ctx, "🔮 Palantir logging disabled.")

    @palantir.command(name="mute", description="Stop logging a category of events.")
    @has_permissions_or_owner(manage_guild=True)
    @commands.guild_only()
    async def palantir_mute(self, ctx, category: str):
        """Stop logging a category of events (members, messages, roles, voice,
        modactions, invites, server)."""
        category = category.lower()
        if category not in CATEGORIES:
            await self._reply(ctx, f"Unknown category. Choose from: {', '.join(CATEGORIES)}")
            return
        guild_conf = self._guild_conf(ctx.guild.id)
        if category not in guild_conf["disabled_categories"]:
            guild_conf["disabled_categories"].append(category)
            self._save_config()
        await self._reply(ctx, f"🔇 Muted `{category}` logging.")

    @palantir.command(name="unmute", description="Resume logging a category of events.")
    @has_permissions_or_owner(manage_guild=True)
    @commands.guild_only()
    async def palantir_unmute(self, ctx, category: str):
        """Resume logging a category of events."""
        category = category.lower()
        if category not in CATEGORIES:
            await self._reply(ctx, f"Unknown category. Choose from: {', '.join(CATEGORIES)}")
            return
        guild_conf = self._guild_conf(ctx.guild.id)
        if category in guild_conf["disabled_categories"]:
            guild_conf["disabled_categories"].remove(category)
            self._save_config()
        await self._reply(ctx, f"🔊 Unmuted `{category}` logging.")

    @palantir.command(name="archive", description="Toggle archiving message attachments (on/off).")
    @has_permissions_or_owner(manage_guild=True)
    @commands.guild_only()
    async def palantir_archive(self, ctx, state: str):
        """Toggle attachment archiving on or off for this server."""
        state = state.lower()
        if state not in ("on", "off"):
            await self._reply(ctx, "Use `on` or `off`.")
            return
        guild_conf = self._guild_conf(ctx.guild.id)
        guild_conf["archive_attachments"] = state == "on"
        self._save_config()
        await self._reply(ctx, f"📎 Attachment archiving turned **{state}**.")


async def setup(bot):
    await bot.add_cog(Palantir(bot))
