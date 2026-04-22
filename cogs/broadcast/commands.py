from __future__ import annotations

import discord
from discord import app_commands
from datetime import datetime
import logging
from typing import Any
from cogs.utils import check_admin, check_admin_or_trusted

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


# ── 辅助函数 ─────────────────────────────────────────────────

def _build_task_options(cog: Any) -> list[discord.SelectOption]:
    """从 cog.config 构建 StringSelect 选项列表（最多25条）。"""
    options: list[discord.SelectOption] = []
    for task_name, task_config in list(cog.config.items())[:25]:
        task_id = task_config.get('id', '?')
        content = task_config.get('content', '')
        desc = content[:80] + ('...' if len(content) > 80 else '')
        options.append(discord.SelectOption(
            label=f"#{task_id} {task_name}"[:100],
            value=task_name,
            description=desc[:100] if desc else '无内容',
        ))
    return options


# ── 控制面板主视图 ────────────────────────────────────────────

class BroadcastControlView(discord.ui.View):
    """广播控制面板的按钮视图"""

    def __init__(self, cog: Any, user_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.user_id = user_id
        self.page = 0

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not check_admin_or_trusted(interaction):
            await interaction.response.send_message("❌ 此面板仅限管理员和受信任用户使用。", ephemeral=True)
            return False
        return True

    # Row 0: 查询 | 新增 | 编辑
    @discord.ui.button(label='🔍 查询', style=discord.ButtonStyle.primary, row=0)
    async def search_task(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SearchTaskModal(self.cog))

    @discord.ui.button(label='➕ 新增', style=discord.ButtonStyle.success, row=0)
    async def add_task(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddTaskModal(self.cog))

    @discord.ui.button(label='✏️ 编辑', style=discord.ButtonStyle.primary, row=0)
    async def edit_task(self, interaction: discord.Interaction, button: discord.ui.Button):
        options = _build_task_options(self.cog)
        if not options:
            await interaction.response.send_message("📭 暂无任务可编辑。", ephemeral=True)
            return
        view = _TaskSelectView(self.cog, options, _ActionType.EDIT)
        await interaction.response.send_message("选择要编辑的任务：", view=view, ephemeral=True)

    # Row 1: 控制 | 删除 | 刷新
    @discord.ui.button(label='⏸️ 控制', style=discord.ButtonStyle.secondary, row=1)
    async def control_task(self, interaction: discord.Interaction, button: discord.ui.Button):
        options = _build_task_options(self.cog)
        if not options:
            await interaction.response.send_message("📭 暂无任务可控制。", ephemeral=True)
            return
        view = _TaskSelectView(self.cog, options, _ActionType.CONTROL)
        await interaction.response.send_message("选择要启停的任务：", view=view, ephemeral=True)

    @discord.ui.button(label='🗑️ 删除', style=discord.ButtonStyle.danger, row=1)
    async def delete_task(self, interaction: discord.Interaction, button: discord.ui.Button):
        options = _build_task_options(self.cog)
        if not options:
            await interaction.response.send_message("📭 暂无任务可删除。", ephemeral=True)
            return
        view = _TaskSelectView(self.cog, options, _ActionType.DELETE)
        await interaction.response.send_message("选择要删除的任务：", view=view, ephemeral=True)

    @discord.ui.button(label='🔄 刷新', style=discord.ButtonStyle.secondary, row=1)
    async def refresh_panel(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            embed = await self.cog.create_panel_embed(interaction, page=self.page)
            self._update_page_buttons()
            await interaction.response.edit_message(embed=embed, view=self)
        except Exception as e:
            await interaction.response.send_message(f"❌ 刷新失败: {str(e)}", ephemeral=True)

    # Row 2: 翻页（仅多页时可见）
    @discord.ui.button(label='◀️ 上一页', style=discord.ButtonStyle.secondary, row=2)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        await self._refresh_panel(interaction)

    @discord.ui.button(label='▶️ 下一页', style=discord.ButtonStyle.secondary, row=2)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        await self._refresh_panel(interaction)

    def _total_pages(self) -> int:
        per_page = 5
        total = len(self.cog.config)
        return max(1, (total + per_page - 1) // per_page)

    def _update_page_buttons(self) -> None:
        """根据总页数和当前页显示/隐藏翻页按钮。"""
        total = self._total_pages()
        self.page = max(0, min(self.page, total - 1))
        show = total > 1
        self.prev_page.disabled = self.page == 0
        self.next_page.disabled = self.page >= total - 1
        # 单页时隐藏翻页行
        for btn in (self.prev_page, self.next_page):
            btn.row = 2 if show else None
            if not show:
                btn.disabled = True

    async def _refresh_panel(self, interaction: discord.Interaction) -> None:
        try:
            self._update_page_buttons()
            embed = await self.cog.create_panel_embed(interaction, page=self.page)
            await interaction.response.edit_message(embed=embed, view=self)
        except Exception as e:
            await interaction.response.send_message(f"❌ 刷新失败: {str(e)}", ephemeral=True)


# ── Select 菜单流程 ──────────────────────────────────────────

class _ActionType:
    CONTROL = 'control'
    DELETE = 'delete'
    EDIT = 'edit'


class _TaskSelectView(discord.ui.View):
    """通用任务选择下拉菜单。"""

    def __init__(self, cog: Any, options: list[discord.SelectOption], action: str):
        super().__init__(timeout=120)
        self.cog = cog
        self.action = action
        self.select = discord.ui.Select(
            placeholder='选择一个任务...',
            options=options,
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

    async def _on_select(self, interaction: discord.Interaction):
        task_name = self.select.values[0]
        task_config = self.cog.config.get(task_name)
        if not task_config:
            await interaction.response.send_message("❌ 任务不存在或已被删除。", ephemeral=True)
            return

        # 所有权检查（编辑/控制/删除均需要）
        if not self.cog._can_manage_task(interaction, task_config):
            await interaction.response.send_message("❌ 你只能管理自己创建的任务。", ephemeral=True)
            return

        if self.action == _ActionType.CONTROL:
            await self._handle_control(interaction, task_name, task_config)
        elif self.action == _ActionType.DELETE:
            await self._handle_delete_confirm(interaction, task_name, task_config)
        elif self.action == _ActionType.EDIT:
            await self._handle_edit(interaction, task_name, task_config)

    async def _handle_control(self, interaction: discord.Interaction, task_name: str, task_config: dict):
        """直接 toggle 任务状态。"""
        try:
            async with self.cog.lock:
                current_status = task_config.get('status', 'inactive')
                new_status = 'inactive' if current_status == 'active' else 'active'

                task_config['status'] = new_status
                self.cog.save_config()

                if new_status == 'active':
                    await self.cog.start_task(task_name, task_config)
                    status_text = "✅ 已启动"
                    status_emoji = "🟢"
                else:
                    if task_name in self.cog.active_tasks:
                        if self.cog.active_tasks[task_name].is_running():
                            self.cog.active_tasks[task_name].cancel()
                        del self.cog.active_tasks[task_name]
                    status_text = "⏸️ 已停止"
                    status_emoji = "🔴"

            task_id = task_config.get('id', '?')
            embed = discord.Embed(
                title=f"{status_emoji} 任务状态已更新",
                description=f"任务 **{task_name}** (ID: {task_id})",
                color=discord.Color.green() if new_status == 'active' else discord.Color.orange(),
                timestamp=datetime.now(),
            )
            embed.add_field(name="新状态", value=status_text, inline=True)
            await interaction.response.edit_message(content=None, embed=embed, view=None)
            logger.info(f"用户 {interaction.user.name} 将任务 {task_name} 状态更改为 {new_status}")
        except Exception as e:
            logger.error(f"控制任务失败: {e}")
            await interaction.response.send_message(f"❌ 控制任务失败: {str(e)}", ephemeral=True)

    async def _handle_delete_confirm(self, interaction: discord.Interaction, task_name: str, task_config: dict):
        """发送删除确认 Embed + 按钮。"""
        task_id = task_config.get('id', '?')
        content_preview = task_config.get('content', '')[:200]
        embed = discord.Embed(
            title="⚠️ 确认删除",
            description=f"即将永久删除任务 **{task_name}** (ID: {task_id})",
            color=discord.Color.red(),
            timestamp=datetime.now(),
        )
        embed.add_field(name="描述", value=task_config.get('description', '无'), inline=False)
        embed.add_field(name="部署者", value=f"<@{task_config.get('author', '?')}>", inline=True)
        embed.add_field(name="内容预览", value=f"```\n{content_preview}\n```" if content_preview else '无', inline=False)
        embed.set_footer(text="此操作不可恢复")

        view = _DeleteConfirmView(self.cog, task_name)
        await interaction.response.edit_message(content=None, embed=embed, view=view)

    async def _handle_edit(self, interaction: discord.Interaction, task_name: str, task_config: dict):
        """弹出预填当前值的编辑 Modal。"""
        modal = EditTaskModal(self.cog, task_name, task_config)
        await interaction.response.send_modal(modal)


# ── 删除确认视图 ──────────────────────────────────────────────

class _DeleteConfirmView(discord.ui.View):
    def __init__(self, cog: Any, task_name: str):
        super().__init__(timeout=60)
        self.cog = cog
        self.task_name = task_name

    @discord.ui.button(label='确认删除', style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        task_config = self.cog.config.get(self.task_name)
        if not task_config:
            await interaction.response.edit_message(content="❌ 任务已不存在。", embed=None, view=None)
            return

        task_id = task_config.get('id', '?')
        try:
            async with self.cog.lock:
                if self.task_name in self.cog.active_tasks:
                    if self.cog.active_tasks[self.task_name].is_running():
                        self.cog.active_tasks[self.task_name].cancel()
                    del self.cog.active_tasks[self.task_name]

                del self.cog.config[self.task_name]
                self.cog.save_config()

                if task_id in self.cog.stats:
                    del self.cog.stats[task_id]
                    self.cog.save_stats()

            embed = discord.Embed(
                title="🗑️ 任务已删除",
                description=f"任务 **{self.task_name}** (ID: {task_id}) 已被永久删除",
                color=discord.Color.red(),
                timestamp=datetime.now(),
            )
            await interaction.response.edit_message(content=None, embed=embed, view=None)
            logger.info(f"用户 {interaction.user.name} 删除了任务: {self.task_name}")
        except Exception as e:
            logger.error(f"删除任务失败: {e}")
            await interaction.response.edit_message(content=f"❌ 删除失败: {str(e)}", embed=None, view=None)

    @discord.ui.button(label='取消', style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="已取消删除。", embed=None, view=None)


# ── Modals ────────────────────────────────────────────────────

class SearchTaskModal(discord.ui.Modal, title='查询广播任务'):

    keyword = discord.ui.TextInput(
        label='搜索关键词',
        placeholder='输入要搜索的内容（在任务内容中匹配）',
        required=True,
        max_length=100,
    )

    def __init__(self, cog: Any):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        keyword = self.keyword.value.lower()
        found_tasks = [
            (name, cfg) for name, cfg in self.cog.config.items()
            if keyword in cfg.get('content', '').lower()
        ]

        if not found_tasks:
            await interaction.followup.send(f"❌ 没有找到包含 '{self.keyword.value}' 的任务", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"🔍 搜索结果（关键词: {self.keyword.value}）",
            color=discord.Color.green(),
            timestamp=datetime.now(),
        )

        for task_name, task_config in found_tasks[:3]:
            task_id = task_config.get('id', 'N/A')
            info_lines = [
                f"**ID:** {task_id}",
                f"**状态:** {task_config.get('status', 'unknown')}",
                f"**描述:** {task_config.get('description', '无')}",
                f"**作者:** <@{task_config.get('author', 'unknown')}>",
                f"**目标:** {task_config.get('thread_or_channel', 'unknown')}",
            ]

            if 'INTERVAL_MINUTES' in task_config:
                info_lines.append(f"**模式:** 间隔 {task_config['INTERVAL_MINUTES']} 分钟")

            content = task_config.get('content', '')
            content_preview = content[:200] + '...' if len(content) > 200 else content
            info_lines.append(f"**内容预览:**\n```\n{content_preview}\n```")

            embed.add_field(name=f"📝 {task_name}", value='\n'.join(info_lines), inline=False)

        if len(found_tasks) > 3:
            embed.add_field(name="ℹ️ 提示", value=f"共找到 {len(found_tasks)} 个任务，仅显示前3个", inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)


class AddTaskModal(discord.ui.Modal, title='新增广播任务'):

    task_name = discord.ui.TextInput(
        label='任务名称',
        placeholder='例如: daily_announcement',
        required=True,
        max_length=50,
    )

    channels = discord.ui.TextInput(
        label='目标频道ID（逗号分隔）',
        placeholder='例如: 123456789,987654321',
        required=True,
        max_length=200,
    )

    interval = discord.ui.TextInput(
        label='间隔分钟数',
        placeholder='例如: 60 表示每60分钟',
        required=True,
        max_length=10,
    )

    description = discord.ui.TextInput(
        label='任务描述',
        placeholder='简要描述这个广播任务的用途',
        required=False,
        max_length=200,
    )

    content = discord.ui.TextInput(
        label='广播内容',
        placeholder='支持 \\n 换行',
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=2000,
    )

    def __init__(self, cog: Any):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            task_name = self.task_name.value

            if task_name in self.cog.config:
                await interaction.followup.send(f"❌ 任务名 '{task_name}' 已存在", ephemeral=True)
                return

            task_id = await self.cog.get_next_task_id()

            new_config = {
                'id': task_id,
                'status': 'active',
                'author': str(interaction.user.id),
                'description': self.description.value.strip() or '未提供',
                'thread_or_channel': self.channels.value,
                'content': self.content.value,
            }

            try:
                interval = int(self.interval.value)
                if interval <= 0:
                    raise ValueError("间隔必须大于0")
                new_config['INTERVAL_MINUTES'] = str(interval)
            except ValueError as e:
                await interaction.followup.send(f"❌ 间隔分钟数无效: {e}", ephemeral=True)
                return

            # 白名单频道校验（非 admin 用户必须目标频道全在白名单中）
            if not check_admin(interaction) and self.cog.allowed_channels:
                target_ids = [int(t.strip()) for t in self.channels.value.split(',')]
                blocked = [str(tid) for tid in target_ids if tid not in self.cog.allowed_channels]
                if blocked:
                    await interaction.followup.send(
                        f"❌ 以下目标频道不在白名单中: {', '.join(blocked)}", ephemeral=True,
                    )
                    return

            is_valid, error_msg = self.cog.validate_task(task_name, new_config)
            if not is_valid:
                await interaction.followup.send(f"❌ 任务配置无效: {error_msg}", ephemeral=True)
                return

            async with self.cog.lock:
                self.cog.config[task_name] = new_config
                self.cog.save_config()

            await self.cog.start_task(task_name, new_config)

            embed = discord.Embed(
                title="✅ 任务创建成功",
                description=f"任务 **{task_name}** 已成功创建并启动",
                color=discord.Color.green(),
                timestamp=datetime.now(),
            )
            embed.add_field(name="任务ID", value=task_id, inline=True)
            embed.add_field(name="状态", value="🟢 运行中", inline=True)
            embed.add_field(name="模式", value=f"间隔 {new_config['INTERVAL_MINUTES']} 分钟", inline=True)

            await interaction.followup.send(embed=embed, ephemeral=True)
            logger.info(f"用户 {interaction.user.name} 创建了新任务: {task_name}")

        except Exception as e:
            logger.error(f"创建任务失败: {e}")
            await interaction.followup.send(f"❌ 创建任务失败: {str(e)}", ephemeral=True)


class EditTaskModal(discord.ui.Modal, title='编辑广播任务'):

    content = discord.ui.TextInput(
        label='广播内容',
        placeholder='支持 \\n 换行',
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=2000,
    )

    channels = discord.ui.TextInput(
        label='目标频道ID（逗号分隔）',
        placeholder='例如: 123456789,987654321',
        required=True,
        max_length=200,
    )

    interval = discord.ui.TextInput(
        label='间隔分钟数',
        placeholder='例如: 60',
        required=True,
        max_length=10,
    )

    description = discord.ui.TextInput(
        label='任务描述',
        placeholder='简要描述这个广播任务的用途',
        required=False,
        max_length=200,
    )

    def __init__(self, cog: Any, task_name: str, task_config: dict):
        super().__init__()
        self.cog = cog
        self.task_name = task_name
        # 预填当前值
        self.content.default = task_config.get('content', '')
        self.channels.default = task_config.get('thread_or_channel', '')
        self.interval.default = task_config.get('INTERVAL_MINUTES', '')
        self.description.default = task_config.get('description', '')

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        task_config = self.cog.config.get(self.task_name)
        if not task_config:
            await interaction.followup.send("❌ 任务已不存在。", ephemeral=True)
            return

        try:
            new_interval = int(self.interval.value)
            if new_interval <= 0:
                raise ValueError("间隔必须大于0")
        except ValueError as e:
            await interaction.followup.send(f"❌ 间隔分钟数无效: {e}", ephemeral=True)
            return

        # 白名单频道校验
        if not check_admin(interaction) and self.cog.allowed_channels:
            target_ids = [int(t.strip()) for t in self.channels.value.split(',')]
            blocked = [str(tid) for tid in target_ids if tid not in self.cog.allowed_channels]
            if blocked:
                await interaction.followup.send(
                    f"❌ 以下目标频道不在白名单中: {', '.join(blocked)}", ephemeral=True,
                )
                return

        old_interval = task_config.get('INTERVAL_MINUTES', '')
        interval_changed = str(new_interval) != old_interval

        async with self.cog.lock:
            task_config['content'] = self.content.value
            task_config['thread_or_channel'] = self.channels.value
            task_config['INTERVAL_MINUTES'] = str(new_interval)
            task_config['description'] = self.description.value.strip() or '未提供'
            self.cog.save_config()

        # 如果间隔变了且任务正在运行，重启 loop
        if interval_changed and task_config.get('status') == 'active':
            if self.task_name in self.cog.active_tasks:
                if self.cog.active_tasks[self.task_name].is_running():
                    self.cog.active_tasks[self.task_name].cancel()
                del self.cog.active_tasks[self.task_name]
            await self.cog.start_task(self.task_name, task_config)

        embed = discord.Embed(
            title="✅ 任务已更新",
            description=f"任务 **{self.task_name}** 已保存修改",
            color=discord.Color.green(),
            timestamp=datetime.now(),
        )
        if interval_changed:
            embed.add_field(name="间隔变更", value=f"{old_interval} → {new_interval} 分钟（已重启）", inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)
        logger.info(f"用户 {interaction.user.name} 编辑了任务: {self.task_name}")


# ── 命令 Mixin ────────────────────────────────────────────────

class BroadcastCommandsMixin:
    def _has_admin_permission(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        return isinstance(member, discord.Member) and member.guild_permissions.administrator

    def _is_bot_admin(self, interaction: discord.Interaction) -> bool:
        return check_admin(interaction)

    def _can_manage_task(self, interaction: discord.Interaction, task_config: dict) -> bool:
        """admin 可管理任何任务，trusted 只能管理自己创建的。"""
        if self._is_bot_admin(interaction):
            return True
        return str(interaction.user.id) == task_config.get('author', '')

    @app_commands.command(name='broadcast_reload', description='[仅管理员] 重新加载广播配置')
    @app_commands.default_permissions(administrator=True)
    async def reload_broadcast(self, interaction: discord.Interaction):
        if not self._has_admin_permission(interaction):
            await interaction.response.send_message("❌ 此命令仅限服务器管理员使用。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            for _task_name, task_loop in self.active_tasks.items():
                if task_loop.is_running():
                    task_loop.cancel()
            self.active_tasks.clear()

            self.load_config()
            self.load_stats()
            await self.start_all_tasks()

            await interaction.followup.send("✅ 广播配置已重新加载", ephemeral=True)
            logger.info(f"用户 {interaction.user} 重新加载了广播配置")
        except Exception as e:
            await interaction.followup.send(f"❌ 重新加载失败: {str(e)}", ephemeral=True)
            logger.error(f"重新加载广播配置失败: {e}")

    @app_commands.command(name='broadcast_status', description='[仅管理员] 查看广播任务状态')
    @app_commands.default_permissions(administrator=True)
    async def broadcast_status(self, interaction: discord.Interaction):
        if not self._has_admin_permission(interaction):
            await interaction.response.send_message("❌ 此命令仅限服务器管理员使用。", ephemeral=True)
            return

        try:
            if not self.config:
                await interaction.response.send_message("📭 当前没有配置任何广播任务", ephemeral=True)
                return

            embed = discord.Embed(title="📢 广播任务状态", color=discord.Color.blue(), timestamp=datetime.now())

            for task_name, task_config in self.config.items():
                task_id = task_config['id']
                status = task_config.get('status', 'unknown')

                stat = self.stats.get(task_id, {})
                daily_count = stat.get('daily_count', 0)
                last_sent = stat.get('last_time_sent', '从未')

                if 'INTERVAL_MINUTES' in task_config:
                    mode = f"间隔 {task_config['INTERVAL_MINUTES']} 分钟"
                else:
                    mode = "未知"

                is_running = task_name in self.active_tasks and self.active_tasks[task_name].is_running()
                run_status = "🟢 运行中" if is_running else "🔴 已停止"

                field_value = (
                    f"状态: {status} {run_status}\n"
                    f"模式: {mode}\n"
                    f"今日执行: {daily_count} 次\n"
                    f"最后发送: {last_sent}"
                )
                embed.add_field(name=f"📝 {task_name}", value=field_value, inline=False)

            embed.set_footer(text=f"请求者: {interaction.user}")
            await interaction.response.send_message(embed=embed, ephemeral=True)

        except Exception as e:
            response = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
            await response(f"❌ 获取状态失败: {str(e)}", ephemeral=True)
            logger.error(f"获取广播状态失败: {e}")

    @app_commands.command(name='召唤广播控制面板', description='管理广播任务')
    async def broadcast_panel(self, interaction: discord.Interaction):
        if not check_admin_or_trusted(interaction):
            await interaction.response.send_message('❌ 此命令仅限管理员和受信任用户使用。', ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            embed = await self.create_panel_embed(interaction)
            view = BroadcastControlView(self, interaction.user.id)
            view._update_page_buttons()
            await interaction.followup.send(embed=embed, view=view)
            logger.info(f"用户 {interaction.user.name} 打开了广播控制面板")
        except Exception as e:
            logger.error(f"创建广播控制面板失败: {e}")
            await interaction.followup.send(f'❌ 创建控制面板失败: {str(e)}', ephemeral=True)

    async def create_panel_embed(self, interaction: discord.Interaction, page: int = 0) -> discord.Embed:
        """创建控制面板的 embed 消息。"""
        embed = discord.Embed(
            title="📢 广播任务控制面板",
            description="管理自动广播任务",
            color=discord.Color.blue(),
            timestamp=datetime.now(),
        )

        tasks_list = list(self.config.items())
        per_page = 5
        total_pages = max(1, (len(tasks_list) + per_page - 1) // per_page)
        page = max(0, min(page, total_pages - 1))
        page_tasks = tasks_list[page * per_page:(page + 1) * per_page]

        if not page_tasks:
            embed.add_field(name="📭 暂无任务", value="当前没有配置任何广播任务", inline=False)
        else:
            for idx, (task_name, task_config) in enumerate(page_tasks, page * per_page + 1):
                task_id = task_config.get('id', 'N/A')
                description = task_config.get('description', '无描述')
                status = task_config.get('status', 'unknown')
                author_id = task_config.get('author', '')

                try:
                    author = await self.bot.fetch_user(int(author_id))
                    author_name = author.name
                except Exception:
                    author_name = f"用户ID: {author_id}"

                target_ids = task_config.get('thread_or_channel', '').split(',')
                target_names = []
                for tid in target_ids[:2]:
                    try:
                        channel = self.bot.get_channel(int(tid.strip()))
                        target_names.append(f"#{channel.name}" if channel else f"ID:{tid.strip()}")
                    except Exception:
                        target_names.append(f"ID:{tid.strip()}")
                if len(target_ids) > 2:
                    target_names.append(f"等{len(target_ids)}个")
                target_str = ', '.join(target_names) if target_names else '未知'

                stat = self.stats.get(task_id, {})
                last_sent = stat.get('last_time_sent', '')
                if last_sent:
                    try:
                        last_sent_formatted = f"{last_sent[:2]}:{last_sent[2:4]}:{last_sent[4:6]}"
                    except Exception:
                        last_sent_formatted = '未知'
                else:
                    last_sent_formatted = '从未'

                mode = f"间隔 {task_config['INTERVAL_MINUTES']} 分钟" if 'INTERVAL_MINUTES' in task_config else "未知"

                is_running = task_name in self.active_tasks and self.active_tasks[task_name].is_running()
                status_emoji = "🟢" if status == 'active' else "🔴"
                run_status = "运行中" if is_running else "已停止"

                # 内容预览
                content_preview = task_config.get('content', '')[:80]
                if len(task_config.get('content', '')) > 80:
                    content_preview += '...'

                field_value = (
                    f"**ID:** {task_id}\n"
                    f"📝 **描述:** {description[:50]}{'...' if len(description) > 50 else ''}\n"
                    f"👤 **部署者:** {author_name}\n"
                    f"📍 **目标:** {target_str}\n"
                    f"⏰ **最后发送:** {last_sent_formatted}\n"
                    f"⚙️ **模式:** {mode}\n"
                    f"📄 **内容:** {content_preview}\n"
                    f"{status_emoji} **状态:** {status} ({run_status})"
                )

                embed.add_field(name=f"{idx}️⃣ {task_name}", value=field_value, inline=False)

        if total_pages > 1:
            embed.set_footer(text=f"请求者: {interaction.user} | 第 {page + 1}/{total_pages} 页")
        else:
            embed.set_footer(text=f"请求者: {interaction.user}")
        return embed

    @app_commands.command(name='广播白名单-编辑', description='[仅管理员] 添加或移除广播白名单频道')
    @app_commands.describe(
        action='操作类型',
        channel_ids='频道/子区 ID，多个用逗号分隔',
    )
    @app_commands.choices(action=[
        app_commands.Choice(name='添加', value='add'),
        app_commands.Choice(name='移除', value='remove'),
        app_commands.Choice(name='查看', value='list'),
    ])
    async def edit_allowed_channels(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        channel_ids: str | None = None,
    ):
        if not check_admin(interaction):
            await interaction.response.send_message('❌ 此命令仅限管理员使用。', ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        if action.value == 'list':
            if not self.allowed_channels:
                await interaction.followup.send('📭 白名单为空，trusted 用户可向任意频道发送广播。', ephemeral=True)
            else:
                lines = []
                for cid in self.allowed_channels:
                    ch = self.bot.get_channel(cid)
                    name = f"#{ch.name}" if ch else f"ID:{cid}"
                    lines.append(f"- {name} (`{cid}`)")
                await interaction.followup.send(
                    f"**当前白名单频道 ({len(self.allowed_channels)}):**\n" + '\n'.join(lines),
                    ephemeral=True,
                )
            return

        if not channel_ids:
            await interaction.followup.send('❌ 请提供频道 ID。', ephemeral=True)
            return

        ids_to_process = []
        for raw in channel_ids.split(','):
            raw = raw.strip()
            if raw.isdigit():
                ids_to_process.append(int(raw))

        if not ids_to_process:
            await interaction.followup.send('❌ 未识别到有效的频道 ID。', ephemeral=True)
            return

        async with self.lock:
            if action.value == 'add':
                added = [cid for cid in ids_to_process if cid not in self.allowed_channels]
                self.allowed_channels.extend(added)
                self.save_config()
                await interaction.followup.send(f'✅ 已添加 {len(added)} 个频道到白名单。', ephemeral=True)
            elif action.value == 'remove':
                removed = [cid for cid in ids_to_process if cid in self.allowed_channels]
                self.allowed_channels = [cid for cid in self.allowed_channels if cid not in ids_to_process]
                self.save_config()
                await interaction.followup.send(f'✅ 已从白名单移除 {len(removed)} 个频道。', ephemeral=True)
