from __future__ import annotations

import asyncio
import discord
from datetime import datetime
from typing import Any

from cogs.shared.interactions import safe_defer
from .commands import _parse_quick_punish_message_link

VOTE_REQUIRED_APPROVALS = 2
VOTE_DENIED_MESSAGE = "❌ 你没有快速处罚权限，无法参与本次投票。"


class PunishDeleteVoteView(discord.ui.View):
    """Persistent vote panel: members who may use quick punish approve to delete the punished
    message; any reject vetoes."""

    def __init__(self, cog):
        super().__init__(timeout=None)
        self.cog = cog

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not self.cog.has_permission(interaction):
            await interaction.response.send_message(VOTE_DENIED_MESSAGE, ephemeral=True)
            return False
        return True

    @discord.ui.button(label="同意删除", emoji="✅", style=discord.ButtonStyle.success, custom_id="punish_vote:approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_vote_click(interaction, "approve")

    @discord.ui.button(label="拒绝删除", emoji="🚫", style=discord.ButtonStyle.danger, custom_id="punish_vote:reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_vote_click(interaction, "reject")


class QuickPunishVoteMixin:
    """Post-punishment delete vote: panel in QUICK_PUNISH_VOTE_CHANNEL,
    requires two approvals from members with quick-punish permission; any reject vetoes."""

    @property
    def vote_enabled(self) -> bool:
        """Return whether the delete vote is configured."""
        return bool(self.vote_channel_id)

    async def cog_load(self):
        await super().cog_load()
        if self.vote_enabled:
            self.bot.add_view(PunishDeleteVoteView(self))

    def cog_unload(self):
        for task in getattr(self, "_vote_tasks", set()):
            if not task.done():
                task.cancel()
        super().cog_unload()

    def _get_vote_lock(self, vote_message_id: str) -> asyncio.Lock:
        """Called outside any await point, so plain get-or-create is race-free.

        Locks are never pruned: `asyncio.Lock.release()` wakes a queued waiter without
        setting `locked()` yet, so a lock that looks idle can still have a waiter, and
        dropping it would let a later click run concurrently with that waiter on the same
        panel. One lock per panel matches the vote table, whose rows are never pruned.
        """
        lock = self._vote_locks.get(vote_message_id)
        if lock is None:
            lock = asyncio.Lock()
            self._vote_locks[vote_message_id] = lock
        return lock

    async def create_punish_vote(self, *, trigger_guild: discord.Guild, target_user: discord.User,
                                 target_message: discord.Message, reason: str,
                                 executor: discord.User, record_id: int) -> None:
        """Post the vote panel after a successful punishment; failures never disturb the punish flow."""
        try:
            existing = await self.get_vote_by_record_id(record_id)
            if existing is not None:
                return

            channel = self.bot.get_channel(self.vote_channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(self.vote_channel_id)

            target_message_link = (
                f"https://discord.com/channels/{trigger_guild.id}/"
                f"{target_message.channel.id}/{target_message.id}"
            )
            embed = self.build_vote_panel_embed(
                record_id=record_id,
                target_user_id=str(target_user.id),
                executor_id=str(executor.id),
                reason=reason,
                target_message_link=target_message_link,
            )
            panel = await channel.send(embed=embed, view=PunishDeleteVoteView(self))
            await self.create_vote(
                record_id=record_id,
                vote_message_id=str(panel.id),
                target_message_link=target_message_link,
                target_user_id=str(target_user.id),
                executor_id=str(executor.id),
                reason=reason,
            )
        except Exception as e:
            print(f"发起处罚消息删除投票失败（不影响处罚流程）: {e}")

    def build_vote_panel_embed(self, *, record_id: int, target_user_id: str, executor_id: str,
                               reason: str, target_message_link: str, status: str = "pending",
                               approver_ids: list[str] | None = None,
                               rejecter_ids: list[str] | None = None,
                               note: str | None = None) -> discord.Embed:
        titles = {
            "pending": "🗳️ 处罚消息删除投票",
            "executed": "✅ 已删除被处罚消息",
            "rejected": "🚫 已否决，保留原消息",
            "delete_failed": "⚠️ 删除失败，请人工处理",
        }
        colors = {
            "pending": discord.Color.orange(),
            "executed": discord.Color.green(),
            "rejected": discord.Color.red(),
            "delete_failed": discord.Color.dark_grey(),
        }
        approver_ids = approver_ids or []
        rejecter_ids = rejecter_ids or []

        embed = discord.Embed(
            title=titles[status],
            color=colors[status],
            timestamp=datetime.now(),
        )
        embed.add_field(name="处罚对象", value=f"<@{target_user_id}> ({target_user_id})", inline=False)
        embed.add_field(name="执行者", value=f"<@{executor_id}>", inline=True)
        embed.add_field(name="原因", value=reason or "未记录", inline=True)
        embed.add_field(name="原消息", value=f"[跳转到消息]({target_message_link})", inline=False)

        def _mentions(ids: list[str]) -> str:
            return "、".join(f"<@{uid}>" for uid in ids) or "无"

        if status == "pending":
            embed.add_field(
                name="投票进度",
                value=(
                    f"同意：{_mentions(approver_ids)}（{len(approver_ids)}/{VOTE_REQUIRED_APPROVALS}）\n"
                    f"需 {VOTE_REQUIRED_APPROVALS} 名投票组成员同意后删除原消息；任一成员拒绝即否决。"
                ),
                inline=False,
            )
        else:
            lines = [f"同意：{_mentions(approver_ids)}", f"否决：{_mentions(rejecter_ids)}"]
            if status == "rejected":
                first_rejecter = f"<@{rejecter_ids[0]}>" if rejecter_ids else "有人"
                lines.append(f"结果：{first_rejecter} 否决了本次删除。")
            elif status == "executed":
                lines.append("结果：达到所需同意票数，原消息已删除。")
            else:
                lines.append("结果：达到所需同意票数，但删除原消息失败。")
            if note:
                lines.append(note)
            embed.add_field(name="投票结果", value="\n".join(lines), inline=False)

        embed.set_footer(text=f"记录ID: {record_id}")
        return embed

    async def handle_vote_click(self, interaction: discord.Interaction, decision: str) -> None:
        await safe_defer(interaction)

        vote_message_id = str(interaction.message.id)
        lock = self._get_vote_lock(vote_message_id)
        async with lock:
            vote = await self.get_vote_by_message_id(vote_message_id)
            if vote is None:
                await interaction.followup.send("❌ 未找到对应的投票记录。", ephemeral=True)
                return
            if vote["status"] != "pending":
                await interaction.followup.send("❌ 本次投票已结束。", ephemeral=True)
                return

            user_id = str(interaction.user.id)
            approver_ids = list(vote["approver_ids"])
            rejecter_ids = list(vote["rejecter_ids"])

            if decision == "reject":
                if user_id in approver_ids:
                    approver_ids.remove(user_id)
                if user_id not in rejecter_ids:
                    rejecter_ids.append(user_id)
                await self.decide_vote(
                    vote["record_id"], status="rejected",
                    approver_ids=approver_ids, rejecter_ids=rejecter_ids,
                )
                await self._finish_vote_panel(
                    interaction, vote, status="rejected",
                    approver_ids=approver_ids, rejecter_ids=rejecter_ids,
                )
                return

            if user_id in approver_ids:
                await interaction.followup.send("❌ 你已经投过同意票了。", ephemeral=True)
                return

            approver_ids.append(user_id)
            if len(approver_ids) < VOTE_REQUIRED_APPROVALS:
                await self.update_vote_progress(
                    vote["record_id"], approver_ids=approver_ids, rejecter_ids=rejecter_ids,
                )
                await self._finish_vote_panel(
                    interaction, vote, status="pending",
                    approver_ids=approver_ids, rejecter_ids=rejecter_ids,
                )
                return

            status, note = await self._delete_punished_message(vote)
            await self.decide_vote(
                vote["record_id"], status=status,
                approver_ids=approver_ids, rejecter_ids=rejecter_ids,
            )
            await self._finish_vote_panel(
                interaction, vote, status=status,
                approver_ids=approver_ids, rejecter_ids=rejecter_ids, note=note,
            )

    async def _finish_vote_panel(self, interaction: discord.Interaction, vote: dict[str, Any], *,
                                 status: str, approver_ids: list[str], rejecter_ids: list[str],
                                 note: str | None = None) -> None:
        """Refresh the panel for `vote`; terminal states drop the buttons."""
        embed = self.build_vote_panel_embed(
            record_id=vote["record_id"],
            target_user_id=vote["target_user_id"],
            executor_id=str(vote["executor_id"] or ""),
            reason=vote["reason"],
            target_message_link=vote["target_message_link"],
            status=status,
            approver_ids=approver_ids,
            rejecter_ids=rejecter_ids,
            note=note,
        )
        if status == "pending":
            await interaction.edit_original_response(embed=embed)
        else:
            await interaction.edit_original_response(embed=embed, view=None)

    async def _delete_punished_message(self, vote: dict[str, Any]) -> tuple[str, str | None]:
        """Delete the punished message; returns (status, note) for the panel."""
        parsed = _parse_quick_punish_message_link(vote["target_message_link"])
        if not parsed:
            return "delete_failed", "原消息链接无效，请人工处理。"

        _, channel_id, message_id = parsed
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.NotFound:
                return "delete_failed", "找不到原消息所在频道，可能已被删除。"
            except (discord.Forbidden, discord.HTTPException) as e:
                print(f"处罚投票解析原消息频道失败: {e}")
                return "delete_failed", "解析原消息频道失败，请人工处理。"

        if isinstance(channel, discord.Thread):
            try:
                await channel.join()
            except (discord.Forbidden, discord.HTTPException):
                pass

        if not hasattr(channel, "fetch_message"):
            return "delete_failed", "原消息所在频道类型不支持删除，请人工处理。"

        try:
            target = await channel.fetch_message(message_id)
        except discord.NotFound:
            return "executed", "原消息已不存在（可能已被提前删除）。"
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"处罚投票获取原消息失败: {e}")
            return "delete_failed", "获取原消息失败，请人工处理。"

        try:
            await target.delete()
        except discord.NotFound:
            return "executed", "原消息已不存在（可能已被提前删除）。"
        except discord.Forbidden:
            return "delete_failed", "机器人缺少删除该消息的权限。"
        except discord.HTTPException as e:
            print(f"处罚投票删除原消息失败: {e}")
            return "delete_failed", "删除原消息时出错，请人工处理。"
        return "executed", None
