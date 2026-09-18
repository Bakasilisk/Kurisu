import logging
import re
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from .management import (
    actor_outranks,
    bot_outranks,
    cog_enabled,
    common_error_reply,
    has_permissions_or_owner,
    reply_ephemeral_aware,
    require_outranks,
)
from .storage import data_path, load_json, save_json_atomic

logger = logging.getLogger(__name__)

WARNINGS_FILE = data_path("warnings.json")
LOCKS_FILE = data_path("channel_locks.json")
MODLOG_FILE = data_path("mod_log.json")

# An embed description caps at 4096 characters (a whole embed at 6000 — the limit
# the help cog hit once already). A guild allows up to 250 roles and a role mention
# is ~22 characters, so a maximally-roled member overruns it: budget under the cap
# and spill the rest into an "…and N more" line rather than losing the whole reply.
ROLE_LIST_CHAR_BUDGET = 3900

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


def format_role_list(roles) -> str:
    """Role mentions, highest first, truncated to ROLE_LIST_CHAR_BUDGET with a
    trailing "…and N more" line. `roles` must already exclude @everyone."""
    shown = []
    length = 0
    for role in roles:
        if length + len(role.mention) + 2 > ROLE_LIST_CHAR_BUDGET:
            break
        shown.append(role.mention)
        length += len(role.mention) + 2
    text = ", ".join(shown)
    remaining = len(roles) - len(shown)
    if remaining:
        text += f"\n…and {remaining} more"
    return text


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
        self.mod_log_channels = load_json(MODLOG_FILE)

    def _load_warnings(self) -> dict:
        return load_json(WARNINGS_FILE)

    def _save_warnings(self):
        save_json_atomic(WARNINGS_FILE, self.warnings)

    def _save_locks(self):
        save_json_atomic(LOCKS_FILE, self.locks)

    def _save_mod_log_channels(self):
        save_json_atomic(MODLOG_FILE, self.mod_log_channels)

    async def cog_check(self, ctx):
        if ctx.guild is None or await self.bot.is_owner(ctx.author):
            return True
        return cog_enabled(self.bot, ctx.guild.id, "moderation")

    async def _hierarchy_error(self, ctx, member, verb: str) -> str | None:
        """Return an error string if the actor or the bot can't act on `member`
        by role hierarchy, else None. The guild owner and the bot owner both
        bypass the actor check; the bot's own role limit always applies."""
        if not await actor_outranks(self.bot, ctx, member):
            return f"You can't {verb} someone with an equal or higher role than you."
        if not bot_outranks(ctx.guild, member):
            return f"My role isn't high enough to {verb} that member."
        return None

    async def _role_error(self, ctx, role, verb: str) -> str | None:
        """Return an error string if `role` itself is out of reach — by what it is,
        by the actor's hierarchy, or by the bot's — else None. The role-side twin of
        _hierarchy_error: handing a role to a member needs both the member and the
        role to be in reach, so the role commands run both. Every message here can
        name the role, so callers must reply with AllowedMentions(roles=False)."""
        if role.is_default():
            return "`@everyone` isn't a role I can add or remove."
        if role.managed:
            # `managed` covers bot roles, integration and subscription roles, and the
            # boost role — Discord owns who holds them, so nobody can assign them by hand.
            if role.is_premium_subscriber():
                return f"{role.mention} is the server's boost role — Discord manages who has it."
            return f"{role.mention} is managed by a bot or integration — nobody can {verb} it by hand."
        if not await actor_outranks(self.bot, ctx, role):
            return f"You can't {verb} a role that's equal to or above your own top role."
        if not bot_outranks(ctx.guild, role):
            return f"My role isn't high enough to {verb} {role.mention}."
        return None

    @staticmethod
    async def _reply(ctx, *args, **kwargs):
        """ctx.reply, but ephemeral (visible only to the invoker) when the
        command was invoked via / rather than the text prefix."""
        return await reply_ephemeral_aware(ctx, *args, **kwargs)

    async def _log_action(
        self, ctx, action: str, color: discord.Color, *, target=None, reason=None, **fields
    ):
        """Record a moderation action to the local logfile, and post an embed
        to the guild's configured mod-log channel if one is set (silently
        no-ops if unconfigured or the bot can no longer post there) — so
        actions taken via an ephemeral slash reply are still visible."""
        detail = ", ".join(f"{name}={value}" for name, value in fields.items())
        logger.info(
            "%s | guild=%s moderator=%s target=%s reason=%r%s",
            action, ctx.guild.id, ctx.author, target, reason,
            f" {detail}" if detail else "",
        )

        channel_id = self.mod_log_channels.get(str(ctx.guild.id))
        if not channel_id:
            return
        channel = ctx.guild.get_channel(channel_id)
        if channel is None:
            return

        embed = discord.Embed(title=action, color=color, timestamp=discord.utils.utcnow())
        embed.add_field(name="Moderator", value=f"{ctx.author.mention} ({ctx.author})")
        if target is not None:
            embed.add_field(name="Target", value=f"{target.mention} ({target})")
        for name, value in fields.items():
            embed.add_field(name=name, value=value)
        if reason is not None:
            embed.add_field(name="Reason", value=reason, inline=False)
        embed.set_footer(text=f"#{ctx.channel.name}")

        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            pass

    @commands.hybrid_group(
        invoke_without_command=True, fallback="show", case_insensitive=True,
        description="Show the current mod-log configuration.",
    )
    @app_commands.default_permissions(manage_guild=True)
    @has_permissions_or_owner(manage_guild=True)
    @commands.guild_only()
    async def modlog(self, ctx):
        """Show the current mod-log configuration."""
        channel_id = self.mod_log_channels.get(str(ctx.guild.id))
        channel = ctx.guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            await self._reply(
                ctx, "No mod-log channel is configured. Use `.modlog set #channel` to set one."
            )
        else:
            await self._reply(ctx, f"Mod-log actions are currently sent to {channel.mention}.")

    @modlog.command(name="set", description="Set the channel moderation actions are logged to.")
    @has_permissions_or_owner(manage_guild=True)
    @commands.guild_only()
    async def modlog_set(self, ctx, channel: discord.TextChannel):
        """Set the channel moderation actions are logged to."""
        self.mod_log_channels[str(ctx.guild.id)] = channel.id
        self._save_mod_log_channels()
        await self._reply(ctx, f"📋 Mod-log channel set to {channel.mention}.")

    @modlog.command(name="disable", description="Stop logging moderation actions.")
    @has_permissions_or_owner(manage_guild=True)
    @commands.guild_only()
    async def modlog_disable(self, ctx):
        """Stop logging moderation actions."""
        had_one = self.mod_log_channels.pop(str(ctx.guild.id), None) is not None
        self._save_mod_log_channels()
        await self._reply(ctx, "📋 Mod-log disabled." if had_one else "Mod-log was not enabled.")

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.MemberNotFound):
            await self._reply(ctx, "I couldn't find that member.")
        elif isinstance(error, commands.UserNotFound):
            await self._reply(ctx, "I couldn't find that user.")
        elif isinstance(error, commands.ChannelNotFound):
            await self._reply(ctx, "I couldn't find that channel.")
        elif isinstance(error, commands.RoleNotFound):
            # Before common_error_reply: RoleNotFound is a BadArgument subclass, and the
            # shared helper would surface discord.py's raw 'Role "x" not found.' instead.
            await self._reply(
                ctx,
                "I couldn't find that role — use its exact name (case matters, and no "
                "quotes needed), a role mention, or its ID.",
            )
        elif isinstance(error, commands.CheckAnyFailure):
            # CheckAnyFailure (from has_permissions_or_owner's check_any) is a
            # CheckFailure sibling, not a MissingPermissions subclass — common_error_reply
            # wouldn't recognize it and would silently swallow it via the trailing
            # CheckFailure branch. Keep it visible, matching the old combined branch.
            await self._reply(ctx, "You don't have permission to do that.")
        elif await common_error_reply(ctx, error, reply=lambda text: self._reply(ctx, text)):
            return
        else:
            raise error

    @commands.hybrid_command(description="Kick a member from the server.")
    @app_commands.default_permissions(kick_members=True)
    @has_permissions_or_owner(kick_members=True)
    @commands.bot_has_permissions(kick_members=True)
    @commands.guild_only()
    async def kick(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        """Kick a member from the server."""
        error = await self._hierarchy_error(ctx, member, "kick")
        if error:
            await self._reply(ctx, error)
            return
        await member.kick(reason=f"{ctx.author}: {reason}")
        await self._reply(ctx, f"👢 Kicked {member.mention} — {reason}")
        await self._log_action(ctx, "Member Kicked", discord.Color.orange(), target=member, reason=reason)

    @commands.hybrid_command(description="Ban a member from the server.")
    @app_commands.default_permissions(ban_members=True)
    @has_permissions_or_owner(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    @commands.guild_only()
    async def ban(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        """Ban a member from the server."""
        error = await self._hierarchy_error(ctx, member, "ban")
        if error:
            await self._reply(ctx, error)
            return
        await member.ban(reason=f"{ctx.author}: {reason}", delete_message_seconds=0)
        await self._reply(ctx, f"🔨 Banned {member.mention} — {reason}")
        await self._log_action(ctx, "Member Banned", discord.Color.red(), target=member, reason=reason)

    @commands.hybrid_command(description="Unban a user by ID or exact username.")
    @app_commands.default_permissions(ban_members=True)
    @has_permissions_or_owner(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    @commands.guild_only()
    async def unban(self, ctx, user: discord.User, *, reason: str = "No reason provided"):
        """Unban a user by ID or exact username."""
        await ctx.guild.unban(user, reason=f"{ctx.author}: {reason}")
        await self._reply(ctx, f"✅ Unbanned {user.mention} — {reason}")
        await self._log_action(ctx, "Member Unbanned", discord.Color.green(), target=user, reason=reason)

    @commands.hybrid_command(aliases=["mute"], description="Time out a member (e.g. 10m, 2h, 1d).")
    @app_commands.default_permissions(moderate_members=True)
    @has_permissions_or_owner(moderate_members=True)
    @commands.bot_has_permissions(moderate_members=True)
    @commands.guild_only()
    async def timeout(
        self, ctx, member: discord.Member, duration: str, *, reason: str = "No reason provided"
    ):
        """Time out a member for a given duration (e.g. 10m, 2h, 1d)."""
        error = await self._hierarchy_error(ctx, member, "time out")
        if error:
            await self._reply(ctx, error)
            return
        delta = parse_duration(duration)
        if delta > timedelta(days=28):
            await self._reply(ctx, "Timeouts can't exceed 28 days.")
            return
        await member.timeout(delta, reason=f"{ctx.author}: {reason}")
        await self._reply(ctx, f"🔇 Timed out {member.mention} for {duration} — {reason}")
        await self._log_action(
            ctx, "Member Timed Out", discord.Color.orange(), target=member, reason=reason,
            Duration=duration,
        )

    @commands.hybrid_command(aliases=["unmute"], description="Remove an active timeout from a member.")
    @app_commands.default_permissions(moderate_members=True)
    @has_permissions_or_owner(moderate_members=True)
    @commands.bot_has_permissions(moderate_members=True)
    @commands.guild_only()
    async def untimeout(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        """Remove an active timeout from a member."""
        error = await self._hierarchy_error(ctx, member, "remove a timeout from")
        if error:
            await self._reply(ctx, error)
            return
        await member.timeout(None, reason=f"{ctx.author}: {reason}")
        await self._reply(ctx, f"🔊 Removed timeout from {member.mention}")
        await self._log_action(
            ctx, "Timeout Removed", discord.Color.green(), target=member, reason=reason
        )

    @commands.hybrid_command(description="Warn a member and record it.")
    @app_commands.default_permissions(moderate_members=True)
    @has_permissions_or_owner(moderate_members=True)
    @commands.guild_only()
    async def warn(self, ctx, member: discord.Member, *, reason: str = "No reason provided"):
        """Warn a member and record it."""
        # Actor-only check: warning is local bookkeeping, not a Discord-side action, so
        # there's no bot-role hierarchy constraint to enforce here.
        if not await require_outranks(
            self.bot, ctx, member, "warn", reply=lambda text: self._reply(ctx, text)
        ):
            return
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

        await self._reply(
            ctx, f"⚠️ Warned {member.mention} — {reason} (total: {len(member_warnings)})"
        )
        await self._log_action(
            ctx, "Member Warned", discord.Color.gold(), target=member, reason=reason,
            **{"Total warnings": str(len(member_warnings))},
        )

    @commands.hybrid_command(name="warnings", aliases=["warnlist"], description="List a member's warnings.")
    @app_commands.default_permissions(moderate_members=True)
    @has_permissions_or_owner(moderate_members=True)
    @commands.guild_only()
    async def warnings_(self, ctx, member: discord.Member):
        """List a member's warnings."""
        member_warnings = self.warnings.get(str(ctx.guild.id), {}).get(str(member.id), [])
        if not member_warnings:
            await self._reply(ctx, f"{member.mention} has no warnings.")
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
        await self._reply(ctx, embed=embed)

    @commands.hybrid_command(description="Clear all warnings for a member.")
    @app_commands.default_permissions(moderate_members=True)
    @has_permissions_or_owner(moderate_members=True)
    @commands.guild_only()
    async def clearwarnings(self, ctx, member: discord.Member):
        """Clear all warnings for a member."""
        # Actor-only check: clearing warnings is local bookkeeping, not a Discord-side
        # action, so there's no bot-role hierarchy constraint to enforce here.
        if not await require_outranks(
            self.bot, ctx, member, "clear warnings for", reply=lambda text: self._reply(ctx, text)
        ):
            return
        guild_warnings = self.warnings.get(str(ctx.guild.id), {})
        count = len(guild_warnings.pop(str(member.id), []))
        self._save_warnings()
        await self._reply(ctx, f"🧹 Cleared {count} warning(s) for {member.mention}")
        await self._log_action(
            ctx, "Warnings Cleared", discord.Color.blue(), target=member,
            **{"Warnings cleared": str(count)},
        )

    # No fallback= on this group, unlike every other group in the repo: a
    # fallback registers the name slash-side only, so `.role list @member` would
    # then try to parse "list" as a member and fail with MemberNotFound. Palantir
    # and stats work around that with a second with_app_command=False subcommand;
    # a group without a fallback simply registers /role add, /role remove and
    # /role list, with the group callback serving the prefix side alone.
    @commands.hybrid_group(
        name="role", invoke_without_command=True, case_insensitive=True,
        description="Add or remove a member's roles.",
    )
    @app_commands.default_permissions(manage_roles=True)
    @has_permissions_or_owner(manage_roles=True)
    @commands.guild_only()
    async def role(self, ctx, member: discord.Member | None = None):
        """Show a member's roles, highest first (defaults to yourself)."""
        await self._show_roles(ctx, member or ctx.author)

    async def _show_roles(self, ctx, member):
        """Render a member's roles — shared by the group callback (`.role @member`)
        and the explicit `role list` subcommand."""
        # member.roles is position-ascending with @everyone pinned at index 0.
        roles = [role for role in reversed(member.roles) if not role.is_default()]
        if not roles:
            await self._reply(ctx, f"{member.mention} has no roles.")
            return
        embed = discord.Embed(
            title=f"Roles for {member}",
            description=format_role_list(roles),
            color=discord.Color.blurple(),
        )
        # The count stays exact even when the description above was truncated.
        embed.set_footer(text=f"{len(roles)} role(s)")
        await self._reply(
            ctx, embed=embed, allowed_mentions=discord.AllowedMentions(roles=False)
        )

    @role.command(name="list", description="Show a member's roles, highest first.")
    @has_permissions_or_owner(manage_roles=True)
    @commands.guild_only()
    @app_commands.describe(member="The member whose roles to list (defaults to you).")
    async def role_list(self, ctx, member: discord.Member | None = None):
        """Show a member's roles, highest first."""
        await self._show_roles(ctx, member or ctx.author)

    async def _role_change_error(self, ctx, member, role, member_verb: str, role_verb: str) -> str | None:
        """The checks `role add` and `role remove` share: the target member must be
        in reach (hierarchy, and not the server owner) and so must the role itself."""
        if member.id == ctx.guild.owner_id:
            # Not covered by the hierarchy checks below: the server owner can hold no
            # roles at all, so those would pass, yet Discord refuses any role change
            # on the owner regardless of position.
            return "I can't change the server owner's roles — Discord doesn't allow it."
        error = await self._hierarchy_error(ctx, member, member_verb)
        if error:
            return error
        return await self._role_error(ctx, role, role_verb)

    # bot_has_guild_permissions, not bot_has_permissions (which the rest of this cog
    # uses): Manage Roles shares its permission bit with a channel overwrite's "Manage
    # Permissions", so the channel-scoped check would refuse in a channel that denies
    # the bot that overwrite even though role management is a guild-wide power — and
    # pass in one that grants it, only for the API call to 403. lock/unlock's
    # bot_has_permissions is correct as-is; that one really is channel-scoped.
    @role.command(name="add", aliases=["give"], description="Give a member a role.")
    @has_permissions_or_owner(manage_roles=True)
    @commands.bot_has_guild_permissions(manage_roles=True)
    @commands.guild_only()
    @app_commands.describe(member="The member to give the role to.", role="The role to give.")
    async def role_add(self, ctx, member: discord.Member, *, role: discord.Role):
        """Give a member a role."""
        # Every reply below passes AllowedMentions(roles=False) (the .verify idiom):
        # a mentionable role would otherwise ping the whole server from a bot reply.
        quiet = discord.AllowedMentions(roles=False)
        error = await self._role_change_error(ctx, member, role, "give a role to", "grant")
        if error:
            await self._reply(ctx, error, allowed_mentions=quiet)
            return
        if role in member.roles:
            await self._reply(
                ctx, f"{member.mention} already has {role.mention}.", allowed_mentions=quiet
            )
            return

        try:
            # The audit-log reason names the moderator because palantir surfaces
            # entry.reason in its modactions embed — without it the log would
            # attribute the change to the bot alone.
            await member.add_roles(role, reason=f"Role granted by {ctx.author} via .role add")
        except discord.Forbidden:
            await self._reply(
                ctx,
                f"Discord refused that role change — check that my role is above "
                f"{role.mention} and that I still have Manage Roles.",
                allowed_mentions=quiet,
            )
            return
        except discord.HTTPException:
            logger.exception("role add: failed to give role %s to member %s", role.id, member.id)
            await self._reply(
                ctx, "That role change failed — Discord returned an error. Try again in a moment."
            )
            return

        await self._reply(
            ctx, f"✅ Gave {member.mention} the {role.mention} role.", allowed_mentions=quiet
        )
        await self._log_action(
            ctx, "Role Added", discord.Color.green(), target=member,
            # Mention plus name: the mention renders in the embed, the name keeps the
            # logfile line readable (_log_action reuses this value for both).
            Role=f"{role.mention} ({role.name})",
        )

    @role.command(name="remove", aliases=["take"], description="Take a role away from a member.")
    @has_permissions_or_owner(manage_roles=True)
    @commands.bot_has_guild_permissions(manage_roles=True)
    @commands.guild_only()
    @app_commands.describe(member="The member to take the role from.", role="The role to take.")
    async def role_remove(self, ctx, member: discord.Member, *, role: discord.Role):
        """Take a role away from a member."""
        quiet = discord.AllowedMentions(roles=False)
        error = await self._role_change_error(ctx, member, role, "take a role from", "take")
        if error:
            await self._reply(ctx, error, allowed_mentions=quiet)
            return
        if role not in member.roles:
            await self._reply(
                ctx, f"{member.mention} doesn't have {role.mention}.", allowed_mentions=quiet
            )
            return

        try:
            await member.remove_roles(role, reason=f"Role removed by {ctx.author} via .role remove")
        except discord.Forbidden:
            await self._reply(
                ctx,
                f"Discord refused that role change — check that my role is above "
                f"{role.mention} and that I still have Manage Roles.",
                allowed_mentions=quiet,
            )
            return
        except discord.HTTPException:
            logger.exception(
                "role remove: failed to take role %s from member %s", role.id, member.id
            )
            await self._reply(
                ctx, "That role change failed — Discord returned an error. Try again in a moment."
            )
            return

        await self._reply(
            ctx, f"➖ Removed the {role.mention} role from {member.mention}.", allowed_mentions=quiet
        )
        await self._log_action(
            ctx, "Role Removed", discord.Color.orange(), target=member,
            Role=f"{role.mention} ({role.name})",
        )

    @commands.hybrid_command(
        aliases=["clear"],
        description="Bulk-delete messages in the current channel, optionally filtered by member.",
    )
    @app_commands.default_permissions(manage_messages=True)
    @has_permissions_or_owner(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    @commands.guild_only()
    async def purge(self, ctx, amount: int, member: discord.Member | None = None):
        """Bulk-delete messages in the current channel, optionally filtered by member."""
        if not 1 <= amount <= 100:
            await self._reply(ctx, "Please choose an amount between 1 and 100.")
            return

        def check(msg):
            return member is None or msg.author == member

        # Remove the invoking prefix message first (slash invocations have none), so it
        # never counts against `amount` or the reported total regardless of the filter.
        ephemeral = ctx.interaction is not None
        if not ephemeral:
            try:
                await ctx.message.delete()
            except discord.NotFound:
                pass

        # Discord can't bulk-delete messages older than 14 days, so channel.purge falls
        # back to deleting those one at a time under rate limiting — amount=100 can take
        # minutes. For a slash invocation this is the initial interaction response, and
        # Discord kills an un-acknowledged interaction token after 3s (10062 "Unknown
        # interaction"). Defer now, ephemeral to match the confirmation reply below so
        # the eventual followup's visibility doesn't flip on the user. ctx.typing() is a
        # no-op defer on a prefix invocation (Context.interaction is None there).
        async with ctx.typing(ephemeral=True):
            deleted = await ctx.channel.purge(limit=amount, check=check)

        # The confirmation is best-effort UX; the audit trail below must land regardless
        # of whether it does, so any failure sending/deleting it is swallowed here rather
        # than allowed to propagate and skip _log_action.
        try:
            if ephemeral:
                confirmation = await self._reply(ctx, f"🧹 Deleted {len(deleted)} message(s).")
            else:
                # Not ctx.reply/self._reply: those reference ctx.message, which we just
                # deleted above. Context.reply's message reference only carries
                # message_id/channel_id/guild_id (no fail_if_not_exists), so Discord's
                # API default of fail_if_not_exists=true makes referencing a deleted
                # message raise HTTP 400. Send a plain, unreferenced message instead.
                confirmation = await ctx.send(f"🧹 Deleted {len(deleted)} message(s).")
                await confirmation.delete(delay=5)
        except discord.HTTPException:
            logger.exception("purge: failed to send or delete the confirmation message")

        await self._log_action(
            ctx, "Messages Purged", discord.Color.blue(),
            **{
                "Channel": ctx.channel.mention,
                "Messages deleted": str(len(deleted)),
                "Filtered to": member.mention if member else "Everyone",
            },
        )

    @commands.hybrid_command(
        description="Set the slowmode delay (in seconds) for the current channel. 0 to disable."
    )
    @app_commands.default_permissions(manage_channels=True)
    @has_permissions_or_owner(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    @commands.guild_only()
    async def slowmode(self, ctx, seconds: int):
        """Set the slowmode delay (in seconds) for the current channel. Use 0 to disable."""
        if not 0 <= seconds <= 21600:
            await self._reply(ctx, "Slowmode must be between 0 and 21600 seconds (6 hours).")
            return
        await ctx.channel.edit(slowmode_delay=seconds)
        if seconds == 0:
            await self._reply(ctx, "🐇 Slowmode disabled.")
        else:
            await self._reply(ctx, f"🐌 Slowmode set to {seconds} second(s).")
        await self._log_action(
            ctx, "Slowmode Changed", discord.Color.blue(),
            **{"Channel": ctx.channel.mention, "Delay": f"{seconds}s"},
        )

    @commands.hybrid_command(description="Prevent @everyone from sending messages in the current channel.")
    @app_commands.default_permissions(manage_channels=True)
    @has_permissions_or_owner(manage_channels=True)
    @commands.bot_has_permissions(manage_roles=True)
    @commands.guild_only()
    async def lock(self, ctx, *, reason: str = "No reason provided"):
        """Prevent @everyone from sending messages in the current channel."""
        channel_id = str(ctx.channel.id)
        if channel_id not in self.locks:
            # Only capture on the first lock, so a second .lock on an already-locked
            # channel doesn't clobber the true pre-lock snapshot.
            self.locks[channel_id] = snapshot_overwrite(ctx.channel, ctx.guild.default_role)
            self._save_locks()

        overwrite = ctx.channel.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = False
        await ctx.channel.set_permissions(
            ctx.guild.default_role, overwrite=overwrite, reason=f"{ctx.author}: {reason}"
        )
        await self._reply(ctx, f"🔒 Channel locked — {reason}")
        await self._log_action(
            ctx, "Channel Locked", discord.Color.dark_red(), reason=reason,
            Channel=ctx.channel.mention,
        )

    @commands.hybrid_command(description="Allow @everyone to send messages in the current channel again.")
    @app_commands.default_permissions(manage_channels=True)
    @has_permissions_or_owner(manage_channels=True)
    @commands.bot_has_permissions(manage_roles=True)
    @commands.guild_only()
    async def unlock(self, ctx, *, reason: str = "No reason provided"):
        """Allow @everyone to send messages in the current channel again."""
        channel_id = str(ctx.channel.id)
        # None if never locked via .lock (e.g. state lost to a restart) — falls back
        # to clearing the overwrite entirely, the old (safe) behavior.
        snapshot = self.locks.get(channel_id)
        # Only pop/persist once the restore actually succeeds — if it raises, the
        # snapshot must survive so a retry (or a later .unlock) can still recover it.
        await restore_overwrite(
            ctx.channel, ctx.guild.default_role, snapshot, reason=f"{ctx.author}: {reason}"
        )
        self.locks.pop(channel_id, None)
        self._save_locks()
        await self._reply(ctx, f"🔓 Channel unlocked — {reason}")
        await self._log_action(
            ctx, "Channel Unlocked", discord.Color.green(), reason=reason,
            Channel=ctx.channel.mention,
        )


async def setup(bot):
    await bot.add_cog(Moderation(bot))
