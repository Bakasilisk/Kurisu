"""Live, on-demand CSV export of the last EXPORT_DEFAULT_WEEKS (4 weeks by default, 1-12
selectable via the web API) of message activity.

Unlike palantir (surveillance boundary, content never leaves the bot) and stats (aggregates
only, SQLite), this cog is the **one place message content leaves the bot** — via Discord DM or
the web download endpoint. It never touches palantir's `palantir_messages.json` cache (that
cache is pre-change content on edit/delete, a different, narrower thing, and CLAUDE.md documents
it as a hard boundary) — every row here comes from a live `channel.history()` call.

Jobs are in-memory only (`Export._jobs`, one per guild), TTL-pruned lazily, never persisted — a
bot restart silently drops a running job (README documents this).
"""
import asyncio
import csv
import gzip
import io
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from .management import cog_enabled, common_error_reply, has_permissions_or_owner, reply_ephemeral_aware

logger = logging.getLogger(__name__)

EXPORT_DEFAULT_WEEKS = 4
EXPORT_MAX_WEEKS = 12
CHANNEL_SLEEP = 1.0
MAX_EXPORT_BYTES = 64 * 1024 * 1024  # 64 MiB — abort with an error, checked live + after serialization
# Memory guard, separate from MAX_EXPORT_BYTES: `rows` (and then _rows_to_csv's sorted()/StringIO/
# .encode() copies) live entirely in RAM on a 961 MB host with ~273 MB free. The live byte
# estimate below undercounts a real row by ~3.4x (measured: a typical row is ~499 B of actual
# Python objects vs. ~146 B counted), so bytes alone can't be trusted to catch a large scan before
# it OOMs the bot process — this row-count ceiling is the second, independent backstop. 150k rows
# is ~120 MB peak (rows + CSV string + encoded bytes) on this host, with headroom.
MAX_EXPORT_ROWS = 150_000
RESULT_TTL_SECONDS = 3600  # 1h
# guild.filesize_limit is wrong for DMs — boost-tier upload limits only apply to guild uploads.
DM_MAX_BYTES = discord.utils.DEFAULT_FILE_SIZE_LIMIT_BYTES  # 10 MiB
CSV_COLUMNS = ("timestamp", "channel", "author_id", "author", "content", "message_id", "attachment_urls")
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
# Optional; base URL of the kurisu-web frontend used for the "download it from the web" hint.
EXPORT_WEB_URL = os.environ.get("EXPORT_WEB_URL", "")


