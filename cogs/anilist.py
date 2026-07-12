import aiohttp
import discord
from discord.ext import commands

from .management import cog_enabled, common_error_reply

ANILIST_URL = "https://graphql.anilist.co"

MEDIA_QUERY = """
query ($search: String, $type: MediaType) {
  Media(search: $search, type: $type) {
    id
    siteUrl
    title { romaji english native }
    coverImage { large }
    averageScore
    format
    episodes
    chapters
    status
    genres
    isAdult
  }
}
"""

FORMAT_LABELS = {
    "TV": "TV",
    "TV_SHORT": "TV Short",
    "MOVIE": "Movie",
    "SPECIAL": "Special",
    "OVA": "OVA",
    "ONA": "ONA",
    "MUSIC": "Music",
    "MANGA": "Manga",
    "NOVEL": "Novel",
    "ONE_SHOT": "One Shot",
}

NO_MATCH_MESSAGE = "No match found on AniList — check the spelling or try a different title."
RATE_LIMITED_MESSAGE = "AniList is rate-limiting requests right now — try again in a bit."
SERVICE_FAILED_MESSAGE = "The AniList service failed — try again in a moment."


class AniListError(Exception):
    """Raised when the AniList API call fails or returns a non-success status."""


class AniList(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._session: aiohttp.ClientSession | None = None

    async def cog_unload(self):
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def cog_check(self, ctx):
        return ctx.guild is None or cog_enabled(self.bot, ctx.guild.id, "anilist")

    async def cog_command_error(self, ctx, error):
        if await common_error_reply(ctx, error):
            return
        raise error

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    # --- AniList call --------------------------------------------------------

    async def _fetch_media(self, title: str, media_type: str) -> dict | None:
        """Returns the matching Media dict, or None if AniList found no match.
        AniList responds with HTTP 404 (not an error) and {"data":{"Media":null}}
        on no match, so 404 is treated the same as 200 here."""
        session = self._get_session()
        try:
            async with session.post(
                ANILIST_URL,
                json={"query": MEDIA_QUERY, "variables": {"search": title, "type": media_type}},
            ) as resp:
                if resp.status == 429:
                    raise AniListError("rate-limited")
                if resp.status not in (200, 404):
                    raise AniListError(f"HTTP {resp.status}")
                payload = await resp.json()
        except aiohttp.ClientError as e:
            raise AniListError(str(e)) from e

        return (payload.get("data") or {}).get("Media")

    # --- Result presentation --------------------------------------------------

    @staticmethod
    def _pick_title(title_obj: dict) -> str:
        return title_obj.get("romaji") or title_obj.get("english") or title_obj.get("native") or "Unknown title"

    def _build_embed(self, media: dict, media_type: str, channel) -> discord.Embed:
        title_obj = media.get("title") or {}
        title = self._pick_title(title_obj)
        english = title_obj.get("english")

        embed = discord.Embed(title=title, color=discord.Color.blurple())
        site_url = media.get("siteUrl")
        if site_url:
            embed.url = site_url
        if english and english != title:
            embed.description = english

        score = media.get("averageScore")
        embed.add_field(name="Score", value=f"{score}/100" if score is not None else "?")

        format_key = media.get("format")
        embed.add_field(name="Format", value=FORMAT_LABELS.get(format_key, format_key or "?"))

        if media_type == "ANIME":
            episodes = media.get("episodes")
            embed.add_field(name="Episodes", value=str(episodes) if episodes is not None else "?")
        else:
            chapters = media.get("chapters")
            embed.add_field(name="Chapters", value=str(chapters) if chapters is not None else "?")

        status = media.get("status")
        embed.add_field(name="Status", value=status.replace("_", " ").title() if status else "?")

        genres = media.get("genres") or []
        if genres:
            embed.add_field(name="Genres", value=", ".join(genres), inline=False)

        embed.set_footer(text="via AniList")

        is_adult = bool(media.get("isAdult"))
        channel_nsfw = bool(getattr(channel, "nsfw", False))
        cover_url = (media.get("coverImage") or {}).get("large")
        if is_adult and not channel_nsfw:
            embed.add_field(name="Cover", value="Adult title — cover hidden in this channel.", inline=False)
        elif cover_url:
            embed.set_thumbnail(url=cover_url)

        return embed

    # --- Shared lookup ---------------------------------------------------------

    async def _lookup(self, ctx, title: str, media_type: str):
        # Deliberately not using reply_ephemeral_aware — like captions, aidetect, and trace,
        # the result is meant for the whole channel to see, for both . and / invocations.
        # This is an intentional deviation from the hybrid-command convention documented
        # in CLAUDE.md.
        async with ctx.typing():
            try:
                media = await self._fetch_media(title, media_type)
            except AniListError as e:
                await ctx.reply(RATE_LIMITED_MESSAGE if str(e) == "rate-limited" else SERVICE_FAILED_MESSAGE)
                return

            if media is None:
                await ctx.reply(NO_MATCH_MESSAGE)
                return

            embed = self._build_embed(media, media_type, ctx.channel)

        await ctx.reply(embed=embed)

    # --- Commands ----------------------------------------------------------------

    @commands.hybrid_command(name="anime", description="Look up an anime on AniList.")
    async def anime(self, ctx, *, title: str):
        """Look up an anime on AniList."""
        await self._lookup(ctx, title, "ANIME")

    @commands.hybrid_command(name="manga", description="Look up a manga on AniList.")
    async def manga(self, ctx, *, title: str):
        """Look up a manga on AniList."""
        await self._lookup(ctx, title, "MANGA")


async def setup(bot):
    await bot.add_cog(AniList(bot))
