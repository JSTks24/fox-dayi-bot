import os

import discord
from discord import app_commands
from discord.ext import commands

from cogs.utils import log_slash_command


class LeaveUnexpectedGuildsView(discord.ui.View):
    """退出异常服务器的二次确认视图"""

    def __init__(self, cog: "GuildGuardCog", requester_id: int):
        super().__init__(timeout=120)
        self.cog = cog
        self.requester_id = requester_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """仅允许命令发起人操作按钮"""
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("❌ 只有命令发起人可以操作此按钮。", ephemeral=True)
            return False
        return True

    def _disable_all_items(self):
        for item in self.children:
            item.disabled = True

    async def on_timeout(self):
        self._disable_all_items()
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception as e:
                print(f"[GuildGuard] 更新超时视图失败: {e}")

    @discord.ui.button(label="确认退出", style=discord.ButtonStyle.danger, emoji="⚠️")
    async def confirm_leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.cog.is_bot_admin_user(interaction.user.id):
            self._disable_all_items()
            await interaction.response.edit_message(content="❌ 你已不再是 Bot 管理员，操作已取消。", embed=None, view=self)
            self.stop()
            return

        await interaction.response.defer(ephemeral=True)

        self._disable_all_items()
        try:
            await interaction.edit_original_response(view=self)
        except Exception:
            pass

        result = await self.cog.execute_leave_unexpected_guilds(
            current_guild_id=interaction.guild.id if interaction.guild else None
        )

        if result.get("error"):
            await interaction.edit_original_response(
                content=f"❌ {result['error']}",
                embed=None,
                view=None,
            )
            self.stop()
            return

        target_count = int(result.get("target_count", 0))
        success_count = int(result.get("success_count", 0))
        failed_count = int(result.get("failed_count", 0))
        before_count = int(result.get("before_count", 0))
        after_count = int(result.get("after_count", 0))

        if target_count == 0:
            await interaction.edit_original_response(
                content="✅ 当前无需退出任何服务器。",
                embed=None,
                view=None,
            )
            self.stop()
            return

        lines = [
            "✅ 已完成退出操作（简洁版）",
            f"- 尝试退出: {target_count} 个服务器",
            f"- 成功退出: {success_count} 个",
            f"- 退出失败: {failed_count} 个",
            f"- 执行前连接数: {before_count}",
            f"- 执行后连接数: {after_count}",
        ]

        failed_samples: list[str] = result.get("failed_samples", [])
        if failed_samples:
            lines.append(f"- 失败示例: {', '.join(failed_samples)}")

        await interaction.edit_original_response(content="\n".join(lines), embed=None, view=None)
        self.stop()

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self._disable_all_items()
        await interaction.response.edit_message(content="已取消操作，不会退出任何服务器。", embed=None, view=self)
        self.stop()


