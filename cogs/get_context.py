import discord
from discord.ext import commands
from discord import app_commands
import os
import io
from datetime import datetime
import asyncio
import logging
from cogs.utils import check_admin, log_slash_command, safe_defer as _safe_defer

# 配置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class GetContextCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
    
    def _parse_user_ids(self, user_ids_str: str) -> list[int]:
        """
        解析用户ID字符串，返回用户ID列表
        """
        if not user_ids_str or not user_ids_str.strip():
            return []
        
        user_ids = []
        for uid_str in user_ids_str.split(','):
            uid_str = uid_str.strip()
            if uid_str:
                # 检查是否为纯数字
                if uid_str.isdigit():
                    user_id = int(uid_str)
                    user_ids.append(user_id)
                else:
                    raise ValueError(f"用户ID必须为纯数字: {uid_str}")
        
        return user_ids
    
    def _validate_user_lists(self, whitelist: list[int], blacklist: list[int]) -> None:
        """
        验证白名单和黑名单，检查是否有重复的用户ID
        """
        if whitelist and blacklist:
            # 检查是否有用户ID同时出现在白名单和黑名单中
            overlap = set(whitelist) & set(blacklist)
            if overlap:
                overlap_ids = ', '.join(str(uid) for uid in overlap)
                raise ValueError(f"以下用户ID同时出现在白名单和黑名单中，请检查: {overlap_ids}")
    
    def _should_include_message(self, author_id: int, whitelist: list[int], blacklist: list[int]) -> bool:
        """
        根据白名单和黑名单判断是否应该包含该消息
        """
        # 如果有白名单，只包含白名单中的用户
        if whitelist:
            return author_id in whitelist
        
        # 如果有黑名单，排除黑名单中的用户
        if blacklist:
            return author_id not in blacklist
        
        # 如果都没有，包含所有用户
        return True
    
    async def _collect_messages(self, channel: discord.TextChannel | discord.Thread, 
                              whitelist: list[int] = None, blacklist: list[int] = None) -> list[dict]:
        """
        收集频道或线程中的所有消息
        返回消息列表，每条消息包含用户名和内容
        """
        if whitelist is None:
            whitelist = []
        if blacklist is None:
            blacklist = []
        
        messages = []
        message_count = 0
        filtered_count = 0
        
        try:
            # 分批获取消息，每次100条
            async for message in channel.history(limit=None):
                # 跳过没有文字内容的消息
                if not message.content.strip():
                    continue
                
                # 根据白名单和黑名单过滤消息
                if not self._should_include_message(message.author.id, whitelist, blacklist):
                    filtered_count += 1
                    continue
                
                # 记录消息
                messages.append({
                    'username': message.author.display_name,
                    'content': message.content,
                    'timestamp': message.created_at,
                    'author_id': message.author.id
                })
                
                message_count += 1
                
                # 每100条消息暂停5秒，避免API速率限制
                if message_count % 100 == 0:
                    logger.info(f"已收集 {message_count} 条消息，暂停5秒...")
                    await asyncio.sleep(5)
            
            # 按时间顺序排序（最早的在前）
            messages.sort(key=lambda x: x['timestamp'])
            
            logger.info(f"总共收集了 {len(messages)} 条有效消息，过滤了 {filtered_count} 条消息")
            return messages
            
        except discord.Forbidden:
            logger.error("没有权限访问该频道的消息历史")
            raise
        except discord.HTTPException as e:
            logger.error(f"Discord API错误: {e}")
            raise
        except Exception as e:
            logger.error(f"收集消息时发生未知错误: {e}")
            raise
    
    def _create_temp_file(self, messages: list[dict], user_id: int) -> str:
        """
        创建临时文件存储消息内容
        返回文件路径
        """
        # 确保context_temp文件夹存在
        temp_dir = 'context_temp'
        if not os.path.exists(temp_dir):
            os.makedirs(temp_dir)
        
        # 生成文件名
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"{timestamp}_{user_id}_context.txt"
        filepath = os.path.join(temp_dir, filename)
        
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write("子区消息内容导出\n")
                f.write(f"导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"总消息数: {len(messages)}\n")
                f.write("=" * 50 + "\n\n")
                
                for msg in messages:
                    f.write(f"{msg['username']}: {msg['content']}\n")
            
            logger.info(f"临时文件已创建: {filepath}")
            return filepath
            
        except Exception as e:
            logger.error(f"创建临时文件失败: {e}")
            raise

    def _read_file_bytes(self, filepath: str) -> bytes:
        with open(filepath, 'rb') as f:
            return f.read()

    def _delete_file_if_exists(self, filepath: str) -> None:
        if os.path.exists(filepath):
            os.remove(filepath)
    
    async def _cleanup_file(self, filepath: str, delay: int = 300):
        """
        延迟删除临时文件（默认5分钟后删除）
        """
        try:
            await asyncio.sleep(delay)
            await asyncio.to_thread(self._delete_file_if_exists, filepath)
            logger.info(f"临时文件已清理: {filepath}")
        except Exception as e:
            logger.error(f"清理临时文件失败: {e}")
    
    @app_commands.command(name='获取子区内容', description='[仅管理员] 获取子区内的所有消息内容')
    @app_commands.describe(
        whitelist='可选：白名单用户ID列表，多个ID用英文逗号分隔（仅获取这些用户的消息）',
        blacklist='可选：黑名单用户ID列表，多个ID用英文逗号分隔（排除这些用户的消息）'
    )
    async def get_context(self, interaction: discord.Interaction, 
                         whitelist: str = None, blacklist: str = None):
        """获取子区内容的斜杠命令"""
        # 永远先defer
        await _safe_defer(interaction)
        
        try:
            # 检查权限
            if not check_admin(interaction):
                await interaction.followup.send(
                    "❌ 权限不足！此命令仅限管理员使用。",
                    ephemeral=True
                )
                log_slash_command(interaction, False)
                return
            
            # 检查是否在线程中
            if not isinstance(interaction.channel, discord.Thread):
                await interaction.followup.send(
                    "❌ 此命令只能在子区（线程）中使用！",
                    ephemeral=True
                )
                log_slash_command(interaction, False)
                return

            thread = interaction.channel
            
            # 解析和验证白名单和黑名单
            try:
                whitelist_ids = self._parse_user_ids(whitelist) if whitelist else []
                blacklist_ids = self._parse_user_ids(blacklist) if blacklist else []
                
                # 验证白名单和黑名单是否有重复
                self._validate_user_lists(whitelist_ids, blacklist_ids)
                
            except ValueError as e:
                await interaction.followup.send(
                    f"❌ 参数错误：{str(e)}",
                    ephemeral=True
                )
                log_slash_command(interaction, False)
                return
            
            # 构建过滤信息
            filter_info = []
            if whitelist_ids:
                filter_info.append(f"白名单用户: {len(whitelist_ids)} 个")
            if blacklist_ids:
                filter_info.append(f"黑名单用户: {len(blacklist_ids)} 个")
            
            filter_text = f" ({', '.join(filter_info)})" if filter_info else ""
            
            # 发送开始处理的消息
            await interaction.followup.send(
                f"🔄 开始收集子区消息{filter_text}，请稍候...",
                ephemeral=True
            )
            
            # 收集消息
            messages = await self._collect_messages(thread, whitelist_ids, blacklist_ids)
            
            if not messages:
                await interaction.followup.send(
                    "ℹ️ 该子区中没有找到任何文字消息。",
                    ephemeral=True
                )
                log_slash_command(interaction, True)
                return
            
            # 创建临时文件
            filepath = await asyncio.to_thread(self._create_temp_file, messages, interaction.user.id)
            
            # 发送文件
            file_bytes = await asyncio.to_thread(self._read_file_bytes, filepath)
            file = discord.File(
                io.BytesIO(file_bytes),
                filename=f"子区内容_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            )

            # 构建成功消息
            success_msg = f"✅ 成功收集了 {len(messages)} 条消息！\n"
            if filter_info:
                success_msg += f"🔍 应用过滤条件: {', '.join(filter_info)}\n"
            success_msg += "📁 文件将在5分钟后自动删除。"

            await interaction.followup.send(
                success_msg,
                file=file,
                ephemeral=True
            )
            log_slash_command(interaction, True)
            
            # 异步清理文件
            asyncio.create_task(self._cleanup_file(filepath))
            
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ 权限错误：无法访问该子区的消息历史。",
                ephemeral=True
            )
            log_slash_command(interaction, False)
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"❌ Discord API错误：{str(e)}",
                ephemeral=True
            )
            log_slash_command(interaction, False)
        except Exception as e:
            logger.error(f"获取子区内容时发生错误: {e}")
            await interaction.followup.send(
                "❌ 处理过程中发生错误，请稍后重试。",
                ephemeral=True
            )
            log_slash_command(interaction, False)

async def setup(bot: commands.Bot):
    await bot.add_cog(GetContextCog(bot))
