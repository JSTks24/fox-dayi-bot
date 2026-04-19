from __future__ import annotations

import discord
from discord import app_commands
import json
import os
from datetime import datetime, timedelta
import logging
from typing import Any

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

class ThreadConfigControlView(discord.ui.View):
    """子区配置控制面板的按钮视图"""
    
    def __init__(self, cog: Any, thread_id: str, user_id: int):
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

class SetOwnerModal(discord.ui.Modal, title='设置楼主'):
    """设置楼主的Modal"""
    
    owner_id = discord.ui.TextInput(
        label='楼主ID',
        placeholder='输入楼主的用户ID（数字）',
        required=True,
        max_length=20
    )
    
    def __init__(self, cog: Any, thread_id: str):
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
    
    def __init__(self, cog: Any, thread_id: str):
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
    
    def __init__(self, cog: Any, thread_id: str):
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

class MentionConfigMixin:
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
            app_commands.Choice(name='移出黑名单', value='remove'),
        ],
        scope=[
            app_commands.Choice(name='仅当前帖子', value='thread'),
            app_commands.Choice(name='全局', value='global'),
        ],
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
