
from __future__ import annotations

from discord.ext import commands as discord_commands
from dotenv import load_dotenv

from .commands import QuickPunishCommandsMixin, quick_punish_context, remote_quick_punish_context
from .core import QuickPunishCoreMixin
from .db import QuickPunishDBMixin
from .notify import QuickPunishNotifyMixin

load_dotenv()


class QuickPunishCog(
    QuickPunishCommandsMixin,
    QuickPunishNotifyMixin,
    QuickPunishDBMixin,
    QuickPunishCoreMixin,
    discord_commands.Cog,
):
    pass


async def setup(bot):
    await bot.add_cog(QuickPunishCog(bot))
    bot.tree.add_command(quick_punish_context)
    bot.tree.add_command(remote_quick_punish_context)