class _TooLarge(Exception):
    """Internal signal only — either the running/final CSV size exceeded MAX_EXPORT_BYTES, or
    the row count exceeded MAX_EXPORT_ROWS (the memory guard). `reason` ("bytes" | "rows")
    lets the caller give a distinct, actionable job.error for each."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


# --- Pure helpers (no bot instance — directly testable) -------------------------------------


def _neutralize(cell: str) -> str:
    """Prepend `'` if `cell` starts with a formula-injection trigger character, so
    Excel/LibreOffice never evaluates exported message content as a formula. Applied to
    channel/author/content cells only — never to numeric IDs."""
    if cell and cell[0] in FORMULA_PREFIXES:
        return "'" + cell
    return cell


def _message_row(source, message: discord.Message) -> tuple:
    """One CSV row for `message`, scanned from `source` (a channel or thread — used for the
    `channel` column rather than `message.channel` so thread/forum-post rows read as the thing
    that was actually scanned). `author.display_name` works for both discord.Member and
    discord.User, so a message from someone who has since left the server still resolves a
    name. All messages are included, bots too — this is a raw log, not a leveling-style
    bot-exclusion."""
    author = message.author
    return (
        message.created_at,
        _neutralize(source.name),
        author.id,
        _neutralize(author.display_name),
        _neutralize(message.content),
        message.id,
        " ".join(a.url for a in message.attachments),
    )


def _rows_to_csv(rows) -> bytes:
    """Blocking (run via asyncio.to_thread): sort by (created_at, message_id), then render as
    UTF-8 CSV with a BOM (utf-8-sig, for Excel) and a fixed-width millisecond-precision ISO8601
    timestamp column so every row lines up. `rows` are `_message_row(...)` tuples."""
    rows = sorted(rows, key=lambda r: (r[0], r[5]))
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for created_at, channel, author_id, author, content, message_id, urls in rows:
        writer.writerow((
            created_at.astimezone(timezone.utc).isoformat(timespec="milliseconds"),
            channel,
            str(author_id),
            author,
            content,
            str(message_id),
            urls,
        ))
    return buf.getvalue().encode("utf-8-sig")


def _clamp_weeks(value) -> int:
    """Coerce `value` (the web API's raw, untrusted `weeks` body field) to an int in
    [1, EXPORT_MAX_WEEKS] — the *only* place this bound is enforced (webapi.py passes the raw
    value straight through). `None`/missing and anything that fails to coerce fall back to
    EXPORT_DEFAULT_WEEKS; values above the max are clamped down to it.

    Deliberately NOT a plain clamp on the low end: a value below 1 (including `0`) also falls
    back to the default rather than clamping *up* to 1. `0` isn't a legitimate choice, and a
    broken client sending something like `Number("")` must not silently hand back the
    *shortest* possible window — falling back to the default is the safer failure mode. Don't
    "fix" this into `max(1, min(...))`.

    OverflowError is caught alongside TypeError/ValueError because `json.loads` accepts the
    bare literal `Infinity`, and `int(float("inf"))` raises OverflowError — uncaught, that
    would escape all the way out as an aiohttp 500 instead of a clamp."""
    try:
        weeks = int(value)
    except (TypeError, ValueError, OverflowError):
        return EXPORT_DEFAULT_WEEKS
    if weeks < 1:
        return EXPORT_DEFAULT_WEEKS
    return min(weeks, EXPORT_MAX_WEEKS)


def _web_hint(guild_id: int) -> str:
    """Text pointing at where the finished export can be picked up if it can't be DM'd — always
    names the 60-minute window."""
    if EXPORT_WEB_URL:
        where = f"{EXPORT_WEB_URL}/guild/{guild_id}/moderation"
    else:
        where = "the Moderation page of the web dashboard"
    return f"you can download it from {where} within 60 minutes"


@dataclass
class ExportJob:
    guild_id: int
    requested_by: int
    origin: str  # "discord" | "web"
    weeks: int = EXPORT_DEFAULT_WEEKS
    state: str = "running"  # "running" | "done" | "error"
    started_at: datetime = field(default_factory=discord.utils.utcnow)
    finished_at: datetime | None = None
    sources_scanned: int = 0
    sources_skipped: int = 0
    messages: int = 0
    error: str | None = None
    data: bytes | None = None
    filename: str | None = None
    task: "asyncio.Task | None" = None

    @property
    def expires_at(self) -> datetime | None:
        """None while running (nothing to expire yet); finished_at + TTL once done/errored."""
        if self.finished_at is None:
            return None
        return self.finished_at + timedelta(seconds=RESULT_TTL_SECONDS)

    def to_json(self) -> dict:
        """Plain dict for webapi / the Discord command — the *only* shape another cog or the
        web API ever sees (never this dataclass, never `task`). Snowflakes as strings
        (guild_id/requested_by exceed 2**53), datetimes as ISO8601 with tz, `bytes`/`filename`
        null unless state == "done"."""
        done = self.state == "done"
        return {
            "state": self.state,
            "guild_id": str(self.guild_id),
            "requested_by": str(self.requested_by),
            "origin": self.origin,
            "weeks": self.weeks,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "sources_scanned": self.sources_scanned,
            "sources_skipped": self.sources_skipped,
            "messages": self.messages,
            "bytes": len(self.data) if done and self.data is not None else None,
            "filename": self.filename if done else None,
            "error": self.error,
        }


class Export(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._jobs: dict[int, ExportJob] = {}

    def cog_unload(self):
        """Cancel every still-running export task so `.cog reload export` (or bot shutdown)
        doesn't leave an orphaned scan hammering the API on a cog instance nobody references
        anymore. _run_export catches CancelledError itself and finishes its bookkeeping (state
        -> "error", a "cancelled" message) rather than letting the exception propagate, so the
        in-flight _run_job still gets to call on_done and DM the requester — see _run_export's
        docstring for why that's a deliberate deviation from typical cancellation hygiene."""
        for job in self._jobs.values():
            if job.task is not None and not job.task.done():
                job.task.cancel()

    async def cog_check(self, ctx):
        return ctx.guild is None or cog_enabled(self.bot, ctx.guild.id, "export")

    @staticmethod
    async def _reply(ctx, *args, **kwargs):
        """ctx.reply, but ephemeral (visible only to the invoker) when invoked via / rather
        than the text prefix."""
        return await reply_ephemeral_aware(ctx, *args, **kwargs)

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.CheckAnyFailure):
            # A CheckFailure sibling, not a MissingPermissions subclass — raised by
            # has_permissions_or_owner on the export command.
            await self._reply(ctx, "You don't have permission to do that.")
            return
        if await common_error_reply(ctx, error, reply=lambda *a, **k: self._reply(ctx, *a, **k)):
            return
        raise error

    def _prune(self, guild_id: int) -> None:
        """Lazily drop a finished job past its TTL so job_status/job_csv never hand back stale
        data. No background sweep — every public accessor calls this first."""
        job = self._jobs.get(guild_id)
        if job is None:
            return
        if job.state != "running" and job.expires_at is not None and discord.utils.utcnow() >= job.expires_at:
            del self._jobs[guild_id]

    # --- Public plain-data interface for webapi (self.bot.get_cog("Export"), never an import) --
    # Plain (non-async) methods, deliberately: they either do no I/O (job_status/job_csv are
    # dict lookups) or only need to *schedule* work (start_export's asyncio.create_task doesn't
    # require awaiting).

    def start_export(self, guild: discord.Guild, requested_by: int, origin: str, on_done=None, weeks=None) -> tuple[str, dict | None]:
        """Start (or report the already-running) export job for `guild`.
        Returns ("disabled", None) | ("running", job_json) | ("started", job_json).
        A finished job never blocks a new one — only "running" does. `on_done`, if given, is
        awaited with the finished ExportJob once _run_export completes; only the Discord command
        passes one (a web-triggered job DMs nobody).

        `weeks` is the *only* place the lookback window is validated (webapi.py passes its raw,
        untrusted body value straight through) — see `_clamp_weeks`. Default `None` keeps this
        call backwards compatible: an old, browser-cached `export.js` that posts no `weeks` at
        all still gets EXPORT_DEFAULT_WEEKS."""
        if not cog_enabled(self.bot, guild.id, "export"):
            return ("disabled", None)
        self._prune(guild.id)
        existing = self._jobs.get(guild.id)
        if existing is not None and existing.state == "running":
            return ("running", existing.to_json())
        job = ExportJob(guild_id=guild.id, requested_by=requested_by, origin=origin, weeks=_clamp_weeks(weeks))
        self._jobs[guild.id] = job
        job.task = asyncio.create_task(self._run_job(job, guild, on_done))
        logger.info("Export: started | guild=%s requested_by=%s origin=%s", guild.id, requested_by, origin)
        return ("started", job.to_json())

    def job_status(self, guild_id: int) -> dict | None:
        self._prune(guild_id)
        job = self._jobs.get(guild_id)
        return job.to_json() if job is not None else None

    def job_csv(self, guild_id: int) -> tuple[bytes, str] | None:
        """Bytes + filename for a finished, unexpired "done" job — None for missing/running/
        error/expired."""
        self._prune(guild_id)
        job = self._jobs.get(guild_id)
        if job is None or job.state != "done" or job.data is None:
            return None
        return (job.data, job.filename)

    # --- Source enumeration ----------------------------------------------------------------

    async def _export_sources(self, guild: discord.Guild, cutoff: datetime):
        """Yield every history-capable messageable object live on_message-style ingest could
        see: text/announcement channels, voice/stage channel text chat, and threads (active +
        archived, public + private, including forum posts). Mirrors Stats._backfill_sources
        (cogs/stats.py) **by convention, never by import** — a shared helper under cogs/
        would surface as a phantom `.feature`-toggleable cog per management._discover_cogs
        (every cogs/*.py besides __init__.py/storage.py is discovered). Deviates from the
        stats version only for the time window: archived-thread pagination stops early via
        `break` once `thread.archive_timestamp < cutoff`, since archived_threads() yields
        newest-archived-first, so everything after that point is outside the export's
        window anyway."""
        for channel in guild.text_channels:
            yield channel
        for channel in guild.voice_channels:
            yield channel
        for channel in guild.stage_channels:
            yield channel

        # Forums have no messages of their own — only their threads (posts) do — so they're
        # scanned here as thread parents, not as sources above.
        for parent in list(guild.text_channels) + list(guild.forums):
            for thread in parent.threads:
                yield thread
            try:
                async for thread in parent.archived_threads(limit=None):
                    if thread.archive_timestamp < cutoff:
                        break
                    yield thread
            except (discord.Forbidden, discord.HTTPException):
                pass
            # Private archived threads: only TextChannel supports the `private=` kwarg
            # (ForumChannel threads have no private variant).
            if isinstance(parent, discord.TextChannel):
                try:
                    async for thread in parent.archived_threads(limit=None, private=True):
                        if thread.archive_timestamp < cutoff:
                            break
                        yield thread
                except (discord.Forbidden, discord.HTTPException):
                    pass

    # --- The scan itself ---------------------------------------------------------------------

    async def _run_export(self, job: ExportJob, guild: discord.Guild) -> None:
        """Scan every readable source for the last `job.weeks` weeks and populate `job` in
        place. Counters (sources_scanned/skipped, messages) update live as sources are
        processed, so polling (job_status) shows progress on a slow, large-server scan.

        Deliberate cancellation handling: a `.cog reload export`/shutdown calls cog_unload,
        which cancels job.task. Rather than letting CancelledError propagate out of this
        coroutine (the usual asyncio hygiene), it's caught here, recorded as a normal "error"
        state with a descriptive message, and swallowed — so the caller (_run_job) still runs
        on_done and the requester gets a "Export failed: cancelled…" DM instead of silence.
        This is a deliberate product decision, not an oversight."""
        cutoff = discord.utils.utcnow() - timedelta(weeks=job.weeks)
        rows: list[tuple] = []
        estimated_bytes = 0
        try:
            async for source in self._export_sources(guild, cutoff):
                try:
                    perms = source.permissions_for(guild.me)
                except discord.ClientException:
                    # Thread whose parent channel isn't cached.
                    job.sources_skipped += 1
                    continue
                if not perms.view_channel or not perms.read_message_history:
                    job.sources_skipped += 1
                    continue
                if source.last_message_id is not None and discord.utils.snowflake_time(source.last_message_id) < cutoff:
                    # Upper-bound skip: nothing in this source can be newer than cutoff, so
                    # don't spend a history() request (or the per-source sleep) on it. Not
                    # counted as skipped — that counter means "unreadable/errored", and this
                    # source is simply out of window.
                    continue
                try:
                    # after= flips discord.py's oldest_first default to True, so this already
                    # yields oldest->newest with no re-filtering on created_at needed.
                    async for message in source.history(limit=None, after=cutoff):
                        row = _message_row(source, message)
                        rows.append(row)
                        job.messages += 1
                        estimated_bytes += len(row[4]) + len(row[6]) + len(row[3]) + 80
                        if estimated_bytes > MAX_EXPORT_BYTES:
                            raise _TooLarge("bytes")
                        if len(rows) > MAX_EXPORT_ROWS:
                            raise _TooLarge("rows")
                except (discord.Forbidden, discord.HTTPException, discord.ClientException):
                    job.sources_skipped += 1
                    continue
                job.sources_scanned += 1
                await asyncio.sleep(CHANNEL_SLEEP)

            data = await asyncio.to_thread(_rows_to_csv, rows)
            if len(data) > MAX_EXPORT_BYTES:
                raise _TooLarge("bytes")
            job.data = data
            job.filename = f"export-{guild.id}-{discord.utils.utcnow():%Y%m%dT%H%MZ}.csv"
            job.state = "done"
        except _TooLarge as exc:
            job.state = "error"
            if exc.reason == "rows":
                job.error = f"export exceeds {MAX_EXPORT_ROWS:,} rows — retry with fewer weeks"
            else:
                job.error = f"export exceeds the {MAX_EXPORT_BYTES // (1024 * 1024)} MiB cap — retry with fewer weeks"
        except asyncio.CancelledError:
            job.state = "error"
            job.error = "cancelled (cog reloaded or bot shutting down)"
            # Deliberately not re-raised — see docstring above.
        except Exception:
            logger.exception("Export: failed | guild=%s", guild.id)
            job.state = "error"
            job.error = "internal error"
        finally:
            job.finished_at = discord.utils.utcnow()
            logger.info(
                "Export: finished | guild=%s state=%s messages=%s scanned=%s skipped=%s bytes=%s",
                guild.id, job.state, job.messages, job.sources_scanned, job.sources_skipped,
                len(job.data) if job.data else 0,
            )

    async def _run_job(self, job: ExportJob, guild: discord.Guild, on_done) -> None:
        await self._run_export(job, guild)
        if on_done is not None:
            try:
                await on_done(job)
            except Exception:
                logger.exception("Export: on_done callback failed | guild=%s", guild.id)

    async def _deliver_discord(self, job: ExportJob, guild: discord.Guild, user: discord.abc.User, channel: discord.abc.Messageable) -> None:
        """DM the finished/errored export to the Discord requester — content-bearing, so it
        must never land in the invoking channel. Falls back to a gzip-compressed attachment if
        the plain CSV exceeds DM_MAX_BYTES, and — only if DMs are closed — to a note posted in
        `channel`. That note is deliberately public but carries no data (no file, no content
        excerpt): it exists so the requester notices without anyone needing to guess why the
        bot went quiet. Don't "fix" it into attaching the file there instead — that would defeat
        the entire DM-only delivery rule this cog exists to enforce. A discord.File's underlying
        stream is consumed on send and is never reused (only ever sent once, here)."""
        hint = _web_hint(guild.id)
        if job.state == "error":
            file = None
            content = f"Export failed: {job.error}"
        elif len(job.data) <= DM_MAX_BYTES:
            file = discord.File(io.BytesIO(job.data), job.filename)
            content = f"Here's your export — {job.messages:,} message(s)."
        else:
            gz = await asyncio.to_thread(gzip.compress, job.data)
            if len(gz) <= DM_MAX_BYTES:
                file = discord.File(io.BytesIO(gz), job.filename + ".gz")
                content = f"Here's your export (gzip-compressed) — {job.messages:,} message(s)."
            else:
                file = None
                content = f"Export done, {job.messages:,} message(s), too large for a DM — {hint}."

        try:
            if file is not None:
                await user.send(content, file=file)
            else:
                await user.send(content)
            return
        except (discord.Forbidden, discord.HTTPException) as exc:
            logger.warning("Export: DM delivery failed | guild=%s user=%s: %s", guild.id, user.id, exc)

        try:
            await channel.send(
                f"{user.mention} I couldn't DM you the export — {hint}.",
                allowed_mentions=discord.AllowedMentions(users=[user]),
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

    # --- Command -------------------------------------------------------------------------------

    @commands.hybrid_command(
        name="export",
        description=f"Export the last {EXPORT_DEFAULT_WEEKS} weeks of messages as CSV (Manage Server only).",
    )
    @app_commands.default_permissions(manage_guild=True)
    @has_permissions_or_owner(manage_guild=True)
    @commands.guild_only()
    async def export(self, ctx):
        """Scan every readable channel and thread (active/archived, including forum posts) for
        the last 4 weeks of messages and DM the requester a CSV. One export job runs per guild at
        a time; a finished job stays available (one Discord DM, or the web dashboard) for 60
        minutes."""
        guild = ctx.guild

        async def _on_done(job: ExportJob) -> None:
            await self._deliver_discord(job, guild, ctx.author, ctx.channel)

        state, job_json = self.start_export(guild, ctx.author.id, "discord", on_done=_on_done)
        if state == "disabled":
            await self._reply(ctx, "Export is disabled for this server — an admin can `.feature enable export`.")
        elif state == "running":
            origin = job_json.get("origin", "unknown") if job_json else "unknown"
            await self._reply(
                ctx,
                f"An export is already running for this server (started via {origin}) — wait for it to finish.",
            )
        else:
            await self._reply(
                ctx,
                "Export started — scanning every readable channel and thread for the last 4 weeks. "
                "I'll DM you the CSV when it's done.",
            )


async def setup(bot):
    await bot.add_cog(Export(bot))
