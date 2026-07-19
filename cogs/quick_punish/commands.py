from __future__ import annotations

import discord
from discord import app_commands
from datetime import datetime
import re
from typing import Any
import io
from cogs.shared.interactions import safe_defer
from cogs.shared.permissions import check_admin_or_trusted


_QUICK_PUNISH_MESSAGE_LINK_RE = re.compile(
    r"^https://(?:discord(?:app)?\.com)/channels/(\d+)/(\d+)/(\d+)$"
)
_SCHEDULED_DELAY_RE = re.compile(r"^([1-9]\d*)([mh])$")


def parse_scheduled_delay(value: str) -> int:
    """Parse a strict 1-120 minute or 1-2 hour delay."""
    match = _SCHEDULED_DELAY_RE.fullmatch(value)
    if not match:
        raise ValueError("延迟时间必须为 1m-120m 或 1h-2h")

    amount = int(match.group(1))
    unit = match.group(2)
    if (unit == "m" and amount <= 120) or (unit == "h" and amount <= 2):
        return amount * (60 if unit == "m" else 3600)
    raise ValueError("延迟时间必须为 1m-120m 或 1h-2h")


def _scheduled_punish_validation_error(
    interaction: discord.Interaction,
    message: discord.Message,
    cog: Any,
) -> str | None:
    if not cog.enabled:
        return "❌ 预约送走功能未启用，请联系机器人开发者。"
    if interaction.guild is None:
        return "❌ 该命令只能在服务器内使用。"
    if not check_admin_or_trusted(interaction):
        return "❌ 没权。只有管理员和 trusted_user 可以预约送走。"
    if message.author.bot:
        return "❌ 不能对Bot使用这个命令。"
    if cog.has_active_scheduled_punishment(message.author.id):
        return "❌ 该用户已有倒计时中的预约送走，不能再次预约或提前执行处罚。"
    return None


def _format_punishment_query_record(record: dict[str, Any], *, markdown_link: bool) -> str:
    reason = record.get("reason") or "未记录"
    executor_name = record.get("executor_name") or "未记录"
    message_link = record.get("original_message_link")
    if message_link:
        message_text = f"[跳转]({message_link})" if markdown_link else message_link
    else:
        message_text = "无（同步/旧记录未保存）"
    return (
        f"全局处罚序号：{record.get('punish_count') or '未记录'}\n"
        f"时间：{record.get('timestamp') or '未记录'}\n"
        f"原因：{reason}\n"
        f"执行者：{executor_name}\n"
        f"状态：{record.get('status') or '未记录'}\n"
        f"来源：{record.get('source_type') or 'local'}\n"
        f"原消息：{message_text}"
    )


def _parse_quick_punish_message_link(message_link: str) -> tuple[int, int, int] | None:
    """Parse a Discord message link into guild/channel/message IDs."""
    if not message_link:
        return None

    match = _QUICK_PUNISH_MESSAGE_LINK_RE.match(message_link.strip())
    if not match:
        return None

    return int(match.group(1)), int(match.group(2)), int(match.group(3))


async def _resolve_quick_punish_channel(interaction: discord.Interaction, channel_id: int):
    """Resolve target channels with cache-first lookup and API fallback."""
    client = interaction.client
    channel = client.get_channel(channel_id)
    if channel is not None:
        return channel

    guild = interaction.guild
    if guild is not None:
        channel = guild.get_channel(channel_id)
        if channel is not None:
            return channel

        thread = guild.get_thread(channel_id)
        if thread is not None:
            return thread

    return await client.fetch_channel(channel_id)


