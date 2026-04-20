import asyncio
import sqlite3

import discord
from discord import app_commands
from discord.ext import commands

from cogs.utils import check_admin, log_slash_command
from paths import USERS_DB

PERMISSION_GROUPS = ("admins", "trusted_users")
USERS_DB_PATH = str(USERS_DB)


class PermissionCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def _update_bot_data(self) -> None:
        """复用主入口的数据库加载逻辑刷新内存权限数据。"""
        loader = getattr(self.bot, "load_database", None)
        if not callable(loader):
            raise RuntimeError("bot.load_database 不可用。")
        await loader(raise_on_error=True)

    def _apply_permission_changes_sync(
        self,
        *,
        group: str,
        action: str,
        target_user_ids: list[int],
        operator_id: int,
        operator_name: str,
    ) -> tuple[list[str], list[str], list[str]]:
        with sqlite3.connect(USERS_DB_PATH) as conn:
            cursor = conn.cursor()

            if action == "remove" and group == "admins":
                cursor.execute("SELECT id FROM admins")
                all_admins = [int(row[0]) for row in cursor.fetchall()]
                target_admins = [
                    uid for uid in target_user_ids if uid in all_admins and uid != operator_id
                ]
                if target_admins:
                    if len(target_admins) == 1:
                        raise PermissionError(
                            f"❌ 您不能删除其他管理员的权限。用户 `{target_admins[0]}` 是管理员。"
                        )
                    admin_list = "`, `".join(str(uid) for uid in target_admins)
                    raise PermissionError(
                        f"❌ 您不能删除其他管理员的权限。以下用户是管理员：`{admin_list}`"
                    )

            success_users: list[str] = []
            already_exists_users: list[str] = []
            not_exists_users: list[str] = []

            for target_user_id in target_user_ids:
                cursor.execute(f"SELECT id FROM {group} WHERE id = ?", (str(target_user_id),))
                user_exists = cursor.fetchone() is not None

                if action == "add":
                    if user_exists:
                        already_exists_users.append(str(target_user_id))
                        continue
                    cursor.execute(f"INSERT INTO {group} (id) VALUES (?)", (str(target_user_id),))
                    success_users.append(str(target_user_id))
                    print(f"👑 管理员 {operator_name} ({operator_id}) 将用户 {target_user_id} 添加到 {group} 组。")
                    continue

                if not user_exists:
                    not_exists_users.append(str(target_user_id))
                    continue

                cursor.execute(f"DELETE FROM {group} WHERE id = ?", (str(target_user_id),))
                success_users.append(str(target_user_id))
                print(f"👑 管理员 {operator_name} ({operator_id}) 将用户 {target_user_id} 从 {group} 组中删除。")

        return success_users, already_exists_users, not_exists_users

    @app_commands.command(name="permission", description="[仅管理员] 管理用户权限组")
    @app_commands.describe(
        user_id="要操作的Discord用户ID（多个ID用英文逗号分隔）",
        group="权限组",
        action="操作类型",
    )
    @app_commands.choices(
        group=[
            app_commands.Choice(name="admins", value="admins"),
            app_commands.Choice(name="trusted_users", value="trusted_users"),
        ]
    )
    @app_commands.choices(
        action=[
            app_commands.Choice(name="增加", value="add"),
            app_commands.Choice(name="删除", value="remove"),
        ]
    )
    @app_commands.check(check_admin)
    async def permission(self, interaction: discord.Interaction, user_id: str, group: str, action: str):
        """管理用户权限组，只有管理员可以使用。"""
        if group not in PERMISSION_GROUPS:
            await interaction.response.send_message("❌ 不支持的权限组。", ephemeral=True)
            log_slash_command(interaction, False)
            return

        user_ids_str = [uid.strip() for uid in user_id.split(",") if uid.strip()]
        if not user_ids_str:
            await interaction.response.send_message("❌ 请提供至少一个有效的用户ID。", ephemeral=True)
            log_slash_command(interaction, False)
            return

        target_user_ids: list[int] = []
        for uid_str in user_ids_str:
            try:
                target_user_ids.append(int(uid_str))
            except ValueError:
                await interaction.response.send_message(
                    f"❌ 无效的用户ID格式: `{uid_str}`。请输入有效的数字ID。",
                    ephemeral=True,
                )
                log_slash_command(interaction, False)
                return

        if action == "remove" and group == "admins" and interaction.user.id in target_user_ids:
            await interaction.response.send_message("❌ 您不能删除自己的管理员权限。", ephemeral=True)
            log_slash_command(interaction, False)
            return

        try:
            success_users, already_exists_users, not_exists_users = await asyncio.to_thread(
                self._apply_permission_changes_sync,
                group=group,
                action=action,
                target_user_ids=target_user_ids,
                operator_id=interaction.user.id,
                operator_name=interaction.user.name,
            )

            if success_users:
                await self._update_bot_data()

            embed = discord.Embed(
                title="📊 权限操作结果",
                color=discord.Color.green() if success_users else discord.Color.orange(),
            )

            if success_users:
                action_text = "增加" if action == "add" else "删除"
                embed.add_field(
                    name=f"✅ 成功{action_text} ({len(success_users)}个用户)",
                    value="`" + "`, `".join(success_users) + "`",
                    inline=False,
                )

            if already_exists_users:
                embed.add_field(
                    name=f"⚠️ 已在 `{group}` 组中 ({len(already_exists_users)}个用户)",
                    value="`" + "`, `".join(already_exists_users) + "`",
                    inline=False,
                )

            if not_exists_users:
                embed.add_field(
                    name=f"⚠️ 不在 `{group}` 组中 ({len(not_exists_users)}个用户)",
                    value="`" + "`, `".join(not_exists_users) + "`",
                    inline=False,
                )

            embed.add_field(name="操作", value="增加" if action == "add" else "删除", inline=True)
            embed.add_field(name="权限组", value=group, inline=True)
            embed.set_footer(text=f"操作由管理员 {interaction.user.name} 执行")

            await interaction.response.send_message(embed=embed, ephemeral=True)
            log_slash_command(interaction, bool(success_users) or not (already_exists_users or not_exists_users))
        except PermissionError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            log_slash_command(interaction, False)
        except sqlite3.Error as exc:
            await interaction.response.send_message(f"❌ 数据库操作失败: {exc}", ephemeral=True)
            print(f"[错误] 权限管理操作失败: {exc}")
            log_slash_command(interaction, False)
        except Exception as exc:
            await interaction.response.send_message(f"❌ 执行权限操作时发生未知错误: {exc}", ephemeral=True)
            print(f"[错误] 权限管理未知错误: {exc}")
            log_slash_command(interaction, False)

    @permission.error
    async def on_permission_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        """处理 permission 命令的特定错误。"""
        if interaction.response.is_done():
            print(f"permission 命令错误已被处理: {error}")
            return

        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message(
                "❌ 您没有权限使用此命令。只有管理员可以管理用户权限。",
                ephemeral=True,
            )
        else:
            print(f"未处理的斜杠命令错误 in PermissionCog: {error}")
            await interaction.response.send_message("❌ 执行命令时发生未知错误。", ephemeral=True)

        log_slash_command(interaction, False)


async def setup(bot: commands.Bot):
    await bot.add_cog(PermissionCog(bot))
