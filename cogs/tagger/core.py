from __future__ import annotations

import discord
from discord.ext import commands
from discord import app_commands
import re
import io
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from cogs.utils import log_slash_command, remove_guild_scoped_context_menus, safe_defer as _safe_defer

from .db import _ensure_dirs_and_db
from .panel import fox14_tag_context

class TaggerCoreMixin:
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        _ensure_dirs_and_db()

        # 后台任务：每日北京时间0点过期扫描
        self._expiry_task: asyncio.Task | None = None

    async def cog_load(self):
        await asyncio.to_thread(self._init_database)
        if self._expiry_task is None or self._expiry_task.done():
            self._expiry_task = asyncio.create_task(self._expiry_scheduler())

    def cog_unload(self):
        # 取消后台任务
        if self._expiry_task and not self._expiry_task.done():
            self._expiry_task.cancel()
        remove_guild_scoped_context_menus(self.bot.tree, [fox14_tag_context])

    def _has_admin_or_trusted(self, interaction: discord.Interaction) -> bool:
        """
        基于内存数据判断是否管理员或受信任用户（来自 bot.py 的加载）
        """
        user_id = interaction.user.id
        admins = getattr(self.bot, 'admins', [])
        trusted = getattr(self.bot, 'trusted_users', [])
        return user_id in admins or user_id in trusted

    @staticmethod
    def _parse_message_link(link: str) -> tuple[int, int, int] | None:
        """
        解析 Discord 消息链接 https://discord.com/channels/{guild}/{channel}/{message}
        返回 (guild_id, channel_id, message_id) 或 None
        """
        if not link:
            return None
        pattern = r'^https://discord\.com/channels/(\d+)/(\d+)/(\d+)$'
        m = re.match(pattern, link.strip())
        if not m:
            return None
        try:
            gid = int(m.group(1))
            cid = int(m.group(2))
            mid = int(m.group(3))
            return gid, cid, mid
        except ValueError:
            return None

    @staticmethod
    def _last_day_of_month(year: int, month: int) -> int:
        if month == 12:
            next_month = datetime(year + 1, 1, 1)
        else:
            next_month = datetime(year, month + 1, 1)
        last_day = (next_month - timedelta(days=1)).day
        return last_day

    @classmethod
    def _add_months(cls, dt: datetime, months: int) -> datetime:
        """
        月份滚动：保持时分秒，日为 min(旧日, 新月最后一天)
        """
        y = dt.year
        m = dt.month + months
        # 进位
        y += (m - 1) // 12
        m = ((m - 1) % 12) + 1
        d = min(dt.day, cls._last_day_of_month(y, m))
        return datetime(y, m, d, dt.hour, dt.minute, dt.second, dt.microsecond)

    @classmethod
    def _parse_expire_input(cls, raw: str | None) -> tuple[bool, str, int | None, str]:
        """
        解析自动过期输入：
        - 支持 '-1' 表示永久
        - 支持 'Nh' 'Nd' 'Nm' 且 N 为正整数
        - 缺省时使用 7d
        返回: (ok, err_msg, expire_at_epoch, normalized_input)
        """
        if not raw or not raw.strip():
            raw = '7d'
        s = raw.strip().lower()

        if s == '-1':
            return True, '', -1, '-1'

        m = re.match(r'^(\d+)([hdm])$', s)
        if not m:
            return False, '过期格式非法，仅支持 -1 或 Nh/Nd/Nm（例如 6h、1d、2m）', None, s

        n = int(m.group(1))
        unit = m.group(2)

        now = datetime.now(timezone.utc)
        if n <= 0:
            return False, '过期时长必须为正整数', None, s

        if unit == 'h':
            target = now + timedelta(hours=n)
        elif unit == 'd':
            target = now + timedelta(days=n)
        elif unit == 'm':
            target = cls._add_months(now, n)
        else:
            return False, '过期格式非法，仅支持 -1 或 Nh/Nd/Nm', None, s

        epoch = int(target.timestamp())
        return True, '', epoch, s

    @staticmethod
    def _format_beijing_time_from_epoch(epoch: int) -> str:
        """
        将 epoch 秒转为北京时间字符串（不依赖外部时区库：UTC+8）
        """
        if epoch == -1:
            return '永久'
        dt_utc = datetime.fromtimestamp(epoch, timezone.utc)
        dt_bj = dt_utc + timedelta(hours=8)
        return dt_bj.strftime('%Y-%m-%d %H:%M:%S (北京时间)')

    @staticmethod
    def _format_records_as_text(records: list[dict[str, Any]]) -> str:
        """
        将记录列表格式化为txt伪表格
        """
        if not records:
            return "暂无记录"

        lines = ["===== Fox14 标记记录 =====", ""]
        for r in records:
            expire_str = TaggerCoreMixin._format_beijing_time_from_epoch(int(r['expire_at_epoch']))
            lines.append(f"记录ID: {r['id']} | 状态: {r['status']}")
            lines.append(f"目标用户ID: {r['target_user_id']}")
            lines.append(f"原因: {r['reason']}")
            lines.append(f"来源消息: {r['message_link']}")
            lines.append(f"标记者: {r['tagger_name']} (ID: {r['tagger_id']})")
            lines.append(f"标记时间: {r['tagged_at']}")
            try:
                scope_id_val = int(r.get('scope_id', -1))
            except Exception:
                scope_id_val = -1
            scope_disp = "全服" if scope_id_val == -1 else f"频道/子区: {scope_id_val}"
            lines.append(f"范围: {scope_disp}")
            lines.append(f"到期: {expire_str}（输入: {r['expire_input']}）")
            lines.append("-" * 60)
        return "\n".join(lines)

    @app_commands.command(name="标记", description="对用户进行标记记录（管理员/受信任用户）")
    @app_commands.describe(
        user="目标用户（可选）",
        message_link="消息链接（https://discord.com/channels/{guild}/{channel}/{message}，可选）",
        reason="标记原因（1~300字）",
        expire="自动过期：-1/Nh/Nd/Nm（可选，默认7d）",
        scope="标记范围（可选，默认单频道；'channel'=单频道，'guild'=全服）",
    )
    @app_commands.choices(
        scope=[
            app_commands.Choice(name="单频道标记", value="channel"),
            app_commands.Choice(name="全服标记", value="guild"),
        ]
    )
    @app_commands.guild_only()
    async def tag_user(self,
                        interaction: discord.Interaction,
                        user: discord.Member | None,
                        message_link: str | None,
                        reason: str,
                        expire: str | None = None,
                        scope: str | None = None):
        await _safe_defer(interaction)

        # 权限
        if not self._has_admin_or_trusted(interaction):
            await interaction.followup.send("❌ 权限不足：仅管理员或受信任用户可用。", ephemeral=True)
            log_slash_command(interaction, False)
            return

        # 校验参数：至少提供user或message_link
        if not user and not message_link:
            await interaction.followup.send("❌ 参数错误：用户与消息链接至少提供其一。", ephemeral=True)
            log_slash_command(interaction, False)
            return

        # 校验原因
        if not reason or not reason.strip():
            await interaction.followup.send("❌ 参数错误：原因必填。", ephemeral=True)
            log_slash_command(interaction, False)
            return
        if len(reason) > 300:
            await interaction.followup.send("❌ 参数错误：原因长度需 ≤ 300。", ephemeral=True)
            log_slash_command(interaction, False)
            return
        reason = reason.strip()

        target_user: discord.User | None = None
        used_message_link = "未提供"

        # 若提供消息链接，解析与抓取消息作者
        if message_link:
            parsed = self._parse_message_link(message_link)
            if not parsed:
                await interaction.followup.send("❌ 消息链接格式非法。", ephemeral=True)
                log_slash_command(interaction, False)
                return
            gid, cid, mid = parsed
            if interaction.guild is None or gid != interaction.guild.id:
                await interaction.followup.send("❌ 消息链接与当前服务器不一致（跨服链接）。", ephemeral=True)
                log_slash_command(interaction, False)
                return

            channel = interaction.client.get_channel(cid)
            if channel is None:
                # 尝试fetch
                try:
                    channel = await interaction.client.fetch_channel(cid)
                except Exception:
                    channel = None
            if channel is None or not hasattr(channel, "fetch_message"):
                await interaction.followup.send("❌ 无法访问该消息所在频道。", ephemeral=True)
                log_slash_command(interaction, False)
                return

            try:
                msg = await channel.fetch_message(mid)
            except Exception:
                await interaction.followup.send("❌ 无法获取消息（可能已删除或无权限）。", ephemeral=True)
                log_slash_command(interaction, False)
                return

            target_user = msg.author
            used_message_link = message_link.strip()

        # 未通过消息链接确定用户，则使用传入的用户
        if target_user is None and user is not None:
            target_user = user

        if target_user is None:
            await interaction.followup.send("❌ 无法确定目标用户。", ephemeral=True)
            log_slash_command(interaction, False)
            return

        # 解析过期
        ok, err, expire_epoch, normalized_input = self._parse_expire_input(expire)
        if not ok or expire_epoch is None:
            await interaction.followup.send(f"❌ {err}", ephemeral=True)
            log_slash_command(interaction, False)
            return
        
        # 计算范围
        scope_choice = (scope or 'channel').strip().lower()
        scope_id = -1 if scope_choice == 'guild' else interaction.channel.id
        
        # 插入数据库
        try:
            record_id = await self._insert_record(
                guild_id=interaction.guild.id,
                target_user_id=target_user.id,
                message_link=used_message_link,
                reason=reason,
                tagger_id=interaction.user.id,
                tagger_name=interaction.user.name,
                expire_at_epoch=expire_epoch,
                expire_input=normalized_input,
                scope_id=scope_id
            )
        except Exception as e:
            await interaction.followup.send(f"❌ 写入数据库失败：{e}", ephemeral=True)
            log_slash_command(interaction, False)
            return

        expire_str = self._format_beijing_time_from_epoch(expire_epoch)
        embed = discord.Embed(
            title="✅ 标记创建成功",
            color=discord.Color.green(),
            timestamp=datetime.now()
        )
        embed.add_field(name="标记ID", value=str(record_id), inline=True)
        embed.add_field(name="目标用户", value=f"{target_user.mention} ({target_user.id})", inline=False)
        embed.add_field(name="原因", value=reason, inline=False)
        embed.add_field(name="来源消息", value=used_message_link, inline=False)
        embed.add_field(name="标记者", value=f"{interaction.user.mention}", inline=False)
        embed.add_field(name="自动过期", value=expire_str, inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)
        log_slash_command(interaction, True)

    @app_commands.command(name="标记-查询", description="查询标记记录（最近10条或指定用户全部，临时消息）")
    @app_commands.describe(user="要查询的用户（可选；不指定则显示最近10条正常记录）")
    @app_commands.guild_only()
    async def tag_query(self, interaction: discord.Interaction, user: discord.Member | None = None):
        await _safe_defer(interaction)

        if not self._has_admin_or_trusted(interaction):
            await interaction.followup.send("❌ 权限不足：仅管理员或受信任用户可用。", ephemeral=True)
            log_slash_command(interaction, False)
            return

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("❌ 仅可在服务器中使用。", ephemeral=True)
            log_slash_command(interaction, False)
            return

        if user:
            records = await self._list_user_normal_records(guild.id, user.id)
            title = f"📋 用户 {user.display_name} (ID: {user.id}) 的标记记录（正常）"
        else:
            records = await self._list_recent_normal_records(guild.id, limit=10)
            title = "📋 最近10条标记记录（正常）"

        if not records:
            await interaction.followup.send("📝 暂无记录。", ephemeral=True)
            log_slash_command(interaction, True)
            return

        # 条目较多时生成txt附件
        if len(records) > 10:
            text = self._format_records_as_text(records)
            file = discord.File(io.BytesIO(text.encode('utf-8')),
                                filename=f"tag_records_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
            await interaction.followup.send("📎 记录较多，已生成文件：", file=file, ephemeral=True)
        else:
            embed = discord.Embed(title=title, color=discord.Color.blue(), timestamp=datetime.now())
            for r in records:
                expire_str = self._format_beijing_time_from_epoch(int(r['expire_at_epoch']))
                field_name = f"#{r['id']} - 用户ID:{r['target_user_id']}"
                scope_id_val = int(r.get('scope_id', -1)) if 'scope_id' in r else -1
                scope_disp = "全服" if scope_id_val == -1 else f"频道/子区: {scope_id_val}"
                field_value = (
                    f"原因: {r['reason'][:100]}{'...' if len(r['reason']) > 100 else ''}\n"
                    f"来源: {r['message_link']}\n"
                    f"标记者: {r['tagger_name']} (ID: {r['tagger_id']})\n"
                    f"标记时间: {r['tagged_at']}\n"
                    f"范围: {scope_disp}\n"
                    f"到期: {expire_str}"
                )
                embed.add_field(name=field_name, value=field_value, inline=False)
            await interaction.followup.send(embed=embed, ephemeral=True)

        log_slash_command(interaction, True)

    @app_commands.command(name="标记-清除", description="按标记ID清除（将状态设为'已清除'）")
    @app_commands.describe(record_id="标记ID（整数）")
    @app_commands.guild_only()
    async def tag_clear(self, interaction: discord.Interaction, record_id: int):
        await _safe_defer(interaction)

        if not self._has_admin_or_trusted(interaction):
            await interaction.followup.send("❌ 权限不足：仅管理员或受信任用户可用。", ephemeral=True)
            log_slash_command(interaction, False)
            return

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("❌ 仅可在服务器中使用。", ephemeral=True)
            log_slash_command(interaction, False)
            return

        rec = await self._fetch_record_by_id(record_id)
        if not rec:
            await interaction.followup.send("❌ 记录不存在。", ephemeral=True)
            log_slash_command(interaction, False)
            return

        if str(guild.id) != str(rec['guild_id']):
            await interaction.followup.send("❌ 该记录不属于当前服务器。", ephemeral=True)
            log_slash_command(interaction, False)
            return

        if rec['status'] == '已清除':
            await interaction.followup.send("ℹ️ 该记录已是'已清除'状态。", ephemeral=True)
            log_slash_command(interaction, True)
            return

        success = await self._clear_record_by_id(record_id)
        if not success:
            await interaction.followup.send("❌ 清除失败，可能记录状态已变化。", ephemeral=True)
            log_slash_command(interaction, False)
            return

        expire_str = self._format_beijing_time_from_epoch(int(rec['expire_at_epoch']))
        embed = discord.Embed(
            title="✅ 清除成功",
            color=discord.Color.green(),
            timestamp=datetime.now()
        )
        embed.add_field(name="标记ID", value=str(rec['id']), inline=True)
        embed.add_field(name="目标用户ID", value=str(rec['target_user_id']), inline=True)
        embed.add_field(name="原因", value=rec['reason'], inline=False)
        embed.add_field(name="来源消息", value=rec['message_link'], inline=False)
        embed.add_field(name="到期", value=expire_str, inline=False)

        await interaction.followup.send(embed=embed, ephemeral=True)
        log_slash_command(interaction, True)

    @app_commands.command(name="标记-下载", description="导出当前服务器全部标记（正常与已清除）为txt附件")
    @app_commands.guild_only()
    async def tag_download(self, interaction: discord.Interaction):
        await _safe_defer(interaction)

        if not self._has_admin_or_trusted(interaction):
            await interaction.followup.send("❌ 权限不足：仅管理员或受信任用户可用。", ephemeral=True)
            log_slash_command(interaction, False)
            return

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("❌ 仅可在服务器中使用。", ephemeral=True)
            log_slash_command(interaction, False)
            return

        records = await self._list_all_records_of_guild(guild.id)
        text = self._format_records_as_text(records)
        file = discord.File(io.BytesIO(text.encode('utf-8')),
                            filename=f"tag_records_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
        await interaction.followup.send("📎 已导出全部标记记录：", file=file, ephemeral=True)
        log_slash_command(interaction, True)
