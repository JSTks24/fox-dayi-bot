from __future__ import annotations

import discord
from discord import app_commands
import os
from datetime import datetime
import logging
import time

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

THREAD_METADATA_CACHE_TTL_SECONDS = 24 * 60 * 60

class MentionKBMixin:
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