async def _resolve_quick_punish_message_from_link(
    interaction: discord.Interaction,
    message_link: str,
) -> tuple[discord.Message | None, str | None]:
    """Resolve and validate the punishment target message from a message link."""
    parsed = _parse_quick_punish_message_link(message_link)
    if not parsed:
        return None, (
            "❌ 无效的消息链接格式。请提供 "
            "`https://discord.com/channels/服务器ID/频道ID/消息ID`。"
        )

    guild = interaction.guild
    if guild is None:
        return None, "❌ 该命令只能在服务器内使用。"

    guild_id, channel_id, message_id = parsed
    if guild_id != guild.id:
        return None, "❌ 只能处理当前服务器的消息链接，不支持跨服务器。"

    try:
        channel = await _resolve_quick_punish_channel(interaction, channel_id)
    except discord.NotFound:
        return None, "❌ 找不到消息所在频道，可能已被删除。"
    except discord.Forbidden:
        return None, "❌ 无法访问该消息所在频道或线程。"
    except discord.HTTPException:
        return None, "❌ 获取消息所在频道失败，请稍后重试。"

    if getattr(getattr(channel, "guild", None), "id", guild.id) != guild.id:
        return None, "❌ 只能处理当前服务器的消息链接，不支持跨服务器。"

    if isinstance(channel, discord.Thread):
        try:
            await channel.join()
        except (discord.Forbidden, discord.HTTPException):
            pass

    if not hasattr(channel, "fetch_message"):
        return None, "❌ 无法访问该消息所在频道或线程。"

    try:
        target_message = await channel.fetch_message(message_id)
    except discord.NotFound:
        return None, "❌ 找不到目标消息，可能已被删除。"
    except discord.Forbidden:
        return None, "❌ 无法访问目标消息，可能是频道或线程不可见。"
    except discord.HTTPException:
        return None, "❌ 获取目标消息失败，请稍后重试。"

    return target_message, None

class QuickPunishModal(discord.ui.Modal):
    """快速处罚确认表单"""
    
    def __init__(self, target_message: discord.Message, cog):
        super().__init__(title=f"快速处罚 - {target_message.author.display_name}")
        self.target_message = target_message
        self.target_user = target_message.author
        self.cog = cog
        # 原因输入框（最多100字符）
        self.reason = discord.ui.TextInput(
            label="处罚原因",
            placeholder="请输入处罚原因（留空则使用默认值'违规第三方'）",
            required=False,
            max_length=100,
            style=discord.TextStyle.short
        )
        self.add_item(self.reason)
    
    # 已移除用户名/ID二次确认输入框及校验机制
    
    async def on_submit(self, interaction: discord.Interaction):
        """处理表单提交"""
        # 立即defer以避免超时
        await safe_defer(interaction)

        # 获取处罚原因
        reason = self.reason.value.strip() or "违规第三方"

        # 构建二次确认Embed（包含用户信息、原因、模板文件名占位）
        user = self.target_user
        embed = discord.Embed(
            title="二次确认 - 快速处罚",
            color=discord.Color.orange(),
            timestamp=datetime.now()
        )
        embed.set_author(
            name=f"{user.display_name} (@{user.name})",
            icon_url=user.avatar.url if getattr(user, "avatar", None) else None
        )
        if getattr(user, "avatar", None):
            embed.set_thumbnail(url=user.avatar.url)
        embed.add_field(name="显示名称", value=user.display_name, inline=True)
        embed.add_field(name="用户名", value=user.name, inline=True)
        embed.add_field(name="ID", value=str(user.id), inline=False)
        embed.add_field(name="处罚原因", value=reason, inline=False)
        embed.add_field(name="私信模板", value="未选择", inline=False)

        # 带有下拉选单与确认/取消按钮的视图
        view = QuickPunishConfirmView(
            cog=self.cog,
            target_message=self.target_message,
            target_user=self.target_user,
            reason=reason
        )

        # 在当前频道以临时消息发送二次确认
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
    
    async def on_error(self, interaction: discord.Interaction, error: Exception):
        """处理错误"""
        print(f"QuickPunishModal错误: {error}")
        try:
            await safe_defer(interaction)
            await interaction.followup.send(
                f"❌ 发生错误：{str(error)}", 
                ephemeral=True
            )
        except Exception:
            pass

