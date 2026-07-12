import glob
import os
import random

import discord
from discord.ext import commands

from .management import cog_enabled
from .storage import data_path

FUSSE_TARGET_USER_ID = 1058738968339955782

NUKO_START = "<:nukoHinten:988561883617439784>"
NUKO_MIDDLE = "<:nukoMittel:988561885131599982>"
NUKO_END = "<:nukoVorne:988561886490533978>"
NUKO_MIN_REPEAT = 3
NUKO_MAX_REPEAT = 12

KING_DIR = data_path(os.path.join("assets", "king"))
KING_SELF_GIF = os.path.join(KING_DIR, "self.gif")
KING_KURI_GIF = os.path.join(KING_DIR, "kuri.gif")
KING_GLOB = os.path.join(KING_DIR, "king*.gif")


class Triggers(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_check(self, ctx):
        return ctx.guild is None or cog_enabled(self.bot, ctx.guild.id, "triggers")

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.CheckFailure):
            return
        raise error

    @commands.command(name="füße")
    async def fusse(self, ctx):
        """Mention a specific user."""
        await ctx.reply(f"<@{FUSSE_TARGET_USER_ID}>")

    @commands.command(name="nuko")
    async def nuko(self, ctx):
        """Post a chain of nuko emotes with a random-length middle section."""
        count = round(random.triangular(NUKO_MIN_REPEAT, NUKO_MAX_REPEAT, NUKO_MIN_REPEAT))
        await ctx.reply(NUKO_START + NUKO_MIDDLE * count + NUKO_END)

    @commands.command(name="sex")
    async def sex(self, ctx):
        """Post a YouTube video."""
        await ctx.reply("https://www.youtube.com/watch?v=qzPKgTuRwbs")

    @commands.command(name="king")
    async def king(self, ctx, member: discord.Member = None):
        """Crown yourself or another member King."""
        if member is None:
            await ctx.reply(f"{ctx.author.mention} ist King.", file=discord.File(KING_SELF_GIF))
        elif member.id == self.bot.user.id:
            await ctx.reply(
                "W-was ich? Nein Bruder, du bist der einzig wahre King.",
                file=discord.File(KING_KURI_GIF),
            )
        else:
            gif_path = random.choice(glob.glob(KING_GLOB))
            await ctx.reply(
                f"{ctx.author.mention} ernennt {member.mention} zum King. Wahre Kings meine Freunde!",
                file=discord.File(gif_path),
            )

    @commands.Cog.listener()
    async def on_message(self, message):
        # Prevent the bot from replying to itself
        if message.author == self.bot.user:
            return

        if message.guild and not cog_enabled(self.bot, message.guild.id, "triggers"):
            return

        # Check if "kurisutina" is in the message content (case-insensitive)
        if "kurisutina" in message.content.lower():
            # Reply with "Hör auf mich Kurisutina zu nennen" in cursive (italics), and "Kurisutina" in bold as well
            response = "*Hör auf mich* ***Kurisutina*** *zu nennen!*"
            await message.reply(response)

        # Check if "horny" is in the message content (case-insensitive)
        if "horny" in message.content.lower():
            await message.reply(f"{message.author.mention} ist Horny!")


async def setup(bot):
    await bot.add_cog(Triggers(bot))