class GuildGuardCog(commands.Cog):
    """服务器白名单守卫：用于退出不在白名单中的服务器"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def is_bot_admin_user(self, user_id: int) -> bool:
        admins = getattr(self.bot, "admins", [])
        return user_id in admins

    def _parse_should_guild_ids(self) -> tuple[set[int], list[str]]:
        """从环境变量解析应在的服务器ID列表"""
        raw = os.getenv("BOT_SHOULD_IN_GUILD_IDS", "").strip()
        if not raw:
            return set(), []

        should_ids: set[int] = set()
        invalid_items: list[str] = []

        for part in raw.split(","):
            token = part.strip()
            if not token:
                continue
            try:
                should_ids.add(int(token))
            except ValueError:
                invalid_items.append(token)

        return should_ids, invalid_items

    def _get_unexpected_guilds(self, should_ids: set[int]) -> list[discord.Guild]:
        return [guild for guild in self.bot.guilds if guild.id not in should_ids]

    def _format_guild_preview(self, guilds: list[discord.Guild], max_items: int = 20, max_chars: int = 900) -> str:
        if not guilds:
            return "（无）"

        lines: list[str] = []
        for guild in guilds:
            line = f"• {guild.name} (`{guild.id}`)"
            if len(lines) >= max_items:
                break
            if len("\n".join(lines + [line])) > max_chars:
                break
            lines.append(line)

        remaining = len(guilds) - len(lines)
        if remaining > 0:
            lines.append(f"... 还有 {remaining} 个服务器未显示")

        return "\n".join(lines)

    async def execute_leave_unexpected_guilds(self, current_guild_id: int | None = None) -> dict:
        should_ids, invalid_items = self._parse_should_guild_ids()
        if not should_ids:
            return {
                "error": "`BOT_SHOULD_IN_GUILD_IDS` 未配置或解析为空，已中止操作以防误退。"
            }

        unexpected_guilds = self._get_unexpected_guilds(should_ids)
        before_count = len(self.bot.guilds)

        if current_guild_id is not None:
            unexpected_guilds.sort(key=lambda g: g.id == current_guild_id)

        success_count = 0
        failed_count = 0
        failed_samples: list[str] = []

        for guild in unexpected_guilds:
            try:
                await guild.leave()
                success_count += 1
            except Exception as e:
                failed_count += 1
                if len(failed_samples) < 3:
                    failed_samples.append(f"{guild.name}({guild.id})")
                print(f"[GuildGuard] 退出服务器失败: {guild.name} ({guild.id}) -> {e}")

        result = {
            "target_count": len(unexpected_guilds),
            "success_count": success_count,
            "failed_count": failed_count,
            "before_count": before_count,
            "after_count": len(self.bot.guilds),
            "failed_samples": failed_samples,
        }

        if invalid_items:
            result["invalid_items"] = invalid_items

        return result

    @app_commands.command(name="答疑bot-退出异常服务器", description="[仅Bot管理员] 退出不在白名单中的服务器（带二次确认）")
    async def leave_unexpected_guilds(self, interaction: discord.Interaction):
        if not self.is_bot_admin_user(interaction.user.id):
            await interaction.response.send_message("❌ 权限不足：仅 Bot 管理员可用。", ephemeral=True)
            log_slash_command(interaction, False)
            return

        should_ids, invalid_items = self._parse_should_guild_ids()
        if not should_ids:
            await interaction.response.send_message(
                "❌ `BOT_SHOULD_IN_GUILD_IDS` 未配置或解析为空，已中止操作以防误退。",
                ephemeral=True,
            )
            log_slash_command(interaction, False)
            return

        unexpected_guilds = self._get_unexpected_guilds(should_ids)
        if not unexpected_guilds:
            await interaction.response.send_message(
                "✅ 当前 Bot 仅在白名单服务器中，无需退出。",
                ephemeral=True,
            )
            log_slash_command(interaction, True)
            return

        embed = discord.Embed(
            title="⚠️ 二次确认：退出异常服务器",
            description="以下服务器不在 `BOT_SHOULD_IN_GUILD_IDS` 白名单中。确认后将立即退出。",
            color=discord.Color.orange(),
        )
        embed.add_field(name="当前连接服务器数", value=str(len(self.bot.guilds)), inline=True)
        embed.add_field(name="白名单服务器数", value=str(len(should_ids)), inline=True)
        embed.add_field(name="待退出服务器数", value=str(len(unexpected_guilds)), inline=True)
        embed.add_field(
            name="待退出服务器预览（名称 + ID）",
            value=self._format_guild_preview(unexpected_guilds),
            inline=False,
        )

        if invalid_items:
            embed.add_field(
                name="⚠️ 无法解析的白名单项",
                value=", ".join(invalid_items[:10]),
                inline=False,
            )

        view = LeaveUnexpectedGuildsView(self, interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()

        log_slash_command(interaction, True)


async def setup(bot: commands.Bot):
    await bot.add_cog(GuildGuardCog(bot))
