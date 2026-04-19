"""
Discord 提及反应 Cog
当机器人被@提及时，根据配置自动回复
"""

import discord
from discord.ext import commands
from discord import app_commands
import json
import asyncio
import os
from datetime import datetime, timedelta
import logging
import traceback
import tiktoken
import time
from cogs.utils import CooldownManager, TTLCache, compress_image, encode_image_to_base64, get_file_size_kb

# 设置日志
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

THREAD_METADATA_CACHE_TTL_SECONDS = 24 * 60 * 60

class MentionCog(commands.Cog):
    """提及反应 Cog，支持自动回复和知识库"""
    
    def __init__(self, bot):
        self.bot = bot
        self.settings: dict = {}
        self.threads: dict = {}
        self.usage_stats: dict = {}
        
        self.settings_path = 'mention/settings.json'
        self.threads_path = 'mention/threads.json'
        self.usage_stats_path = 'mention/usage_stats.json'
        self.kb_path = 'mention/kb'
        self.prompt_log_path = 'mention/promptLog'
        self.thread_metadata_path = 'mention/threadsMetadata'
        
        # 确保目录存在
        os.makedirs('mention', exist_ok=True)
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
        self.temp_dir = 'mention_temp'
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
    
    def load_settings(self) -> None:
        """加载全局设置文件"""
        try:
            if os.path.exists(self.settings_path):
                with open(self.settings_path, encoding='utf-8') as f:
                    self.settings = json.load(f)
                logger.info("已加载全局设置")
            else:
                logger.warning(f"设置文件不存在: {self.settings_path}，使用默认设置")
                self.settings = {
                    "global_enabled": False,
                    "allowed_thread_ids": [],
                    "moderator_role_ids": [],
                    "allowed_role_ids": [],
                    "global_blacklisted_user_ids": [],
                    "thread_cooldown_seconds": 5,
                    "user_cooldown_seconds": 20,
                    "max_daily_requests_per_user": 100,
                    "read_reply_history_depth": 5,
                    "log_save_days": 1
                }
                self.save_settings()
        except Exception as e:
            logger.error(f"加载设置文件失败: {e}")
            self.settings = {}
    
    def save_settings(self) -> None:
        """保存全局设置到文件"""
        try:
            with open(self.settings_path, 'w', encoding='utf-8') as f:
                json.dump(self.settings, f, indent=4, ensure_ascii=False)
            logger.debug("全局设置已保存")
        except Exception as e:
            logger.error(f"保存设置文件失败: {e}")
    
    def load_threads(self) -> None:
        """加载子区配置文件"""
        try:
            if os.path.exists(self.threads_path):
                with open(self.threads_path, encoding='utf-8') as f:
                    self.threads = json.load(f)
                logger.info(f"已加载 {len(self.threads)} 个子区配置")
            else:
                logger.warning(f"子区配置文件不存在: {self.threads_path}")
                self.threads = {}
        except Exception as e:
            logger.error(f"加载子区配置文件失败: {e}")
            self.threads = {}
    
    def save_threads(self) -> None:
        """保存子区配置到文件"""
        try:
            with open(self.threads_path, 'w', encoding='utf-8') as f:
                json.dump(self.threads, f, indent=4, ensure_ascii=False)
            logger.debug("子区配置已保存")
        except Exception as e:
            logger.error(f"保存子区配置文件失败: {e}")
    
    def load_usage_stats(self) -> None:
        """加载使用统计数据"""
        try:
            if os.path.exists(self.usage_stats_path):
                with open(self.usage_stats_path, encoding='utf-8') as f:
                    self.usage_stats = json.load(f)
                logger.info("已加载使用统计数据")
                # 清理过期数据
                self.cleanup_old_stats()
            else:
                logger.info(f"统计文件不存在，创建新文件: {self.usage_stats_path}")
                self.usage_stats = {}
                self.save_usage_stats()
        except Exception as e:
            logger.error(f"加载统计文件失败: {e}")
            self.usage_stats = {}
    
    def save_usage_stats(self) -> None:
        """保存使用统计数据到文件"""
        try:
            with open(self.usage_stats_path, 'w', encoding='utf-8') as f:
                json.dump(self.usage_stats, f, indent=4, ensure_ascii=False)
            logger.debug("使用统计数据已保存")
        except Exception as e:
            logger.error(f"保存统计文件失败: {e}")
    
    def cleanup_old_stats(self) -> None:
        """清理过期的统计数据"""
        try:
            log_save_days = self.settings.get('log_save_days', 1)
            cutoff_date = (datetime.now() - timedelta(days=log_save_days)).strftime('%Y-%m-%d')
            
            for user_id in list(self.usage_stats.keys()):
                user_data = self.usage_stats[user_id]
                # 清理过期的日期记录
                for date in list(user_data.keys()):
                    if date < cutoff_date:
                        del user_data[date]
                # 如果用户没有任何记录了，删除用户
                if not user_data:
                    del self.usage_stats[user_id]
            
            logger.info(f"已清理 {log_save_days} 天前的统计数据")
        except Exception as e:
            logger.error(f"清理统计数据失败: {e}")
    
    # ===== 权限检查 =====
    
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
    
    # ===== 冷却检查 =====

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
    
    # ===== fail2ban 功能 =====
    
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
    
    # ===== 提及事件处理 =====
    
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
    
    async def check_preset_reply(self, message: discord.Message, thread_id: str) -> str | None:
        """
        检查是否匹配预设回复
        返回: 预设回复内容，或 None
        """
        if thread_id not in self.threads:
            return None
        
        thread_config = self.threads[thread_id]
        presets = thread_config.get('xSettings', {}).get('preset', [])
        
        # 移除提及部分
        content = message.content
        for mention in message.mentions:
            content = content.replace(f'<@{mention.id}>', '').replace(f'<@!{mention.id}>', '')
        content = content.strip()
        
        # 检查每个预设
        for preset in presets:
            if len(preset) < 3:
                continue
            
            whitelist = preset[0]
            blacklist = preset[1]
            reply = preset[2]
            
            # 检查白名单和黑名单
            if whitelist in content and (not blacklist or blacklist not in content):
                return reply
        
        return None
    
    async def generate_ai_response(self, message: discord.Message, thread_id: str):
        """生成AI回复（流式）"""
        temp_files = []  # 用于跟踪需要清理的临时文件
        
        try:
            # 发送处理中的消息
            processing_msg = await message.reply("🤔 Vula 思考中...")
            
            # 获取消息内容、上下文和图片
            user_message_content, context_messages, image_paths = await self.extract_message_context(message, thread_id)
            temp_files.extend(image_paths)  # 记录原始图片路径
            
            # 如果有图片，进行压缩
            compressed_image_paths = []
            if image_paths:
                logger.info(f"📸 检测到 {len(image_paths)} 张图片，开始压缩...")
                for img_path in image_paths:
                    compressed_path = await compress_image(img_path)
                    compressed_image_paths.append(compressed_path)
                    if compressed_path != img_path:
                        temp_files.append(compressed_path)  # 记录压缩后的图片路径
                logger.info("✅ 图片压缩完成")
            
            # 构建提示词
            system_prompt = await self.build_prompt(thread_id, context_messages)
            
            # 调用OpenAI API
            client = self.bot.openai_client
            if not client:
                await processing_msg.edit(content="❌ OpenAI客户端未初始化")
                return
            
            # 构建用户消息内容（支持多模态）
            if compressed_image_paths:
                # 有图片：构建多模态消息
                user_content = [{"type": "text", "text": user_message_content}]
                for img_path in compressed_image_paths:
                    size_kb = get_file_size_kb(img_path)
                    logger.info(f"📎 添加图片到API请求: {os.path.basename(img_path)} ({size_kb:.2f}KB)")
                    base64_image = encode_image_to_base64(img_path)
                    user_content.append({
                        "type": "image_url",
                        "image_url": {"url": base64_image}
                    })
                
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}
                ]
            else:
                # 纯文本消息
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message_content}
                ]
            
            # 获取模型名称和编码器
            model_name = os.getenv("OPENAI_MODEL", "gpt-4")
            try:
                encoding = tiktoken.encoding_for_model(model_name)
            except KeyError:
                # 如果模型不支持，使用默认编码器
                encoding = tiktoken.get_encoding("cl100k_base")
            
            # 计算输入token数
            input_tokens = 0
            for msg in messages:
                if isinstance(msg["content"], str):
                    input_tokens += len(encoding.encode(msg["content"]))
                elif isinstance(msg["content"], list):
                    # 多模态消息，只计算文本部分
                    for item in msg["content"]:
                        if item.get("type") == "text":
                            input_tokens += len(encoding.encode(item["text"]))
            
            # 记录开始时间
            start_time = time.time()
            
            # 流式调用 API
            stream = await client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=1.0,
                stream=True
            )
            
            # 处理流式响应
            ai_response = ""
            loop = asyncio.get_running_loop()
            last_update_time = loop.time()
            has_started_output = False
            
            async for chunk in stream:
                if chunk.choices and len(chunk.choices) > 0:
                    delta = chunk.choices[0].delta
                    if delta.content:
                        ai_response += delta.content
                        
                        # 第一次收到内容时，标记已开始输出
                        if not has_started_output:
                            has_started_output = True
                            last_update_time = loop.time()
                        
                        # 根据配置的间隔更新消息
                        stream_interval = self.settings.get('stream_interval', 5)
                        current_time = loop.time()
                        if current_time - last_update_time >= stream_interval:
                            try:
                                # 限制显示长度，避免超过Discord消息限制
                                display_text = ai_response[:2000] if len(ai_response) <= 2000 else ai_response[:2000]
                                await processing_msg.edit(content=display_text)
                                last_update_time = current_time
                            except discord.errors.HTTPException as e:
                                # 如果编辑失败（比如内容太长），记录但继续
                                logger.warning(f"更新流式消息失败: {e}")
            
            # 计算结束时间和统计信息
            end_time = time.time()
            elapsed_time = end_time - start_time
            
            # 计算输出token数
            output_tokens = len(encoding.encode(ai_response))
            
            # 计算平均每秒输出token量
            tokens_per_second = output_tokens / elapsed_time if elapsed_time > 0 else 0
            
            # 构建统计信息（使用Discord的-#语法显示小字）
            stats_text = f"\n\n-# 输入: {input_tokens} | 输出: {output_tokens} | 用时: {elapsed_time:.2f}s | 速度: {tokens_per_second:.1f}"
            
            # 流式响应完成后，编辑成最终版本
            if not ai_response:
                await processing_msg.edit(content="❌ API返回空响应")
                return
            
            # 编辑成最终完整回复（包含统计信息）
            # 如果回复太长，需要分段发送
            final_content = ai_response + stats_text
            if len(final_content) <= 2000:
                await processing_msg.edit(content=final_content)
            else:
                # 如果加上统计信息后超长，尝试只在最后一段加统计信息
                if len(ai_response) <= 2000:
                    # AI回复本身不超长，但加上统计信息后超长
                    # 尝试缩短统计信息或分段
                    await processing_msg.edit(content=ai_response[:2000])
                    remaining = ai_response[2000:] + stats_text
                    chunks = [remaining[i:i+2000] for i in range(0, len(remaining), 2000)]
                    for chunk in chunks:
                        await processing_msg.reply(chunk)
                else:
                    # AI回复本身就超长
                    # 第一条消息编辑为前2000字符
                    await processing_msg.edit(content=ai_response[:2000])
                    # 剩余内容作为回复发送，统计信息放在最后一段
                    remaining = ai_response[2000:]
                    chunks = [remaining[i:i+2000] for i in range(0, len(remaining), 2000)]
                    for i, chunk in enumerate(chunks):
                        if i == len(chunks) - 1:
                            # 最后一段，加上统计信息
                            final_chunk = chunk + stats_text
                            if len(final_chunk) <= 2000:
                                await processing_msg.reply(final_chunk)
                            else:
                                # 如果最后一段加上统计信息后还是超长，分成两段
                                await processing_msg.reply(chunk)
                                await processing_msg.reply(stats_text)
                        else:
                            await processing_msg.reply(chunk)
            
            logger.info(f"成功生成AI回复 (thread: {thread_id}, user: {message.author.id}, length: {len(ai_response)})")
            
        except asyncio.TimeoutError:
            await processing_msg.edit(content="⏱️ 处理超时，请稍后再试")
            logger.warning(f"AI生成超时 (thread: {thread_id})")
        except Exception as e:
            logger.error(f"AI生成失败: {e}")
            logger.error(traceback.format_exc())
            try:
                await processing_msg.edit(content=f"❌ 生成失败: {str(e)}")
            except Exception:
                pass
        finally:
            # 清理临时文件
            for temp_file in temp_files:
                try:
                    if temp_file and os.path.exists(temp_file):
                        os.remove(temp_file)
                        logger.debug(f"🗑️ 已删除临时文件: {os.path.basename(temp_file)}")
                except Exception as e:
                    logger.warning(f"删除临时文件失败 {temp_file}: {e}")
    
    async def extract_message_context(self, message: discord.Message, thread_id: str) -> tuple[str, list[str], list[str]]:
        """
        提取消息内容和上下文
        返回: (用户消息文本, 上下文消息列表, 图片路径列表)
        """
        # 移除@机器人的部分
        user_message = message.content
        for mention in message.mentions:
            user_message = user_message.replace(f'<@{mention.id}>', '').replace(f'<@!{mention.id}>', '')
        user_message = user_message.strip()
        
        # 如果没有文本内容，使用默认提示
        if not user_message:
            user_message = "请帮我看看这个问题"
        
        # 处理当前消息的图片附件
        image_paths = []
        image_attachments = [att for att in message.attachments if att.content_type and att.content_type.startswith('image/')]
        
        if image_attachments:
            logger.info(f"📸 检测到当前消息 {len(image_attachments)} 张图片附件")
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            user_id = message.author.id
            
            for idx, attachment in enumerate(image_attachments):
                try:
                    # 保存图片到临时目录
                    _, ext = os.path.splitext(attachment.filename)
                    temp_path = os.path.join(self.temp_dir, f"{timestamp}_{user_id}_{idx}{ext}")
                    await attachment.save(temp_path)
                    image_paths.append(temp_path)
                    logger.info(f"  保存当前消息图片 {idx+1}: {attachment.filename} ({attachment.size / 1024:.2f} KB)")
                except Exception as e:
                    logger.error(f"保存图片附件失败: {e}")
        
        context_messages = []
        
        # 获取被回复的消息
        if message.reference and message.reference.message_id:
            try:
                replied_message = await message.channel.fetch_message(message.reference.message_id)
                replied_content = replied_message.content if replied_message.content else "[无文字内容]"
                
                # 处理被回复消息的图片附件
                replied_image_attachments = [att for att in replied_message.attachments if att.content_type and att.content_type.startswith('image/')]
                if replied_image_attachments:
                    logger.info(f"📸 检测到被回复消息 {len(replied_image_attachments)} 张图片附件")
                    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                    replied_user_id = replied_message.author.id
                    
                    for idx, attachment in enumerate(replied_image_attachments):
                        try:
                            # 保存被回复消息的图片到临时目录
                            _, ext = os.path.splitext(attachment.filename)
                            temp_path = os.path.join(self.temp_dir, f"{timestamp}_replied_{replied_user_id}_{idx}{ext}")
                            await attachment.save(temp_path)
                            image_paths.append(temp_path)
                            logger.info(f"  保存被回复消息图片 {idx+1}: {attachment.filename} ({attachment.size / 1024:.2f} KB)")
                        except Exception as e:
                            logger.error(f"保存被回复消息图片附件失败: {e}")
                    
                    # 在上下文中标注图片数量
                    replied_content += f" [包含{len(replied_image_attachments)}张图片]" if replied_content else f"[包含{len(replied_image_attachments)}张图片]"
                
                #检查是否有其他附件
                other_attachments = [att for att in replied_message.attachments if not (att.content_type and att.content_type.startswith('image/'))]
                if other_attachments:
                    replied_content += f" [其他附件×{len(other_attachments)}]"
                
                context_messages.append(f"[被回复的消息] {replied_message.author.display_name}: {replied_content}")
            except Exception as e:
                logger.warning(f"获取被回复消息失败: {e}")
        
        # 获取历史消息
        thread_config = self.threads.get(thread_id, {})
        history_depth = thread_config.get('xSettings', {}).get('read_user_interaction_history', 
                                                                self.settings.get('read_reply_history_depth', 5))
        
        if history_depth > 0:
            try:
                # 获取当前消息之前的n条消息
                history = []
                async for hist_msg in message.channel.history(limit=history_depth + 1, before=message):
                    if not hist_msg.author.bot or hist_msg.author.id == self.bot.user.id:
                        msg_content = self._extract_message_text_with_attachments(hist_msg)
                        # 处理 embed 消息
                        if hist_msg.embeds:
                            embed_texts = self._extract_embed_content(hist_msg.embeds)
                            if embed_texts:
                                msg_content += " " + " ".join(embed_texts)
                        history.append(f"{hist_msg.author.display_name}: {msg_content}")
                
                # 反转顺序（从旧到新）
                history.reverse()
                context_messages.extend(history)
                
            except Exception as e:
                logger.warning(f"获取历史消息失败: {e}")
        
        return user_message, context_messages, image_paths
    
    def _extract_message_text_with_attachments(self, message: discord.Message) -> str:
        """
        提取消息的文本内容，如果有图片附件则标注
        
        Args:
            message: Discord消息对象
            
        Returns:
            处理后的文本内容
        """
        content = message.content if message.content else ""
        
        # 检查是否有图片附件
        image_attachments = [att for att in message.attachments if att.content_type and att.content_type.startswith('image/')]
        if image_attachments:
            if content:
                content += f" [图片附件×{len(image_attachments)}]"
            else:
                content = f"[图片附件×{len(image_attachments)}]"
        
        # 检查是否有其他附件
        other_attachments = [att for att in message.attachments if not (att.content_type and att.content_type.startswith('image/'))]
        if other_attachments:
            if content:
                content += f" [其他附件×{len(other_attachments)}]"
            else:
                content = f"[其他附件×{len(other_attachments)}]"
        
        return content if content else "[空消息]"
    
    def _extract_embed_content(self, embeds: list[discord.Embed]) -> list[str]:
        """
        提取 embed 消息的文本内容
        
        Args:
            embeds: Embed对象列表
            
        Returns:
            提取的文本内容列表
        """
        embed_texts = []
        
        for embed in embeds:
            parts = []
            
            # 提取标题
            if embed.title:
                parts.append(f"[Embed标题: {embed.title}]")
            
            # 提取描述
            if embed.description:
                # 限制描述长度，避免过长
                desc = embed.description[:200] + "..." if len(embed.description) > 200 else embed.description
                parts.append(f"[Embed内容: {desc}]")
            
            # 提取字段
            if embed.fields:
                field_texts = []
                for field in embed.fields[:3]:  # 最多提取3个字段
                    field_texts.append(f"{field.name}: {field.value[:100]}")
                if field_texts:
                    parts.append(f"[Embed字段: {'; '.join(field_texts)}]")
            
            # 提取URL
            if embed.url:
                parts.append(f"[Embed链接: {embed.url}]")
            
            if parts:
                embed_texts.append(" ".join(parts))
        
        return embed_texts
    
    def save_prompt_log(self, prompt: str) -> None:
        """
        保存提示词到日志文件，并自动清理超过数量限制的旧文件
        """
        try:
            # 获取配置的保存数量
            prompt_log_count = self.settings.get('prompt_log_count', 5)
            
            # 生成文件名（时间戳）
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
            log_file = os.path.join(self.prompt_log_path, f"prompt_{timestamp}.txt")
            
            # 保存提示词
            with open(log_file, 'w', encoding='utf-8') as f:
                f.write(prompt)
            logger.info(f"📝 已保存提示词日志: {os.path.basename(log_file)}")
            
            # 清理超过数量限制的旧文件
            log_files = sorted(
                [f for f in os.listdir(self.prompt_log_path) if f.startswith('prompt_') and f.endswith('.txt')],
                reverse=True  # 从新到旧排序
            )
            
            # 删除超出数量的旧文件
            if len(log_files) > prompt_log_count:
                files_to_delete = log_files[prompt_log_count:]
                for old_file in files_to_delete:
                    old_file_path = os.path.join(self.prompt_log_path, old_file)
                    try:
                        os.remove(old_file_path)
                        logger.debug(f"🗑️ 已删除旧提示词日志: {old_file}")
                    except Exception as e:
                        logger.warning(f"删除旧提示词日志失败 {old_file}: {e}")
                
                logger.info(f"🧹 已清理 {len(files_to_delete)} 个旧提示词日志文件")
        
        except Exception as e:
            logger.error(f"保存提示词日志失败: {e}")
    
    async def get_thread_metadata(self, thread_id: str) -> str:
        """
        获取子区元数据（子区名字、楼主名字、首楼内容）
        如果已缓存则从文件读取，否则从Discord获取并缓存
        
        Returns:
            格式化的子区信息字符串
        """
        metadata_file = os.path.join(self.thread_metadata_path, f"{thread_id}.txt")
        stale_metadata = ""

        if os.path.exists(metadata_file):
            try:
                with open(metadata_file, encoding='utf-8') as f:
                    cached_metadata = f.read().strip()

                if cached_metadata:
                    cache_age_seconds = max(0.0, time.time() - os.path.getmtime(metadata_file))
                    if cache_age_seconds < THREAD_METADATA_CACHE_TTL_SECONDS:
                        logger.info(f"从缓存加载子区 {thread_id} 的元数据")
                        return cached_metadata

                    stale_metadata = cached_metadata
                    logger.info(f"子区 {thread_id} 的元数据缓存已过期，准备刷新")
            except OSError as e:
                logger.warning(f"读取子区元数据缓存失败: {e}")
        
        # 没有缓存，从Discord获取
        try:
            channel = self.bot.get_channel(int(thread_id))
            if not channel:
                try:
                    channel = await self.bot.fetch_channel(int(thread_id))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                    logger.warning(f"无法获取子区 {thread_id} 的频道对象: {e}")
                    return stale_metadata
            
            # 获取子区名字
            thread_name = channel.name if hasattr(channel, 'name') else "未知子区"
            
            # 获取楼主信息
            owner_id = self.threads.get(thread_id, {}).get('ownerID', 0)
            if owner_id:
                try:
                    owner = await self.bot.fetch_user(int(owner_id))
                    owner_name = owner.display_name if owner else "未知用户"
                except Exception:
                    owner_name = f"用户ID:{owner_id}"
            else:
                owner_name = "未设置"
            
            # 获取首楼内容（第一条消息）
            first_message_content = ""
            try:
                # 获取频道的第一条消息
                async for message in channel.history(limit=1, oldest_first=True):
                    first_message_content = message.content[:500] if message.content else "[无文字内容]"
                    break
            except Exception as e:
                logger.warning(f"获取首楼内容失败: {e}")
                first_message_content = "[无法获取]"
            
            # 构建元数据字符串
            metadata = f"你现在位于：{thread_name}\n楼主是：{owner_name}\n子区首楼内容为：{first_message_content}"
            
            # 保存到缓存
            try:
                with open(metadata_file, 'w', encoding='utf-8') as f:
                    f.write(metadata)
                logger.info(f"已缓存子区 {thread_id} 的元数据")
            except OSError as e:
                logger.warning(f"保存子区元数据缓存失败: {e}")
            
            return metadata
            
        except Exception as e:
            logger.error(f"获取子区元数据失败: {e}")
            return stale_metadata
    
    async def build_prompt(self, thread_id: str, context_messages: list[str]) -> str:
        """
        构建系统提示词
        """
        thread_config = self.threads.get(thread_id, {})
        use_default_kb = thread_config.get('xSettings', {}).get('use_default_knowledge_base', True)
        
        # 选择基础提示词
        if use_default_kb:
            base_prompt_path = 'prompt/ALL.txt'
        else:
            base_prompt_path = 'prompt/raw.txt'
        
        # 加载基础提示词
        try:
            with open(base_prompt_path, encoding='utf-8') as f:
                system_prompt = f.read().strip()
        except FileNotFoundError:
            logger.warning(f"提示词文件不存在: {base_prompt_path}")
            system_prompt = "You are a helpful assistant."
        
        # 插入子区元数据
        thread_metadata = await self.get_thread_metadata(thread_id)
        if thread_metadata:
            system_prompt += "\n\n[子区信息]\n" + thread_metadata
        
        # 加载子区专属知识库
        kb_file = os.path.join(self.kb_path, f"{thread_id}.txt")
        has_custom_kb = False
        if os.path.exists(kb_file):
            try:
                with open(kb_file, encoding='utf-8') as f:
                    kb_content = f.read().strip()
                    if kb_content:
                        system_prompt += "\n\n[专属知识库]\n" + kb_content
                        has_custom_kb = True
                        logger.info(f"已加载子区 {thread_id} 的知识库")
            except Exception as e:
                logger.warning(f"加载子区知识库失败: {e}")
        
        # ⚠️ 警告：如果禁用了默认知识库但没有上传自定义知识库
        if not use_default_kb and not has_custom_kb:
            logger.warning(f"⚠️ 子区 {thread_id} 禁用了默认知识库但没有上传自定义知识库，bot可能无法提供专业答疑")
        
        # 添加上下文消息
        if context_messages:
            context_text = "\n\n[近期对话]\n" + "\n".join(context_messages)
            system_prompt += context_text
        
        # 保存提示词日志
        self.save_prompt_log(system_prompt)
        
        return system_prompt
    
    # ===== 斜杠命令 =====
    
    @app_commands.command(name='答疑bot-上传知识库', description='[OP] 为当前帖子上传知识库文件')
    async def upload_kb(self, interaction: discord.Interaction, file: discord.Attachment):
        """上传知识库文件"""
        thread_id = str(interaction.channel_id)
        
        # 检查权限
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message('❌ 无法验证权限', ephemeral=True)
            return
        
        if not self.check_permission(interaction.user, thread_id, 'op'):
            await interaction.response.send_message('❌ 只有楼主可以上传知识库', ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            # 检查子区配置是否存在
            if thread_id not in self.threads:
                await interaction.followup.send('❌ 该帖子还未配置答疑bot，请先联系管理员使用 `/答疑bot-创建子区配置` 命令创建配置', ephemeral=True)
                return
            
            # 检查文件类型
            if not file.filename.endswith('.txt'):
                await interaction.followup.send('❌ 只支持 .txt 文件', ephemeral=True)
                return
            
            # 下载并保存文件
            kb_file_path = os.path.join(self.kb_path, f"{thread_id}.txt")
            await file.save(kb_file_path)
            
            # 读取文件大小
            file_size = os.path.getsize(kb_file_path)
            
            embed = discord.Embed(
                title="✅ 知识库上传成功",
                description=f"已为帖子 <#{thread_id}> 上传知识库",
                color=discord.Color.green(),
                timestamp=datetime.now()
            )
            embed.add_field(name="文件名", value=file.filename, inline=True)
            embed.add_field(name="文件大小", value=f"{file_size / 1024:.2f} KB", inline=True)
            embed.set_footer(text=f"上传者: {interaction.user}")
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            logger.info(f"用户 {interaction.user.id} 为子区 {thread_id} 上传了知识库")
            
        except Exception as e:
            logger.error(f"上传知识库失败: {e}")
            await interaction.followup.send(f'❌ 上传失败: {str(e)}', ephemeral=True)
    
    @app_commands.command(name='答疑bot-下载知识库', description='[OP/Moderator] 下载当前帖子的知识库文件')
    async def download_kb(self, interaction: discord.Interaction):
        """下载知识库文件"""
        thread_id = str(interaction.channel_id)
        
        # 检查权限
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message('❌ 无法验证权限', ephemeral=True)
            return
        
        if not self.check_permission(interaction.user, thread_id, 'op'):
            await interaction.response.send_message('❌ 只有楼主或管理员可以下载知识库', ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            kb_file_path = os.path.join(self.kb_path, f"{thread_id}.txt")
            
            if not os.path.exists(kb_file_path):
                await interaction.followup.send('❌ 该帖子还没有上传知识库', ephemeral=True)
                return
            
            # 发送文件
            file = discord.File(kb_file_path, filename=f"知识库_{thread_id}.txt")
            await interaction.followup.send(
                content=f"📥 帖子 <#{thread_id}> 的知识库文件：",
                file=file,
                ephemeral=True
            )
            
            logger.info(f"用户 {interaction.user.id} 下载了子区 {thread_id} 的知识库")
            
        except Exception as e:
            logger.error(f"下载知识库失败: {e}")
            await interaction.followup.send(f'❌ 下载失败: {str(e)}', ephemeral=True)
    
    @app_commands.command(name='答疑bot-切换开启状态', description='[OP/Moderator] 切换当前帖子的答疑bot开启状态')
    async def toggle_status(self, interaction: discord.Interaction):
        """切换开启状态"""
        thread_id = str(interaction.channel_id)
        
        # 检查权限
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message('❌ 无法验证权限', ephemeral=True)
            return
        
        if not self.check_permission(interaction.user, thread_id, 'op'):
            await interaction.response.send_message('❌ 只有楼主或管理员可以切换状态', ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)  # 私密回复
        
        try:
            # 检查子区是否在允许列表中
            allowed_threads = [str(tid) for tid in self.settings.get('allowed_thread_ids', [])]
            
            if thread_id in allowed_threads:
                # 从列表中移除
                allowed_threads.remove(thread_id)
                status = "已关闭"
                status_emoji = "🔴"
                color = discord.Color.red()
            else:
                # 添加到列表
                allowed_threads.append(thread_id)
                status = "已开启"
                status_emoji = "🟢"
                color = discord.Color.green()
            
            # 更新设置
            self.settings['allowed_thread_ids'] = allowed_threads
            self.save_settings()
            
            embed = discord.Embed(
                title=f"{status_emoji} 答疑bot状态已更新",
                description=f"当前帖子的答疑bot功能 **{status}**",
                color=color,
                timestamp=datetime.now()
            )
            embed.set_footer(text=f"操作者: {interaction.user}")
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            logger.info(f"用户 {interaction.user.id} 将子区 {thread_id} 的状态切换为 {status}")
            
        except Exception as e:
            logger.error(f"切换状态失败: {e}")
            await interaction.followup.send(f'❌ 操作失败: {str(e)}', ephemeral=True)
    
    @app_commands.command(name='答疑bot-黑名单', description='[OP/Moderator] 管理黑名单用户')
    @app_commands.describe(
        user='要操作的用户',
        operation='操作类型：加入或移出黑名单',
        scope='操作范围：仅当前帖子或全局（仅Moderator可全局操作）'
    )
    @app_commands.choices(
        operation=[
            app_commands.Choice(name='加入黑名单', value='add'),
            app_commands.Choice(name='移出黑名单', value='remove')
        ],
        scope=[
            app_commands.Choice(name='仅当前帖子', value='thread'),
            app_commands.Choice(name='全局', value='global')
        ]
    )
    async def blacklist_user(self, interaction: discord.Interaction, user: discord.User, operation: str, scope: str = 'thread'):
        """管理黑名单用户"""
        thread_id = str(interaction.channel_id)
        
        # 检查权限
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message('❌ 无法验证权限', ephemeral=True)
            return
        
        # 全局操作需要moderator权限
        if scope == 'global':
            if not self.check_permission(interaction.user, thread_id, 'moderator'):
                await interaction.response.send_message('❌ 只有管理员可以全局操作黑名单', ephemeral=True)
                return
        else:
            if not self.check_permission(interaction.user, thread_id, 'op'):
                await interaction.response.send_message('❌ 只有楼主或管理员可以操作黑名单', ephemeral=True)
                return
        
        await interaction.response.defer(ephemeral=True)  # 私密回复
        
        try:
            user_id = str(user.id)
            
            if scope == 'global':
                # 全局黑名单操作
                global_blacklist = self.settings.get('global_blacklisted_user_ids', [])
                global_blacklist_str = [str(uid) for uid in global_blacklist]
                
                if operation == 'add':
                    # 加入全局黑名单
                    if user_id not in global_blacklist_str:
                        global_blacklist.append(user_id)
                        self.settings['global_blacklisted_user_ids'] = global_blacklist
                        self.save_settings()
                        
                        embed = discord.Embed(
                            title="🚫 全局拉黑成功",
                            description=f"用户 {user.mention} 已被加入全局黑名单",
                            color=discord.Color.red(),
                            timestamp=datetime.now()
                        )
                        embed.set_footer(text=f"操作者: {interaction.user}")
                        
                        await interaction.followup.send(embed=embed, ephemeral=True)
                        logger.info(f"管理员 {interaction.user.id} 将用户 {user_id} 加入全局黑名单")
                    else:
                        await interaction.followup.send(f'⚠️ 用户 {user.mention} 已经在全局黑名单中', ephemeral=True)
                
                elif operation == 'remove':
                    # 移出全局黑名单
                    if user_id in global_blacklist_str:
                        # 找到并移除
                        global_blacklist = [uid for uid in global_blacklist if str(uid) != user_id]
                        self.settings['global_blacklisted_user_ids'] = global_blacklist
                        self.save_settings()
                        
                        embed = discord.Embed(
                            title="✅ 全局解除拉黑成功",
                            description=f"用户 {user.mention} 已从全局黑名单中移除",
                            color=discord.Color.green(),
                            timestamp=datetime.now()
                        )
                        embed.set_footer(text=f"操作者: {interaction.user}")
                        
                        await interaction.followup.send(embed=embed, ephemeral=True)
                        logger.info(f"管理员 {interaction.user.id} 将用户 {user_id} 从全局黑名单中移除")
                    else:
                        await interaction.followup.send(f'⚠️ 用户 {user.mention} 不在全局黑名单中', ephemeral=True)
            
            else:
                # 帖子黑名单操作
                if thread_id not in self.threads:
                    await interaction.followup.send('❌ 该帖子还未配置答疑bot', ephemeral=True)
                    return
                
                thread_blacklist = self.threads[thread_id].get('blacklisted_users_ID', [])
                thread_blacklist_str = [str(uid) for uid in thread_blacklist]
                
                if operation == 'add':
                    # 加入帖子黑名单
                    if user_id not in thread_blacklist_str:
                        thread_blacklist.append(user_id)
                        self.threads[thread_id]['blacklisted_users_ID'] = thread_blacklist
                        self.save_threads()
                        
                        embed = discord.Embed(
                            title="🚫 拉黑成功",
                            description=f"用户 {user.mention} 已被加入当前帖子黑名单",
                            color=discord.Color.orange(),
                            timestamp=datetime.now()
                        )
                        embed.set_footer(text=f"操作者: {interaction.user}")
                        
                        await interaction.followup.send(embed=embed, ephemeral=True)
                        logger.info(f"用户 {interaction.user.id} 将用户 {user_id} 加入子区 {thread_id} 黑名单")
                    else:
                        await interaction.followup.send(f'⚠️ 用户 {user.mention} 已经在该帖子的黑名单中', ephemeral=True)
                
                elif operation == 'remove':
                    # 移出帖子黑名单
                    if user_id in thread_blacklist_str:
                        # 找到并移除
                        thread_blacklist = [uid for uid in thread_blacklist if str(uid) != user_id]
                        self.threads[thread_id]['blacklisted_users_ID'] = thread_blacklist
                        self.save_threads()
                        
                        embed = discord.Embed(
                            title="✅ 解除拉黑成功",
                            description=f"用户 {user.mention} 已从当前帖子黑名单中移除",
                            color=discord.Color.green(),
                            timestamp=datetime.now()
                        )
                        embed.set_footer(text=f"操作者: {interaction.user}")
                        
                        await interaction.followup.send(embed=embed, ephemeral=True)
                        logger.info(f"用户 {interaction.user.id} 将用户 {user_id} 从子区 {thread_id} 黑名单中移除")
                    else:
                        await interaction.followup.send(f'⚠️ 用户 {user.mention} 不在该帖子的黑名单中', ephemeral=True)
            
        except Exception as e:
            logger.error(f"黑名单操作失败: {e}")
            await interaction.followup.send(f'❌ 操作失败: {str(e)}', ephemeral=True)
    
    @app_commands.command(name='答疑bot-创建子区配置', description='[Admin] 为指定子区创建默认配置')
    @app_commands.describe(thread_id='子区ID（可选，不填则使用当前子区）')
    async def create_thread_config(self, interaction: discord.Interaction, thread_id: str | None = None):
        """创建子区配置"""
        # 检查权限
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message('❌ 无法验证权限', ephemeral=True)
            return
        
        # 检查是否为 admin (从 bot.admins 读取，该列表从 users.db 加载)
        if interaction.user.id not in self.bot.admins:
            await interaction.response.send_message('❌ 只有管理员可以创建子区配置', ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            # 如果没有提供 thread_id，使用当前频道ID
            target_channel = None
            if thread_id is None:
                # 检查是否在子区中
                if not isinstance(interaction.channel, discord.Thread):
                    await interaction.followup.send('❌ 当前不在子区中，请指定 thread_id 参数或在子区中使用此命令', ephemeral=True)
                    return
                thread_id = str(interaction.channel_id)
                target_channel = interaction.channel
            else:
                # 获取指定的子区
                try:
                    target_channel = self.bot.get_channel(int(thread_id))
                    if not target_channel:
                        target_channel = await self.bot.fetch_channel(int(thread_id))
                except Exception as e:
                    await interaction.followup.send(f'❌ 无法获取子区 `{thread_id}`：{str(e)}', ephemeral=True)
                    return
            
            # 检查子区是否已存在
            if thread_id in self.threads:
                await interaction.followup.send(f'❌ 子区 `{thread_id}` 的配置已存在', ephemeral=True)
                return
            
            # 获取子区的第一条消息，确定楼主
            owner_id = 0
            try:
                async for message in target_channel.history(limit=1, oldest_first=True):
                    owner_id = message.author.id
                    logger.info(f"检测到子区 {thread_id} 的楼主为用户 {owner_id}")
                    break
            except Exception as e:
                logger.warning(f"获取子区首条消息失败: {e}，楼主ID将设置为0")
            
            # 获取下一个ID
            max_id = max([int(t.get('id', 0)) for t in self.threads.values()], default=0)
            
            # 创建默认配置
            self.threads[thread_id] = {
                "id": max_id + 1,
                "ownerID": owner_id,  # 设置为楼主ID
                "blacklisted_users_ID": [],
                "xSettings": {
                    "thread_cd_seconds": -1,  # 不启用子区冷却
                    "user_cd_seconds": 30,
                    "read_user_interaction_history": 3,
                    "use_default_knowledge_base": True,
                    "preset": []
                }
            }
            self.save_threads()
            
            # 构建私密回复的embed
            embed = discord.Embed(
                title="✅ 子区配置创建成功",
                description=f"已为子区 <#{thread_id}> 创建默认配置",
                color=discord.Color.green(),
                timestamp=datetime.now()
            )
            embed.add_field(name="配置ID", value=str(max_id + 1), inline=True)
            embed.add_field(name="子区冷却", value="不启用 (-1秒)", inline=True)
            embed.add_field(name="用户冷却", value="30秒", inline=True)
            embed.add_field(name="历史消息深度", value="3条", inline=True)
            embed.add_field(name="使用默认知识库", value="是", inline=True)
            embed.add_field(name="楼主ID", value=f"<@{owner_id}>" if owner_id != 0 else "未检测到 (0)", inline=True)
            embed.set_footer(text=f"创建者: {interaction.user}")
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            logger.info(f"管理员 {interaction.user.id} 为子区 {thread_id} 创建了配置，楼主ID: {owner_id}")
            
            # 在子区中发送公开欢迎消息
            try:
                welcome_message = self.settings.get('op_welcome_message', '自助答疑bot已初始化！')
                if owner_id != 0:
                    # @楼主并发送欢迎消息
                    await target_channel.send(f"<@{owner_id}> {welcome_message}")
                else:
                    # 没有检测到楼主，只发送欢迎消息
                    await target_channel.send(welcome_message)
                logger.info(f"已在子区 {thread_id} 发送欢迎消息")
            except Exception as e:
                logger.error(f"发送欢迎消息失败: {e}")
                # 不影响主流程，只记录错误
            
        except Exception as e:
            logger.error(f"创建子区配置失败: {e}")
            await interaction.followup.send(f'❌ 创建失败: {str(e)}', ephemeral=True)
    
    @app_commands.command(name='答疑bot-子区配置控制面板', description='[Admin/OP] 查看和编辑当前子区的配置')
    async def thread_config_panel(self, interaction: discord.Interaction):
        """显示子区配置控制面板"""
        thread_id = str(interaction.channel_id)
        
        # 检查权限
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message('❌ 无法验证权限', ephemeral=True)
            return
        
        if not self.check_permission(interaction.user, thread_id, 'op'):
            await interaction.response.send_message('❌ 只有楼主或管理员可以查看配置面板', ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            # 检查子区配置是否存在
            if thread_id not in self.threads:
                await interaction.followup.send('❌ 该子区还未配置答疑bot，请先创建配置', ephemeral=True)
                return
            
            # 创建控制面板embed
            embed = await self.create_thread_config_panel_embed(thread_id, interaction)
            
            # 创建按钮视图
            view = ThreadConfigControlView(self, thread_id, interaction.user.id)
            
            # 发送面板
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
            logger.info(f"用户 {interaction.user.id} 打开了子区 {thread_id} 的配置控制面板")
            
        except Exception as e:
            logger.error(f"创建配置控制面板失败: {e}")
            await interaction.followup.send(f'❌ 创建控制面板失败: {str(e)}', ephemeral=True)
    
    async def create_thread_config_panel_embed(self, thread_id: str, interaction: discord.Interaction) -> discord.Embed:
        """创建子区配置控制面板的embed消息"""
        embed = discord.Embed(
            title="⚙️ 子区配置控制面板",
            description=f"管理子区 <#{thread_id}> 的配置",
            color=discord.Color.blue(),
            timestamp=datetime.now()
        )
        
        thread_config = self.threads.get(thread_id, {})
        x_settings = thread_config.get('xSettings', {})
        
        # 基本信息
        config_id = thread_config.get('id', 'N/A')
        owner_id = thread_config.get('ownerID', 0)
        owner_mention = f"<@{owner_id}>" if owner_id != 0 else "未设置"
        
        embed.add_field(name="📋 配置ID", value=str(config_id), inline=True)
        embed.add_field(name="👤 楼主", value=owner_mention, inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)  # 空白占位
        
        # 冷却设置
        thread_cd = x_settings.get('thread_cd_seconds', -1)
        thread_cd_display = "不启用" if thread_cd < 0 else f"{thread_cd}秒"
        user_cd = x_settings.get('user_cd_seconds', 30)
        
        embed.add_field(name="⏰ 子区冷却", value=thread_cd_display, inline=True)
        embed.add_field(name="⏱️ 用户冷却", value=f"{user_cd}秒", inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)  # 空白占位
        
        # 其他设置
        history_depth = x_settings.get('read_user_interaction_history', 3)
        use_default_kb = x_settings.get('use_default_knowledge_base', True)
        use_default_kb_display = "✅ 是" if use_default_kb else "❌ 否"
        
        embed.add_field(name="📜 历史消息深度", value=f"{history_depth}条", inline=True)
        embed.add_field(name="📚 使用默认知识库", value=use_default_kb_display, inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)  # 空白占位
        
        # 黑名单
        blacklist = thread_config.get('blacklisted_users_ID', [])
        blacklist_count = len(blacklist)
        embed.add_field(name="🚫 黑名单用户数", value=str(blacklist_count), inline=True)
        
        # 预设数量
        presets = x_settings.get('preset', [])
        preset_count = len(presets)
        embed.add_field(name="🎯 预设回复数", value=str(preset_count), inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)  # 空白占位
        
        embed.set_footer(text=f"请求者: {interaction.user}")
        return embed
    
    @app_commands.command(name='答疑bot-预设回复控制面板', description='[OP] 管理当前帖子的预设回复')
    async def preset_panel(self, interaction: discord.Interaction):
        """显示预设回复控制面板"""
        thread_id = str(interaction.channel_id)
        
        # 检查权限
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message('❌ 无法验证权限', ephemeral=True)
            return
        
        if not self.check_permission(interaction.user, thread_id, 'op'):
            await interaction.response.send_message('❌ 只有楼主可以管理预设回复', ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            # 确保子区配置存在
            if thread_id not in self.threads:
                await interaction.followup.send('❌ 该帖子还未配置答疑bot，请先上传知识库', ephemeral=True)
                return
            
            # 创建控制面板embed
            embed = await self.create_preset_panel_embed(thread_id, interaction)
            
            # 创建按钮视图
            view = PresetControlView(self, thread_id, interaction.user.id)
            
            # 发送面板
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
            logger.info(f"用户 {interaction.user.id} 打开了子区 {thread_id} 的预设回复控制面板")
            
        except Exception as e:
            logger.error(f"创建预设回复控制面板失败: {e}")
            await interaction.followup.send(f'❌ 创建控制面板失败: {str(e)}', ephemeral=True)
    
    async def create_preset_panel_embed(self, thread_id: str, interaction: discord.Interaction) -> discord.Embed:
        """创建预设回复控制面板的embed消息"""
        embed = discord.Embed(
            title="🎯 预设回复控制面板",
            description=f"管理帖子 <#{thread_id}> 的预设回复",
            color=discord.Color.blue(),
            timestamp=datetime.now()
        )
        
        thread_config = self.threads.get(thread_id, {})
        presets = thread_config.get('xSettings', {}).get('preset', [])
        
        if not presets:
            embed.add_field(
                name="📭 暂无预设",
                value="当前没有配置任何预设回复",
                inline=False
            )
        else:
            for idx, preset in enumerate(presets[:5], 1):  # 最多显示5个
                if len(preset) >= 3:
                    whitelist = preset[0]
                    blacklist = preset[1]
                    reply = preset[2]
                    
                    field_value = (
                        f"**白名单关键词:** {whitelist}\n"
                        f"**黑名单关键词:** {blacklist if blacklist else '无'}\n"
                        f"**回复内容:** {reply[:100]}{'...' if len(reply) > 100 else ''}"
                    )
                    
                    embed.add_field(
                        name=f"{idx}️⃣ 预设 #{idx}",
                        value=field_value,
                        inline=False
                    )
            
            if len(presets) > 5:
                embed.add_field(
                    name="ℹ️ 提示",
                    value=f"共有 {len(presets)} 个预设，仅显示前5个",
                    inline=False
                )
        
        embed.set_footer(text=f"请求者: {interaction.user}")
        return embed


# ===== UI组件类 =====

class ThreadConfigControlView(discord.ui.View):
    """子区配置控制面板的按钮视图"""
    
    def __init__(self, cog: MentionCog, thread_id: str, user_id: int):
        super().__init__(timeout=300)  # 5分钟超时
        self.cog = cog
        self.thread_id = thread_id
        self.user_id = user_id
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """检查交互用户是否为原始用户"""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ 只有召唤面板的用户才能使用这些按钮。", ephemeral=True)
            return False
        return True
    
    @discord.ui.button(label='👤 设置楼主', style=discord.ButtonStyle.primary, row=0)
    async def set_owner(self, interaction: discord.Interaction, button: discord.ui.Button):
        """设置楼主按钮"""
        modal = SetOwnerModal(self.cog, self.thread_id)
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label='⏰ 设置冷却', style=discord.ButtonStyle.primary, row=0)
    async def set_cooldown(self, interaction: discord.Interaction, button: discord.ui.Button):
        """设置冷却按钮"""
        modal = SetCooldownModal(self.cog, self.thread_id)
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label='📜 设置历史深度', style=discord.ButtonStyle.primary, row=0)
    async def set_history(self, interaction: discord.Interaction, button: discord.ui.Button):
        """设置历史消息深度按钮"""
        modal = SetHistoryDepthModal(self.cog, self.thread_id)
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label='📚 切换默认知识库', style=discord.ButtonStyle.secondary, row=1)
    async def toggle_default_kb(self, interaction: discord.Interaction, button: discord.ui.Button):
        """切换默认知识库按钮"""
        await interaction.response.defer(ephemeral=True)
        
        try:
            thread_config = self.cog.threads.get(self.thread_id, {})
            x_settings = thread_config.get('xSettings', {})
            current_value = x_settings.get('use_default_knowledge_base', True)
            
            # 切换值
            new_value = not current_value
            self.cog.threads[self.thread_id]['xSettings']['use_default_knowledge_base'] = new_value
            self.cog.save_threads()
            
            status = "✅ 已启用" if new_value else "❌ 已禁用"
            color = discord.Color.green() if new_value else discord.Color.red()
            
            embed = discord.Embed(
                title="📚 默认知识库状态已更新",
                description=f"使用默认知识库: {status}",
                color=color,
                timestamp=datetime.now()
            )
            
            if not new_value:
                embed.add_field(
                    name="⚠️ 警告",
                    value="禁用默认知识库后，如果没有上传自定义知识库，bot可能无法提供专业答疑",
                    inline=False
                )
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            logger.info(f"用户 {interaction.user.id} 将子区 {self.thread_id} 的默认知识库设置为 {new_value}")
            
            # 刷新面板
            panel_embed = await self.cog.create_thread_config_panel_embed(self.thread_id, interaction)
            await interaction.message.edit(embed=panel_embed, view=self)
            
        except Exception as e:
            logger.error(f"切换默认知识库失败: {e}")
            await interaction.followup.send(f"❌ 操作失败: {str(e)}", ephemeral=True)
    
    @discord.ui.button(label='🔄 刷新面板', style=discord.ButtonStyle.secondary, row=1)
    async def refresh_panel(self, interaction: discord.Interaction, button: discord.ui.Button):
        """刷新面板按钮"""
        try:
            embed = await self.cog.create_thread_config_panel_embed(self.thread_id, interaction)
            await interaction.response.edit_message(embed=embed, view=self)
        except Exception as e:
            await interaction.response.send_message(f"❌ 刷新失败: {str(e)}", ephemeral=True)


class PresetControlView(discord.ui.View):
    """预设回复控制面板的按钮视图"""
    
    def __init__(self, cog: MentionCog, thread_id: str, user_id: int):
        super().__init__(timeout=300)  # 5分钟超时
        self.cog = cog
        self.thread_id = thread_id
        self.user_id = user_id
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """检查交互用户是否为原始用户"""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ 只有召唤面板的用户才能使用这些按钮。", ephemeral=True)
            return False
        return True
    
    @discord.ui.button(label='🔍 查看预设', style=discord.ButtonStyle.primary, row=0)
    async def view_preset(self, interaction: discord.Interaction, button: discord.ui.Button):
        """查看预设按钮"""
        modal = ViewPresetModal(self.cog, self.thread_id)
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label='➕ 新增预设', style=discord.ButtonStyle.success, row=0)
    async def add_preset(self, interaction: discord.Interaction, button: discord.ui.Button):
        """新增预设按钮"""
        modal = AddPresetModal(self.cog, self.thread_id)
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label='✏️ 修改预设', style=discord.ButtonStyle.secondary, row=0)
    async def edit_preset(self, interaction: discord.Interaction, button: discord.ui.Button):
        """修改预设按钮"""
        modal = EditPresetModal(self.cog, self.thread_id)
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label='🗑️ 删除预设', style=discord.ButtonStyle.danger, row=0)
    async def delete_preset(self, interaction: discord.Interaction, button: discord.ui.Button):
        """删除预设按钮"""
        modal = DeletePresetModal(self.cog, self.thread_id)
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label='🔄 刷新面板', style=discord.ButtonStyle.secondary, row=1)
    async def refresh_panel(self, interaction: discord.Interaction, button: discord.ui.Button):
        """刷新面板按钮"""
        try:
            embed = await self.cog.create_preset_panel_embed(self.thread_id, interaction)
            await interaction.response.edit_message(embed=embed, view=self)
        except Exception as e:
            await interaction.response.send_message(f"❌ 刷新失败: {str(e)}", ephemeral=True)


