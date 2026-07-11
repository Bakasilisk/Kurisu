import discord
from discord.ext import commands

from .management import reply_ephemeral_aware

FIELD_VALUE_LIMIT = 1024


class Help(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @staticmethod
    async def _reply(ctx, *args, **kwargs):
        """ctx.reply, but ephemeral (visible only to the invoker) when the
        command was invoked via / rather than the text prefix."""
        return await reply_ephemeral_aware(ctx, *args, **kwargs)

    @staticmethod
    def _line(command: commands.Command) -> str:
        text = f"`.{command.qualified_name}`"
        if isinstance(command, (commands.HybridCommand, commands.HybridGroup)):
            text += f" / `/{command.qualified_name}`"
        return f"{text} — {command.short_doc or 'No description.'}"

    @commands.hybrid_command(name="help", description="Show the commands you can use here, grouped by category.")
    async def help_command(self, ctx):
        """Show the commands you can use here, grouped by category."""
        by_cog: dict[str, list[commands.Command]] = {}
        for command in self.bot.walk_commands():
            if command.hidden:
                continue
            try:
                allowed = await command.can_run(ctx)
            except commands.CommandError:
                continue
            if not allowed:
                continue
            by_cog.setdefault(command.cog_name or "Other", []).append(command)

        embed = discord.Embed(title="📖 Commands", color=discord.Color.blurple())

        if not by_cog:
            embed.description = "Nothing to show."
            await self._reply(ctx, embed=embed)
            return

        for cog_name in sorted(by_cog):
            commands_ = sorted(by_cog[cog_name], key=lambda c: c.qualified_name)
            value = "\n".join(self._line(command) for command in commands_)
            if len(value) > FIELD_VALUE_LIMIT:
                value = value[: FIELD_VALUE_LIMIT - 1] + "…"
            embed.add_field(name=cog_name, value=value, inline=False)

        await self._reply(ctx, embed=embed)


async def setup(bot):
    await bot.add_cog(Help(bot))
