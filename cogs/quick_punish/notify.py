from __future__ import annotations

import discord
import os
from datetime import datetime
from typing import Any
import aiofiles
from paths import XIAOZUOWEN_DIR


class QuickPunishNotifyMixin:
    def _load_dm_templates(self):
        """Scan the DM template directory and load txt template names to paths."""
        self.dm_templates = {}
        base_dir = os.fspath(XIAOZUOWEN_DIR)
        try:
            for fn in os.listdir(base_dir):
                fn_lower = fn.lower()
                if fn_lower.endswith('.txt') and fn_lower != 'public.txt':
                    self.dm_templates[fn] = os.path.join(base_dir, fn)
            print(f"已加载DM模板: {list(self.dm_templates.keys())}")
        except Exception as e:
            print(f"加载DM模板失败: {e}")

    def _get_dm_template_select_options(self) -> list[discord.SelectOption]:
        """构建可选的私信模板下拉选项，首项固定为默认模板。"""
        options: list[discord.SelectOption] = [
            discord.SelectOption(
                label="默认（第三方API）",
                value="__none__",
                description="使用 default.txt"
            )
        ]
        try:
            for fn in sorted(self.dm_templates.keys()):
                fn_lower = fn.lower()
                if fn_lower in {"default.txt", "public.txt"}:
                    continue
                options.append(discord.SelectOption(label=fn, value=fn))
        except Exception as e:
            print(f"构建模板选项失败: {e}")
        return options

    def build_punishment_result_embed(self,
                                     success: bool,
                                     message: str,
                                     punishment_history: list[dict[str, Any]],
                                     operator: discord.abc.User) -> discord.Embed:
        """构建处罚执行结果回执Embed，供不同交互入口复用"""
        if success:
            embed = discord.Embed(
                title="✅ 处罚执行成功",
                description=message,
                color=discord.Color.green(),
                timestamp=datetime.now()
            )
            if punishment_history:
                history_lines = []
                for record in punishment_history[:5]:  # 最多显示5条历史记录
                    try:
                        timestamp_dt = datetime.fromisoformat(record['timestamp'])
                        time_str = timestamp_dt.strftime('%Y-%m-%d %H:%M')
                    except Exception:
                        time_str = record['timestamp'][:16]

                    status_emoji = {
                        'executed': '✅',
                        'failed': '❌',
                        'revoked': '↩️'
                    }.get(record['status'], '❓')

                    source_tag = f"[{record.get('source_type', 'local')}]"

                    history_lines.append(
                        f"{status_emoji} {source_tag} **第{record['punish_count']}次** - {time_str}\n"
                        f"   原因: {record['reason'][:30]}{'...' if len(record['reason']) > 30 else ''}\n"
                        f"   执行者: {record['executor_name']}"
                    )

                embed.add_field(
                    name=f"📋 该用户的处罚历史（共{len(punishment_history)}条）",
                    value="\n".join(history_lines) if history_lines else "无历史记录",
                    inline=False
                )

            embed.set_footer(text=f"执行者: {operator.name}")
            return embed

        is_duplicate_prevented = self._is_duplicate_punishment_message(message)
        embed = discord.Embed(
            title="⚠️ 本次未重复执行处罚" if is_duplicate_prevented else "❌ 处罚执行失败",
            description=message,
            color=discord.Color.orange() if is_duplicate_prevented else discord.Color.red(),
            timestamp=datetime.now()
        )
        embed.set_footer(text=f"操作人: {operator.name}")
        return embed

    async def send_dm(self, user: discord.User, message_content: str) -> bool:
        """发送私信给用户"""
        try:
            await user.send(message_content)
            return True
        except discord.Forbidden:
            print(f"无法发送私信给用户 {user.name} ({user.id})")
            return False
        except Exception as e:
            print(f"发送私信时出错: {e}")
            return False

    async def send_log_embed(self, channel: discord.abc.Messageable, user: discord.User,
                            executor: discord.User, reason: str, message_link: str,
                            removed_roles: list[int], record_id: int,
                            trigger_guild: discord.Guild | None = None,
                            sync_results: list[dict[str, Any]] | None = None,
                            original_message: discord.Message | None = None) -> discord.Message | None:
        """发送日志Embed到指定频道，并转发原消息"""
        embed = discord.Embed(
            title="⚠️ 答题处罚执行",
            color=discord.Color.red(),
            timestamp=datetime.now()
        )

        embed.add_field(name="处罚对象", value=f"{user.mention} ({user.id})", inline=False)
        embed.add_field(name="执行者", value=f"{executor.mention}", inline=True)
        embed.add_field(name="原因", value=reason, inline=True)
        if message_link:
            embed.add_field(name="原消息", value=f"[跳转到消息]({message_link})", inline=False)

        if trigger_guild:
            embed.add_field(
                name="触发服务器",
                value=f"{trigger_guild.name} ({trigger_guild.id})",
                inline=False
            )

        if removed_roles:
            roles_str = ", ".join([f"<@&{role_id}>" for role_id in removed_roles])
            embed.add_field(name="触发服移除身份组", value=roles_str, inline=False)

        if sync_results:
            detail_lines = []
            for result in sync_results:
                gname = result.get("guild_name", "未知服务器")
                gid = result.get("guild_id", "-")
                if result.get("success"):
                    role_text = ", ".join([f"<@&{rid}>" for rid in result.get("removed_roles", [])]) or "无"
                    detail_lines.append(f"✅ {gname} ({gid})\n移除: {role_text}")
                else:
                    detail_lines.append(f"❌ {gname} ({gid})\n原因: {result.get('error', '未知错误')}")

            if detail_lines:
                embed.add_field(name="双服执行明细", value="\n\n".join(detail_lines)[:1024], inline=False)

        embed.set_footer(text=f"记录ID: {record_id}")

        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"发送日志Embed时出错: {e}")
            return None

        if original_message:
            return await self._forward_original_message(channel, original_message)
        return None

    async def _forward_original_message(
        self,
        channel: discord.abc.Messageable,
        message: discord.Message,
    ) -> discord.Message | None:
        """Create an immutable Discord snapshot of the punished message."""
        try:
            return await message.forward(channel)
        except Exception as e:
            print(f"创建原消息快照时出错: {type(e).__name__}: {e}")
            try:
                await channel.send("⚠️ 无法创建原消息快照；处罚日志已保留。")
            except Exception as warning_error:
                print(f"发送原消息快照失败警告时出错: {type(warning_error).__name__}: {warning_error}")
            return None

    async def _build_dm_content(self, target_message: discord.Message | None,
                               reason: str, executor: discord.User,
                               punish_count: int,
                               dm_template_filename: str | None = None,
                               removal_results: list[dict[str, Any]] | None = None) -> str:
        """构建私信内容"""
        # 读取3rd.txt文件内容
        third_content = "请重新完成新人验证答题。"  # 默认内容

        # 构建“服务器 + 被移除身份组”说明
        server_role_parts: list[str] = []
        for item in (removal_results or []):
            if not item.get("success"):
                continue

            guild_id = str(item.get("guild_id", "")).strip()
            guild_name = item.get("guild_name", "未知服务器")
            removed_role_ids = self._parse_json_list(item.get("removed_roles", []))

            role_names: list[str] = []
            guild_obj = self.bot.get_guild(int(guild_id)) if guild_id.isdigit() else None
            for role_id in removed_role_ids:
                role_name = None
                if guild_obj:
                    role_obj = guild_obj.get_role(role_id)
                    if role_obj:
                        role_name = f"@{role_obj.name}"
                if not role_name:
                    role_name = f"ID:{role_id}"
                role_names.append(role_name)

            roles_text = "、".join(role_names) if role_names else "无可展示身份组"
            server_role_parts.append(f"{guild_name}（{roles_text}）")

        server_role_text = "，".join(server_role_parts) if server_role_parts else "未记录到具体服务器与身份组"
        confirm_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # Use the selected DM template file from the configured template directory.
        try:
            if dm_template_filename and dm_template_filename in getattr(self, "dm_templates", {}):
                template_path = self.dm_templates[dm_template_filename]
                async with aiofiles.open(template_path, encoding='utf-8') as f:
                    third_content = await f.read()
        except Exception as e:
            print(f"读取模板文件失败: {e}")
        
        reverify_line = "\n请仔细阅读以上内容和社区规则，重新完成新人验证答题。"
        if self.reverify_link:
            reverify_line = f"\n请仔细阅读以上内容和社区规则，重新完成新人验证答题： {self.reverify_link}"

        dm_parts = [
            "# === 重新答题通知 ===\n",
            "你好，\n",
            f"由于 {reason}，你在以下服务器的一些身份组已被移除：{server_role_text}。\n",
            f"此操作在{confirm_time}由{executor.name}确认。\n",
            third_content.strip(),
            reverify_line,
        ]
        
        if self.appeal_channel_id:
            rules_text = "社区规则"
            if self.rules_link:
                rules_text = f"[社区规则]({self.rules_link})"

            dm_parts.append(
                "\n## ⚠️ 请勿回复此消息，机器人不会读取或转发私信。\n\n"
                f"在**重新阅读上方内容和{rules_text}之后**，如果你认为本次处罚存在事实性错误"
                "（例如：处罚对象搞错了、使用的API/云酒馆被误认为违规第三方提供等），"
                f"请使用 <#{self.appeal_channel_id}> 频道申诉。\n\n"
                "⛔ **以下无效申诉将不予回复：**\n"
                " - 不读完上方说明和社区rule就开ticket，只反问「我做了什么」，「凭什么罚我」的\n"
                "- 以「不理解相关规则」或「不知道相关规则」为理由，主张无知者无罪的\n"
                "- 觉得规则不合理，想来找管理辩论，更改规则的\n"
            )
        
        return "\n".join(dm_parts)

    async def _send_channel_notification(self, channel: discord.TextChannel,
                                        user: discord.User, executor: discord.User,
                                        reason: str, removed_roles: list[int]):
        """在原频道发送处罚通知"""
        embed = discord.Embed(
            title="⚠️ 答题处罚",
            color=discord.Color.orange(),
            timestamp=datetime.now()
        )
        
        embed.add_field(name="对象", value=f"{user.mention}", inline=True)
        embed.add_field(name="执行者", value=f"{executor.mention}", inline=True)
        embed.add_field(name="原因", value=reason, inline=False)
        
        # 已移除：不再显示"已移除身份组"字段
        # if removed_roles:
        #     roles_str = ", ".join([f"<@&{role_id}>" for role_id in removed_roles])
        #     embed.add_field(name="已移除身份组", value=roles_str, inline=False)
        
        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"发送频道通知时出错: {e}")

    async def _get_log_destinations(self) -> list[discord.abc.Messageable]:
        """获取所有有效的日志发送目标（支持多个子区和频道同时发送）"""
        destinations: list[discord.abc.Messageable] = []
        seen_ids: set = set()  # 去重：同一个ID不重复添加

        # 1) 收集所有子区
        for thread_id in self.log_thread_ids:
            if thread_id in seen_ids:
                continue
            ch = self.bot.get_channel(thread_id)
            if not ch:
                try:
                    ch = await self.bot.fetch_channel(thread_id)
                except Exception:
                    ch = None
            if ch is None:
                print(f"警告：无法获取日志子区 {thread_id}，已跳过")
                continue
            # 如果是已归档的子区，自动解档
            if isinstance(ch, discord.Thread) and ch.archived:
                try:
                    await ch.edit(archived=False)
                except Exception:
                    pass
            seen_ids.add(thread_id)
            destinations.append(ch)

        # 2) 收集所有频道
        for channel_id in self.log_channel_ids:
            if channel_id in seen_ids:
                continue
            ch = self.bot.get_channel(channel_id)
            if not ch:
                try:
                    ch = await self.bot.fetch_channel(channel_id)
                except Exception:
                    ch = None
            if ch is None:
                print(f"警告：无法获取日志频道 {channel_id}，已跳过")
                continue
            seen_ids.add(channel_id)
            destinations.append(ch)

        return destinations

    async def send_revoke_log_embed(self, channel: discord.abc.Messageable,
                                   record: dict, revoker: discord.User,
                                   restored_roles: list[int], failed_roles: list[int],
                                   restore_targets: dict[str, list[int]]):
        """发送撤销日志Embed到指定频道"""
        embed = discord.Embed(
            title="↩️ 快速处罚撤销",
            color=discord.Color.green(),
            timestamp=datetime.now()
        )

        embed.add_field(
            name="撤销对象",
            value=f"{record['user_name']} (ID: {record['user_id']})",
            inline=False
        )
        embed.add_field(name="撤销者", value=f"{revoker.mention}", inline=True)
        embed.add_field(name="原执行者", value=record['executor_name'], inline=True)
        embed.add_field(name="来源", value=record.get('source_type', 'local'), inline=True)

        embed.add_field(name="原处罚原因", value=record['reason'], inline=False)
        embed.add_field(name="原处罚时间", value=record['timestamp'], inline=False)

        target_lines = [f"{gid}: {len(roles)}个" for gid, roles in restore_targets.items()]
        if target_lines:
            embed.add_field(name="恢复目标服务器", value="\n".join(target_lines), inline=False)

        if restored_roles:
            roles_str = ", ".join([f"<@&{role_id}>" for role_id in restored_roles])
            embed.add_field(name="✅ 已恢复身份组", value=roles_str, inline=False)

        if failed_roles:
            failed_str = ", ".join([f"ID:{role_id}" for role_id in failed_roles])
            embed.add_field(name="❌ 恢复失败的身份组", value=failed_str, inline=False)

        embed.set_footer(text=f"撤销的记录ID: {record['id']}")

        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"发送撤销日志Embed时出错: {e}")
