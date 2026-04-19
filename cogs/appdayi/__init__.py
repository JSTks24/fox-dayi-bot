
from __future__ import annotations

import asyncio

from discord.ext import commands

from .core import AppDayiCoreMixin
from .reply_chain import AppDayiReplyChainMixin


class AppDayi(AppDayiReplyChainMixin, AppDayiCoreMixin, commands.Cog):
    pass


__all__ = ["AppDayi", "asyncio", "setup"]


async def setup(bot: commands.Bot):
    if not getattr(bot, "openai_client", None):
        print("⚠️ [AppDayi] bot.openai_client 未初始化，相关功能将不可用。")
    await bot.add_cog(AppDayi(bot))
