import discord
from discord import app_commands
from discord.ext import commands

from cogs.utils import check_admin, log_slash_command


class ReloadCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _reload_bot_data(self) -> None:
        """复用主入口的数据库加载逻辑，并在失败时保留旧数据。"""
        loader = getattr(self.bot, "load_database", None)
        if not callable(loader):
            raise RuntimeError("bot.load_database 不可用。")

        old_admins = list(getattr(self.bot, "admins", []))
        old_trusted_users = list(getattr(self.bot, "trusted_users", []))

        try:
            loader(raise_on_error=True)
        except Exception:
            self.bot.admins = old_admins
            self.bot.trusted_users = old_trusted_users
            raise

    @app_commands.command(name="reload-db", description="[仅管理员] 重新加载数据库文件 users.db")
    @app_commands.check(check_admin)
    async def reload_db(self, interaction: discord.Interaction):
        """重新加载 SQLite 数据库文件。"""
        try:
            self._reload_bot_data()
            await interaction.response.send_message("✅ 数据库 `users.db` 已成功重新加载。", ephemeral=True)
            log_slash_command(interaction, True)
            print(f"👑 数据库已由管理员 {interaction.user.name} ({interaction.user.id}) 手动重新加载。")
            print(f"👑 新的管理员ID: {self.bot.admins}")
            print(f"🤝 新的受信任用户ID: {self.bot.trusted_users}")
        except Exception as exc:
            await interaction.response.send_message(f"❌ 重新加载数据库时发生错误: {exc}", ephemeral=True)
            print(f"[错误] 手动重新加载数据库失败: {exc}")
            log_slash_command(interaction, False)

    @reload_db.error
    async def on_reload_db_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        """处理 reload_db 命令的特定错误。"""
        if interaction.response.is_done():
            log_slash_command(interaction, False)
            return

        if isinstance(error, app_commands.CheckFailure):
            await interaction.response.send_message("❌ 你没有权限使用此命令。", ephemeral=True)
        else:
            print(f"未处理的斜杠命令错误 in ReloadCog: {error}")
            await interaction.response.send_message("❌ 执行命令时发生未知错误。", ephemeral=True)

        log_slash_command(interaction, False)


async def setup(bot: commands.Bot):
    await bot.add_cog(ReloadCog(bot))
