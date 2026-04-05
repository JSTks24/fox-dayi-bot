import asyncio
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from cogs.logger import log_slash_command


def is_admin(interaction: discord.Interaction) -> bool:
    """检查用户是否为机器人的管理员。"""
    return interaction.user.id in getattr(interaction.client, "admins", [])


class RoleSyncCog(commands.Cog):
    """自动同步 Discord 身份组成员到 trusted_users。"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.config: dict[str, Any] | None = None
        self._sync_task: asyncio.Task | None = None
        self._config_path = Path("cogs/config/role_sync_config.json")
        self._load_config()

    def _load_config(self) -> None:
        """从 JSON 文件加载配置。"""
        if not self._config_path.exists():
            self.config = None
            return

        try:
            with self._config_path.open("r", encoding="utf-8") as f:
                data = json.load(f)

            if not isinstance(data, dict):
                raise ValueError("配置文件根节点必须是 JSON object")

            guild_id = data.get("guild_id")
            if guild_id is None:
                raise ValueError("缺少 guild_id")

            role_ids = data.get("role_ids", [])
            if not isinstance(role_ids, list):
                raise ValueError("role_ids 必须是列表")

            base_interval = int(data.get("base_interval_minutes", 5))
            current_interval = int(data.get("current_interval_minutes", base_interval))
            max_interval = int(data.get("max_interval_minutes", 360))

            if base_interval < 1 or current_interval < 1 or max_interval < 1:
                raise ValueError("间隔必须为正整数")
            if base_interval > max_interval:
                raise ValueError("base_interval_minutes 不能大于 max_interval_minutes")

            data["guild_id"] = str(guild_id)
            data["role_ids"] = [str(role_id) for role_id in role_ids if str(role_id).strip()]
            data["base_interval_minutes"] = base_interval
            data["current_interval_minutes"] = min(current_interval, max_interval)
            data["max_interval_minutes"] = max_interval
            data["enabled"] = bool(data.get("enabled", False))
            data.setdefault("last_sync_time", None)
            data.setdefault("last_sync_added_count", 0)
            data.setdefault("created_by", "")
            data.setdefault("created_at", "")

            self.config = data
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            print(f"[role_sync] 加载配置失败，已忽略配置文件: {exc}")
            self.config = None

    def _save_config(self) -> None:
        """保存当前配置到 JSON 文件。"""
        if self.config is None:
            return

        try:
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            with self._config_path.open("w", encoding="utf-8") as f:
                json.dump(self.config, f, ensure_ascii=False, indent=2)
        except OSError as exc:
            print(f"[role_sync] 保存配置失败: {exc}")
            raise

    def _update_bot_memory(self) -> None:
        """从数据库重新加载 trusted_users 到 bot.trusted_users。"""
        try:
            with sqlite3.connect("users.db") as conn:
                cursor = conn.cursor()
                cursor.execute("CREATE TABLE IF NOT EXISTS trusted_users (id TEXT PRIMARY KEY)")
                cursor.execute("SELECT id FROM trusted_users")
                self.bot.trusted_users = [int(row[0]) for row in cursor.fetchall()]
        except sqlite3.Error as exc:
            print(f"[role_sync] 刷新 bot.trusted_users 失败: {exc}")

    async def _ensure_guild_members_ready(self, guild: discord.Guild) -> None:
        """尽量确保 guild.members 可用于扫描。"""
        if guild.chunked:
            return

        if not self.bot.intents.members:
            print("[role_sync] Members intent 未开启，guild.members 可能不完整。")
            return

        try:
            await guild.chunk(cache=True)
        except Exception as exc:
            print(f"[role_sync] 拉取服务器成员缓存失败: {exc}")

    async def _do_sync(self, guild: discord.Guild, *, reverse_sync: bool = False) -> tuple[int, int]:
        """执行一次同步，返回 (新增人数, 已存在人数)。"""
        del reverse_sync  # 预留未来扩展

        if not self.config:
            return 0, 0

        await self._ensure_guild_members_ready(guild)

        role_objects: list[discord.Role] = []
        invalid_role_ids: list[str] = []

        for role_id in self.config.get("role_ids", []):
            try:
                role = guild.get_role(int(role_id))
            except (TypeError, ValueError):
                role = None

            if role is None:
                invalid_role_ids.append(str(role_id))
            else:
                role_objects.append(role)

        if invalid_role_ids:
            print(f"[role_sync] 以下身份组不存在或无效，已跳过: {', '.join(invalid_role_ids)}")

        if not role_objects:
            print("[role_sync] 没有可用的身份组可供同步，本轮跳过。")
            return 0, 0

        target_user_ids = {
            str(member.id)
            for member in guild.members
            if any(role in member.roles for role in role_objects)
        }

        try:
            with sqlite3.connect("users.db") as conn:
                cursor = conn.cursor()
                cursor.execute("CREATE TABLE IF NOT EXISTS trusted_users (id TEXT PRIMARY KEY)")
                cursor.execute("SELECT id FROM trusted_users")
                existing_ids = {row[0] for row in cursor.fetchall()}

                new_ids = sorted(target_user_ids - existing_ids, key=int)
                existed_count = len(target_user_ids & existing_ids)

                if new_ids:
                    cursor.executemany(
                        "INSERT INTO trusted_users (id) VALUES (?)",
                        ((user_id,) for user_id in new_ids),
                    )

                conn.commit()
        except sqlite3.Error as exc:
            print(f"[role_sync] 同步 trusted_users 失败: {exc}")
            raise

        if new_ids:
            self._update_bot_memory()

        return len(new_ids), existed_count

    async def _sync_loop(self) -> None:
        """自动同步循环，带指数退避。"""
        await self.bot.wait_until_ready()

        while True:
            if not self.config or not self.config.get("enabled"):
                print("[role_sync] 自动同步任务结束：未启用或配置不存在。")
                return

            interval = int(self.config.get("current_interval_minutes", self.config.get("base_interval_minutes", 5)))

            try:
                await asyncio.sleep(interval * 60)
            except asyncio.CancelledError:
                raise

            if not self.config or not self.config.get("enabled"):
                print("[role_sync] 自动同步任务结束：配置已停用。")
                return

            try:
                guild_id = int(self.config["guild_id"])
            except (KeyError, TypeError, ValueError):
                print("[role_sync] 配置中的 guild_id 无效，自动同步任务已停止。")
                return

            guild = self.bot.get_guild(guild_id)
            if guild is None:
                print(f"[role_sync] 找不到服务器 {guild_id}，跳过本轮自动同步。")
                continue

            try:
                added, existed = await self._do_sync(guild)

                if added > 0:
                    self.config["current_interval_minutes"] = self.config["base_interval_minutes"]
                else:
                    new_interval = self.config["current_interval_minutes"] * 2
                    self.config["current_interval_minutes"] = min(
                        new_interval,
                        self.config["max_interval_minutes"],
                    )

                self.config["last_sync_time"] = datetime.now().isoformat(timespec="seconds")
                self.config["last_sync_added_count"] = added
                self._save_config()

                print(
                    f"[role_sync] 自动同步完成: 新增={added}, 已存在={existed}, "
                    f"下次间隔={self.config['current_interval_minutes']} 分钟"
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[role_sync] 自动同步出错: {exc}")

    def _start_task(self) -> None:
        """启动或重启自动同步任务。"""
        if not self.config or not self.config.get("enabled"):
            return

        self._stop_task()
        self._sync_task = asyncio.create_task(self._sync_loop(), name="role_sync_loop")

    def _stop_task(self) -> None:
        """停止自动同步任务。"""
        if self._sync_task and not self._sync_task.done():
            self._sync_task.cancel()
        self._sync_task = None

    def _format_interval(self, minutes: int) -> str:
        """格式化分钟展示。"""
        if minutes >= 60 and minutes % 60 == 0:
            return f"{minutes} 分钟 ({minutes // 60}h)"
        return f"{minutes} 分钟"

    def _format_role_list(self, guild: discord.Guild | None) -> str:
        """格式化配置中的身份组列表。"""
        if not self.config:
            return "无"

        role_texts: list[str] = []
        for role_id in self.config.get("role_ids", []):
            role = None
            if guild is not None:
                try:
                    role = guild.get_role(int(role_id))
                except (TypeError, ValueError):
                    role = None

            if role is not None:
                role_texts.append(role.mention)
            else:
                role_texts.append(f"`{role_id}`（已删除）")

        return ", ".join(role_texts) if role_texts else "无"

    async def _send_ephemeral(
        self,
        interaction: discord.Interaction,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
    ) -> None:
        """安全发送 ephemeral 响应。"""
        kwargs: dict[str, Any] = {"ephemeral": True}
        if content is not None:
            kwargs["content"] = content
        if embed is not None:
            kwargs["embed"] = embed

        if interaction.response.is_done():
            await interaction.followup.send(**kwargs)
        else:
            await interaction.response.send_message(**kwargs)

    async def cog_load(self) -> None:
        """Cog 加载时恢复自动同步任务。"""
        if self.config and self.config.get("enabled"):
            self._start_task()
            print(
                f"[role_sync] 已从配置恢复自动同步任务，"
                f"当前间隔={self.config['current_interval_minutes']} 分钟"
            )

    def cog_unload(self) -> None:
        """Cog 卸载时清理自动同步任务。"""
        self._stop_task()

    @app_commands.command(name="setup_rolesync", description="[仅管理员] 设置自动身份组同步任务")
    @app_commands.describe(
        role="要监控的第一个身份组",
        interval="基础检测间隔（分钟）",
        role2="可选的第二个身份组",
        max_interval="指数退避上限（分钟）",
    )
    @app_commands.check(is_admin)
    async def setup_rolesync(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        interval: app_commands.Range[int, 1, 360],
        role2: discord.Role | None = None,
        max_interval: app_commands.Range[int, 1, 1440] = 360,
    ) -> None:
        """配置自动同步身份组成员到 trusted_users。"""
        if interaction.guild is None:
            await interaction.response.send_message("❌ 该命令只能在服务器中使用。", ephemeral=True)
            log_slash_command(interaction, False)
            return

        if interval > max_interval:
            await interaction.response.send_message(
                "❌ 基础检测间隔不能大于退避上限。",
                ephemeral=True,
            )
            log_slash_command(interaction, False)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            selected_roles = [role]
            if role2 is not None and role2.id != role.id:
                selected_roles.append(role2)

            now = datetime.now().isoformat(timespec="seconds")
            self._stop_task()
            self.config = {
                "guild_id": str(interaction.guild.id),
                "role_ids": [str(selected_role.id) for selected_role in selected_roles],
                "base_interval_minutes": int(interval),
                "current_interval_minutes": int(interval),
                "max_interval_minutes": int(max_interval),
                "enabled": True,
                "last_sync_time": None,
                "last_sync_added_count": 0,
                "created_by": str(interaction.user.id),
                "created_at": now,
            }
            self._save_config()
            self._start_task()

            added, existed = await self._do_sync(interaction.guild)
            self.config["last_sync_time"] = datetime.now().isoformat(timespec="seconds")
            self.config["last_sync_added_count"] = added
            self._save_config()

            embed = discord.Embed(
                title="✅ 自动身份组同步已配置",
                color=discord.Color.green(),
            )
            embed.add_field(name="📋 监控身份组", value=self._format_role_list(interaction.guild), inline=False)
            embed.add_field(name="⏱️ 基础间隔", value=self._format_interval(int(interval)), inline=True)
            embed.add_field(name="⏱️ 退避上限", value=self._format_interval(int(max_interval)), inline=True)
            embed.add_field(name="🎯 目标", value="trusted_users", inline=True)
            embed.add_field(
                name="首次同步结果",
                value=f"➕ 新增: {added} 人\nℹ️ 已存在: {existed} 人",
                inline=False,
            )
            embed.set_footer(text=f"配置服务器: {interaction.guild.name}")

            await interaction.followup.send(embed=embed, ephemeral=True)
            log_slash_command(interaction, True)
            print(
                f"[role_sync] 管理员 {interaction.user} ({interaction.user.id}) 配置了自动同步，"
                f"guild={interaction.guild.id}, roles={self.config['role_ids']}, interval={interval}, max={max_interval}"
            )
        except Exception as exc:
            print(f"[role_sync] setup_rolesync 执行失败: {exc}")
            await self._send_ephemeral(interaction, content=f"❌ 配置自动同步失败: {exc}")
            log_slash_command(interaction, False)

    @app_commands.command(name="rolesync", description="[仅管理员] 手动执行一次身份组同步")
    @app_commands.describe(reset_backoff="是否重置指数退避到基础间隔")
    @app_commands.check(is_admin)
    async def rolesync(self, interaction: discord.Interaction, reset_backoff: bool = False) -> None:
        """手动触发一次身份组同步。"""
        if not self.config or not self.config.get("enabled"):
            await interaction.response.send_message("❌ 当前还没有已启用的自动同步配置。", ephemeral=True)
            log_slash_command(interaction, False)
            return

        if interaction.guild is None:
            await interaction.response.send_message("❌ 该命令只能在服务器中使用。", ephemeral=True)
            log_slash_command(interaction, False)
            return

        configured_guild_id = int(self.config["guild_id"])
        if interaction.guild.id != configured_guild_id:
            await interaction.response.send_message(
                f"❌ 当前自动同步绑定的服务器 ID 为 `{configured_guild_id}`，请在对应服务器中使用此命令。",
                ephemeral=True,
            )
            log_slash_command(interaction, False)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            added, existed = await self._do_sync(interaction.guild)
            self.config["last_sync_time"] = datetime.now().isoformat(timespec="seconds")
            self.config["last_sync_added_count"] = added

            if reset_backoff:
                self.config["current_interval_minutes"] = self.config["base_interval_minutes"]

            self._save_config()

            if self.config.get("enabled"):
                self._start_task()

            current_interval = int(self.config["current_interval_minutes"])
            interval_text = self._format_interval(current_interval)
            if reset_backoff:
                interval_text += "（已重置退避）"

            embed = discord.Embed(
                title="✅ 手动同步完成",
                color=discord.Color.green() if added > 0 else discord.Color.blurple(),
            )
            embed.add_field(name="同步结果", value=f"➕ 新增: {added} 人\nℹ️ 已存在: {existed} 人", inline=False)
            embed.add_field(name="⏱️ 当前检测间隔", value=interval_text, inline=False)
            embed.add_field(name="⏱️ 下次自动同步", value=f"约 {self._format_interval(current_interval)} 后", inline=False)
            embed.add_field(name="📋 监控身份组", value=self._format_role_list(interaction.guild), inline=False)

            await interaction.followup.send(embed=embed, ephemeral=True)
            log_slash_command(interaction, True)
            print(
                f"[role_sync] 管理员 {interaction.user} ({interaction.user.id}) 手动触发同步，"
                f"新增={added}, 已存在={existed}, current_interval={current_interval}"
            )
        except Exception as exc:
            print(f"[role_sync] rolesync 执行失败: {exc}")
            await self._send_ephemeral(interaction, content=f"❌ 手动同步失败: {exc}")
            log_slash_command(interaction, False)

    @setup_rolesync.error
    async def on_setup_rolesync_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        """处理 setup_rolesync 的命令错误。"""
        if isinstance(error, app_commands.CheckFailure):
            message = "❌ 你没有权限使用此命令。"
        else:
            print(f"[role_sync] 未处理的 setup_rolesync 错误: {error}")
            message = "❌ 配置自动同步时发生未知错误。"

        await self._send_ephemeral(interaction, content=message)
        log_slash_command(interaction, False)

    @rolesync.error
    async def on_rolesync_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        """处理 rolesync 的命令错误。"""
        if isinstance(error, app_commands.CheckFailure):
            message = "❌ 你没有权限使用此命令。"
        else:
            print(f"[role_sync] 未处理的 rolesync 错误: {error}")
            message = "❌ 执行手动同步时发生未知错误。"

        await self._send_ephemeral(interaction, content=message)
        log_slash_command(interaction, False)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RoleSyncCog(bot))
