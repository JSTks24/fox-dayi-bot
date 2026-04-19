import discord
from discord.ext import commands
from discord import app_commands
import os
import re
import io
import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any
from cogs.utils import CooldownManager, log_slash_command, safe_defer as _safe_defer


DB_DIR = 'tagger'
DB_PATH = os.path.join(DB_DIR, 'tagger.db')
RECORD_SELECT_COLUMNS = (
    "id, status, guild_id, target_user_id, message_link, reason, "
    "tagged_at, tagger_id, tagger_name, expire_at_epoch, expire_input, scope_id"
)


def _ensure_dirs_and_db():
    if not os.path.exists(DB_DIR):
        os.makedirs(DB_DIR, exist_ok=True)


class Fox14Tagger(commands.Cog):
    """Fox14 用户标记系统"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        _ensure_dirs_and_db()

        # ---- 告警功能配置与冷却窗口（内存） ----
        self._alert_enabled: bool = True
        self._target_channel_id: int | None = None
        self._alert_channel_id: int | None = None
        self._min_interval_minutes: int = 30
        
        try:
            target_str = os.getenv("TARGET_CHANNEL_OR_THREAD", "").strip()
            alert_str = os.getenv("ALERT_CHANNEL_OR_THREAD", "").strip()
            interval_str = os.getenv("MIN_INTERVAL", "").strip()
            
            if target_str:
                self._target_channel_id = int(target_str)
            
            if alert_str:
                self._alert_channel_id = int(alert_str)
            else:
                self._alert_enabled = False
                print("[tagger] 未配置 ALERT_CHANNEL_OR_THREAD，告警功能禁用")
            
            if interval_str:
                # 至少 1 分钟，避免 0 导致频繁告警
                self._min_interval_minutes = max(1, int(interval_str))
            
        except Exception as e:
            self._alert_enabled = False
            print(f"[tagger] 解析 .env 失败：{e}，告警功能禁用")

        self._alert_cooldowns = CooldownManager(default_seconds=self._min_interval_minutes * 60)
        
        if self._alert_enabled:
            print(f"[tagger] Fox14 标记告警已启用：ALERT={self._alert_channel_id}, MIN_INTERVAL={self._min_interval_minutes}min, TARGET(不参与触发)={self._target_channel_id}")

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
        self.bot.tree.remove_command(fox14_tag_context.name, type=fox14_tag_context.type)

    # ------------- 工具与校验器 -------------

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

    # ------------- 数据库 -------------

    def _get_conn(self):
        return sqlite3.connect(DB_PATH)

    def _refresh_alert_cooldown(self, guild_id: int, user_id: int) -> bool:
        """刷新滑动冷却窗口，并返回本次是否需要发送告警。"""
        key = (guild_id, user_id)
        is_on_cooldown, _ = self._alert_cooldowns.check(key)
        self._alert_cooldowns.set_cooldown(key)
        return not is_on_cooldown

    def _init_database(self):
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute('''
                CREATE TABLE IF NOT EXISTS tag_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    status TEXT NOT NULL,
                    guild_id TEXT NOT NULL,
                    target_user_id TEXT NOT NULL,
                    message_link TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    tagged_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    tagger_id TEXT NOT NULL,
                    tagger_name TEXT NOT NULL,
                    expire_at_epoch INTEGER NOT NULL,
                    expire_input TEXT NOT NULL,
                    scope_id INTEGER NOT NULL DEFAULT -1
                )
            ''')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_tag_records_guild_status ON tag_records (guild_id, status, id DESC)')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_tag_records_expiry_scan ON tag_records (status, expire_at_epoch)')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_tag_records_scope ON tag_records (guild_id, target_user_id, status, expire_at_epoch, scope_id, id DESC)')

    def _row_to_record(self, row: tuple[Any, ...]) -> dict[str, Any]:
        keys = ['id', 'status', 'guild_id', 'target_user_id', 'message_link', 'reason',
                'tagged_at', 'tagger_id', 'tagger_name', 'expire_at_epoch', 'expire_input', 'scope_id']
        return dict(zip(keys, row, strict=False))

    async def _insert_record(self,
                             guild_id: int,
                             target_user_id: int,
                             message_link: str,
                             reason: str,
                             tagger_id: int,
                             tagger_name: str,
                             expire_at_epoch: int,
                             expire_input: str,
                             scope_id: int) -> int:
        def _write() -> int:
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute('''
                    INSERT INTO tag_records
                    (status, guild_id, target_user_id, message_link, reason, tagger_id, tagger_name, expire_at_epoch, expire_input, scope_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    '正常',
                    str(guild_id),
                    str(target_user_id),
                    message_link,
                    reason,
                    str(tagger_id),
                    tagger_name,
                    int(expire_at_epoch),
                    expire_input,
                    int(scope_id)
                ))
                return cur.lastrowid

        return await asyncio.to_thread(_write)

    async def _fetch_record_by_id(self, record_id: int) -> dict[str, Any] | None:
        def _read() -> dict[str, Any] | None:
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute('''
                    SELECT
                    ''' + RECORD_SELECT_COLUMNS + '''
                    FROM tag_records
                    WHERE id = ?
                ''', (record_id,))
                row = cur.fetchone()
                return self._row_to_record(row) if row else None

        return await asyncio.to_thread(_read)

    async def _clear_record_by_id(self, record_id: int) -> bool:
        def _clear() -> bool:
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute('UPDATE tag_records SET status = ? WHERE id = ? AND status = ?', ('已清除', record_id, '正常'))
                return cur.rowcount > 0

        return await asyncio.to_thread(_clear)

    async def _list_recent_normal_records(self, guild_id: int, limit: int = 10) -> list[dict[str, Any]]:
        def _read() -> list[dict[str, Any]]:
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute('''
                    SELECT
                    ''' + RECORD_SELECT_COLUMNS + '''
                    FROM tag_records
                    WHERE guild_id = ? AND status = '正常'
                    ORDER BY id DESC
                    LIMIT ?
                ''', (str(guild_id), limit))
                return [self._row_to_record(row) for row in cur.fetchall()]

        return await asyncio.to_thread(_read)

    async def _list_user_normal_records(self, guild_id: int, user_id: int) -> list[dict[str, Any]]:
        def _read() -> list[dict[str, Any]]:
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute('''
                    SELECT
                    ''' + RECORD_SELECT_COLUMNS + '''
                    FROM tag_records
                    WHERE guild_id = ? AND target_user_id = ? AND status = '正常'
                    ORDER BY id DESC
                ''', (str(guild_id), str(user_id)))
                return [self._row_to_record(row) for row in cur.fetchall()]

        return await asyncio.to_thread(_read)

    async def _list_all_records_of_guild(self, guild_id: int) -> list[dict[str, Any]]:
        def _read() -> list[dict[str, Any]]:
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute('''
                    SELECT
                    ''' + RECORD_SELECT_COLUMNS + '''
                    FROM tag_records
                    WHERE guild_id = ?
                    ORDER BY id DESC
                ''', (str(guild_id),))
                return [self._row_to_record(row) for row in cur.fetchall()]

        return await asyncio.to_thread(_read)

    async def _expiry_scan_once(self) -> int:
        """
        过期扫描：将 status='正常' 且 expire_at_epoch!=-1 且 expire_at_epoch<=当前 的记录批量更新为 '已清除'
        返回受影响行数
        """
        def _scan() -> int:
            now_epoch = int(datetime.now(timezone.utc).timestamp())
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute('''
                    UPDATE tag_records
                    SET status = '已清除'
                    WHERE status = '正常'
                      AND expire_at_epoch != -1
                      AND expire_at_epoch <= ?
                ''', (now_epoch,))
                return cur.rowcount

        return await asyncio.to_thread(_scan)

    # ------------- 调度：每日北京时间0点 -------------

    async def _seconds_until_next_beijing_midnight(self) -> int:
        """
        计算距离下一次北京时间 0 点的秒数（无外部时区库，按UTC+8）
        """
        now_utc = datetime.now(timezone.utc)
        bj_now = now_utc + timedelta(hours=8)
        bj_midnight_next = datetime(
            bj_now.year,
            bj_now.month,
            bj_now.day,
            tzinfo=timezone.utc,
        ) + timedelta(days=1)
        delta = bj_midnight_next - bj_now
        seconds = int(delta.total_seconds())
        # 容错，至少为1秒
        return max(1, seconds)

    async def _expiry_scheduler(self):
        """
        每日北京时间0点执行一次过期扫描
        """
        try:
            while True:
                delay = await self._seconds_until_next_beijing_midnight()
                await asyncio.sleep(delay)
                try:
                    await self._expiry_scan_once()
                except Exception as e:
                    print(f"[tagger] 过期扫描出错: {e}")
                # 下一轮继续
        except asyncio.CancelledError:
            # 任务取消时静默退出
            pass
        except Exception as e:
            print(f"[tagger] 调度任务异常退出: {e}")

    # ------------- 文本格式化 -------------

    @staticmethod
    def _format_records_as_text(records: list[dict[str, Any]]) -> str:
        """
        将记录列表格式化为txt伪表格
        """
        if not records:
            return "暂无记录"
        
        lines = ["===== Fox14 标记记录 =====", ""]
        for r in records:
            expire_str = Fox14Tagger._format_beijing_time_from_epoch(int(r['expire_at_epoch']))
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

    # ------------- 命令实现 -------------

    @app_commands.command(name="标记", description="对用户进行标记记录（管理员/受信任用户）")
    @app_commands.describe(
        user="目标用户（可选）",
        message_link="消息链接（https://discord.com/channels/{guild}/{channel}/{message}，可选）",
        reason="标记原因（必填，<=300字符）",
        expire="自动过期（可选，'-1' 或 'Nh'/'Nd'/'Nm'；默认 7d）",
        scope="标记范围（可选，默认单频道；'channel'=单频道，'guild'=全服）"
    )
    @app_commands.choices(
        scope=[
            app_commands.Choice(name="单频道标记", value="channel"),
            app_commands.Choice(name="全服标记", value="guild")
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
    @app_commands.describe(
        user="要查询的用户（可选；不指定则显示最近10条正常记录）"
    )
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
    @app_commands.describe(
        record_id="标记ID（整数）"
    )
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


# ---- 辅助方法与事件监听器：被标记用户在目标频道/子区发言时发送告警 ----

    async def _get_alert_destination(self) -> discord.abc.Messageable | None:
        """
        获取告警目标频道或子区对象（Messageable）。
        """
        if not self._alert_channel_id:
            return None
        ch = self.bot.get_channel(self._alert_channel_id)
        if ch is None:
            try:
                ch = await self.bot.fetch_channel(self._alert_channel_id)
            except Exception:
                ch = None
        return ch  # 可能是 TextChannel 或 Thread，均可 send()

    async def _get_effective_user_records(self, guild_id: int, user_id: int, now_epoch: int) -> list[dict[str, Any]]:
        """
        获取用户在当前服务器的有效标记记录：
        - status='正常'
        - expire_at_epoch == -1 或 > now
        返回按 id DESC（最近优先）的列表。
        """
        records = await self._list_user_normal_records(guild_id, user_id)

        def _is_valid(rec: dict[str, Any]) -> bool:
            try:
                exp = int(rec.get('expire_at_epoch', -1))
            except Exception:
                return False
            return exp == -1 or exp > now_epoch

        return [r for r in records if _is_valid(r)]

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """
        当被标记用户在 TARGET_CHANNEL_OR_THREAD 发言时，在 ALERT_CHANNEL_OR_THREAD 发送一次告警，
        并对该用户进入滑动冷却窗口（每次发言刷新为 MIN_INTERVAL 分钟，窗口内不重复告警）。
        """
        try:
            # 功能未启用或无 Guild
            if not getattr(self, "_alert_enabled", False):
                return
            if message.guild is None:
                return
            # 跳过机器人消息
            if message.author.bot:
                return
            # 触发范围：有任意全服标记则任意频道触发；否则仅当存在针对当前频道/子区的标记时触发
            now_epoch = int(datetime.now(timezone.utc).timestamp())
            user_id = message.author.id
            guild_id = message.guild.id
            
            # 查询有效标记记录
            effective_records = await self._get_effective_user_records(guild_id, user_id, now_epoch)
            if not effective_records:
                return  # 未被标记或均已过期/清除
            
            has_guild_scope = any(int(r.get('scope_id', -1)) == -1 for r in effective_records)
            has_channel_scope = any(int(r.get('scope_id', -1)) == int(message.channel.id) for r in effective_records)
            if not (has_guild_scope or has_channel_scope):
                return  # 范围不匹配，不触发

            # 冷却窗口判定（滑动刷新）
            send_alert = self._refresh_alert_cooldown(guild_id, user_id)
            
            if not send_alert:
                return  # 窗口内不重复告警

            # 组装告警 Embed
            total = len(effective_records)
            latest = effective_records[0]  # id DESC 列表首项为最近一次标记
            reason = latest.get('reason', '')
            reason_disp = (reason[:50] + ('...' if len(reason) > 50 else '')) if reason else '（无）'
            last_tag_time = latest.get('tagged_at', '未知')
            last_msg_link = latest.get('message_link', '')
            embed = discord.Embed(
                title="⚠️ 警告：被标记用户出现！ ⚠️",
                color=discord.Color.red(),
                timestamp=datetime.now()
            )
            # 用户信息
            try:
                avatar_url = message.author.display_avatar.url
            except Exception:
                avatar_url = None
            embed.set_author(name=getattr(message.author, "display_name", message.author.name), icon_url=avatar_url)
            embed.add_field(name="用户", value=f"{message.author.mention} ({message.author.id})", inline=False)
            embed.add_field(name="有效标记总数", value=str(total), inline=True)
            embed.add_field(name="上次被标记时间", value=str(last_tag_time), inline=True)
            embed.add_field(name="上次被标记原因", value=reason_disp, inline=False)
            if last_msg_link and last_msg_link != "未提供":
                embed.add_field(name="最近标记来源消息", value=str(last_msg_link), inline=False)
            embed.add_field(name="目标位置", value=message.jump_url, inline=False)

            # 发送到告警目标
            dest = await self._get_alert_destination()
            if dest is None:
                print(f"[tagger] 无法获取告警目标对象：ALERT_CHANNEL_OR_THREAD={self._alert_channel_id}")
                return
            try:
                await dest.send(embed=embed)
            except Exception as e:
                print(f"[tagger] 发送告警失败：{e}")

        except Exception as e:
            # 避免事件抛出导致全局异常
            print(f"[tagger] on_message 处理异常：{e}")


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
        ok, err, expire_epoch, normalized_input = Fox14Tagger._parse_expire_input(raw_expire)
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
        expire_str = Fox14Tagger._format_beijing_time_from_epoch(expire_epoch)
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
    def __init__(self, cog: Fox14Tagger, target_message: discord.Message):
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
                joined_disp = Fox14Tagger._format_beijing_time_from_epoch(epoch)
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
                expire_str = "永不过期" if expire_epoch == -1 else Fox14Tagger._format_beijing_time_from_epoch(expire_epoch)
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
        ok, err, expire_epoch, normalized_input = Fox14Tagger._parse_expire_input('60d')
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


async def setup(bot: commands.Bot):
    await bot.add_cog(Fox14Tagger(bot))
    # 注册上下文菜单命令
    bot.tree.add_command(fox14_tag_context)