# ===== Modal类 =====

class SetOwnerModal(discord.ui.Modal, title='设置楼主'):
    """设置楼主的Modal"""
    
    owner_id = discord.ui.TextInput(
        label='楼主ID',
        placeholder='输入楼主的用户ID（数字）',
        required=True,
        max_length=20
    )
    
    def __init__(self, cog: MentionCog, thread_id: str):
        super().__init__()
        self.cog = cog
        self.thread_id = thread_id
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        try:
            owner_id = int(self.owner_id.value)
            
            if self.thread_id not in self.cog.threads:
                await interaction.followup.send('❌ 该子区配置不存在', ephemeral=True)
                return
            
            # 更新楼主ID
            self.cog.threads[self.thread_id]['ownerID'] = owner_id
            self.cog.save_threads()
            
            embed = discord.Embed(
                title="✅ 楼主设置成功",
                description=f"已将子区 <#{self.thread_id}> 的楼主设置为 <@{owner_id}>",
                color=discord.Color.green(),
                timestamp=datetime.now()
            )
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            logger.info(f"用户 {interaction.user.id} 将子区 {self.thread_id} 的楼主设置为 {owner_id}")
            
        except ValueError:
            await interaction.followup.send("❌ 请输入有效的用户ID（纯数字）", ephemeral=True)
        except Exception as e:
            logger.error(f"设置楼主失败: {e}")
            await interaction.followup.send(f"❌ 设置失败: {str(e)}", ephemeral=True)


