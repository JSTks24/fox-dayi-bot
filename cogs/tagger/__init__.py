
from __future__ import annotations

from discord.ext import commands

from .alert import TaggerAlertMixin
from .core import TaggerCoreMixin
from .db import TaggerDBMixin
from .panel import fox14_tag_context


class Fox14Tagger(TaggerAlertMixin, TaggerDBMixin, TaggerCoreMixin, commands.Cog):
    pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Fox14Tagger(bot))
    # ponytail: discord.py 2.6 caps message menus at five per scope; keep this global until a release supports Discord's limit of 15.
    bot.tree.add_command(fox14_tag_context)
