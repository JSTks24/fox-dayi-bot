from __future__ import annotations

import discord
from discord.ext import commands
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

class TaggerAlertMixin:
    def _refresh_alert_cooldown(self, guild_id: int, user_id: int) -> bool:
        """刷新滑动冷却窗口，并返回本次是否需要发送告警。"""
        key = (guild_id, user_id)
        is_on_cooldown, _ = self._alert_cooldowns.check(key)
        self._alert_cooldowns.set_cooldown(key)
        return not is_on_cooldown

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