class SetCooldownModal(discord.ui.Modal, title='设置冷却时间'):
    """设置冷却时间的Modal"""
    
    thread_cd = discord.ui.TextInput(
        label='子区冷却（秒）',
        placeholder='输入-1表示不启用子区冷却',
        required=True,
        max_length=10
    )
    
    user_cd = discord.ui.TextInput(
        label='用户冷却（秒）',
        placeholder='输入用户冷却时间（秒）',
        required=True,
        max_length=10
    )
    
    def __init__(self, cog: MentionCog, thread_id: str):
        super().__init__()
        self.cog = cog
        self.thread_id = thread_id
        
        # 设置当前值为默认值
        thread_config = self.cog.threads.get(thread_id, {})
        x_settings = thread_config.get('xSettings', {})
        self.thread_cd.default = str(x_settings.get('thread_cd_seconds', -1))
        self.user_cd.default = str(x_settings.get('user_cd_seconds', 30))
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        try:
            thread_cd = int(self.thread_cd.value)
            user_cd = int(self.user_cd.value)
            
            if self.thread_id not in self.cog.threads:
                await interaction.followup.send('❌ 该子区配置不存在', ephemeral=True)
                return
            
            # 验证输入
            if user_cd < 0:
                await interaction.followup.send('❌ 用户冷却时间不能为负数', ephemeral=True)
                return
            
            # 更新配置
            if 'xSettings' not in self.cog.threads[self.thread_id]:
                self.cog.threads[self.thread_id]['xSettings'] = {}
            
            self.cog.threads[self.thread_id]['xSettings']['thread_cd_seconds'] = thread_cd
            self.cog.threads[self.thread_id]['xSettings']['user_cd_seconds'] = user_cd
            self.cog.save_threads()
            
            thread_cd_display = "不启用" if thread_cd < 0 else f"{thread_cd}秒"
            
            embed = discord.Embed(
                title="✅ 冷却设置成功",
                description=f"已更新子区 <#{self.thread_id}> 的冷却设置",
                color=discord.Color.green(),
                timestamp=datetime.now()
            )
            embed.add_field(name="子区冷却", value=thread_cd_display, inline=True)
            embed.add_field(name="用户冷却", value=f"{user_cd}秒", inline=True)
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            logger.info(f"用户 {interaction.user.id} 更新了子区 {self.thread_id} 的冷却设置")
            
        except ValueError:
            await interaction.followup.send("❌ 请输入有效的数字", ephemeral=True)
        except Exception as e:
            logger.error(f"设置冷却失败: {e}")
            await interaction.followup.send(f"❌ 设置失败: {str(e)}", ephemeral=True)


