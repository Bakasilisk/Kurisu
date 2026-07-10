from discord.ext import commands


class Triggers(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message):
        # Prevent the bot from replying to itself
        if message.author == self.bot.user:
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
