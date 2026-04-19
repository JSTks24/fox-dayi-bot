from __future__ import annotations

import discord
from discord.ext import commands
import os
from datetime import datetime, timedelta
import logging
import traceback
from cogs.utils import CooldownManager, TTLCache
from paths import (
    MENTION_KB_DIR,
    MENTION_SETTINGS_FILE,
    MENTION_TEMP_DIR,
    MENTION_THREAD_METADATA_DIR,
    MENTION_THREADS_FILE,
    MENTION_USAGE_STATS_FILE,
    PROMPT_LOG_DIR,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

class MentionCoreMixin:
    def __init__(self, bot):
        self.bot = bot
        self.settings: dict = {}
        self.threads: dict = {}
        self.usage_stats: dict = {}
        
        self.settings_path = str(MENTION_SETTINGS_FILE)
        self.threads_path = str(MENTION_THREADS_FILE)
        self.usage_stats_path = str(MENTION_USAGE_STATS_FILE)
        self.kb_path = str(MENTION_KB_DIR)
        self.prompt_log_path = str(PROMPT_LOG_DIR)
        self.thread_metadata_path = str(MENTION_THREAD_METADATA_DIR)

        # 确保目录存在
        os.makedirs(os.path.dirname(self.settings_path), exist_ok=True)
        os.makedirs(self.kb_path, exist_ok=True)
        os.makedirs(self.prompt_log_path, exist_ok=True)
        os.makedirs(self.thread_metadata_path, exist_ok=True)
        
        # 加载配置
        self.load_settings()
        self.load_threads()
        self.load_usage_stats()
        self.thread_cooldowns = CooldownManager(self.settings.get('thread_cooldown_seconds', 5))
        self.user_cooldowns = CooldownManager(self.settings.get('user_cooldown_seconds', 20))
        self.fail2ban_records = TTLCache[list[datetime]]()
        self.fail2ban_banned = TTLCache[datetime]()
        
        # 临时文件目录
        self.temp_dir = str(MENTION_TEMP_DIR)
        os.makedirs(self.temp_dir, exist_ok=True)
        
        logger.info("MentionCog 已初始化")

    def cog_unload(self):
        """Cog 卸载时的清理工作"""
        logger.info("正在卸载 MentionCog...")
        self.save_usage_stats()
        # 清理临时文件目录
        try:
            if os.path.exists(self.temp_dir):
                import shutil
                shutil.rmtree(self.temp_dir)
                logger.info(f"已清理临时文件目录: {self.temp_dir}")
        except Exception as e:
            logger.warning(f"清理临时文件目录失败: {e}")
        logger.info("MentionCog 已卸载")

    def get_user_permission_level(self, user: discord.Member, thread_id: str) -> str:
        """
        获取用户的权限等级
        返回: 'admin', 'moderator', 'op', 'user', 'none'
        """
        # 检查是否为 admin (从 bot.admins 读取，该列表从 users.db 加载)
        if user.id in self.bot.admins:
            return 'admin'
        
        # 检查是否为 moderator
        moderator_role_ids = [str(rid) for rid in self.settings.get('moderator_role_ids', [])]
        user_role_ids = [str(role.id) for role in user.roles]
        if any(role_id in moderator_role_ids for role_id in user_role_ids):
            return 'moderator'
        
        # 检查是否为 OP (楼主)
        if thread_id in self.threads:
            thread_config = self.threads[thread_id]
            if str(user.id) == str(thread_config.get('ownerID', '')):
                return 'op'
        
        # 检查是否有允许的身份组
        allowed_role_ids = [str(rid) for rid in self.settings.get('allowed_role_ids', [])]
        if any(role_id in allowed_role_ids for role_id in user_role_ids):
            return 'user'
        
        return 'none'

    def check_permission(self, user: discord.Member, thread_id: str, required_level: str) -> bool:
        """
        检查用户是否有所需权限
        required_level: 'admin', 'moderator', 'op', 'user'
        """
        user_level = self.get_user_permission_level(user, thread_id)
        
        # 权限级别排序
        levels = ['none', 'user', 'op', 'moderator', 'admin']
        
        try:
            user_level_index = levels.index(user_level)
            required_level_index = levels.index(required_level)
            return user_level_index >= required_level_index
        except ValueError:
            return False

    def _get_thread_cooldown_seconds(self, thread_id: str) -> int:
        thread_config = self.threads.get(thread_id, {})
        return thread_config.get(
            'xSettings',
            {},
        ).get('thread_cd_seconds', self.settings.get('thread_cooldown_seconds', 5))

    def check_thread_cooldown(self, thread_id: str) -> tuple[bool, int]:
        """
        检查子区冷却
        返回: (is_on_cooldown, remaining_seconds)
        """
        return self.thread_cooldowns.check(thread_id)

    def update_thread_cooldown(self, thread_id: str) -> None:
        """更新子区冷却时间"""
        self.thread_cooldowns.set_cooldown(thread_id, seconds=self._get_thread_cooldown_seconds(thread_id))

    def check_user_cooldown(self, user_id: str) -> tuple[bool, int]:
        """
        检查用户冷却
        返回: (is_on_cooldown, remaining_seconds)
        """
        return self.user_cooldowns.check(user_id)

    def update_user_cooldown(self, user_id: str) -> None:
        """更新用户冷却时间"""
        self.user_cooldowns.set_cooldown(user_id, seconds=self.settings.get('user_cooldown_seconds', 20))

    def check_daily_limit(self, user_id: str) -> tuple[bool, int]:
        """
        检查用户每日请求限制
        返回: (is_exceeded, current_count)
        """
        max_requests = self.settings.get('max_daily_requests_per_user', 100)
        today = datetime.now().strftime('%Y-%m-%d')
        
        if user_id not in self.usage_stats:
            self.usage_stats[user_id] = {}
        
        if today not in self.usage_stats[user_id]:
            self.usage_stats[user_id][today] = 0
        
        current_count = self.usage_stats[user_id][today]
        
        return current_count >= max_requests, current_count

    def increment_daily_count(self, user_id: str) -> None:
        """增加用户每日请求计数"""
        today = datetime.now().strftime('%Y-%m-%d')
        
        if user_id not in self.usage_stats:
            self.usage_stats[user_id] = {}
        
        if today not in self.usage_stats[user_id]:
            self.usage_stats[user_id][today] = 0
        
        self.usage_stats[user_id][today] += 1
        self.save_usage_stats()

    def check_fail2ban(self, user_id: str) -> tuple[bool, int | None]:
        """
        检查用户是否被 fail2ban 封禁
        返回: (is_banned, remaining_minutes)
        """
        ban_until = self.fail2ban_banned.get(user_id)
        if ban_until is None:
            return False, None
        now = datetime.now()
        
        if now < ban_until:
            # 仍在封禁期内
            remaining = (ban_until - now).total_seconds() / 60
            return True, int(remaining) + 1
        else:
            # 封禁已过期，清除记录
            self.fail2ban_banned.delete(user_id)
            self.fail2ban_records.delete(user_id)
            return False, None

    def record_fail2ban_failure(self, user_id: str) -> bool:
        """
        记录用户的失败请求
        返回: 是否触发了 fail2ban 封禁
        """
        now = datetime.now()
        
        # 获取配置
        max_tries = self.settings.get('fail2ban_max_tries', 3)
        min_time_minutes = self.settings.get('fail2ban_min_time_minutes', 2)
        ban_time_minutes = self.settings.get('fail2ban_ban_time_minutes', 60)
        
        cutoff_time = now - timedelta(minutes=min_time_minutes)
        fail_records = self.fail2ban_records.get(user_id, [])
        fail_records = [
            fail_time for fail_time in fail_records
            if fail_time > cutoff_time
        ]
        fail_records.append(now)
        self.fail2ban_records.set(user_id, fail_records, ttl_seconds=min_time_minutes * 60)
        
        # 检查是否达到封禁阈值
        if len(fail_records) >= max_tries:
            # 触发封禁
            ban_until = now + timedelta(minutes=ban_time_minutes)
            self.fail2ban_banned.set(user_id, ban_until, ttl_seconds=ban_time_minutes * 60)
            self.fail2ban_records.delete(user_id)
            logger.warning(f"🚫 用户 {user_id} 触发 fail2ban，封禁至 {ban_until.strftime('%Y-%m-%d %H:%M:%S')}")
            return True
        
        return False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """监听消息，检测是否提及机器人"""
        # 忽略机器人自己的消息
        if message.author.bot:
            return
        
        # 检查全局开关
        if not self.settings.get('global_enabled', False):
            return
        
        # 检查是否提及了机器人
        if self.bot.user not in message.mentions:
            return
        
        # 检查是否在允许的子区中
        thread_id = str(message.channel.id)
        allowed_threads = [str(tid) for tid in self.settings.get('allowed_thread_ids', [])]
        
        if thread_id not in allowed_threads:
            logger.debug(f"子区 {thread_id} 不在允许列表中")
            return
        
        # 处理提及
        await self.handle_mention(message)

    async def handle_mention(self, message: discord.Message):
        """处理提及事件"""
        try:
            thread_id = str(message.channel.id)
            user_id = str(message.author.id)
            
            # 检查用户权限
            if not isinstance(message.author, discord.Member):
                logger.warning(f"用户 {user_id} 不是 Member 对象")
                return
            
            # 检查 fail2ban 状态（在所有检查之前，被封禁的用户不会有任何反应）
            is_banned, remaining_minutes = self.check_fail2ban(user_id)
            if is_banned:
                logger.info(f"用户 {user_id} 被 fail2ban 封禁中，剩余 {remaining_minutes} 分钟，忽略请求")
                return  # 直接返回，不给任何反应
            
            permission_level = self.get_user_permission_level(message.author, thread_id)
            if permission_level == 'none':
                # 记录失败并检查是否触发封禁
                triggered_ban = self.record_fail2ban_failure(user_id)
                if triggered_ban:
                    # 触发封禁，发送带有封禁提示的消息
                    ban_time = self.settings.get('fail2ban_ban_time_minutes', 60)
                    await message.reply(f"❌ 缺少使用该功能的权限。请确保你拥有所需的身份组。\n\n⚠️ 由于请求连续失败，你已被机器人忽略 {ban_time} 分钟。")
                else:
                    await message.reply("❌ 缺少使用该功能的权限。请确保你拥有所需的身份组。")
                logger.info(f"用户 {user_id} 没有权限使用提及功能")
                return
            
            # 检查全局黑名单
            global_blacklist = [str(uid) for uid in self.settings.get('global_blacklisted_user_ids', [])]
            if user_id in global_blacklist:
                # 记录失败并检查是否触发封禁
                triggered_ban = self.record_fail2ban_failure(user_id)
                if triggered_ban:
                    # 触发封禁，发送带有封禁提示的消息
                    ban_time = self.settings.get('fail2ban_ban_time_minutes', 60)
                    await message.reply(f"❌ 你已被管理员封禁，无法使用自助答疑BOT。\n\n⚠️ 由于请求连续失败，你已被机器人忽略 {ban_time} 分钟。")
                else:
                    await message.reply("❌ 你已被管理员封禁，无法使用自助答疑BOT。")
                logger.info(f"用户 {user_id} 在全局黑名单中")
                return
            
            # 检查子区黑名单
            if thread_id in self.threads:
                thread_blacklist = [str(uid) for uid in self.threads[thread_id].get('blacklisted_users_ID', [])]
                if user_id in thread_blacklist:
                    # 记录失败并检查是否触发封禁
                    triggered_ban = self.record_fail2ban_failure(user_id)
                    if triggered_ban:
                        # 触发封禁，发送带有封禁提示的消息
                        ban_time = self.settings.get('fail2ban_ban_time_minutes', 60)
                        await message.reply(f"❌ 你已被楼主封禁，无法在本帖中使用自助答疑BOT。\n\n⚠️ 由于请求连续失败，你已被机器人忽略 {ban_time} 分钟。")
                    else:
                        await message.reply("❌ 你已被楼主封禁，无法在本帖中使用自助答疑BOT。")
                    logger.info(f"用户 {user_id} 在子区 {thread_id} 的黑名单中")
                    return
            
            # 检查子区冷却
            is_thread_cooldown, thread_remaining = self.check_thread_cooldown(thread_id)
            if is_thread_cooldown:
                # 记录失败并检查是否触发封禁
                triggered_ban = self.record_fail2ban_failure(user_id)
                if triggered_ban:
                    # 触发封禁，发送带有封禁提示的消息
                    ban_time = self.settings.get('fail2ban_ban_time_minutes', 60)
                    await message.reply(f"⏰ 该帖子的自助答疑功能冷却中，请稍后再试。\n\n⚠️ 由于请求连续失败，你已被机器人忽略 {ban_time} 分钟。")
                else:
                    await message.reply("⏰ 该帖子的自助答疑功能冷却中，请稍后再试。")
                logger.info(f"子区 {thread_id} 在冷却中")
                return
            
            # 检查用户冷却
            is_user_cooldown, user_remaining = self.check_user_cooldown(user_id)
            if is_user_cooldown:
                # 记录失败并检查是否触发封禁
                triggered_ban = self.record_fail2ban_failure(user_id)
                if triggered_ban:
                    # 触发封禁，发送带有封禁提示的消息
                    ban_time = self.settings.get('fail2ban_ban_time_minutes', 60)
                    await message.reply(f"⏰ 用户的自助答疑功能冷却中，请在 **{user_remaining}** 秒后再试。\n\n⚠️ 由于请求连续失败，你已被机器人忽略 {ban_time} 分钟。")
                else:
                    await message.reply(f"⏰ 用户的自助答疑功能冷却中，请在 **{user_remaining}** 秒后再试。")
                logger.info(f"用户 {user_id} 在冷却中")
                return
            
            # 检查每日限制
            is_exceeded, current_count = self.check_daily_limit(user_id)
            if is_exceeded:
                # 记录失败并检查是否触发封禁
                triggered_ban = self.record_fail2ban_failure(user_id)
                if triggered_ban:
                    # 触发封禁，发送带有封禁提示的消息
                    ban_time = self.settings.get('fail2ban_ban_time_minutes', 60)
                    await message.reply(f"❌ 用户每日请求次数已达上限（{current_count}）。\n\n⚠️ 由于请求连续失败，你已被机器人忽略 {ban_time} 分钟。")
                else:
                    await message.reply(f"❌ 用户每日请求次数已达上限（{current_count}）。")
                logger.info(f"用户 {user_id} 超出每日限制")
                return
            
            # 检查预设回复
            preset_reply = await self.check_preset_reply(message, thread_id)
            if preset_reply:
                await message.reply(preset_reply)
                logger.info(f"使用预设回复处理用户 {user_id} 的提及")
                return
            
            # 更新冷却和计数
            self.update_thread_cooldown(thread_id)
            self.update_user_cooldown(user_id)
            self.increment_daily_count(user_id)
            
            # 调用 AI 生成回复
            await self.generate_ai_response(message, thread_id)
            
        except Exception as e:
            logger.error(f"处理提及时发生错误: {e}")
            logger.error(traceback.format_exc())
            try:
                await message.reply("❌ 处理你的请求时发生错误，请稍后再试。")
            except Exception:
                pass