class SetHistoryDepthModal(discord.ui.Modal, title='设置历史消息深度'):
    """设置历史消息深度的Modal"""
    
    history_depth = discord.ui.TextInput(
        label='历史消息深度',
        placeholder='输入要读取的历史消息条数（0表示不读取）',
        required=True,
        max_length=3
    )
    
    def __init__(self, cog: MentionCog, thread_id: str):
        super().__init__()
        self.cog = cog
        self.thread_id = thread_id
        
        # 设置当前值为默认值
        thread_config = self.cog.threads.get(thread_id, {})
        x_settings = thread_config.get('xSettings', {})
        self.history_depth.default = str(x_settings.get('read_user_interaction_history', 3))
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        try:
            depth = int(self.history_depth.value)
            
            if self.thread_id not in self.cog.threads:
                await interaction.followup.send('❌ 该子区配置不存在', ephemeral=True)
                return
            
            # 验证输入
            if depth < 0:
                await interaction.followup.send('❌ 历史消息深度不能为负数', ephemeral=True)
                return
            
            if depth > 50:
                await interaction.followup.send('❌ 历史消息深度不能超过50条', ephemeral=True)
                return
            
            # 更新配置
            if 'xSettings' not in self.cog.threads[self.thread_id]:
                self.cog.threads[self.thread_id]['xSettings'] = {}
            
            self.cog.threads[self.thread_id]['xSettings']['read_user_interaction_history'] = depth
            self.cog.save_threads()
            
            embed = discord.Embed(
                title="✅ 历史消息深度设置成功",
                description=f"已将子区 <#{self.thread_id}> 的历史消息深度设置为 {depth} 条",
                color=discord.Color.green(),
                timestamp=datetime.now()
            )
            
            if depth == 0:
                embed.add_field(
                    name="ℹ️ 提示",
                    value="设置为0表示bot不会读取历史消息，只会回复当前消息",
                    inline=False
                )
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            logger.info(f"用户 {interaction.user.id} 将子区 {self.thread_id} 的历史消息深度设置为 {depth}")
            
        except ValueError:
            await interaction.followup.send("❌ 请输入有效的数字", ephemeral=True)
        except Exception as e:
            logger.error(f"设置历史消息深度失败: {e}")
            await interaction.followup.send(f"❌ 设置失败: {str(e)}", ephemeral=True)


