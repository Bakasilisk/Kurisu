import os
from io import BytesIO

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from .management import cog_enabled, common_error_reply

TRACE_URL = "https://api.trace.moe/search"
CONFIDENT = 0.87
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_UPLOAD_BYTES = 8 * 1024 * 1024

_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

NO_MATCH_MESSAGE = "No match found — the scene might be too obscure, cropped, or edited."
RATE_LIMITED_MESSAGE = "trace.moe is rate-limiting requests right now — try again in a bit."
SERVICE_FAILED_MESSAGE = "The trace.moe service failed — try again in a moment."


class TraceError(Exception):
    """Raised when the trace.moe API call fails or returns a non-success status."""


def _looks_like_image(name: str, content_type: str | None) -> bool:
    if content_type and content_type.startswith("image/"):
        return True
    return name.lower().endswith(_IMAGE_EXTENSIONS)


def _first_image_attachment(attachments) -> discord.Attachment | None:
    for attachment in attachments:
        if _looks_like_image(attachment.filename, attachment.content_type):
            return attachment
    return None


def _first_embed_image_url(embeds) -> str | None:
    for embed in embeds:
        if embed.image and embed.image.url:
            return embed.image.url
        if embed.thumbnail and embed.thumbnail.url:
            return embed.thumbnail.url
    return None


