import os

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from .management import cog_enabled, common_error_reply

SIGHTENGINE_URL = "https://api.sightengine.com/1.0/check.json"
AI_HIGH = 0.7
AI_LOW = 0.3
MAX_IMAGE_BYTES = 25 * 1024 * 1024

_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

NOT_CONFIGURED_MESSAGE = (
    "AI detection isn't configured — the bot owner needs to set "
    "SIGHTENGINE_API_USER and SIGHTENGINE_API_SECRET."
)


class DetectorError(Exception):
    """Raised when the Sightengine API call fails or returns a non-success status."""


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


class AIDetect(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.api_user = os.environ.get("SIGHTENGINE_API_USER")
        self.api_secret = os.environ.get("SIGHTENGINE_API_SECRET")
        self._session: aiohttp.ClientSession | None = None

        self._menu = app_commands.ContextMenu(name="Check if AI", callback=self._menu_check)
        self.bot.tree.add_command(self._menu)

    async def cog_unload(self):
        self.bot.tree.remove_command(self._menu.name, type=discord.AppCommandType.message)
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def cog_check(self, ctx):
        return ctx.guild is None or cog_enabled(self.bot, ctx.guild.id, "aidetect")

    async def cog_command_error(self, ctx, error):
        if await common_error_reply(ctx, error):
            return
        raise error

    def _configured(self) -> bool:
        return bool(self.api_user and self.api_secret)

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
            await ctx.reply(f"That image is too large to check (max {MAX_IMAGE_BYTES // (1024 * 1024)} MB).")
            return None, None
        try:
            data = await attachment.read()
        except (discord.HTTPException, discord.NotFound):
            await ctx.reply("Couldn't download that attachment — try again.")
            return None, None
        return data, attachment.url

    # --- Sightengine call -------------------------------------------------------

    async def _score_image(self, *, data: bytes | None, url: str | None) -> float:
        session = self._get_session()
        params = {"api_user": self.api_user, "api_secret": self.api_secret, "models": "genai"}
        try:
            if data is not None:
                form = aiohttp.FormData()
                for key, value in params.items():
                    form.add_field(key, value)
                form.add_field("media", data, filename="image")
                async with session.post(SIGHTENGINE_URL, data=form) as resp:
                    payload = await resp.json()
            else:
                form_params = dict(params)
                form_params["url"] = url
                async with session.post(SIGHTENGINE_URL, data=form_params) as resp:
                    payload = await resp.json()
        except aiohttp.ClientError as e:
            raise DetectorError(str(e)) from e

        if payload.get("status") != "success":
            raise DetectorError(str(payload.get("error", "unknown error")))
        try:
            return float(payload["type"]["ai_generated"])
        except (KeyError, TypeError, ValueError) as e:
            raise DetectorError("malformed response") from e

    # --- Result presentation -----------------------------------------------------

    @staticmethod
    def _build_result_embed(prob: float, thumb_url: str | None) -> discord.Embed:
        if prob >= AI_HIGH:
            verdict = "Likely AI-generated"
            color = discord.Color.red()
        elif prob <= AI_LOW:
            verdict = "Likely authentic"
            color = discord.Color.green()
        else:
            verdict = "Uncertain"
            color = discord.Color.greyple()

        embed = discord.Embed(
            title="AI-image check",
            description=f"**{prob * 100:.0f}%** — {verdict}",
            color=color,
        )
        if thumb_url:
            embed.set_thumbnail(url=thumb_url)
        embed.set_footer(text="Probabilistic estimate from Sightengine — not definitive proof.")
        return embed

    # --- Command -----------------------------------------------------------------

    @commands.hybrid_command(name="aicheck", description="Estimate whether an image is AI-generated.")
    async def aicheck(self, ctx, url: str | None = None):
        if not self._configured():
            await ctx.reply(NOT_CONFIGURED_MESSAGE)
            return

        if url is not None and not url.lower().startswith(("http://", "https://")):
            await ctx.reply("That doesn't look like a valid image URL.")
            return

        data, thumb_url = await self._resolve_image_source(ctx, url)
        if data is None and thumb_url is None:
            await ctx.reply("Attach an image, reply to an image message, or pass an image URL.")
            return

        # Deliberately not using reply_ephemeral_aware — like captions, the result is
        # meant for the whole channel to see, for both . and / invocations. This is an
        # intentional deviation from the hybrid-command convention documented in CLAUDE.md.
        async with ctx.typing():
            try:
                prob = await self._score_image(data=data, url=thumb_url if data is None else None)
            except DetectorError:
                await ctx.reply("The AI-detector service failed — try again in a moment.")
                return

        embed = self._build_result_embed(prob, thumb_url)
        await ctx.reply(embed=embed)

    # --- Context menu --------------------------------------------------------------

    async def _menu_check(self, interaction: discord.Interaction, message: discord.Message):
        # Context menus bypass cog_check, so honor the per-guild toggle manually.
        if interaction.guild is not None and not cog_enabled(self.bot, interaction.guild.id, "aidetect"):
            await interaction.response.send_message("AI detection is disabled in this server.", ephemeral=True)
            return

        if not self._configured():
            await interaction.response.send_message(NOT_CONFIGURED_MESSAGE, ephemeral=True)
            return

        data = None
        thumb_url = None
        attachment = _first_image_attachment(message.attachments)
        if attachment is not None:
            if attachment.size > MAX_IMAGE_BYTES:
                await interaction.response.send_message(
                    f"That image is too large to check (max {MAX_IMAGE_BYTES // (1024 * 1024)} MB).",
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
            prob = await self._score_image(data=data, url=thumb_url if data is None else None)
        except DetectorError:
            await interaction.followup.send("The AI-detector service failed — try again in a moment.", ephemeral=True)
            return

        embed = self._build_result_embed(prob, thumb_url)
        await interaction.followup.send(embed=embed)


async def setup(bot):
    await bot.add_cog(AIDetect(bot))