class ViewPresetModal(discord.ui.Modal, title='查看预设详情'):
    """查看预设的Modal"""
    
    preset_index = discord.ui.TextInput(
        label='预设编号',
        placeholder='输入要查看的预设编号（从1开始）',
        required=True,
        max_length=3
    )
    
    def __init__(self, cog: MentionCog, thread_id: str):
        super().__init__()
        self.cog = cog
        self.thread_id = thread_id
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        try:
            index = int(self.preset_index.value) - 1
            
            thread_config = self.cog.threads.get(self.thread_id, {})
            presets = thread_config.get('xSettings', {}).get('preset', [])
            
            if index < 0 or index >= len(presets):
                await interaction.followup.send(f"❌ 预设编号无效，当前共有 {len(presets)} 个预设", ephemeral=True)
                return
            
            preset = presets[index]
            if len(preset) < 3:
                await interaction.followup.send("❌ 预设格式错误", ephemeral=True)
                return
            
            embed = discord.Embed(
                title=f"🔍 预设 #{index + 1} 详情",
                color=discord.Color.blue(),
                timestamp=datetime.now()
            )
            
            embed.add_field(name="白名单关键词", value=preset[0], inline=False)
            embed.add_field(name="黑名单关键词", value=preset[1] if preset[1] else "无", inline=False)
            embed.add_field(name="回复内容", value=preset[2], inline=False)
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            
        except ValueError:
            await interaction.followup.send("❌ 请输入有效的数字", ephemeral=True)
        except Exception as e:
            logger.error(f"查看预设失败: {e}")
            await interaction.followup.send(f"❌ 查看失败: {str(e)}", ephemeral=True)


