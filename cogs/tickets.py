import io
import logging
import time
from datetime import datetime, timezone

import discord
from discord.ext import commands

from .management import cog_enabled, common_error_reply, has_permissions_or_owner, reply_ephemeral_aware
from .storage import backfill_defaults, data_path, load_json, save_json_atomic

logger = logging.getLogger(__name__)

TICKETS_FILE = data_path("tickets.json")

# Not imported from palantir (reload safety — `.cog reload palantir` would
# otherwise leave this module holding a stale reference to the old module's
# CATEGORY_COLORS dict).
LOG_COLOR = discord.Color.gold()

MAX_OPEN_PER_USER = 1  # per guild — keeps plain DM follow-ups unambiguous; a
# user can still have several open tickets across different guilds, which is
# what the DM listener's disambiguation step is for.
MAX_TEXT_LENGTH = 1500
MAX_MESSAGES_PER_TICKET = 200
DM_COOLDOWN_SECONDS = 5
CLOSED_RETENTION_DAYS = 30
TRANSCRIPT_LINE_LIMIT = 300
TRANSCRIPT_DESC_LIMIT = 4000
ATTACHMENT_MAX_BYTES = 8 * 1024 * 1024  # Discord's default per-file upload cap
# (mirrors palantir's ARCHIVE_MAX_BYTES) — larger attachments are listed by
# name only, never re-uploaded.


def _default_guild_config() -> dict:
    # Factory, not a module-level constant: storage.backfill_defaults assigns
    # missing keys by reference, so a shared dict here would let every guild's
    # "tickets" mapping alias the same object.
    return {"channel_id": None, "tickets": {}}


async def palantir_log(bot, guild: discord.Guild, embed: discord.Embed) -> None:
    """Best-effort mirror of a ticket event to Palantir's "tickets" category.
    Looks the cog up by name (rather than importing the module) so a
    `.cog reload palantir` is picked up without reloading this cog too. This
    trail is mutable by `.palantir mute tickets` — the logger.info calls next
    to every call site are the actual audit trail of record."""
    palantir = bot.get_cog("Palantir")
    if palantir is None:
        return
    await palantir.log_event(guild, "tickets", embed)


async def _is_mod(ctx) -> bool:
    return ctx.guild is not None and (
        ctx.permissions.manage_messages or await ctx.bot.is_owner(ctx.author)
    )


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


