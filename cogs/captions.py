import asyncio
import io
import os
from dataclasses import dataclass

import discord
from discord.ext import commands
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

from .management import cog_enabled, common_error_reply
from .storage import data_path

IMAGE_DIR = data_path(os.path.join("assets", "captions"))
MAX_TEXT_LENGTH = 200


@dataclass(frozen=True)
class Region:
    box: tuple[int, int, int, int]  # (left, top, right, bottom) pixel bounding box
    max_font_size: int = 36
    min_font_size: int = 10
    padding: int = 6
    fill: str = "black"


@dataclass(frozen=True)
class Template:
    name: str
    image_path: str
    regions: tuple[Region, ...]


MAKIMA = Template(
    name="makima",
    image_path=os.path.join(IMAGE_DIR, "Makima.png"),
    regions=(
        Region(box=(55, 45, 235, 245)),
    ),
)

DENJI = Template(
    name="denji",
    image_path=os.path.join(IMAGE_DIR, "denji.png"),
    regions=(
        Region(box=(142, 172, 433, 410)),
    ),
)

NANACHI = Template(
    name="nanachi",
    image_path=os.path.join(IMAGE_DIR, "nanachi.png"),
    regions=(
        Region(box=(489, 0, 638, 426)),  # right bubble, read first in this manga's right-to-left order
        Region(box=(5, 95, 139, 467)),  # left bubble, read second
    ),
)


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    words = text.split()
    lines, current = [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _fit_text(draw: ImageDraw.ImageDraw, text: str, region: Region):
    """Word-wrap and shrink-to-fit `text` within `region`'s padded box. Never raises —
    at the font-size floor it just proceeds and lets the text overflow."""
    left, top, right, bottom = region.box
    max_w = (right - left) - 2 * region.padding
    max_h = (bottom - top) - 2 * region.padding

    size = region.max_font_size
    while True:
        font = ImageFont.load_default(size=size)
        lines = _wrap_text(draw, text, font, max_w)
        wrapped = "\n".join(lines)
        bbox = draw.multiline_textbbox((0, 0), wrapped, font=font, align="center")
        block_w, block_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if (block_w <= max_w and block_h <= max_h) or size <= region.min_font_size:
            break
        size -= 2

    x = left + region.padding + (max_w - block_w) / 2 - bbox[0]
    y = top + region.padding + (max_h - block_h) / 2 - bbox[1]
    return font, wrapped, (x, y)


def _render(base_image: Image.Image, template: Template, texts: list[str]) -> io.BytesIO:
    image = base_image.copy()
    draw = ImageDraw.Draw(image)
    for text, region in zip(texts, template.regions):
        font, wrapped, position = _fit_text(draw, text, region)
        draw.multiline_text(position, wrapped, font=font, fill=region.fill, align="center")
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    buffer.seek(0)
    return buffer


class Captions(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._image_cache: dict[str, Image.Image] = {}

    async def cog_check(self, ctx):
        return ctx.guild is None or cog_enabled(self.bot, ctx.guild.id, "captions")

    async def cog_command_error(self, ctx, error):
        if await common_error_reply(ctx, error):
            return
        raise error

    def _load_base_image(self, template: Template) -> Image.Image:
        cached = self._image_cache.get(template.name)
        if cached is not None:
            return cached
        image = Image.open(template.image_path).convert("RGBA")
        self._image_cache[template.name] = image
        return image

    async def _render_and_send(self, ctx, template: Template, texts: list[str]):
        for text in texts:
            if len(text) > MAX_TEXT_LENGTH:
                await ctx.reply(f"That text is too long for this bubble (max {MAX_TEXT_LENGTH} characters).")
                return

        try:
            base_image = self._load_base_image(template)
        except (FileNotFoundError, UnidentifiedImageError, OSError):
            await ctx.reply("This template's image is missing or unreadable — let the bot owner know.")
            return

        buffer = await asyncio.to_thread(_render, base_image, template, texts)
        file = discord.File(buffer, filename=f"{template.name}.png")
        # Deliberately not using reply_ephemeral_aware — captions are meant to be seen
        # by the channel, not just the invoker, for both . and / invocations. This is an
        # intentional deviation from the hybrid-command convention documented in CLAUDE.md.
        await ctx.reply(file=file)

    @commands.hybrid_command(name="makima", description="Caption the Makima image.")
    async def makima(self, ctx, *, text1: str):
        await self._render_and_send(ctx, MAKIMA, [text1])

    @commands.hybrid_command(name="denji", description="Caption the Denji image.")
    async def denji(self, ctx, *, text1: str):
        await self._render_and_send(ctx, DENJI, [text1])

    @commands.hybrid_command(name="nanachi", description="Caption the Nanachi image (right bubble, left bubble).")
    async def nanachi(self, ctx, text1: str, text2: str):
        await self._render_and_send(ctx, NANACHI, [text1, text2])


async def setup(bot):
    await bot.add_cog(Captions(bot))