class AddPresetModal(discord.ui.Modal, title='新增预设回复'):
    """新增预设的Modal"""
    
    whitelist = discord.ui.TextInput(
        label='白名单关键词',
        placeholder='消息必须包含此关键词才会触发',
        required=True,
        max_length=100
    )
    
    blacklist = discord.ui.TextInput(
        label='黑名单关键词（可选）',
        placeholder='消息包含此关键词则不会触发',
        required=False,
        max_length=100
    )
    
    reply = discord.ui.TextInput(
        label='回复内容',
        placeholder='触发时机器人将回复此内容',
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=2000
    )
    
    def __init__(self, cog: MentionCog, thread_id: str):
        super().__init__()
        self.cog = cog
        self.thread_id = thread_id
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        try:
            if self.thread_id not in self.cog.threads:
                await interaction.followup.send('❌ 该帖子还未配置答疑bot', ephemeral=True)
                return
            
            # 添加新预设
            new_preset = [
                self.whitelist.value,
                self.blacklist.value if self.blacklist.value else "",
                self.reply.value
            ]
            
            if 'xSettings' not in self.cog.threads[self.thread_id]:
                self.cog.threads[self.thread_id]['xSettings'] = {}
            
            if 'preset' not in self.cog.threads[self.thread_id]['xSettings']:
                self.cog.threads[self.thread_id]['xSettings']['preset'] = []
            
            self.cog.threads[self.thread_id]['xSettings']['preset'].append(new_preset)
            self.cog.save_threads()
            
            embed = discord.Embed(
                title="✅ 预设添加成功",
                description=f"已为帖子 <#{self.thread_id}> 添加新预设",
                color=discord.Color.green(),
                timestamp=datetime.now()
            )
            embed.add_field(name="白名单关键词", value=self.whitelist.value, inline=False)
            embed.add_field(name="黑名单关键词", value=self.blacklist.value if self.blacklist.value else "无", inline=False)
            embed.add_field(name="回复内容", value=self.reply.value[:200] + ('...' if len(self.reply.value) > 200 else ''), inline=False)
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            logger.info(f"用户 {interaction.user.id} 为子区 {self.thread_id} 添加了新预设")
            
        except Exception as e:
            logger.error(f"添加预设失败: {e}")
            await interaction.followup.send(f"❌ 添加失败: {str(e)}", ephemeral=True)


