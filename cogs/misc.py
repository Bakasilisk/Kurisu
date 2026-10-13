import discord
from discord.ext import commands

from .management import cog_enabled


class Misc(commands.Cog):
    """Small standalone commands that don't belong to any other cog."""

    def __init__(self, bot):
        self.bot = bot

    async def cog_check(self, ctx):
        return ctx.guild is None or cog_enabled(self.bot, ctx.guild.id, "misc")

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.BadArgument):
            await ctx.reply("Nutzer nicht gefunden. Ich hab nachgesehen — den gibt es nicht.")
            return
        if isinstance(error, commands.CheckFailure):
            return
        raise error

    @commands.command(name="avi", aliases=["avatar", "pfp"])
    async def avi(self, ctx, user: discord.Member | discord.User = None):
        """Post a user's profile picture (yours by default)."""
        target = user or ctx.author
        avatar = target.display_avatar.with_size(4096)
        embed = discord.Embed(title=f"Avatar von {target.display_name}", url=avatar.url)
        if isinstance(target, discord.Member) and target.color.value:
            embed.color = target.color
        embed.set_image(url=avatar.url)
        await ctx.reply(embed=embed)


async def setup(bot):
    await bot.add_cog(Misc(bot))