class RemoteQuickPunishModal(discord.ui.Modal):
    """远距快速处罚表单：在一个modal里完成原因与私信模板选择，提交即执行"""

    def __init__(self, target_message: discord.Message, cog):
        super().__init__(title=target_message.author.display_name)
        self.target_message = target_message
        self.target_user = target_message.author
        self.cog = cog

        self.reason = discord.ui.TextInput(
            placeholder="请输入处罚原因（留空则使用默认值'违规第三方'）",
            required=False,
            max_length=100,
            style=discord.TextStyle.short
        )
        self.add_item(discord.ui.Label(
            text="处罚原因",
            description="留空则使用默认值“违规第三方”",
            component=self.reason
        ))

        self.template_select = discord.ui.Select(
            placeholder="选择私信模板（选择“默认”则使用第三方API）",
            min_values=1,
            max_values=1,
            options=self.cog._get_dm_template_select_options()
        )
        self.add_item(discord.ui.Label(
            text="私信模板",
            description="请选择模板；选择“默认（第三方API）”时将使用 default.txt",
            component=self.template_select
        ))

    async def on_submit(self, interaction: discord.Interaction):
        await safe_defer(interaction)

        reason = self.reason.value.strip() or "违规第三方"
        selected_values = getattr(self.template_select, "values", []) or []
        chosen_template = selected_values[0] if selected_values and selected_values[0] != "__none__" else "default.txt"

        success, message, punishment_history = await self.cog.execute_punishment(
            trigger_guild=interaction.guild,
            target_user=self.target_user,
            target_message=self.target_message,
            reason=reason,
            executor=interaction.user,
            dm_template_filename=chosen_template
        )

        result_embed = self.cog.build_punishment_result_embed(success, message, punishment_history, interaction.user)
        await interaction.followup.send(embed=result_embed, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"RemoteQuickPunishModal错误: {error}")
        try:
            await safe_defer(interaction)
            await interaction.followup.send(f"❌ 发生错误：{str(error)}", ephemeral=True)
        except Exception:
            pass


class ScheduledQuickPunishModal(discord.ui.Modal):
    """Schedule a punishment after collecting the remote-punish fields."""

    def __init__(self, target_message: discord.Message, cog: Any) -> None:
        super().__init__(title=f"预约送走 - {target_message.author.display_name}")
        self.target_message = target_message
        self.cog = cog

        self.reason = discord.ui.TextInput(
            placeholder="请输入处罚原因（留空则使用默认值'违规第三方'）",
            required=False,
            max_length=100,
            style=discord.TextStyle.short,
        )
        self.add_item(discord.ui.Label(text="处罚原因", component=self.reason))

        self.template_select = discord.ui.Select(
            placeholder="选择私信模板（选择“默认”则使用第三方API）",
            min_values=1,
            max_values=1,
            options=self.cog._get_dm_template_select_options(),
        )
        self.add_item(discord.ui.Label(text="私信模板", component=self.template_select))

        self.delay = discord.ui.TextInput(
            placeholder="例如 30m 或 2h",
            required=True,
            max_length=4,
            style=discord.TextStyle.short,
        )
        self.add_item(discord.ui.Label(text="延迟时间", description="允许 1m-120m 或 1h-2h", component=self.delay))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await safe_defer(interaction, ephemeral=False)
        if error_message := _scheduled_punish_validation_error(interaction, self.target_message, self.cog):
            await interaction.followup.send(error_message, ephemeral=True)
            return
        try:
            delay_seconds = parse_scheduled_delay(self.delay.value)
        except ValueError as error:
            await interaction.followup.send(f"❌ {error}", ephemeral=True)
            return

        selected_values = getattr(self.template_select, "values", []) or []
        chosen_template = selected_values[0] if selected_values and selected_values[0] != "__none__" else "default.txt"
        success, message = await self.cog.schedule_punishment(
            target_message=self.target_message,
            trigger_guild=interaction.guild,
            creator=interaction.user,
            reason=self.reason.value.strip() or "违规第三方",
            dm_template_filename=chosen_template,
            delay_seconds=delay_seconds,
        )
        if success:
            embed = self.cog.build_scheduled_punishment_created_embed(
                target_user=self.target_message.author,
                operator=interaction.user,
                execution_notice=message,
            )
            await interaction.followup.send(embed=embed, ephemeral=False)
            return
        await interaction.followup.send("❌ " + message, ephemeral=True)