class EditPresetModal(discord.ui.Modal, title='修改预设回复'):
    """修改预设的Modal"""
    
    preset_index = discord.ui.TextInput(
        label='预设编号',
        placeholder='输入要修改的预设编号（从1开始）',
        required=True,
        max_length=3
    )
    
    whitelist = discord.ui.TextInput(
        label='白名单关键词',
        placeholder='消息必须包含此关键词才会触发',
        required=True,
        max_length=100
    )
    
    blacklist = discord.ui.TextInput(
        label='黑名单关键词（可选）',
        placeholder='消息包含此关键词则不会触发',
        required=False,
        max_length=100
    )
    
    reply = discord.ui.TextInput(
        label='回复内容',
        placeholder='触发时机器人将回复此内容',
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=2000
    )
    
    def __init__(self, cog: MentionCog, thread_id: str):
        super().__init__()
        self.cog = cog
        self.thread_id = thread_id
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        try:
            index = int(self.preset_index.value) - 1
            
            thread_config = self.cog.threads.get(self.thread_id, {})
            presets = thread_config.get('xSettings', {}).get('preset', [])
            
            if index < 0 or index >= len(presets):
                await interaction.followup.send(f"❌ 预设编号无效，当前共有 {len(presets)} 个预设", ephemeral=True)
                return
            
            # 更新预设
            presets[index] = [
                self.whitelist.value,
                self.blacklist.value if self.blacklist.value else "",
                self.reply.value
            ]
            
            self.cog.threads[self.thread_id]['xSettings']['preset'] = presets
            self.cog.save_threads()
            
            embed = discord.Embed(
                title="✅ 预设修改成功",
                description=f"已更新预设 #{index + 1}",
                color=discord.Color.green(),
                timestamp=datetime.now()
            )
            embed.add_field(name="白名单关键词", value=self.whitelist.value, inline=False)
            embed.add_field(name="黑名单关键词", value=self.blacklist.value if self.blacklist.value else "无", inline=False)
            embed.add_field(name="回复内容", value=self.reply.value[:200] + ('...' if len(self.reply.value) > 200 else ''), inline=False)
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            logger.info(f"用户 {interaction.user.id} 修改了子区 {self.thread_id} 的预设 #{index + 1}")
            
        except ValueError:
            await interaction.followup.send("❌ 请输入有效的数字", ephemeral=True)
        except Exception as e:
            logger.error(f"修改预设失败: {e}")
            await interaction.followup.send(f"❌ 修改失败: {str(e)}", ephemeral=True)


