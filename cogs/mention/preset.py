from __future__ import annotations

import discord
from discord import app_commands
from datetime import datetime
import logging
from typing import Any

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

class PresetControlView(discord.ui.View):
    """预设回复控制面板的按钮视图"""
    
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

class ViewPresetModal(discord.ui.Modal, title='查看预设详情'):
    """查看预设的Modal"""
    
    preset_index = discord.ui.TextInput(
        label='预设编号',
        placeholder='输入要查看的预设编号（从1开始）',
        required=True,
        max_length=3
    )
    
    def __init__(self, cog: Any, thread_id: str):
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
    
    def __init__(self, cog: Any, thread_id: str):
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
    
    def __init__(self, cog: Any, thread_id: str):
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
    
    def __init__(self, cog: Any, thread_id: str):
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

class MentionPresetMixin:
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
