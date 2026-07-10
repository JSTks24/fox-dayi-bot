
from __future__ import annotations

from discord.ext import commands

from cogs.shared.context_menus import register_guild_scoped_context_menus
from .alert import TaggerAlertMixin
from .core import TaggerCoreMixin
from .db import TaggerDBMixin
from .panel import fox14_tag_context


class Fox14Tagger(TaggerAlertMixin, TaggerDBMixin, TaggerCoreMixin, commands.Cog):
    pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Fox14Tagger(bot))
    register_guild_scoped_context_menus(
        bot.tree,
        [fox14_tag_context],
        label="Fox14Tagger",
    )