class DeletePresetModal(discord.ui.Modal, title='删除预设回复'):
    """删除预设的Modal"""
    
    preset_index = discord.ui.TextInput(
        label='预设编号',
        placeholder='输入要删除的预设编号（从1开始，此操作不可恢复）',
        required=True,
        max_length=3
    )
    
    def __init__(self, cog: MentionCog, thread_id: str):
        super().__init__()
        self.cog = cog
        self.thread_id = thread_id
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        try:
            index = int(self.preset_index.value) - 1
            
            thread_config = self.cog.threads.get(self.thread_id, {})
            presets = thread_config.get('xSettings', {}).get('preset', [])
            
            if index < 0 or index >= len(presets):
                await interaction.followup.send(f"❌ 预设编号无效，当前共有 {len(presets)} 个预设", ephemeral=True)
                return
            
            # 保存被删除的预设信息
            deleted_preset = presets[index]
            
            # 删除预设
            presets.pop(index)
            self.cog.threads[self.thread_id]['xSettings']['preset'] = presets
            self.cog.save_threads()
            
            embed = discord.Embed(
                title="🗑️ 预设已删除",
                description=f"已删除预设 #{index + 1}",
                color=discord.Color.red(),
                timestamp=datetime.now()
            )
            
            if len(deleted_preset) >= 3:
                embed.add_field(name="白名单关键词", value=deleted_preset[0], inline=False)
                embed.add_field(name="黑名单关键词", value=deleted_preset[1] if deleted_preset[1] else "无", inline=False)
                embed.add_field(name="回复内容", value=deleted_preset[2][:200] + ('...' if len(deleted_preset[2]) > 200 else ''), inline=False)
            
            embed.set_footer(text="此操作不可恢复")
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            logger.info(f"用户 {interaction.user.id} 删除了子区 {self.thread_id} 的预设 #{index + 1}")
            
        except ValueError:
            await interaction.followup.send("❌ 请输入有效的数字", ephemeral=True)
        except Exception as e:
            logger.error(f"删除预设失败: {e}")
            await interaction.followup.send(f"❌ 删除失败: {str(e)}", ephemeral=True)


async def setup(bot):
    """添加 Cog 到 bot"""
    await bot.add_cog(MentionCog(bot))
    logger.info("MentionCog 已加载")
