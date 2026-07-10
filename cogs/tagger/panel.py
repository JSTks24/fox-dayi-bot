from __future__ import annotations

import discord
from discord import app_commands
from datetime import datetime
from typing import Any
from cogs.shared.interactions import safe_defer as _safe_defer

class Fox14TagModal(discord.ui.Modal):
    """消息上下文标记确认表单"""
    def __init__(self, target_message: discord.Message, cog, scope_selection: str = "channel"):
        super().__init__(title=f"标记 - {target_message.author.display_name}")
        self.target_message = target_message
        self.target_user = target_message.author
        self.cog = cog
        self.scope_selection = scope_selection

        self.reason = discord.ui.TextInput(
            label="标记原因",
            placeholder="请输入标记原因（必填，≤300字符）",
            required=True,
            max_length=300,
            style=discord.TextStyle.short
        )

        self.expire = discord.ui.TextInput(
            label="自动过期",
            placeholder="支持 -1/Nh/Nd/Nm；留空默认7d",
            required=False,
            style=discord.TextStyle.short
        )

        self.add_item(self.reason)
        self.add_item(self.expire)

    async def on_submit(self, interaction: discord.Interaction):
        # 提交时遵循黄金法则：统一使用 safe_defer，占坑后续用 followup
        await _safe_defer(interaction)

        # 权限校验
        if not self.cog._has_admin_or_trusted(interaction):
            await interaction.followup.send("❌ 权限不足：仅管理员或受信任用户可用。", ephemeral=True)
            return

        # 服务器一致性
        if interaction.guild is None or self.target_message.guild is None or interaction.guild.id != self.target_message.guild.id:
            await interaction.followup.send("❌ 消息与当前服务器不一致（跨服或缺失）。", ephemeral=True)
            return

        # 原因校验
        reason = (self.reason.value or "").strip()
        if not reason:
            await interaction.followup.send("❌ 参数错误：原因必填。", ephemeral=True)
            return
        if len(reason) > 300:
            await interaction.followup.send("❌ 参数错误：原因长度需 ≤ 300。", ephemeral=True)
            return

        # 解析过期
        raw_expire = self.expire.value if self.expire.value else None
        ok, err, expire_epoch, normalized_input = type(self.cog)._parse_expire_input(raw_expire)
        if not ok or expire_epoch is None:
            await interaction.followup.send(f"❌ {err}", ephemeral=True)
            return

        # 来源消息链接
        message_link = f"https://discord.com/channels/{self.target_message.guild.id}/{self.target_message.channel.id}/{self.target_message.id}"

        # 写入数据库
        try:
            scope_id = -1 if (self.scope_selection or "channel").lower() == "guild" else interaction.channel.id
            record_id = await self.cog._insert_record(
                guild_id=self.target_message.guild.id,
                target_user_id=self.target_user.id,
                message_link=message_link,
                reason=reason,
                tagger_id=interaction.user.id,
                tagger_name=interaction.user.name,
                expire_at_epoch=expire_epoch,
                expire_input=normalized_input,
                scope_id=scope_id
            )
        except Exception as e:
            await interaction.followup.send(f"❌ 写入数据库失败：{e}", ephemeral=True)
            return

        # 成功反馈
        expire_str = type(self.cog)._format_beijing_time_from_epoch(expire_epoch)
        embed = discord.Embed(
            title="✅ 标记创建成功",
            color=discord.Color.green(),
            timestamp=datetime.now()
        )
        embed.add_field(name="标记ID", value=str(record_id), inline=True)
        embed.add_field(name="目标用户", value=f"{self.target_user.mention} ({self.target_user.id})", inline=False)
        embed.add_field(name="原因", value=reason, inline=False)
        embed.add_field(name="来源消息", value=message_link, inline=False)
        embed.add_field(name="标记者", value=f"{interaction.user.mention}", inline=False)
        embed.add_field(name="自动过期", value=expire_str, inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        try:
            await _safe_defer(interaction)
        except Exception:
            pass
        try:
            await interaction.followup.send(f"❌ 发生错误：{str(error)}", ephemeral=True)
        except Exception:
            pass

@app_commands.context_menu(name="标记")
@app_commands.guild_only()
async def fox14_tag_context(interaction: discord.Interaction, message: discord.Message):
    """Fox14 标记的消息上下文菜单命令：以该消息为来源标记其作者"""
    # 获取cog实例
    cog = interaction.client.get_cog('Fox14Tagger')
    if not cog:
        await interaction.response.send_message("❌ 模块未加载", ephemeral=True)
        return

    # 权限校验
    if not cog._has_admin_or_trusted(interaction):
        await interaction.response.send_message("❌ 权限不足：仅管理员或受信任用户可用。", ephemeral=True)
        return

    # 入口遵循黄金法则：先 defer，然后编辑原始临时响应为面板
    await _safe_defer(interaction)

    view = Fox14TagPanelView(cog=cog, target_message=message)
    embed = await view.build_embed(interaction.guild)
    await interaction.edit_original_response(embed=embed, view=view)

class Fox14TagPanelView(discord.ui.View):
    """四按钮标记面板 View：公益站 / 不发插头 / 第三方平台 / 自定义"""
    def __init__(self, cog: Any, target_message: discord.Message):
        super().__init__(timeout=900)  # 约15分钟
        self.cog = cog
        self.target_message = target_message
        self.target_user = target_message.author
        self.message_link = f"https://discord.com/channels/{target_message.guild.id}/{target_message.channel.id}/{target_message.id}"
        # 范围选择，默认单频道
        self.scope_selection: str = "channel"
        scope_select = discord.ui.Select(
            placeholder="选择标记范围",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="单频道标记", value="channel", default=True),
                discord.SelectOption(label="全服标记", value="guild")
            ]
        )
        async def _on_scope_change(interaction: discord.Interaction):
            await _safe_defer(interaction)
            try:
                self.scope_selection = scope_select.values[0]
            except Exception:
                self.scope_selection = "channel"
            # 轻量反馈
            await interaction.followup.send(f"范围已设置为：{'全服' if self.scope_selection=='guild' else '单频道'}", ephemeral=True)
        scope_select.callback = _on_scope_change
        self.add_item(scope_select)

    async def _fetch_member(self, guild: discord.Guild | None) -> discord.Member | None:
        """尝试从缓存或远端获取成员对象"""
        if guild is None:
            return None
        member = guild.get_member(self.target_user.id)
        if member:
            return member
        try:
            return await guild.fetch_member(self.target_user.id)
        except Exception:
            return None

    async def build_embed(self, guild: discord.Guild | None) -> discord.Embed:
        """构建面板 Embed（不显示头像）"""
        embed = discord.Embed(
            title="Fox14 标记面板",
            color=discord.Color.orange(),
            timestamp=datetime.now()
        )
        display_name = getattr(self.target_user, "display_name", self.target_user.name)
        embed.add_field(name="用户名", value=str(display_name), inline=True)
        embed.add_field(name="用户ID", value=str(self.target_user.id), inline=True)

        # 加入时间
        joined_disp = "未知"
        member = await self._fetch_member(guild)
        if member and getattr(member, "joined_at", None):
            try:
                epoch = int(member.joined_at.timestamp())
                joined_disp = type(self.cog)._format_beijing_time_from_epoch(epoch)
            except Exception:
                joined_disp = "未知"
        embed.add_field(name="加入时间", value=joined_disp, inline=False)

        # 最近3条“正常”记录
        records: list[dict[str, Any]] = []
        if guild is not None:
            try:
                records = (await self.cog._list_user_normal_records(guild.id, self.target_user.id))[:3]
            except Exception:
                records = []
        if not records:
            embed.add_field(name="最近记录", value="暂无记录", inline=False)
        else:
            for r in records:
                try:
                    expire_epoch = int(r['expire_at_epoch'])
                except Exception:
                    expire_epoch = -1
                expire_str = "永不过期" if expire_epoch == -1 else type(self.cog)._format_beijing_time_from_epoch(expire_epoch)
                msg_link = r['message_link'] if r.get('message_link') and r['message_link'] != "未提供" else "未提供"
                reason = r.get('reason', '')
                reason_disp = (reason[:100] + ('...' if len(reason) > 100 else '')) if reason else '（无）'
                field_name = f"#{r['id']} - {r['tagged_at']}"
                field_value = (
                    f"标记者: {r['tagger_name']} (ID: {r['tagger_id']})\n"
                    f"原因: {reason_disp}\n"
                    f"目标消息: {msg_link}\n"
                    f"过期: {expire_str}"
                )
                embed.add_field(name=field_name, value=field_value, inline=False)
        return embed

    def _disable_all(self):
        """禁用所有按钮，避免重复提交"""
        for item in self.children:
            try:
                item.disabled = True
            except Exception:
                pass

    async def _do_quick_tag(self, interaction: discord.Interaction, reason_text: str):
        """三个快捷按钮的统一处理：先 defer，再写库，最后刷新面板并禁用按钮"""
        await _safe_defer(interaction)

        # 权限与一致性校验
        if not self.cog._has_admin_or_trusted(interaction):
            await interaction.followup.send("❌ 权限不足：仅管理员或受信任用户可用。", ephemeral=True)
            return
        if interaction.guild is None or self.target_message.guild is None or interaction.guild.id != self.target_message.guild.id:
            await interaction.followup.send("❌ 消息与当前服务器不一致（跨服或缺失）。", ephemeral=True)
            return

        # 过期固定为 60 天
        ok, err, expire_epoch, normalized_input = type(self.cog)._parse_expire_input('60d')
        if not ok or expire_epoch is None:
            await interaction.followup.send(f"❌ {err}", ephemeral=True)
            return
        
        # 写入数据库
        try:
            scope_id = -1 if (self.scope_selection or "channel").lower() == "guild" else interaction.channel.id
            await self.cog._insert_record(
                guild_id=self.target_message.guild.id,
                target_user_id=self.target_user.id,
                message_link=self.message_link,
                reason=reason_text,
                tagger_id=interaction.user.id,
                tagger_name=interaction.user.name,
                expire_at_epoch=expire_epoch,
                expire_input=normalized_input,
                scope_id=scope_id
            )
        except Exception as e:
            await interaction.followup.send(f"❌ 写入数据库失败：{e}", ephemeral=True)
            return

        # 刷新面板并禁用按钮
        try:
            embed = await self.build_embed(interaction.guild)
            self._disable_all()
            await interaction.edit_original_response(embed=embed, view=self)
        except Exception:
            pass

    @discord.ui.button(label="公益站", style=discord.ButtonStyle.primary)
    async def btn_gongyi(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._do_quick_tag(interaction, "公益站")

    @discord.ui.button(label="不发插头", style=discord.ButtonStyle.primary)
    async def btn_no_plug(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._do_quick_tag(interaction, "不发插头")

    @discord.ui.button(label="第三方平台", style=discord.ButtonStyle.primary)
    async def btn_thirdparty(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._do_quick_tag(interaction, "第三方平台")

    @discord.ui.button(label="自定义", style=discord.ButtonStyle.secondary)
    async def btn_custom(self, interaction: discord.Interaction, button: discord.ui.Button):
        # send_modal 例外：不能 defer
        modal = Fox14TagModal(target_message=self.target_message, cog=self.cog, scope_selection=self.scope_selection)
        await interaction.response.send_modal(modal)
