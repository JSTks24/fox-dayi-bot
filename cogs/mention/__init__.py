
from __future__ import annotations

from discord.ext import commands

from .ai import MentionAIMixin
from .config import MentionConfigMixin
from .core import MentionCoreMixin
from .kb import MentionKBMixin, THREAD_METADATA_CACHE_TTL_SECONDS
from .preset import MentionPresetMixin


class MentionCog(
    MentionPresetMixin,
    MentionConfigMixin,
    MentionAIMixin,
    MentionKBMixin,
    MentionCoreMixin,
    commands.Cog,
):
    pass


__all__ = ["MentionCog", "THREAD_METADATA_CACHE_TTL_SECONDS", "setup"]


async def setup(bot):
    await bot.add_cog(MentionCog(bot))
