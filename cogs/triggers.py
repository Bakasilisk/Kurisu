from discord.ext import commands

from .management import cog_enabled

FUSSE_TARGET_USER_ID = 1058738968339955782


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