async def _send_quick_punish_modal(
    interaction: discord.Interaction,
    message: discord.Message,
    cog,
    mode: str,
):
    """Open the configured quick punish modal for the resolved target message."""
    modal_cls = RemoteQuickPunishModal if mode == "remote" else QuickPunishModal
    await interaction.response.send_modal(modal_cls(target_message=message, cog=cog))

class QuickPunishConfirmView(discord.ui.View):
    """二次确认视图：包含模板选择下拉选单与确认/取消按钮"""
    def __init__(self, cog, target_message: discord.Message, target_user: discord.User, reason: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.target_message = target_message
        self.target_user = target_user
        self.reason = reason
        self.selected_template_filename: str | None = None
        # Add the dropdown from the configured DM template directory.
        self.add_item(TemplateSelect(cog=self.cog))

    def _disable_all(self):
        for child in self.children:
            try:
                child.disabled = True
            except Exception:
                pass

    @discord.ui.button(label="OK你已经死亡", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 黄金法则：先defer
        await safe_defer(interaction)

        # Fall back to the default DM template when none is selected.
        chosen_template = self.selected_template_filename or "default.txt"

        # 执行处罚
        success, message, punishment_history = await self.cog.execute_punishment(
            trigger_guild=interaction.guild,
            target_user=self.target_user,
            target_message=self.target_message,
            reason=self.reason,
            executor=interaction.user,
            dm_template_filename=chosen_template
        )

        # 更新消息（移除交互视图）
        self._disable_all()
        embed = self.cog.build_punishment_result_embed(success, message, punishment_history, interaction.user)
        await interaction.edit_original_response(embed=embed, view=None)

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 黄金法则：先defer
        await safe_defer(interaction)
        self._disable_all()
        embed = discord.Embed(
            title="操作已取消",
            description="本次快速处罚未执行。",
            color=discord.Color.greyple(),
            timestamp=datetime.now()
        )
        await interaction.edit_original_response(embed=embed, view=None)

class RevokeConfirmView(discord.ui.View):
    """撤销二次确认：当最近记录是sync时，确认是否回溯撤销local记录"""

    def __init__(self, cog, target_user_id: str, latest_record: dict[str, Any], revoke_record: dict[str, Any], restore_roles: bool = True):
        super().__init__(timeout=180)
        self.cog = cog
        self.target_user_id = target_user_id
        self.latest_record = latest_record
        self.revoke_record = revoke_record
        self.restore_roles = restore_roles

    def _disable_all(self):
        for child in self.children:
            try:
                child.disabled = True
            except Exception:
                pass

    @discord.ui.button(label="确认继续撤销", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await safe_defer(interaction)
        self._disable_all()

        success, message = await self.cog._execute_revoke_record(
            interaction=interaction,
            user_id=self.target_user_id,
            record=self.revoke_record,
            restore_roles=self.restore_roles
        )

        result_embed = discord.Embed(
            title="✅ 撤销完成" if success else "❌ 撤销失败",
            description=message,
            color=discord.Color.green() if success else discord.Color.red(),
            timestamp=datetime.now()
        )
        await interaction.edit_original_response(embed=result_embed, view=None)

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await safe_defer(interaction)
        self._disable_all()
        embed = discord.Embed(
            title="操作已取消",
            description="本次撤销未执行。",
            color=discord.Color.greyple(),
            timestamp=datetime.now()
        )
        await interaction.edit_original_response(embed=embed, view=None)

    async def on_timeout(self):
        self._disable_all()

class TemplateSelect(discord.ui.Select):
    """下拉选单：选择要发送的私信模板（文件名）"""
    def __init__(self, cog):
        self.cog = cog
        options = self.cog._get_dm_template_select_options()
        super().__init__(placeholder="要发送的私信模板（选择“默认”则使用第三方API）", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        # 黄金法则：先defer
        await safe_defer(interaction)

        chosen = self.values[0]
        view: QuickPunishConfirmView = self.view
        if chosen == "__none__":
            view.selected_template_filename = None
        else:
            view.selected_template_filename = chosen

        # 更新确认Embed，显示已选择的模板文件名
        user = view.target_user
        embed = discord.Embed(
            title="二次确认 - 快速处罚",
            color=discord.Color.orange(),
            timestamp=datetime.now()
        )
        embed.set_author(
            name=f"{user.display_name} (@{user.name})",
            icon_url=user.avatar.url if getattr(user, "avatar", None) else None
        )
        if getattr(user, "avatar", None):
            embed.set_thumbnail(url=user.avatar.url)
        embed.add_field(name="显示名称", value=user.display_name, inline=True)
        embed.add_field(name="用户名", value=user.name, inline=True)
        embed.add_field(name="ID", value=str(user.id), inline=False)
        embed.add_field(name="处罚原因", value=view.reason or "违规第三方", inline=False)
        embed.add_field(name="私信模板", value=view.selected_template_filename or "默认（第三方API）", inline=False)

        await interaction.edit_original_response(embed=embed, view=view)

async def _validate_quick_punish_context(interaction: discord.Interaction,
                                         message: discord.Message,
                                         action_name: str) -> Any:
    cog = interaction.client.get_cog('QuickPunishCog')
    if not cog:
        await interaction.response.send_message("❌ 模块未加载", ephemeral=True)
        return None

    if not cog.enabled:
        await interaction.response.send_message(f"❌ {action_name}功能未启用，请联系机器人开发者。", ephemeral=True)
        return None

    if not cog.has_permission(interaction):
        await interaction.response.send_message(
            f"❌ 没权。只有管理组和类脑自研答疑AI可以给人{action_name}。",
            ephemeral=True
        )
        return None

    if message.author.bot:
        await interaction.response.send_message("❌ 不能对Bot使用这个命令。", ephemeral=True)
        return None

    if cog.has_active_scheduled_punishment(message.author.id):
        await interaction.response.send_message(
            "❌ 该用户已有倒计时中的预约送走，不能提前执行处罚。",
            ephemeral=True,
        )
        return None

    return cog

@app_commands.context_menu(name="愉悦送走")
@app_commands.guild_only()
async def quick_punish_context(interaction: discord.Interaction, message: discord.Message):
    """快速处罚上下文菜单命令"""
    cog = await _validate_quick_punish_context(interaction, message, "愉悦送走")
    if not cog:
        return

    await _send_quick_punish_modal(interaction, message, cog, "normal")

@app_commands.context_menu(name="远距送走")
@app_commands.guild_only()
async def remote_quick_punish_context(interaction: discord.Interaction, message: discord.Message):
    """远距快速处罚上下文菜单命令：在一个modal内完成所有输入并直接执行"""
    cog = await _validate_quick_punish_context(interaction, message, "远距送走")
    if not cog:
        return

    await _send_quick_punish_modal(interaction, message, cog, "remote")


@app_commands.context_menu(name="预约送走")
@app_commands.guild_only()
async def scheduled_quick_punish_context(interaction: discord.Interaction, message: discord.Message) -> None:
    """Open the in-memory scheduled punishment modal."""
    cog = interaction.client.get_cog("QuickPunishCog")
    if not cog:
        await interaction.response.send_message("❌ 模块未加载", ephemeral=True)
        return
    if error_message := _scheduled_punish_validation_error(interaction, message, cog):
        await interaction.response.send_message(error_message, ephemeral=True)
        return

    await interaction.response.send_modal(ScheduledQuickPunishModal(message, cog))

class QuickPunishCommandsMixin:
    @app_commands.command(name="快速处罚", description="通过消息链接打开快速处罚流程")
    @app_commands.describe(
        message_link="目标消息链接（右键消息 -> 复制消息链接）",
        mode="处罚模式",
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="普通", value="normal"),
            app_commands.Choice(name="远距", value="remote"),
        ]
    )
    @app_commands.guild_only()
    async def quick_punish_slash(
        self,
        interaction: discord.Interaction,
        message_link: str,
        mode: app_commands.Choice[str],
    ):
        """Open the quick punish flow from a message link."""
        target_message, error_message = await _resolve_quick_punish_message_from_link(
            interaction,
            message_link,
        )
        if error_message:
            await interaction.response.send_message(error_message, ephemeral=True)
            return

        cog = await _validate_quick_punish_context(interaction, target_message, "快速处罚")
        if not cog:
            return

        await _send_quick_punish_modal(interaction, target_message, cog, mode.value)

    @app_commands.command(name="预约送走-取消", description="取消自己尚未开始执行的全部预约送走")
    @app_commands.guild_only()
    async def scheduled_quick_punish_cancel(self, interaction: discord.Interaction) -> None:
        await safe_defer(interaction)
        if not self.enabled:
            await interaction.followup.send("❌ 快速处罚功能未启用", ephemeral=True)
            return
        if not check_admin_or_trusted(interaction):
            await interaction.followup.send("❌ 您没有权限使用此命令", ephemeral=True)
            return

        cancelled, skipped_running, residue_links = await self.cancel_scheduled_punishments(interaction.user.id)
        parts = [f"已取消 {cancelled} 个尚未执行的预约。"]
        if skipped_running:
            parts.append(f"跳过 {skipped_running} 个正在执行的预约。")
        if residue_links:
            parts.append("以下留存消息删除失败：\n" + "\n".join(residue_links))
        await interaction.followup.send("\n".join(parts), ephemeral=True)

    @app_commands.command(name="预约送走-列表", description="查看当前全部预约送走")
    @app_commands.guild_only()
    async def scheduled_quick_punish_list(self, interaction: discord.Interaction) -> None:
        await safe_defer(interaction)
        if not self.enabled:
            await interaction.followup.send("❌ 快速处罚功能未启用", ephemeral=True)
            return
        if not check_admin_or_trusted(interaction):
            await interaction.followup.send("❌ 您没有权限使用此命令", ephemeral=True)
            return
        content = self.format_scheduled_punishments()
        if not content:
            await interaction.followup.send("📝 当前没有预约送走。", ephemeral=True)
            return
        if len(content) <= 4096:
            await interaction.followup.send(
                embed=discord.Embed(title="预约送走列表", description=content, color=discord.Color.blue()),
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            file=discord.File(io.BytesIO(content.encode("utf-8")), filename="scheduled_quick_punishments.txt"),
            ephemeral=True,
        )

    @app_commands.command(name="快速处罚-查询", description="按用户查询快速处罚记录")
    @app_commands.describe(user="选择要查询的用户", user_id="直接输入用户 ID（优先于用户选择器）")
    @app_commands.guild_only()
    async def quick_punish_query(
        self,
        interaction: discord.Interaction,
        user: discord.User | None = None,
        user_id: str | None = None,
    ) -> None:
        """Query all stored punishment statuses for one user ID."""
        await safe_defer(interaction)
        if not self.enabled:
            await interaction.followup.send("❌ 快速处罚功能未启用", ephemeral=True)
            return
        if not self.has_permission(interaction):
            await interaction.followup.send("❌ 您没有权限使用此命令", ephemeral=True)
            return

        if user_id is not None:
            resolved_user_id = user_id.strip()
            if not re.fullmatch(r"[0-9]+", resolved_user_id) or int(resolved_user_id) <= 0:
                await interaction.followup.send("❌ 无效的用户ID格式，请输入正十进制数字", ephemeral=True)
                return
        elif user is not None:
            resolved_user_id = str(user.id)
        else:
            await interaction.followup.send("❌ 用户和用户ID至少填写一个", ephemeral=True)
            return

        records = await self.get_punishments_for_user(resolved_user_id)
        if not records:
            await interaction.followup.send(
                f"📝 未找到用户 {resolved_user_id} 的快速处罚记录",
                ephemeral=True,
            )
            return

        if user_id is None and user is not None:
            display_name = getattr(user, "display_name", None) or getattr(user, "name", resolved_user_id)
        else:
            display_name = records[0].get("user_name") or resolved_user_id
        title = f"快速处罚记录：{display_name} ({resolved_user_id})"

        if len(records) <= 10:
            embed = discord.Embed(
                title=title,
                color=discord.Color.blue(),
                timestamp=datetime.now(),
            )
            for record in records:
                embed.add_field(
                    name=f"记录 #{record['id']}",
                    value=_format_punishment_query_record(record, markdown_link=True),
                    inline=False,
                )
            embed.set_footer(text=f"查询者: {interaction.user.name}")
            await interaction.followup.send(embed=embed, ephemeral=True)
            return

        formatted_records = [
            f"记录 #{record['id']}\n{_format_punishment_query_record(record, markdown_link=False)}"
            for record in records
        ]
        content = f"{title}\n\n" + "\n\n".join(formatted_records)
        await interaction.followup.send(
            f"📋 找到 {len(records)} 条快速处罚记录，已生成文件：",
            file=discord.File(
                io.BytesIO(content.encode("utf-8")),
                filename=f"punish_records_{resolved_user_id}.txt",
            ),
            ephemeral=True,
        )

    @app_commands.command(name="快速处罚-撤销", description="撤销最近一次的快速处罚")
    @app_commands.describe(
        user_id="要撤销处罚的用户ID",
        restore_roles="是否恢复身份组（默认：是）"
    )
    @app_commands.guild_only()
    async def quick_punish_revoke(self, interaction: discord.Interaction, user_id: str, restore_roles: bool = True):
        """撤销快速处罚命令"""
        await safe_defer(interaction)

        if not self.enabled:
            await interaction.followup.send(
                "❌ 快速处罚功能未启用",
                ephemeral=True
            )
            return

        if not self.has_permission(interaction):
            await interaction.followup.send(
                "❌ 您没有权限使用此命令",
                ephemeral=True
            )
            return

        try:
            user_id = user_id.strip()
            int(user_id)
        except ValueError:
            await interaction.followup.send(
                "❌ 无效的用户ID格式，请输入纯数字ID",
                ephemeral=True
            )
            return

        latest_record = await self.get_last_punishment_for_user(user_id)
        if not latest_record:
            await interaction.followup.send(
                f"❌ 未找到用户 {user_id} 的处罚记录",
                ephemeral=True
            )
            return

        local_record = await self.get_last_revocable_local_record_for_user(user_id)

        if latest_record.get("source_type") == "sync":
            if not local_record:
                await interaction.followup.send(
                    f"在数据库中找不到（{user_id}）的上次处罚移除了什么身份组，可能是由于上次处罚来源于同步，请检查日志频道。",
                    ephemeral=True
                )
                return

            warn_embed = discord.Embed(
                title="⚠️ 二次确认 - 最新记录为同步记录",
                description=(
                    "最新处罚记录来源为 **sync**，该记录通常不包含可恢复身份组。\n"
                    "确认后将自动回溯并撤销最近一条可恢复的 **local** 记录。"
                ),
                color=discord.Color.orange(),
                timestamp=datetime.now()
            )
            warn_embed.add_field(
                name="最新记录（sync）",
                value=f"ID: {latest_record['id']}\n时间: {latest_record['timestamp']}\n原因: {latest_record['reason']}",
                inline=False
            )
            warn_embed.add_field(
                name="将撤销记录（local）",
                value=f"ID: {local_record['id']}\n时间: {local_record['timestamp']}\n原因: {local_record['reason']}",
                inline=False
            )

            view = RevokeConfirmView(
                cog=self,
                target_user_id=user_id,
                latest_record=latest_record,
                revoke_record=local_record,
                restore_roles=restore_roles
            )
            await interaction.followup.send(embed=warn_embed, view=view, ephemeral=True)
            return

        if latest_record.get("source_type") == "local":
            target_record = latest_record
        else:
            target_record = local_record

        if not target_record:
            await interaction.followup.send(
                f"在数据库中找不到（{user_id}）的上次处罚移除了什么身份组，可能是由于上次处罚来源于同步，请检查日志频道。",
                ephemeral=True
            )
            return

        _, message = await self._execute_revoke_record(
            interaction=interaction,
            user_id=user_id,
            record=target_record,
            restore_roles=restore_roles
        )
        await interaction.followup.send(message, ephemeral=True)
