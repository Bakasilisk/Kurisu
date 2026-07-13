import discord
from discord.ext import commands

from .management import reply_ephemeral_aware


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

    async def _accessible_by_cog(self, ctx) -> dict[str, list[commands.Command]]:
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
        return by_cog

    @commands.hybrid_command(
        name="help",
        description="List the cogs you can use here, or view one cog's commands.",
    )
    @discord.app_commands.describe(cog="A cog to show commands for, e.g. Moderation.")
    async def help_command(self, ctx, cog: str = None):
        """List the cogs you can use here, or view one cog's commands."""
        by_cog = await self._accessible_by_cog(ctx)

        if not by_cog:
            embed = discord.Embed(title="📖 Commands", description="Nothing to show.", color=discord.Color.blurple())
            await self._reply(ctx, embed=embed)
            return

        if cog is None:
            names = sorted(by_cog)
            description = "\n".join(f"- {name}" for name in names)
            description += "\n\nUse `.help <cog>` / `/help cog:<cog>` to view a cog's commands."
            embed = discord.Embed(title="📖 Commands", description=description, color=discord.Color.blurple())
            await self._reply(ctx, embed=embed)
            return

        match = next((name for name in by_cog if name.lower() == cog.lower()), None)
        if match is None:
            embed = discord.Embed(
                title="📖 Commands",
                description=f"No cog named `{cog}`. Available: " + ", ".join(sorted(by_cog)),
                color=discord.Color.red(),
            )
            await self._reply(ctx, embed=embed)
            return

        commands_ = sorted(by_cog[match], key=lambda c: c.qualified_name)
        value = "\n".join(self._line(command) for command in commands_)
        embed = discord.Embed(title=f"📖 {match} Commands", description=value, color=discord.Color.blurple())
        await self._reply(ctx, embed=embed)


async def setup(bot):
    await bot.add_cog(Help(bot))
