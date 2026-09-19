
from __future__ import annotations

from discord.ext import commands as discord_commands
from dotenv import load_dotenv

from cogs.shared.context_menus import register_guild_scoped_context_menus
from .commands import (
    QuickPunishCommandsMixin,
    quick_punish_context,
    remote_quick_punish_context,
    scheduled_quick_punish_context,
)
from .core import QuickPunishCoreMixin
from .db import QuickPunishDBMixin
from .notify import QuickPunishNotifyMixin
from .vote import QuickPunishVoteMixin

load_dotenv()


class QuickPunishCog(
    QuickPunishCommandsMixin,
    QuickPunishNotifyMixin,
    QuickPunishVoteMixin,
    QuickPunishDBMixin,
    QuickPunishCoreMixin,
    discord_commands.Cog,
):
    pass


async def setup(bot):
    await bot.add_cog(QuickPunishCog(bot))
    register_guild_scoped_context_menus(
        bot.tree,
        [quick_punish_context, remote_quick_punish_context, scheduled_quick_punish_context],
        label="QuickPunish",
    )
