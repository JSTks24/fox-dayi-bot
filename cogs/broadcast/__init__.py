from __future__ import annotations

from discord.ext import commands as discord_commands, tasks
import asyncio
import os
import pytz
import logging

from .commands import BroadcastCommandsMixin
from .scheduler import BroadcastSchedulerMixin
from .storage import BroadcastStorageMixin

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

class BroadcastCog(
    BroadcastCommandsMixin,
    BroadcastSchedulerMixin,
    BroadcastStorageMixin,
    discord_commands.Cog,
):
    def __init__(self, bot):
        self.bot = bot
        self.config: dict[str, dict] = {}
        self.stats: dict[str, dict] = {}
        self.active_tasks: dict[str, tasks.Loop] = {}
        self.config_path = 'broadcast/broadcast_threads.json'
        self.stats_path = 'broadcast/broadcast_stats.json'
        self.lock = asyncio.Lock()  # 防止并发修改
        self.tz_shanghai = pytz.timezone('Asia/Shanghai')
        
        # 确保目录存在
        os.makedirs('broadcast', exist_ok=True)
        
        # 加载配置和状态
        self.load_config()
        self.load_stats()


    async def cog_load(self) -> None:
        """Start background tasks after the cog is fully loaded."""
        if not self.auto_save.is_running():
            self.auto_save.start()
        await self.start_all_tasks()


    def cog_unload(self):
        """Cog 卸载时的清理工作"""
        logger.info("正在卸载 BroadcastCog...")
        
        # 停止所有任务
        for task_name, task_loop in self.active_tasks.items():
            if task_loop.is_running():
                task_loop.cancel()
                logger.info(f"已停止任务: {task_name}")
        
        # 停止自动保存
        if self.auto_save.is_running():
            self.auto_save.cancel()
        
        # 保存最终状态
        self.save_stats()
        logger.info("BroadcastCog 已卸载")


async def setup(bot):
    await bot.add_cog(BroadcastCog(bot))