class Tickets(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.data = load_json(TICKETS_FILE)
        # Global (not per-guild) counter: DM-side commands like `.ticket reply
        # <id> <text>` have no guild context to key an id off of, so one
        # globally unique counter resolves without asking which server. No
        # in-memory index of ticket id -> location is kept; _find_ticket scans
        # every guild's tickets dict — a handful of guilds with at most a few
        # dozen tickets each makes that cheap enough, and it avoids a second
        # structure that could drift from self.data.
        self.data.setdefault("next_id", 1)
        self.data.setdefault("guilds", {})
        self._dm_last: dict[int, float] = {}
        # In-memory only — must not write on load, so a fresh/missing file
        # doesn't create tickets.json until something actually changes.
        self._prune_closed()

    def _save(self):
        save_json_atomic(TICKETS_FILE, self.data)

    def _guild_conf(self, guild_id: int) -> dict:
        entry = self.data["guilds"].setdefault(str(guild_id), {})
        return backfill_defaults(entry, _default_guild_config())

    async def cog_check(self, ctx):
        return ctx.guild is None or cog_enabled(self.bot, ctx.guild.id, "tickets")

    @staticmethod
    async def _reply(ctx, *args, **kwargs):
        """ctx.reply, but ephemeral (visible only to the invoker) when the
        command was invoked via / rather than the text prefix."""
        return await reply_ephemeral_aware(ctx, *args, **kwargs)

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.ChannelNotFound):
            await self._reply(ctx, "I couldn't find that channel.")
        elif isinstance(error, commands.NoPrivateMessage):
            await self._reply(ctx, "That command only works in a server.")
        elif isinstance(error, commands.CheckAnyFailure):
            await self._reply(ctx, "You don't have permission to do that.")
        elif await common_error_reply(ctx, error, reply=lambda *a, **k: self._reply(ctx, *a, **k)):
            return
        else:
            raise error

    # --- Lookup helpers ------------------------------------------------------

    def _find_ticket(self, ticket_id: int):
        """Scan every guild for `ticket_id` — no in-memory index (see __init__).
        Returns (guild_conf, ticket) or None."""
        key = str(ticket_id)
        for guild_conf in self.data["guilds"].values():
            ticket = guild_conf["tickets"].get(key)
            if ticket is not None:
                return guild_conf, ticket
        return None

    def _open_tickets_for_user(self, user_id: int) -> list:
        return [
            ticket
            for guild_conf in self.data["guilds"].values()
            for ticket in guild_conf["tickets"].values()
            if ticket["user_id"] == user_id and ticket["status"] == "open"
        ]

    def _prune_closed(self) -> None:
        """Drop closed tickets older than CLOSED_RETENTION_DAYS from memory.
        In-memory only (no save here — see __init__ and _close_ticket, which
        persists the result on its own next save)."""
        cutoff = time.time() - CLOSED_RETENTION_DAYS * 86400
        for guild_conf in self.data["guilds"].values():
            tickets = guild_conf["tickets"]
            for tid in list(tickets):
                ticket = tickets[tid]
                if ticket["status"] != "closed" or not ticket.get("closed_at"):
                    continue
                try:
                    closed_ts = _parse_iso(ticket["closed_at"])
                except ValueError:
                    continue
                if closed_ts < cutoff:
                    del tickets[tid]

    def _ticket_channel(self, guild, guild_conf):
        channel_id = guild_conf.get("channel_id")
        if guild is None or not channel_id:
            return None
        return guild.get_channel(channel_id)

    def _jump_url(self, ticket: dict, guild_conf: dict) -> str | None:
        message_id = ticket.get("channel_message_id")
        channel_id = guild_conf.get("channel_id")
        if not message_id or not channel_id:
            return None
        return f"https://discord.com/channels/{ticket['guild_id']}/{channel_id}/{message_id}"

    def _in_ticket_channel_or_slash(self, ctx, guild_conf: dict) -> bool:
        """Mod subcommands via the `.` prefix are only accepted inside the
        configured ticket channel — anywhere else the mod's own invoking
        message (with their name on it) would sit in a channel the user can
        see. Slash invocations are always fine: the reply is ephemeral."""
        if ctx.interaction is not None:
            return True
        return ctx.channel.id == guild_conf.get("channel_id")

    @staticmethod
    def _prefix_hint(subcommand: str, guild_conf: dict) -> str:
        channel_id = guild_conf.get("channel_id")
        where = f" or run this in <#{channel_id}>" if channel_id else ""
        return f"Use `/ticket {subcommand}` (private){where}."

    # --- Delivery helpers (never raise) --------------------------------------

    async def _dm_user(self, user_id: int, embed: discord.Embed) -> bool:
        user = self.bot.get_user(user_id)
        if user is None:
            try:
                user = await self.bot.fetch_user(user_id)
            except discord.HTTPException:
                return False
        try:
            await user.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            return True
        except (discord.Forbidden, discord.HTTPException):
            return False

    async def _post_to_channel(self, guild, guild_conf, embed, files=None):
        channel = self._ticket_channel(guild, guild_conf)
        if channel is None:
            return None
        try:
            return await channel.send(
                embed=embed, files=files or None, allowed_mentions=discord.AllowedMentions.none()
            )
        except (discord.Forbidden, discord.HTTPException):
            return None

    def _append(self, ticket: dict, sender: str, author_id: int, content: str, **extra) -> bool:
        """Append a transcript entry, enforcing MAX_MESSAGES_PER_TICKET. Callers
        that need to react to a full ticket check length themselves first (see
        _relay_user_message); this is the defensive backstop."""
        if len(ticket["messages"]) >= MAX_MESSAGES_PER_TICKET:
            return False
        entry = {"at": _iso_now(), "from": sender, "author_id": author_id, "content": content}
        entry.update(extra)
        ticket["messages"].append(entry)
        return True

    async def _collect_files(self, attachments) -> tuple:
        """Re-upload attachments (never store CDN URLs — they expire after
        ~24h). Best-effort: a download failure is just noted in the transcript,
        never raised."""
        files: list[discord.File] = []
        notes: list[str] = []
        for attachment in attachments:
            if attachment.size > ATTACHMENT_MAX_BYTES:
                notes.append(f"[attachment: {attachment.filename} — too large to re-upload]")
                continue
            try:
                files.append(await attachment.to_file())
                notes.append(f"[attachment: {attachment.filename}]")
            except (discord.HTTPException, discord.NotFound):
                notes.append(f"[attachment: {attachment.filename} — failed to download]")
        return files, "\n".join(notes)

    # --- Embed builders (single place for the anonymity audit) --------------

    def _user_embed(self, ticket_id: int, title: str, description: str) -> discord.Embed:
        """User-facing DM embed. Must never carry a mod's name, mention,
        avatar, or author block — that's the whole point of routing replies
        through the bot."""
        embed = discord.Embed(title=title, description=description, color=LOG_COLOR, timestamp=discord.utils.utcnow())
        embed.set_footer(text=f"Ticket #{ticket_id} · reply in this DM to continue")
        return embed

    def _channel_embed(self, title: str, description: str, actor, **fields) -> discord.Embed:
        """Mod-only ticket-channel embed — names the actor (user or mod)."""
        embed = discord.Embed(title=title, description=description, color=LOG_COLOR, timestamp=discord.utils.utcnow())
        embed.set_author(name=str(actor), icon_url=actor.display_avatar.url)
        for name, value in fields.items():
            embed.add_field(name=name, value=value, inline=False)
        return embed

    def _log_embed(self, title: str, ticket: dict, *, moderator=None, reason=None, description=None) -> discord.Embed:
        embed = discord.Embed(title=title, color=LOG_COLOR, timestamp=discord.utils.utcnow())
        if description:
            embed.description = description
        embed.add_field(name="Ticket", value=f"#{ticket['id']}", inline=False)
        embed.add_field(name="User", value=f"<@{ticket['user_id']}> ({ticket['user_id']})", inline=False)
        if moderator is not None:
            embed.add_field(name="Moderator", value=f"<@{moderator.id}> ({moderator})", inline=False)
        if reason:
            embed.add_field(name="Reason", value=reason, inline=False)
        return embed

    # --- Core shared operations -----------------------------------------------

    async def _relay_user_message(self, ticket, guild_conf, guild, author, content, attachments) -> str:
        """Append a user follow-up to `ticket` and echo it to the ticket
        channel. Returns "full" (message cap hit — nothing was sent),
        "gone" (appended, but the ticket channel no longer exists), or "ok"."""
        if len(ticket["messages"]) >= MAX_MESSAGES_PER_TICKET:
            return "full"

        files, note = await self._collect_files(attachments)
        full_content = f"{content}\n{note}" if note else content
        self._append(ticket, "user", author.id, full_content)
        self._save()

        channel = self._ticket_channel(guild, guild_conf)
        if channel is None:
            return "gone"

        embed = self._channel_embed(
            f"📨 Follow-up on ticket #{ticket['id']}", content or "*(no text — see attachment)*", author,
        )
        jump = self._jump_url(ticket, guild_conf)
        if jump:
            embed.add_field(name="Jump to ticket", value=jump, inline=False)
        await self._post_to_channel(guild, guild_conf, embed, files=files or None)
        await palantir_log(self.bot, guild, self._log_embed("🎫 Ticket Follow-up", ticket, description=content))
        return "ok"

    async def _mod_reply(self, ctx, ticket: dict, guild, text: str) -> None:
        delivered = await self._dm_user(
            ticket["user_id"], self._user_embed(ticket["id"], "💬 Reply from the mod team", text)
        )
        self._append(ticket, "mod", ctx.author.id, text, delivered=delivered)
        guild_conf = self._guild_conf(ticket["guild_id"])
        self._save()

        if guild is not None:
            fields = {"To": f"<@{ticket['user_id']}>"}
            jump = self._jump_url(ticket, guild_conf)
            if jump:
                fields["Jump to ticket"] = jump
            description = text if delivered else f"{text}\n\n⚠️ not delivered (DMs closed)"
            embed = self._channel_embed(f"💬 Reply sent on ticket #{ticket['id']}", description, ctx.author, **fields)
            await self._post_to_channel(guild, guild_conf, embed)
            await palantir_log(
                self.bot, guild,
                self._log_embed("💬 Ticket Reply", ticket, moderator=ctx.author, description=text),
            )

        # Unconditional — the actual audit trail of record, independent of
        # whether Palantir is configured/muted for this guild.
        logger.info(
            "ticket reply | guild=%s ticket=%s moderator=%s user=%s delivered=%s text=%r",
            ticket["guild_id"], ticket["id"], ctx.author, ticket["user_id"], delivered, text,
        )

        if delivered:
            await self._reply(ctx, f"Sent to ticket #{ticket['id']}.")
        else:
            await self._reply(
                ctx, f"Saved to ticket #{ticket['id']}, but I couldn't DM the user (DMs closed or bot blocked)."
            )

    async def _close_ticket(self, ticket: dict, guild_conf: dict, guild, *, closer, by_mod: bool, reason) -> None:
        ticket["status"] = "closed"
        ticket["closed_at"] = _iso_now()
        ticket["closed_by"] = closer.id if by_mod else "user"
        ticket["close_reason"] = reason
        self._save()
        self._prune_closed()

        if by_mod:
            dm_description = "This ticket was closed by the mod team."
        else:
            dm_description = "You closed this ticket."
        if reason:
            dm_description += f"\nReason: {reason}"
        delivered = await self._dm_user(
            ticket["user_id"], self._user_embed(ticket["id"], f"🔒 Ticket #{ticket['id']} closed", dm_description)
        )

        if guild is not None:
            fields = {"Reason": reason} if reason else {}
            actor_desc = closer.mention if by_mod else "the ticket owner"
            embed = self._channel_embed(
                f"🔒 Ticket #{ticket['id']} closed", f"Closed by {actor_desc}.", closer, **fields,
            )
            await self._post_to_channel(guild, guild_conf, embed)
            await palantir_log(
                self.bot, guild,
                self._log_embed(
                    "🔒 Ticket Closed", ticket, moderator=closer if by_mod else None, reason=reason,
                ),
            )

        logger.info(
            "ticket close | guild=%s ticket=%s closed_by=%s by_mod=%s reason=%r delivered=%s",
            ticket["guild_id"], ticket["id"], ticket["closed_by"], by_mod, reason, delivered,
        )

    # --- Commands --------------------------------------------------------------

    @commands.hybrid_group(
        invoke_without_command=True, fallback="help", case_insensitive=True,
        description="Get help from the mod team, or manage tickets you have access to.",
    )
    async def ticket(self, ctx):
        """Show ticket usage."""
        await self._reply(
            ctx,
            "Use `/ticket open <text>` to start a ticket — mods will reply here by DM. "
            "Already have a ticket open? Just reply to my DM.",
        )

    @ticket.command(name="open", description="Open a support ticket — mods will reply by DM.")
    @commands.guild_only()
    async def ticket_open(self, ctx, *, text: str):
        """Open a support ticket."""
        guild_conf = self._guild_conf(ctx.guild.id)
        if not guild_conf["channel_id"] or self._ticket_channel(ctx.guild, guild_conf) is None:
            await self._reply(
                ctx, "Tickets aren't set up on this server — ask a mod to run `/ticket channel set #channel`."
            )
            return
        if len(text) > MAX_TEXT_LENGTH:
            await self._reply(ctx, f"Ticket text can't exceed {MAX_TEXT_LENGTH} characters.")
            return

        existing = [
            t for t in self._open_tickets_for_user(ctx.author.id) if t["guild_id"] == ctx.guild.id
        ]
        if len(existing) >= MAX_OPEN_PER_USER:
            await self._reply(
                ctx, f"You already have open ticket #{existing[0]['id']} — reply to me in DM or close it first."
            )
            return

        # Bump the counter before the first await — no other coroutine can run
        # between the read and the increment, so this can't race.
        ticket_id = self.data["next_id"]
        self.data["next_id"] += 1

        attachments = ctx.message.attachments if ctx.interaction is None else []
        files, note = await self._collect_files(attachments)
        stored_content = f"{text}\n{note}" if note else text

        ticket = {
            "id": ticket_id, "guild_id": ctx.guild.id, "user_id": ctx.author.id,
            "status": "open", "created_at": _iso_now(), "closed_at": None, "closed_by": None,
            "close_reason": None, "channel_message_id": None,
            "messages": [{"at": _iso_now(), "from": "user", "author_id": ctx.author.id, "content": stored_content}],
        }
        guild_conf["tickets"][str(ticket_id)] = ticket

        embed = self._channel_embed(
            f"🎫 Ticket #{ticket_id} opened", text, ctx.author,
            User=f"{ctx.author.mention} ({ctx.author.id})",
        )
        embed.set_footer(text=f"/ticket reply {ticket_id} <text> · /ticket close {ticket_id}")
        message = await self._post_to_channel(ctx.guild, guild_conf, embed, files=files or None)
        if message is None:
            # Channel exists but the post failed (permissions, outage). A ticket
            # nobody can see is worse than none — drop it rather than persist it.
            del guild_conf["tickets"][str(ticket_id)]
            await self._reply(
                ctx, "I couldn't post your ticket to the ticket channel — ask a mod to check my permissions there."
            )
            return
        ticket["channel_message_id"] = message.id
        self._save()

        dm_ok = await self._dm_user(
            ctx.author.id,
            self._user_embed(
                ticket_id, f"🎫 Ticket #{ticket_id} opened",
                f"The mod team of **{ctx.guild.name}** will answer here; just reply in this DM.",
            ),
        )
        await palantir_log(self.bot, ctx.guild, self._log_embed("🎫 Ticket Opened", ticket, description=text))
        logger.info(
            "ticket open | guild=%s ticket=%s user=%s delivered=%s", ctx.guild.id, ticket_id, ctx.author, dm_ok
        )

        reply = f"🎫 Ticket **#{ticket_id}** opened — mods will answer you by DM."
        if not dm_ok:
            reply += (
                "\nI couldn't DM you: enable *Allow direct messages from server members* "
                "for this server or you won't receive answers."
            )
        if ctx.interaction is None:
            reply += "\nTip: `/ticket open` keeps your request private."
        await self._reply(ctx, reply)

    @ticket.command(name="reply", description="Reply to a ticket (mods in a server; DM for your own ticket).")
    async def ticket_reply(self, ctx, ticket_id: int, *, text: str):
        """Reply to a ticket."""
        if len(text) > MAX_TEXT_LENGTH:
            await self._reply(ctx, f"Ticket text can't exceed {MAX_TEXT_LENGTH} characters.")
            return

        found = self._find_ticket(ticket_id)

        if ctx.guild is not None:
            if found is None or found[1]["guild_id"] != ctx.guild.id:
                await self._reply(ctx, f"No ticket #{ticket_id} in this server.")
                return
            guild_conf, found_ticket = found
            is_mod = await _is_mod(ctx)
            if found_ticket["user_id"] == ctx.author.id and not is_mod:
                await self._reply(ctx, "Reply to me in DM to add to your ticket.")
                return
            if not is_mod:
                await self._reply(ctx, "You don't have permission to do that.")
                return
            if not self._in_ticket_channel_or_slash(ctx, guild_conf):
                await self._reply(ctx, self._prefix_hint("reply", guild_conf))
                return
            if found_ticket["status"] == "closed":
                await self._reply(ctx, f"Ticket #{ticket_id} is closed.")
                return
            if len(found_ticket["messages"]) >= MAX_MESSAGES_PER_TICKET:
                await self._reply(ctx, "This ticket is full — ask a mod to close it and open a new one.")
                return
            await self._mod_reply(ctx, found_ticket, ctx.guild, text)
            return

        # DM path: no guild context, so the caller can only be replying to
        # their own ticket.
        if found is None or found[1]["user_id"] != ctx.author.id:
            await self._reply(ctx, f"You don't have a ticket #{ticket_id}.")
            return
        guild_conf, found_ticket = found
        if found_ticket["status"] == "closed":
            await self._reply(ctx, f"Ticket #{ticket_id} is closed.")
            return
        if not cog_enabled(self.bot, found_ticket["guild_id"], "tickets"):
            await self._reply(ctx, "Tickets are currently disabled on that server.")
            return
        guild = self.bot.get_guild(found_ticket["guild_id"])
        if guild is None:
            await self._reply(ctx, "I'm no longer in that server.")
            return

        result = await self._relay_user_message(
            found_ticket, guild_conf, guild, ctx.author, text, ctx.message.attachments
        )
        if result == "full":
            await self._reply(ctx, "This ticket is full — ask a mod to close it and open a new one.")
        elif result == "gone":
            await self._reply(ctx, f"Couldn't deliver — the ticket channel on {guild.name} is gone; ask a mod.")
        else:
            await self._reply(ctx, f"📨 Forwarded to ticket #{ticket_id}.")

    @ticket.command(name="close", description="Close a ticket.")
    async def ticket_close(self, ctx, ticket_id: int, *, reason: str | None = None):
        """Close a ticket."""
        found = self._find_ticket(ticket_id)
        if found is None:
            # Same wording as the real permission failure, so probing ids
            # doesn't reveal which tickets exist in other servers.
            await self._reply(ctx, "You don't have permission to do that.")
            return
        guild_conf, found_ticket = found

        is_owner = found_ticket["user_id"] == ctx.author.id
        is_mod = (
            ctx.guild is not None
            and ctx.guild.id == found_ticket["guild_id"]
            and await _is_mod(ctx)
        )
        if not (is_owner or is_mod):
            await self._reply(ctx, "You don't have permission to do that.")
            return
        if is_mod and not is_owner and not self._in_ticket_channel_or_slash(ctx, guild_conf):
            await self._reply(ctx, self._prefix_hint("close", guild_conf))
            return
        if found_ticket["status"] == "closed":
            await self._reply(ctx, f"Ticket #{ticket_id} is already closed.")
            return

        guild = self.bot.get_guild(found_ticket["guild_id"])  # may be None — _close_ticket copes
        await self._close_ticket(
            found_ticket, guild_conf, guild, closer=ctx.author, by_mod=is_mod and not is_owner, reason=reason,
        )
        await self._reply(ctx, f"🔒 Ticket #{ticket_id} closed.")

    @ticket.command(name="list", description="List open tickets in this server.")
    @has_permissions_or_owner(manage_messages=True)
    @commands.guild_only()
    async def ticket_list(self, ctx):
        """List open tickets in this server."""
        guild_conf = self._guild_conf(ctx.guild.id)
        if not self._in_ticket_channel_or_slash(ctx, guild_conf):
            await self._reply(ctx, self._prefix_hint("list", guild_conf))
            return

        open_tickets = sorted(
            (t for t in guild_conf["tickets"].values() if t["status"] == "open"),
            key=lambda t: t["created_at"],
        )
        if not open_tickets:
            await self._reply(ctx, "No open tickets.")
            return

        lines = [
            f"**#{t['id']}** — <@{t['user_id']}> — <t:{int(_parse_iso(t['created_at']))}:R> — "
            f"{t['messages'][0]['content'][:80]}"
            for t in open_tickets
        ]
        embed = discord.Embed(title="Open Tickets", description="\n".join(lines), color=LOG_COLOR)
        await self._reply(ctx, embed=embed)

    @ticket.command(name="show", description="Show a ticket's full transcript.")
    @has_permissions_or_owner(manage_messages=True)
    @commands.guild_only()
    async def ticket_show(self, ctx, ticket_id: int):
        """Show a ticket's transcript."""
        guild_conf = self._guild_conf(ctx.guild.id)
        if not self._in_ticket_channel_or_slash(ctx, guild_conf):
            await self._reply(ctx, self._prefix_hint("show", guild_conf))
            return

        found_ticket = guild_conf["tickets"].get(str(ticket_id))
        if found_ticket is None:
            await self._reply(ctx, f"No ticket #{ticket_id} in this server.")
            return

        header = (
            f"User: <@{found_ticket['user_id']}>\n"
            f"Status: {found_ticket['status']}\n"
            f"Opened: <t:{int(_parse_iso(found_ticket['created_at']))}:f>"
        )
        if found_ticket["status"] == "closed" and found_ticket.get("closed_at"):
            closed_by = found_ticket.get("closed_by")
            closed_by_text = "the ticket owner" if closed_by in (None, "user") else f"<@{closed_by}>"
            header += f"\nClosed: <t:{int(_parse_iso(found_ticket['closed_at']))}:f> by {closed_by_text}"
            if found_ticket.get("close_reason"):
                header += f"\nReason: {found_ticket['close_reason']}"

        def transcript_line(m: dict, *, limit: int | None) -> str:
            label = "User" if m["from"] == "user" else f"Mod (<@{m['author_id']}>)"
            content = m["content"]
            if limit is not None and len(content) > limit:
                content = content[:limit] + "…"
            line = f"<t:{int(_parse_iso(m['at']))}:t> **{label}**: {content}"
            if m.get("delivered") is False:
                line += " ⚠️ undelivered"
            return line

        display_lines = [transcript_line(m, limit=TRANSCRIPT_LINE_LIMIT) for m in found_ticket["messages"]]
        truncated_any_line = any(
            len(m["content"]) > TRANSCRIPT_LINE_LIMIT for m in found_ticket["messages"]
        )

        desc_lines = []
        total = 0
        desc_truncated = False
        for line in reversed(display_lines):
            total += len(line) + 1
            if total > TRANSCRIPT_DESC_LIMIT:
                desc_truncated = True
                break
            desc_lines.append(line)
        desc_lines.reverse()

        description = "\n".join(desc_lines) if desc_lines else "*(no messages yet)*"
        embed = discord.Embed(
            title=f"Ticket #{ticket_id}", description=f"{header}\n\n{description}", color=LOG_COLOR,
        )

        files = []
        if desc_truncated or truncated_any_line:
            full_text = "\n".join(transcript_line(m, limit=None) for m in found_ticket["messages"])
            files.append(discord.File(io.BytesIO(full_text.encode()), filename=f"ticket-{ticket_id}.txt"))

        await self._reply(ctx, embed=embed, files=files or None)

    @ticket.group(
        name="channel", invoke_without_command=True, fallback="show", case_insensitive=True,
        description="Show the current ticket-channel configuration.",
    )
    @has_permissions_or_owner(manage_guild=True)
    @commands.guild_only()
    async def ticket_channel(self, ctx):
        """Show the current ticket-channel configuration."""
        guild_conf = self._guild_conf(ctx.guild.id)
        channel = self._ticket_channel(ctx.guild, guild_conf)
        if channel is None:
            await self._reply(ctx, "No ticket channel is configured. Use `/ticket channel set #channel` to set one.")
        else:
            await self._reply(ctx, f"Tickets are currently sent to {channel.mention}.")

    @ticket_channel.command(name="set", description="Set the channel ticket activity is posted to.")
    @has_permissions_or_owner(manage_guild=True)
    @commands.guild_only()
    async def ticket_channel_set(self, ctx, channel: discord.TextChannel):
        """Set the channel ticket activity is posted to."""
        guild_conf = self._guild_conf(ctx.guild.id)
        guild_conf["channel_id"] = channel.id
        self._save()
        await self._reply(ctx, f"🎫 Tickets will be sent to {channel.mention}.")

    @ticket_channel.command(name="disable", description="Stop accepting new tickets on this server.")
    @has_permissions_or_owner(manage_guild=True)
    @commands.guild_only()
    async def ticket_channel_disable(self, ctx):
        """Stop accepting new tickets on this server."""
        guild_conf = self._guild_conf(ctx.guild.id)
        guild_conf["channel_id"] = None
        self._save()
        await self._reply(ctx, "🎫 Ticket channel disabled — new tickets will be rejected.")

    # --- DM listener -----------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is not None or message.author.bot:
            return
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return

        ctx = await self.bot.get_context(message)
        if ctx.valid:
            # A real command (`.ticket reply 3 hi`, `.remindme ...`, ...) —
            # let the framework's own dispatch handle it so it isn't also
            # relayed as a ticket follow-up below. Non-commands like "..." or
            # "hmm" are *not* valid, so they intentionally fall through and
            # get forwarded — that's the whole point of this listener.
            return

        text = message.content.strip()
        attachments = message.attachments
        if not text and not attachments:
            return

        # Listeners bypass cog_check, so gate on cog_enabled explicitly below.
        all_open = self._open_tickets_for_user(message.author.id)
        enabled = [
            t for t in all_open
            if cog_enabled(self.bot, t["guild_id"], "tickets") and self.bot.get_guild(t["guild_id"]) is not None
        ]

        if not all_open:
            # Stay silent for strangers; only hint at /ticket open if the
            # author shares a guild with a configured, enabled ticket
            # channel — otherwise the bot would reply to every random DM.
            for guild in self.bot.guilds:
                if guild.get_member(message.author.id) is None:
                    continue
                guild_conf = self._guild_conf(guild.id)
                if guild_conf["channel_id"] and cog_enabled(self.bot, guild.id, "tickets"):
                    try:
                        await message.channel.send(
                            "You have no open ticket. Open one in a server with `/ticket open <text>`.",
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                    except discord.HTTPException:
                        pass
                    return
            return

        if not enabled:
            guild = self.bot.get_guild(all_open[0]["guild_id"])
            name = guild.name if guild is not None else "that server"
            try:
                await message.channel.send(
                    f"Tickets are currently disabled on {name}.", allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                pass
            return

        if len(enabled) > 1:
            parts = []
            for t in enabled:
                g = self.bot.get_guild(t["guild_id"])
                parts.append(f"#{t['id']} in {g.name if g is not None else t['guild_id']}")
            try:
                await message.channel.send(
                    "You have several open tickets (" + ", ".join(parts) +
                    ") — use `.ticket reply <id> <text>` to pick one.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                pass
            return

        now = time.time()
        last = self._dm_last.get(message.author.id, 0)
        if now - last < DM_COOLDOWN_SECONDS:
            try:
                await message.channel.send(
                    "Slow down — one message every 5 seconds.", allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                pass
            return
        self._dm_last[message.author.id] = now

        if len(text) > MAX_TEXT_LENGTH:
            try:
                await message.channel.send(
                    f"Ticket text can't exceed {MAX_TEXT_LENGTH} characters.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                pass
            return

        ticket = enabled[0]
        guild = self.bot.get_guild(ticket["guild_id"])
        guild_conf = self._guild_conf(ticket["guild_id"])
        result = await self._relay_user_message(ticket, guild_conf, guild, message.author, text, attachments)
        try:
            if result == "full":
                await message.channel.send(
                    "This ticket is full — ask a mod to close it and open a new one.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            elif result == "gone":
                await message.channel.send(
                    f"Couldn't deliver — the ticket channel on {guild.name if guild else 'that server'} is "
                    f"gone; ask a mod.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await message.channel.send(
                    f"📨 Forwarded to ticket #{ticket['id']}.", allowed_mentions=discord.AllowedMentions.none(),
                )
        except discord.HTTPException:
            pass


async def setup(bot):
    await bot.add_cog(Tickets(bot))
