from __future__ import annotations

import discord
import asyncio
import os
from contextlib import asynccontextmanager
import json
from typing import Any
import aiofiles

from cogs.utils import remove_guild_scoped_context_menus
from .commands import quick_punish_context, remote_quick_punish_context
from paths import QUICK_PUNISH_SYNC_CONFIG_FILE, XIAOZUOWEN_DIR

QUICK_PUNISH_SYNC_CONFIG_PATH = str(QUICK_PUNISH_SYNC_CONFIG_FILE)
PUBLIC_NOTICE_PATH = XIAOZUOWEN_DIR / "public.txt"

class QuickPunishCoreMixin:
    def __init__(self, bot):
        self.bot = bot
        self._punish_locks: dict[int, asyncio.Lock] = {}
        self._punish_locks_guard = asyncio.Lock()

        # 从环境变量加载配置
        self.enabled = os.getenv("QUICK_PUNISH_ENABLED", "false").lower() == "true"
        self.sync_config_path = QUICK_PUNISH_SYNC_CONFIG_PATH
        self.allowed_roles = self._parse_role_ids(os.getenv("QUICK_PUNISH_ROLES", ""))
        self.remove_roles = self._parse_role_ids(os.getenv("QUICK_PUNISH_REMOVE_ROLES", ""))
        self.log_channel_ids = self._parse_channel_ids(os.getenv("QUICK_PUNISH_LOG_CHANNEL", ""))
        self.log_thread_ids = self._parse_channel_ids(os.getenv("QUICK_PUNISH_LOG_THREAD", ""))
        self.interface_channel_id = self._parse_channel_id(os.getenv("QUICK_PUNISH_INTERFACE_CHANNEL"))
        self.appeal_channel_id = self._parse_channel_id(os.getenv("QUICK_PUNISH_APPEAL_CHANNEL"))
        self.reverify_link = os.getenv("QUICK_PUNISH_REVERIFY_LINK", "").strip()
        self.rules_link = os.getenv("QUICK_PUNISH_RULES_LINK", "").strip()

        # 双服同步配置（JSON优先，env作为兼容fallback）
        self.sync_config = self._load_sync_config()

        # Load DM templates from the configured template directory.
        self.dm_templates: dict[str, str] = {}
        self._load_dm_templates()

    async def cog_load(self):
        await asyncio.to_thread(self.init_database)

    def cog_unload(self):
        remove_guild_scoped_context_menus(
            self.bot.tree,
            [quick_punish_context, remote_quick_punish_context],
        )

    def _parse_role_ids(self, role_str: str) -> list[int]:
        """解析身份组ID字符串"""
        if not role_str:
            return []
        try:
            return [int(role_id.strip()) for role_id in role_str.split(",") if role_id.strip()]
        except ValueError:
            print(f"警告：无法解析身份组ID: {role_str}")
            return []

    def _parse_channel_id(self, channel_str: str) -> int | None:
        """解析频道ID字符串"""
        if not channel_str:
            return None
        try:
            return int(channel_str.strip())
        except ValueError:
            print(f"警告：无法解析频道ID: {channel_str}")

    def _parse_channel_ids(self, channel_str: str) -> list[int]:
        """解析频道ID字符串（支持逗号分隔的多个ID）"""
        if not channel_str:
            return []
        result = []
        for part in channel_str.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                result.append(int(part))
            except ValueError:
                print(f"警告：无法解析频道ID: {part}")
        return result

    def _load_sync_config(self) -> dict[str, Any]:
        """加载并校验双服同步配置"""
        default_config: dict[str, Any] = {
            "version": 1,
            "sync_guild_ids": [],
            "guilds": {},
            "policy": {"mode": "best_effort"}
        }

        if not os.path.exists(self.sync_config_path):
            print(f"警告：未找到同步配置文件 {self.sync_config_path}，将回退到env配置")
            return default_config

        try:
            with open(self.sync_config_path, encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            print(f"警告：读取同步配置失败: {e}")
            return default_config

        if not isinstance(raw, dict):
            print("警告：quick_punish_sync.json 顶层必须是对象")
            return default_config

        sync_ids: list[str] = []
        for gid in raw.get("sync_guild_ids", []):
            try:
                sync_ids.append(str(int(str(gid).strip())))
            except Exception:
                print(f"警告：sync_guild_ids 中存在无效guild id: {gid}")

        guild_cfg_raw = raw.get("guilds", {}) if isinstance(raw.get("guilds", {}), dict) else {}
        guilds: dict[str, dict[str, list[int]]] = {}
        for gid, cfg in guild_cfg_raw.items():
            gid_str = str(gid).strip()
            if not gid_str:
                continue
            if not isinstance(cfg, dict):
                cfg = {}
            allowed_raw = cfg.get("allowed_roles", [])
            remove_raw = cfg.get("punish_remove_roles", [])
            guilds[gid_str] = {
                "allowed_roles": [int(x) for x in allowed_raw if str(x).strip().isdigit()] if isinstance(allowed_raw, list) else [],
                "punish_remove_roles": [int(x) for x in remove_raw if str(x).strip().isdigit()] if isinstance(remove_raw, list) else []
            }

        if not sync_ids:
            print("警告：quick_punish_sync.json 的 sync_guild_ids 为空，将仅处理触发服务器")

        for gid in sync_ids:
            if gid not in guilds:
                guilds[gid] = {"allowed_roles": [], "punish_remove_roles": []}
                print(f"警告：同步服 {gid} 未配置 guilds 块，已按空配置处理")

        policy = raw.get("policy", {}) if isinstance(raw.get("policy", {}), dict) else {}
        mode = str(policy.get("mode", "best_effort")).strip().lower() or "best_effort"
        if mode != "best_effort":
            print(f"警告：当前仅支持 best_effort，收到 {mode}，将回退为 best_effort")
            mode = "best_effort"

        return {
            "version": int(raw.get("version", 1)) if str(raw.get("version", "1")).isdigit() else 1,
            "sync_guild_ids": sync_ids,
            "guilds": guilds,
            "policy": {"mode": mode}
        }

    def _get_sync_guild_ids(self, trigger_guild_id: int | None = None) -> list[str]:
        ids = list(self.sync_config.get("sync_guild_ids", []))
        if trigger_guild_id is not None:
            gid = str(trigger_guild_id)
            if gid not in ids:
                ids.append(gid)
        if not ids and trigger_guild_id is not None:
            return [str(trigger_guild_id)]
        return ids

    def _get_guild_sync_config(self, guild_id: int) -> dict[str, list[int]]:
        cfg = self.sync_config.get("guilds", {}).get(str(guild_id), {})
        if not isinstance(cfg, dict):
            cfg = {}
        return {"allowed_roles": cfg.get("allowed_roles", []), "punish_remove_roles": cfg.get("punish_remove_roles", [])}

    def _get_punish_lock(self, user_id: int) -> asyncio.Lock:
        lock = self._punish_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._punish_locks[user_id] = lock
        return lock

    @asynccontextmanager
    async def _punish_execution_lock(self, user_id: int):
        async with self._punish_locks_guard:
            punish_lock = self._get_punish_lock(user_id)
            if punish_lock.locked():
                raise RuntimeError("punish_lock_busy")
            await punish_lock.acquire()

        try:
            yield
        finally:
            punish_lock.release()
            async with self._punish_locks_guard:
                if self._punish_locks.get(user_id) is punish_lock and not punish_lock.locked():
                    self._punish_locks.pop(user_id, None)

    def _is_already_processed_result(self, result: dict[str, Any] | None) -> bool:
        already_processed_codes = {"no_removable_roles", "removal_conflict"}
        return bool(result) and result.get("code") in already_processed_codes

    def _all_sync_results_already_processed(self, results: list[dict[str, Any]]) -> bool:
        return bool(results) and all(self._is_already_processed_result(item) for item in results)

    def _is_duplicate_punishment_message(self, message: str) -> bool:
        duplicate_markers = (
            "正在被其他管理员处理",
            "先一步处罚",
            "不会重复执行",
            "已不再拥有可移除的处罚身份组"
        )
        return any(marker in message for marker in duplicate_markers)

    def has_permission(self, interaction: discord.Interaction) -> bool:
        """检查用户是否有快速处罚权限（仅校验触发服allowed_roles）"""
        if not self.enabled:
            return False

        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return False

        guild_cfg = self._get_guild_sync_config(interaction.guild.id)
        allowed_roles = guild_cfg.get("allowed_roles", []) or self.allowed_roles
        if not allowed_roles:
            return False

        user_roles = [role.id for role in interaction.user.roles]
        return any(role_id in user_roles for role_id in allowed_roles)

    async def remove_user_roles(self, member: discord.Member, roles_to_remove: list[int]) -> tuple[list[int], bool]:
        """移除用户的身份组
        返回: (实际被移除的身份组ID列表, 是否成功移除了至少一个身份组)
        """
        removed_roles = []
        roles_to_remove_objs = []
        
        # 在移除前的最后一刻再次检查用户是否拥有这些身份组
        for role_id in roles_to_remove:
            role = member.guild.get_role(role_id)
            if role and role in member.roles:  # 最终检查
                roles_to_remove_objs.append(role)
                removed_roles.append(role_id)
        
        # 如果没有任何身份组需要移除，返回空列表和False
        if not roles_to_remove_objs:
            return [], False
        
        try:
            await member.remove_roles(*roles_to_remove_objs, reason="快速处罚")
            return removed_roles, True
        except Exception as e:
            print(f"移除身份组时出错: {e}")
            raise

    async def restore_user_roles(self, member: discord.Member, roles_to_restore: list[int]) -> tuple[list[int], list[int]]:
        """恢复用户的身份组
        返回: (成功恢复的身份组ID列表, 失败的身份组ID列表)
        """
        restored_roles = []
        failed_roles = []
        roles_to_add = []
        
        for role_id in roles_to_restore:
            role = member.guild.get_role(role_id)
            if role:
                # 检查用户是否已有该身份组
                if role not in member.roles:
                    roles_to_add.append(role)
                    restored_roles.append(role_id)
                else:
                    # 用户已有该身份组，也算成功
                    restored_roles.append(role_id)
            else:
                # 身份组不存在
                failed_roles.append(role_id)
        
        if roles_to_add:
            try:
                await member.add_roles(*roles_to_add, reason="快速处罚撤销")
            except Exception as e:
                print(f"恢复身份组时出错: {e}")
                # 如果添加失败，将这些角色移到失败列表
                for role in roles_to_add:
                    restored_roles.remove(role.id)
                    failed_roles.append(role.id)
        
        return restored_roles, failed_roles

    async def _resolve_member_in_guild(self, guild: discord.Guild, user_id: int,
                                       force_fetch: bool = False) -> discord.Member | None:
        cached_member = guild.get_member(user_id)
        if cached_member and not force_fetch:
            return cached_member
        try:
            return await guild.fetch_member(user_id)
        except Exception:
            return cached_member

    async def _execute_role_removal_in_guild(self, guild_id: str,
                                             target_user_id: int,
                                             trigger_guild_id: int | None = None,
                                             force_fetch: bool = False) -> dict[str, Any]:
        result = {
            "guild_id": guild_id,
            "guild_name": guild_id,
            "success": False,
            "removed_roles": [],
            "error": None,
            "code": None
        }

        if not str(guild_id).isdigit():
            result["error"] = "服务器配置中的 guild id 无效"
            result["code"] = "invalid_guild_id"
            return result

        guild = self.bot.get_guild(int(guild_id))
        if not guild:
            result["error"] = "机器人不在该服务器，或暂时无法获取服务器对象"
            result["code"] = "guild_unavailable"
            return result

        result["guild_name"] = guild.name
        member = await self._resolve_member_in_guild(guild, target_user_id, force_fetch=force_fetch)
        if not member:
            result["error"] = "用户当前不在该服务器，无法同步移除身份组"
            result["code"] = "member_not_found"
            return result

        guild_cfg = self._get_guild_sync_config(guild.id)
        configured_remove_roles = guild_cfg.get("punish_remove_roles", [])
        if not configured_remove_roles and trigger_guild_id is not None and str(guild.id) == str(trigger_guild_id):
            configured_remove_roles = self.remove_roles

        if not configured_remove_roles:
            result["error"] = "该服务器未配置可移除的处罚身份组"
            result["code"] = "no_configured_roles"
            return result

        user_role_ids = [role.id for role in member.roles]
        roles_to_remove = [role_id for role_id in configured_remove_roles if role_id in user_role_ids]
        if not roles_to_remove:
            result["error"] = "用户当前已不再拥有可移除的处罚身份组，可能已被其他管理员先一步处理"
            result["code"] = "no_removable_roles"
            return result

        try:
            removed_roles, removal_success = await self.remove_user_roles(member, roles_to_remove)
            if not removal_success:
                result["error"] = "执行时发现用户已不再拥有需要移除的身份组，可能已被其他管理员先一步处罚"
                result["code"] = "removal_conflict"
                return result
            result["success"] = True
            result["removed_roles"] = removed_roles
            result["code"] = "success"
            return result
        except Exception as e:
            result["error"] = f"移除身份组时发生错误：{str(e)}"
            result["code"] = "removal_error"
            return result

    def _format_sync_results(self, results: list[dict[str, Any]]) -> str:
        lines = []
        for item in results:
            if item.get("success"):
                lines.append(f"✅ {item.get('guild_name')}：移除{len(item.get('removed_roles', []))}个身份组")
            else:
                lines.append(f"❌ {item.get('guild_name')}：{item.get('error', '未知错误')}")
        return "\n".join(lines) if lines else "无执行结果"

    async def execute_punishment(self, interaction: discord.Interaction,
                                target_user: discord.User,
                                target_message: discord.Message,
                                reason: str,
                                executor: discord.User,
                                dm_template_filename: str | None = None) -> tuple[bool, str, list[dict]]:
        """执行处罚的主要逻辑，返回(成功状态, 消息, 处罚历史)"""
        trigger_guild = interaction.guild
        if trigger_guild is None:
            return False, "无法识别触发服务器", []

        try:
            async with self._punish_execution_lock(target_user.id):
                sync_guild_ids = self._get_sync_guild_ids(trigger_guild.id)
                sync_results = list(await asyncio.gather(*[
                    self._execute_role_removal_in_guild(
                        guild_id,
                        target_user.id,
                        trigger_guild_id=trigger_guild.id,
                        force_fetch=True,
                    )
                    for guild_id in sync_guild_ids
                ]))

                success_results = [r for r in sync_results if r.get("success")]
                trigger_result = next(
                    (r for r in sync_results if str(r.get("guild_id")) == str(trigger_guild.id)),
                    None
                )
                if not success_results:
                    if self._is_already_processed_result(trigger_result) or self._all_sync_results_already_processed(sync_results):
                        return False, (
                            f"用户 {target_user.mention} 当前已不再拥有可移除的处罚身份组，"
                            "很可能已被其他管理员先一步处罚，本次不会重复执行。\n"
                            f"{self._format_sync_results(sync_results)}"
                        ), []
                    return False, (
                        "本次处罚未执行：未能在任何目标服务器完成身份组移除。\n"
                        f"{self._format_sync_results(sync_results)}"
                    ), []

                removed_roles_by_guild = {
                    str(r["guild_id"]): r.get("removed_roles", [])
                    for r in success_results if r.get("removed_roles")
                }
                trigger_removed_roles = removed_roles_by_guild.get(str(trigger_guild.id), [])

                record_id, punish_count = await self.log_to_database_with_count(
                    user=target_user,
                    message=target_message,
                    executor=executor,
                    reason=reason,
                    removed_roles=trigger_removed_roles,
                    punish_count=0,
                    status="executed",
                    source_type="local",
                    removed_roles_by_guild=removed_roles_by_guild,
                    source_guild_id=str(trigger_guild.id)
                )

                dm_content = await self._build_dm_content(
                    target_message=target_message,
                    reason=reason,
                    executor=executor,
                    punish_count=punish_count,
                    dm_template_filename=dm_template_filename,
                    removal_results=success_results
                )
                dm_sent = await self.send_dm(target_user, dm_content)

                await self._send_channel_notification(
                    channel=target_message.channel,
                    user=target_user,
                    executor=executor,
                    reason=reason,
                    removed_roles=trigger_removed_roles
                )

                try:
                    async with aiofiles.open(PUBLIC_NOTICE_PATH, encoding='utf-8') as f:
                        public_content = await f.read()
                    await target_message.channel.send(public_content.strip())
                except Exception as e:
                    print(f"发送public.txt内容失败: {e}")

                log_destinations = await self._get_log_destinations()
                if log_destinations:
                    message_link = f"https://discord.com/channels/{trigger_guild.id}/{target_message.channel.id}/{target_message.id}"
                    for log_dest in log_destinations:
                        await self.send_log_embed(
                            channel=log_dest,
                            user=target_user,
                            executor=executor,
                            reason=reason,
                            message_link=message_link,
                            removed_roles=trigger_removed_roles,
                            record_id=record_id,
                            trigger_guild=trigger_guild,
                            sync_results=sync_results,
                            original_message=target_message
                        )

                if self.interface_channel_id:
                    try:
                        interface_channel = self.bot.get_channel(self.interface_channel_id)
                        if interface_channel:
                            await interface_channel.send(f'{{"punish": {target_user.id}}}')
                        else:
                            print("警告：未找到 QUICK_PUNISH_INTERFACE_CHANNEL，已跳过接口发送")
                    except Exception as e:
                        print(f"警告：接口频道发送失败（不影响主流程）: {e}")

                punishment_history = await self.get_user_punishment_history(str(target_user.id))
                success_msg = f"用户 {target_user.mention} 已被处罚（第{punish_count}次，全局）\n{self._format_sync_results(sync_results)}"
                if not dm_sent:
                    success_msg += "\n⚠️ 注意：私信发送失败（用户可能关闭了私信）"

                return True, success_msg, punishment_history
        except RuntimeError as e:
            if str(e) != "punish_lock_busy":
                raise
            return False, (
                f"用户 {target_user.mention} 正在被其他管理员处理。\n"
                "为避免重复处罚，本次操作已被拦截；若对方已完成处罚，请稍后再查看记录。"
            ), []
        except Exception as e:
            print(f"执行处罚时出错: {e}")
            try:
                await self.log_to_database_with_count(
                    user=target_user,
                    message=target_message,
                    executor=executor,
                    reason=reason,
                    removed_roles=[],
                    punish_count=0,
                    status="failed",
                    source_type="local",
                    removed_roles_by_guild={},
                    source_guild_id=str(trigger_guild.id)
                )
            except Exception:
                pass
            return False, f"执行处罚时出错：{str(e)}", []

    async def _execute_revoke_record(self, interaction: discord.Interaction, user_id: str, record: dict[str, Any], restore_roles: bool = True) -> tuple[bool, str]:
        restored_roles: list[int] = []
        failed_roles: list[int] = []
        detail_lines: list[str] = []

        if restore_roles:
            restore_targets = self._build_restore_targets(record, interaction.guild.id if interaction.guild else None)
            if not restore_targets:
                return False, f"在数据库中找不到（{user_id}）的上次处罚移除了什么身份组，可能是由于上次处罚来源于同步，请检查日志频道。"

            for guild_id, roles in restore_targets.items():
                guild = self.bot.get_guild(int(guild_id)) if str(guild_id).isdigit() else None
                if not guild:
                    failed_roles.extend(roles)
                    detail_lines.append(f"❌ {guild_id}: 机器人不在该服务器")
                    continue

                member = await self._resolve_member_in_guild(guild, int(user_id))
                if not member:
                    failed_roles.extend(roles)
                    detail_lines.append(f"❌ {guild.name}: 用户不在服务器")
                    continue

                restored, failed = await self.restore_user_roles(member, roles)
                restored_roles.extend(restored)
                failed_roles.extend(failed)
                detail_lines.append(f"✅ {guild.name}: 恢复 {len(restored)} 个，失败 {len(failed)} 个")
        else:
            restore_targets = {}
            detail_lines.append("⏭️ 已跳过身份组恢复")

        success = await self.revoke_punishment(record['id'])
        if not success:
            return False, "❌ 撤销处罚失败，可能记录已被修改"

        log_destinations = await self._get_log_destinations()
        for log_dest in log_destinations:
            await self.send_revoke_log_embed(
                channel=log_dest,
                record=record,
                revoker=interaction.user,
                restored_roles=restored_roles,
                failed_roles=failed_roles,
                restore_targets=restore_targets
            )

        message = (
            f"✅ 成功撤销对用户 **{record['user_name']}** (ID: {user_id}) 的处罚\n"
            f"记录ID: {record['id']}\n"
            f"原处罚时间: {record['timestamp']}\n"
            f"原处罚原因: {record['reason']}\n"
            f"来源: {record.get('source_type', 'local')}\n"
            + "\n".join(detail_lines)
        )
        return True, message
