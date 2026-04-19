from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from datetime import datetime
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from cogs.utils import check_admin, log_slash_command


class RoleSyncSkipError(RuntimeError):
    """用于表示当前同步轮次应被跳过，而不是视为程序崩溃。"""


class RoleSyncCog(commands.Cog):
    """自动同步 Discord 身份组成员到 trusted_users。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config: dict[str, Any] | None = None
        self._sync_task: asyncio.Task | None = None
        self._sync_lock = asyncio.Lock()
        self._config_path = "cogs/config/role_sync_config.json"
        self._load_config()

    def _now_iso(self) -> str:
        return datetime.now().isoformat(timespec="seconds")

    def _load_config(self) -> None:
        """从 JSON 加载配置；文件不存在或损坏时不启动任务。"""
        if not os.path.exists(self._config_path):
            self.config = None
            return

        try:
            with open(self._config_path, encoding="utf-8") as file:
                data = json.load(file)

            if not isinstance(data, dict):
                raise ValueError("配置文件根节点必须是对象")

            guild_id = str(data["guild_id"])
            role_ids_raw = data.get("role_ids", [])
            if not isinstance(role_ids_raw, list):
                raise ValueError("role_ids 必须是列表")

            role_ids = []
            for role_id in role_ids_raw:
                role_id_str = str(role_id).strip()
                if role_id_str:
                    role_ids.append(role_id_str)

            if not role_ids:
                raise ValueError("role_ids 不能为空")

            base_interval = int(data["base_interval_minutes"])
            max_interval = int(data.get("max_interval_minutes", 360))
            if max_interval < base_interval:
                max_interval = base_interval

            current_interval = int(data.get("current_interval_minutes", base_interval))
            current_interval = max(1, min(current_interval, max_interval))

            self.config = {
                "guild_id": guild_id,
                "role_ids": list(dict.fromkeys(role_ids)),
                "base_interval_minutes": max(1, base_interval),
                "current_interval_minutes": current_interval,
                "max_interval_minutes": max(1, max_interval),
                "enabled": bool(data.get("enabled", True)),
                "last_sync_time": data.get("last_sync_time"),
                "last_sync_added_count": int(data.get("last_sync_added_count", 0)),
                "created_by": str(data.get("created_by", "")),
                "created_at": data.get("created_at"),
            }
        except Exception as error:
            print(f"[role_sync] 加载配置失败，已忽略配置文件: {error}")
            self.config = None

    def _save_config(self) -> None:
        """保存当前配置到 JSON。"""
        if self.config is None:
            return

        try:
            os.makedirs(os.path.dirname(self._config_path), exist_ok=True)
            with open(self._config_path, "w", encoding="utf-8") as file:
                json.dump(self.config, file, ensure_ascii=False, indent=2)
        except Exception as error:
            print(f"[role_sync] 保存配置失败: {error}")
            raise

    def _load_trusted_users_sync(self) -> list[int]:
        with sqlite3.connect("users.db") as conn:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE IF NOT EXISTS trusted_users (id TEXT PRIMARY KEY)")
            cursor.execute("SELECT id FROM trusted_users")
            return [int(row[0]) for row in cursor.fetchall()]

    async def _update_bot_memory(self) -> None:
        """从数据库重新加载 trusted_users 到 bot 内存。"""
        try:
            self.bot.trusted_users = await asyncio.to_thread(self._load_trusted_users_sync)
        except sqlite3.Error as error:
            print(f"[role_sync] 刷新 bot.trusted_users 失败: {error}")
            raise

    def _sync_trusted_users_sync(self, target_user_ids: set[str]) -> tuple[list[str], int]:
        with sqlite3.connect("users.db") as conn:
            cursor = conn.cursor()
            cursor.execute("CREATE TABLE IF NOT EXISTS trusted_users (id TEXT PRIMARY KEY)")
            cursor.execute("SELECT id FROM trusted_users")
            existing_ids = {str(row[0]) for row in cursor.fetchall()}

            new_ids = sorted(target_user_ids - existing_ids, key=int)
            existed_count = len(target_user_ids & existing_ids)

            if new_ids:
                cursor.executemany(
                    "INSERT OR IGNORE INTO trusted_users (id) VALUES (?)",
                    [(user_id,) for user_id in new_ids],
                )

        return new_ids, existed_count

    def _record_sync_result(self, added_count: int) -> None:
        if self.config is None:
            return

        self.config["last_sync_time"] = self._now_iso()
        self.config["last_sync_added_count"] = int(added_count)

    def _get_configured_guild(self) -> discord.Guild | None:
        if not self.config:
            return None

        try:
            guild_id = int(self.config["guild_id"])
        except (KeyError, TypeError, ValueError):
            return None

        return self.bot.get_guild(guild_id)

    def _format_interval(self, minutes: int) -> str:
        if minutes < 60:
            return f"{minutes} 分钟"
        if minutes % 60 == 0:
            return f"{minutes} 分钟 ({minutes // 60} 小时)"
        return f"{minutes} 分钟 ({minutes / 60:.1f} 小时)"

    def _format_role_list(self, guild: discord.Guild | None) -> str:
        if not self.config:
            return "未配置"

        parts: list[str] = []
        for role_id in self.config.get("role_ids", []):
            role = None
            if guild is not None:
                try:
                    role = guild.get_role(int(role_id))
                except (TypeError, ValueError):
                    role = None

            if role is not None:
                parts.append(role.mention)
            else:
                parts.append(f"`已删除身份组: {role_id}`")

        return ", ".join(parts) if parts else "未配置"

    async def _send_ephemeral(
        self,
        interaction: discord.Interaction,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
    ) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(content=content, embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(content=content, embed=embed, ephemeral=True)

    async def _do_sync(
        self,
        guild: discord.Guild,
        *,
        reverse_sync: bool = False,
    ) -> tuple[int, int]:
        """执行一次同步，返回 (新增人数, 已存在人数)。"""
        _ = reverse_sync  # 预留未来反向同步扩展

        if not self.config or not self.config.get("enabled"):
            raise RoleSyncSkipError("自动同步尚未配置或未启用")

        async with self._sync_lock:
            if not guild.chunked:
                await guild.chunk()

            valid_roles: list[discord.Role] = []
            missing_role_ids: list[str] = []

            for role_id in self.config.get("role_ids", []):
                try:
                    role = guild.get_role(int(role_id))
                except (TypeError, ValueError):
                    role = None

                if role is None:
                    missing_role_ids.append(str(role_id))
                    continue

                valid_roles.append(role)

            if missing_role_ids:
                print(
                    f"[role_sync] 以下身份组不存在，已在本轮忽略: "
                    f"{', '.join(missing_role_ids)}"
                )

            if not valid_roles:
                raise RoleSyncSkipError("配置中的身份组均不存在，已跳过本轮同步。")

            target_user_ids: set[str] = set()
            for role in valid_roles:
                for member in role.members:
                    target_user_ids.add(str(member.id))

            try:
                new_ids, existed_count = await asyncio.to_thread(
                    self._sync_trusted_users_sync,
                    target_user_ids,
                )
            except sqlite3.Error as error:
                print(f"[role_sync] 数据库同步失败: {error}")
                raise

            if new_ids:
                await self._update_bot_memory()

            return len(new_ids), existed_count

    async def _sync_loop(self) -> None:
        """按当前间隔持续执行自动同步，并根据结果进行指数退避。"""
        await self.bot.wait_until_ready()
        print("[role_sync] 自动同步循环已启动")

        try:
            while True:
                if not self.config or not self.config.get("enabled"):
                    print("[role_sync] 配置未启用，自动同步循环退出")
                    return

                interval_minutes = int(
                    self.config.get(
                        "current_interval_minutes",
                        self.config.get("base_interval_minutes", 5),
                    )
                )
                await asyncio.sleep(interval_minutes * 60)

                guild = self._get_configured_guild()
                if guild is None:
                    configured_guild_id = self.config.get("guild_id") if self.config else "unknown"
                    print(f"[role_sync] 找不到服务器 {configured_guild_id}，跳过本轮同步")
                    continue

                try:
                    added, existed = await self._do_sync(guild)
                except RoleSyncSkipError as error:
                    print(f"[role_sync] {error}")
                    continue

                if added > 0:
                    self.config["current_interval_minutes"] = self.config["base_interval_minutes"]
                else:
                    self.config["current_interval_minutes"] = min(
                        int(self.config["current_interval_minutes"]) * 2,
                        int(self.config["max_interval_minutes"]),
                    )

                self._record_sync_result(added)
                self._save_config()

                print(
                    f"[role_sync] 自动同步完成: 新增={added}, 已存在={existed}, "
                    f"下次间隔={self.config['current_interval_minutes']} 分钟"
                )
        except asyncio.CancelledError:
            print("[role_sync] 自动同步循环已取消")
            raise
        except Exception as error:
            print(f"[role_sync] 自动同步出错: {error}")

    def _start_task(self) -> None:
        """启动或重启自动同步任务。"""
        self._stop_task()

        if not self.config or not self.config.get("enabled"):
            return

        self._sync_task = asyncio.create_task(self._sync_loop(), name="role_sync_loop")

    def _stop_task(self) -> None:
        """停止当前自动同步任务。"""
        if self._sync_task and not self._sync_task.done():
            self._sync_task.cancel()
        self._sync_task = None

    async def cog_load(self) -> None:
        """Cog 加载时恢复持久化的自动同步。"""
        if self.config and self.config.get("enabled"):
            self._start_task()
            print(
                f"[role_sync] 已从配置恢复自动同步任务，"
                f"当前间隔={self.config['current_interval_minutes']} 分钟"
            )

    def cog_unload(self) -> None:
        """Cog 卸载时停止自动同步任务。"""
        self._stop_task()

    @app_commands.command(
        name="setup_rolesync",
        description="[仅管理员] 配置自动同步身份组到 trusted_users",
    )
    @app_commands.describe(
        role="要监控的主身份组",
        interval="基础检测间隔（分钟）",
        role2="第二个要监控的身份组（可选）",
        max_interval="指数退避上限（分钟），默认 360",
    )
    @app_commands.guild_only()
    @app_commands.check(check_admin)
    async def setup_rolesync(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        interval: app_commands.Range[int, 1, 360],
        role2: discord.Role | None = None,
        max_interval: app_commands.Range[int, 1, 1440] = 360,
    ) -> None:
        """设置自动身份组同步任务。"""
        if interaction.guild is None:
            await self._send_ephemeral(interaction, content="❌ 此命令只能在服务器内使用。")
            log_slash_command(interaction, False)
            return

        if role2 is not None and role.id == role2.id:
            await self._send_ephemeral(interaction, content="❌ `role` 和 `role2` 不能是同一个身份组。")
            log_slash_command(interaction, False)
            return

        if interval > max_interval:
            await self._send_ephemeral(
                interaction,
                content="❌ 基础检测间隔不能大于退避上限。",
            )
            log_slash_command(interaction, False)
            return

        await interaction.response.defer(ephemeral=True)

        role_ids = [str(role.id)]
        if role2 is not None:
            role_ids.append(str(role2.id))

        now = self._now_iso()
        self.config = {
            "guild_id": str(interaction.guild.id),
            "role_ids": list(dict.fromkeys(role_ids)),
            "base_interval_minutes": int(interval),
            "current_interval_minutes": int(interval),
            "max_interval_minutes": int(max_interval),
            "enabled": True,
            "last_sync_time": None,
            "last_sync_added_count": 0,
            "created_by": str(interaction.user.id),
            "created_at": now,
        }

        try:
            self._save_config()
            self._start_task()

            added, existed = await self._do_sync(interaction.guild)
            self._record_sync_result(added)
            self._save_config()

            embed = discord.Embed(
                title="✅ 自动身份组同步已配置",
                color=discord.Color.green(),
            )
            embed.add_field(
                name="📋 监控身份组",
                value=self._format_role_list(interaction.guild),
                inline=False,
            )
            embed.add_field(
                name="⏱️ 基础间隔",
                value=self._format_interval(int(interval)),
                inline=True,
            )
            embed.add_field(
                name="⏱️ 退避上限",
                value=self._format_interval(int(max_interval)),
                inline=True,
            )
            embed.add_field(name="🎯 目标", value="`trusted_users`", inline=False)
            embed.add_field(name="➕ 首次同步新增", value=str(added), inline=True)
            embed.add_field(name="ℹ️ 首次同步已存在", value=str(existed), inline=True)
            embed.set_footer(text=f"配置服务器: {interaction.guild.name}")

            await interaction.edit_original_response(content=None, embed=embed)
            log_slash_command(interaction, True)
        except RoleSyncSkipError as error:
            print(f"[role_sync] setup_rolesync 首次同步被跳过: {error}")
            await interaction.edit_original_response(
                content=f"❌ 配置已保存，但首次同步被跳过：{error}",
                embed=None,
            )
            log_slash_command(interaction, False)
        except Exception as error:
            print(f"[role_sync] setup_rolesync 执行失败: {error}")
            await interaction.edit_original_response(
                content=f"❌ 设置自动同步失败：{error}",
                embed=None,
            )
            log_slash_command(interaction, False)

    @app_commands.command(
        name="rolesync",
        description="[仅管理员] 立即执行一次身份组同步",
    )
    @app_commands.describe(reset_backoff="是否将当前退避间隔重置为基础间隔")
    @app_commands.guild_only()
    @app_commands.check(check_admin)
    async def rolesync(
        self,
        interaction: discord.Interaction,
        reset_backoff: bool = False,
    ) -> None:
        """手动触发一次同步。"""
        if not self.config or not self.config.get("enabled"):
            await self._send_ephemeral(
                interaction,
                content="❌ 尚未配置自动同步，请先使用 `/setup_rolesync`。",
            )
            log_slash_command(interaction, False)
            return

        guild = self._get_configured_guild()
        if guild is None:
            await self._send_ephemeral(
                interaction,
                content=f"❌ 找不到已配置的服务器 `{self.config['guild_id']}`，无法执行同步。",
            )
            log_slash_command(interaction, False)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            added, existed = await self._do_sync(guild)

            if reset_backoff:
                self.config["current_interval_minutes"] = self.config["base_interval_minutes"]

            self._record_sync_result(added)
            self._save_config()

            task_running = self._sync_task is not None and not self._sync_task.done()
            if task_running:
                self._start_task()

            current_interval = int(self.config["current_interval_minutes"])
            next_sync_text = (
                f"约 {self._format_interval(current_interval)} 后"
                if self.config.get("enabled") and (task_running or self._sync_task is not None)
                else "未运行自动任务"
            )

            interval_value = self._format_interval(current_interval)
            if reset_backoff:
                interval_value += "（已重置退避）"

            embed = discord.Embed(
                title="✅ 手动同步完成",
                color=discord.Color.green(),
            )
            embed.add_field(name="📋 监控身份组", value=self._format_role_list(guild), inline=False)
            embed.add_field(name="➕ 新增", value=str(added), inline=True)
            embed.add_field(name="ℹ️ 已存在", value=str(existed), inline=True)
            embed.add_field(name="⏱️ 当前检测间隔", value=interval_value, inline=False)
            embed.add_field(name="🕒 下次自动同步", value=next_sync_text, inline=False)
            embed.set_footer(text=f"目标服务器: {guild.name}")

            await interaction.edit_original_response(content=None, embed=embed)
            log_slash_command(interaction, True)
        except RoleSyncSkipError as error:
            print(f"[role_sync] rolesync 被跳过: {error}")
            await interaction.edit_original_response(content=f"❌ {error}", embed=None)
            log_slash_command(interaction, False)
        except Exception as error:
            print(f"[role_sync] rolesync 执行失败: {error}")
            await interaction.edit_original_response(content=f"❌ 手动同步失败：{error}", embed=None)
            log_slash_command(interaction, False)

    @setup_rolesync.error
    async def on_setup_rolesync_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        """处理 setup_rolesync 命令错误。"""
        if isinstance(error, app_commands.CheckFailure):
            message = "❌ 你没有权限使用此命令。"
        else:
            print(f"[role_sync] 未处理的 setup_rolesync 错误: {error}")
            message = "❌ 执行命令时发生未知错误。"

        await self._send_ephemeral(interaction, content=message)
        log_slash_command(interaction, False)

    @rolesync.error
    async def on_rolesync_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        """处理 rolesync 命令错误。"""
        if isinstance(error, app_commands.CheckFailure):
            message = "❌ 你没有权限使用此命令。"
        else:
            print(f"[role_sync] 未处理的 rolesync 错误: {error}")
            message = "❌ 执行命令时发生未知错误。"

        await self._send_ephemeral(interaction, content=message)
        log_slash_command(interaction, False)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RoleSyncCog(bot))
