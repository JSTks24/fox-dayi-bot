import os
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

from cogs.utils import safe_defer
from paths import (
    API_TABLE_BAD_FILE,
    API_TABLE_GOOD_FILE,
    API_TABLE_HISTORY_FILE,
)

from .url_matcher import URLMatcher

API_TABLE_GOOD_PATH = os.fspath(API_TABLE_GOOD_FILE)
API_TABLE_BAD_PATH = os.fspath(API_TABLE_BAD_FILE)
API_TABLE_HISTORY_PATH = os.fspath(API_TABLE_HISTORY_FILE)


class URLTableCog(commands.Cog):
    """速查表增删查命令。"""

    def __init__(self, bot: commands.Bot, matcher: URLMatcher):
        self.bot = bot
        self.matcher = matcher

    def _check_permission(self, user_id: int) -> bool:
        return user_id in self.bot.admins or user_id in self.bot.trusted_users

    def _log_operation_to_history(
        self,
        user: discord.User | discord.Member,
        operation_type: str,
        url: str,
        name: str | None = None,
        description: str | None = None,
        success: bool = True
    ):
        try:
            history_file = API_TABLE_HISTORY_PATH
            os.makedirs(os.path.dirname(history_file), exist_ok=True)

            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            result_status = "成功" if success else "失败"

            log_entry = (
                "=" * 80 + "\n"
                f"时间: {timestamp}\n"
                f"操作者: {user.name} ({user.id})\n"
                f"操作类型: {operation_type}\n"
                f"URL: {url}\n"
            )

            if name:
                log_entry += f"名称: {name}\n"
            if description:
                log_entry += f"描述: {description}\n"

            log_entry += f"结果: {result_status}\n"
            log_entry += "=" * 80 + "\n\n"

            with open(history_file, 'a', encoding='utf-8') as f:
                f.write(log_entry)

            print(f"✅ 已记录操作历史: {operation_type} - {url}")

        except Exception as e:
            print(f"⚠️ 记录操作历史失败: {e}")

    @app_commands.command(name='url速查表-编辑', description='编辑URL速查表（添加/删除URL）')
    @app_commands.describe(
        url='要操作的URL',
        操作='选择操作类型',
        名称='API站点名称（添加时可选）',
        描述='API站点描述（添加时可选）'
    )
    @app_commands.choices(操作=[
        app_commands.Choice(name='添加到好API', value='add_good'),
        app_commands.Choice(name='添加到坏API', value='add_bad'),
        app_commands.Choice(name='删除', value='delete')
    ])
    async def url_table_edit(
        self,
        interaction: discord.Interaction,
        url: str,
        操作: app_commands.Choice[str],
        名称: str | None = None,
        描述: str | None = None
    ):
        await safe_defer(interaction)

        user_id = interaction.user.id

        if not self._check_permission(user_id):
            await interaction.followup.send('❌ 没权。此命令仅限答疑组使用。', ephemeral=True)
            return

        domain, path = self.matcher.normalize(url)
        normalized_url = domain + path if path else domain

        operation = 操作.value

        try:
            if operation == 'delete':
                good_data = self.matcher.load_json(API_TABLE_GOOD_PATH)
                bad_data = self.matcher.load_json(API_TABLE_BAD_PATH)

                deleted_from = []

                if 'good' in good_data and normalized_url in good_data['good']:
                    del good_data['good'][normalized_url]
                    self.matcher.save_json(API_TABLE_GOOD_PATH, good_data)
                    deleted_from.append('好API列表')

                if 'bad' in bad_data and normalized_url in bad_data['bad']:
                    del bad_data['bad'][normalized_url]
                    self.matcher.save_json(API_TABLE_BAD_PATH, bad_data)
                    deleted_from.append('坏API列表')

                if deleted_from:
                    self._log_operation_to_history(
                        user=interaction.user,
                        operation_type="删除",
                        url=normalized_url,
                        success=True
                    )
                    await interaction.followup.send(
                        f"✅ 已从 {' 和 '.join(deleted_from)} 中删除URL:\n`{normalized_url}`",
                        ephemeral=True
                    )
                else:
                    self._log_operation_to_history(
                        user=interaction.user,
                        operation_type="删除",
                        url=normalized_url,
                        success=False
                    )
                    await interaction.followup.send(
                        f"⚠️ 未找到URL: `{normalized_url}`",
                        ephemeral=True
                    )

            elif operation == 'add_good':
                good_data = self.matcher.load_json(API_TABLE_GOOD_PATH)

                if 'good' not in good_data:
                    good_data['good'] = {}

                value = [名称 or "", 描述 or ""]
                good_data['good'][normalized_url] = value

                if self.matcher.save_json(API_TABLE_GOOD_PATH, good_data):
                    self._log_operation_to_history(
                        user=interaction.user,
                        operation_type="添加到好API",
                        url=normalized_url,
                        name=名称,
                        description=描述,
                        success=True
                    )
                    await interaction.followup.send(
                        f"✅ 已添加到好API列表:\n"
                        f"URL: `{normalized_url}`\n"
                        f"名称: {名称 or '(未提供)'}\n"
                        f"描述: {描述 or '(未提供)'}",
                        ephemeral=True
                    )
                else:
                    self._log_operation_to_history(
                        user=interaction.user,
                        operation_type="添加到好API",
                        url=normalized_url,
                        name=名称,
                        description=描述,
                        success=False
                    )
                    await interaction.followup.send("❌ 保存失败，请检查文件权限。", ephemeral=True)

            elif operation == 'add_bad':
                bad_data = self.matcher.load_json(API_TABLE_BAD_PATH)

                if 'bad' not in bad_data:
                    bad_data['bad'] = {}

                value = [名称 or "", 描述 or ""]
                bad_data['bad'][normalized_url] = value

                if self.matcher.save_json(API_TABLE_BAD_PATH, bad_data):
                    self._log_operation_to_history(
                        user=interaction.user,
                        operation_type="添加到坏API",
                        url=normalized_url,
                        name=名称,
                        description=描述,
                        success=True
                    )
                    await interaction.followup.send(
                        f"✅ 已添加到坏API列表:\n"
                        f"URL: `{normalized_url}`\n"
                        f"名称: {名称 or '(未提供)'}\n"
                        f"描述: {描述 or '(未提供)'}",
                        ephemeral=True
                    )
                else:
                    self._log_operation_to_history(
                        user=interaction.user,
                        operation_type="添加到坏API",
                        url=normalized_url,
                        name=名称,
                        description=描述,
                        success=False
                    )
                    await interaction.followup.send("❌ 保存失败，请检查文件权限。", ephemeral=True)

        except Exception as e:
            print(f"❌ 编辑URL速查表时出错: {e}")
            import traceback
            traceback.print_exc()
            await interaction.followup.send(f"❌ 操作失败: {str(e)}", ephemeral=True)

    @app_commands.command(name='url速查表-查询', description='查询URL在速查表中的状态')
    @app_commands.describe(url='要查询的URL')
    async def url_table_query(self, interaction: discord.Interaction, url: str):
        await safe_defer(interaction)

        user_id = interaction.user.id

        if not self._check_permission(user_id):
            await interaction.followup.send('❌ 没权。此命令仅限答疑组使用。', ephemeral=True)
            return

        try:
            status, entry = self.matcher.match(url)
            domain, path = self.matcher.normalize(url)
            normalized_url = domain + path if path else domain

            if status == 'good' and entry is not None:
                result = (
                    f"**状态:** ✅ 合规\n"
                    f"**URL:** `{normalized_url}`\n"
                    f"**命中规则:** `{entry['matched_key']}`\n"
                    f"**名称:** {entry['name'] or '(无)'}\n"
                    f"**描述:** {entry['description'] or '(无)'}"
                )
            elif status == 'bad' and entry is not None:
                result = (
                    f"**状态:** 🚫 违规\n"
                    f"**URL:** `{normalized_url}`\n"
                    f"**命中规则:** `{entry['matched_key']}`\n"
                    f"**名称:** {entry['name'] or '(无)'}\n"
                    f"**描述:** {entry['description'] or '(无)'}"
                )
            else:
                result = (
                    f"**状态:** ❓ 未知\n"
                    f"**URL:** `{normalized_url}`\n"
                    f"该URL不在速查表中。"
                )

            await interaction.followup.send(result, ephemeral=True)

        except Exception as e:
            print(f"❌ 查询URL时出错: {e}")
            import traceback
            traceback.print_exc()
            await interaction.followup.send(f"❌ 查询失败: {str(e)}", ephemeral=True)