class TraceMoe(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.api_key = os.environ.get("TRACE_MOE_API_KEY")
        self._session: aiohttp.ClientSession | None = None

        self._menu = app_commands.ContextMenu(name="Trace anime", callback=self._menu_trace)
        self.bot.tree.add_command(self._menu)

    async def cog_unload(self):
        self.bot.tree.remove_command(self._menu.name, type=discord.AppCommandType.message)
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def cog_check(self, ctx):
        return ctx.guild is None or cog_enabled(self.bot, ctx.guild.id, "trace")

    async def cog_command_error(self, ctx, error):
        if await common_error_reply(ctx, error):
            return
        raise error

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    # --- Image resolution -----------------------------------------------------

    async def _resolve_image_source(self, ctx, url: str | None) -> tuple[bytes | None, str | None]:
        """Returns (data, thumb_url). Priority: attachment on the invoking message,
        then a url arg, then the replied-to message's attachment/embed image.
        Returns (None, None) if nothing usable was found. Replies directly on a
        rejected (non-image / oversized) candidate and returns (None, None) too."""
        attachment = _first_image_attachment(ctx.message.attachments)
        if attachment is not None:
            return await self._read_attachment(ctx, attachment)

        if url:
            return None, url

        reference = ctx.message.reference
        if reference is not None:
            resolved = reference.resolved
            if resolved is None or isinstance(resolved, discord.DeletedReferencedMessage):
                try:
                    resolved = await ctx.channel.fetch_message(reference.message_id)
                except (discord.NotFound, discord.HTTPException):
                    resolved = None
            if resolved is not None:
                attachment = _first_image_attachment(resolved.attachments)
                if attachment is not None:
                    return await self._read_attachment(ctx, attachment)
                embed_url = _first_embed_image_url(resolved.embeds)
                if embed_url:
                    return None, embed_url

        return None, None

    async def _read_attachment(self, ctx, attachment: discord.Attachment) -> tuple[bytes | None, str | None]:
        if attachment.size > MAX_IMAGE_BYTES:
            await ctx.reply(f"That image is too large to trace (max {MAX_IMAGE_BYTES // (1024 * 1024)} MB).")
            return None, None
        try:
            data = await attachment.read()
        except (discord.HTTPException, discord.NotFound):
            await ctx.reply("Couldn't download that attachment — try again.")
            return None, None
        return data, attachment.url

    # --- trace.moe call -------------------------------------------------------

    async def _search(self, *, data: bytes | None, url: str | None) -> list[dict]:
        session = self._get_session()
        params = {"anilistInfo": "", "cutBorders": ""}
        headers = {"x-trace-key": self.api_key} if self.api_key else None
        try:
            if data is not None:
                form = aiohttp.FormData()
                form.add_field("image", data, filename="image")
                async with session.post(TRACE_URL, params=params, data=form, headers=headers) as resp:
                    if resp.status in (402, 429):
                        raise TraceError("rate-limited")
                    payload = await resp.json()
            else:
                get_params = dict(params)
                get_params["url"] = url
                async with session.get(TRACE_URL, params=get_params, headers=headers) as resp:
                    if resp.status in (402, 429):
                        raise TraceError("rate-limited")
                    payload = await resp.json()
        except aiohttp.ClientError as e:
            raise TraceError(str(e)) from e

        if payload.get("error"):
            return []
        return payload.get("result") or []

    async def _fetch_clip(self, video_url: str) -> discord.File | None:
        """Best-effort fetch of the muted preview clip. Never raises — a failed or
        oversized clip just means no attachment, not a failed reply."""
        session = self._get_session()
        # trace.moe's `video` URL usually has no query string of its own, so pick the
        # separator based on whether one is already present rather than assuming "&".
        separator = "&" if "?" in video_url else "?"
        clip_url = f"{video_url}{separator}size=l&mute"
        try:
            async with session.get(clip_url) as resp:
                if resp.status != 200:
                    return None
                clip_bytes = await resp.read()
        except aiohttp.ClientError:
            return None
        if len(clip_bytes) > MAX_UPLOAD_BYTES:
            return None
        return discord.File(BytesIO(clip_bytes), filename="trace.mp4")

    # --- Result presentation -----------------------------------------------------

    @staticmethod
    def _format_time(seconds) -> str:
        total = max(0, int(seconds or 0))
        return f"{total // 60}:{total % 60:02d}"

    @staticmethod
    def _pick_title(title_obj: dict) -> str:
        return title_obj.get("romaji") or title_obj.get("english") or title_obj.get("native") or "Unknown title"

    @staticmethod
    def _confidence_color(similarity: float) -> discord.Color:
        return discord.Color.green() if similarity >= CONFIDENT else discord.Color.greyple()

    @classmethod
    def _format_runner_up(cls, result: dict) -> str:
        anilist = result.get("anilist") or {}
        title = cls._pick_title(anilist.get("title") or {})
        similarity = float(result.get("similarity") or 0.0)
        return f"{title} — {similarity * 100:.1f}%"

    async def _build_result(self, results: list[dict], channel) -> tuple[discord.Embed, discord.File | None]:
        top = results[0]
        anilist = top.get("anilist") or {}
        title_obj = anilist.get("title") or {}
        title = self._pick_title(title_obj)
        english = title_obj.get("english")

        similarity = float(top.get("similarity") or 0.0)
        confident = similarity >= CONFIDENT

        embed = discord.Embed(title=title, color=self._confidence_color(similarity))
        anilist_id = anilist.get("id")
        if anilist_id:
            embed.url = f"https://anilist.co/anime/{anilist_id}"
        if english and english != title:
            embed.description = english

        episode = top.get("episode")
        embed.add_field(name="Episode", value=str(episode) if episode not in (None, "") else "?")

        from_s = top.get("from") or 0
        to_s = top.get("to") or 0
        time_str = self._format_time(from_s)
        if to_s and round(to_s) != round(from_s):
            time_str += f"–{self._format_time(to_s)}"
        embed.add_field(name="Time", value=time_str)

        embed.add_field(name="Match", value=f"{similarity * 100:.1f}%")

        if not confident:
            embed.add_field(name="Note", value="Low confidence — this match may be wrong.", inline=False)

        runner_ups = [self._format_runner_up(r) for r in results[1:3]]
        if runner_ups:
            embed.add_field(name="Runner-up matches", value="\n".join(runner_ups), inline=False)

        embed.set_footer(text="via trace.moe")

        is_adult = bool(anilist.get("isAdult"))
        channel_nsfw = bool(getattr(channel, "nsfw", False))
        file = None
        if is_adult and not channel_nsfw:
            embed.add_field(name="Preview", value="Adult title — preview hidden in this channel.", inline=False)
        else:
            image_url = top.get("image")
            if image_url:
                embed.set_image(url=image_url)
            video_url = top.get("video")
            if video_url:
                file = await self._fetch_clip(video_url)

        return embed, file

    # --- Command -----------------------------------------------------------------

    @commands.hybrid_command(name="trace", description="Find the anime source of a screenshot.")
    async def trace(self, ctx, url: str | None = None):
        if url is not None and not url.lower().startswith(("http://", "https://")):
            await ctx.reply("That doesn't look like a valid image URL.")
            return

        data, thumb_url = await self._resolve_image_source(ctx, url)
        if data is None and thumb_url is None:
            await ctx.reply("Attach an image, reply to an image message, or pass an image URL.")
            return

        # Deliberately not using reply_ephemeral_aware — like captions and aidetect, the
        # result is meant for the whole channel to see, for both . and / invocations. This
        # is an intentional deviation from the hybrid-command convention documented in
        # CLAUDE.md.
        async with ctx.typing():
            try:
                results = await self._search(data=data, url=thumb_url if data is None else None)
            except TraceError as e:
                await ctx.reply(RATE_LIMITED_MESSAGE if str(e) == "rate-limited" else SERVICE_FAILED_MESSAGE)
                return

            if not results:
                await ctx.reply(NO_MATCH_MESSAGE)
                return

            embed, file = await self._build_result(results, ctx.channel)

        if file is not None:
            await ctx.reply(embed=embed, file=file)
        else:
            await ctx.reply(embed=embed)

    # --- Context menu --------------------------------------------------------------

    async def _menu_trace(self, interaction: discord.Interaction, message: discord.Message):
        # Context menus bypass cog_check, so honor the per-guild toggle manually.
        if interaction.guild is not None and not cog_enabled(self.bot, interaction.guild.id, "trace"):
            await interaction.response.send_message("Anime tracing is disabled in this server.", ephemeral=True)
            return

        data = None
        thumb_url = None
        attachment = _first_image_attachment(message.attachments)
        if attachment is not None:
            if attachment.size > MAX_IMAGE_BYTES:
                await interaction.response.send_message(
                    f"That image is too large to trace (max {MAX_IMAGE_BYTES // (1024 * 1024)} MB).",
                    ephemeral=True,
                )
                return
            try:
                data = await attachment.read()
                thumb_url = attachment.url
            except (discord.HTTPException, discord.NotFound):
                await interaction.response.send_message(
                    "Couldn't download that attachment — try again.", ephemeral=True
                )
                return
        else:
            thumb_url = _first_embed_image_url(message.embeds)

        if data is None and thumb_url is None:
            await interaction.response.send_message("That message doesn't have an image.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=False)
        try:
            results = await self._search(data=data, url=thumb_url if data is None else None)
        except TraceError as e:
            await interaction.followup.send(
                RATE_LIMITED_MESSAGE if str(e) == "rate-limited" else SERVICE_FAILED_MESSAGE, ephemeral=True
            )
            return

        if not results:
            await interaction.followup.send(NO_MATCH_MESSAGE)
            return

        embed, file = await self._build_result(results, interaction.channel)
        if file is not None:
            await interaction.followup.send(embed=embed, file=file)
        else:
            await interaction.followup.send(embed=embed)


async def setup(bot):
    await bot.add_cog(TraceMoe(bot))
