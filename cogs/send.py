import discord
from discord.ext import commands
from discord import app_commands
import re
from cogs.utils import check_admin, log_slash_command, safe_defer as _safe_defer

class SendCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def parse_message_link(self, message_link: str) -> tuple:
        """
        解析Discord消息链接，返回(guild_id, channel_id, message_id)
        支持格式: https://discord.com/channels/guild_id/channel_id/message_id
        """
        pattern = r'https://discord\.com/channels/(\d+)/(\d+)/(\d+)'
        match = re.match(pattern, message_link)
        if match:
            return int(match.group(1)), int(match.group(2)), int(match.group(3))
        return None, None, None

    async def resolve_target_channel(self, guild: discord.Guild, channel_id: int):
        """Resolve channels with an API fallback so thread IDs also work."""
        target_channel = guild.get_channel(channel_id)
        if target_channel:
            return target_channel

        try:
            fetched_channel = await self.bot.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

        channel_guild = getattr(fetched_channel, "guild", None)
        if channel_guild is not None and channel_guild.id != guild.id:
            return None
        return fetched_channel

    @staticmethod
    def _normalize_send_content(content: str | None) -> str | None:
        if content is None:
            return None
        normalized = content.strip()
        return normalized or None

    @staticmethod
    def _build_embed(
        embed_title: str | None,
        embed_description: str | None,
        embed_color: str | None,
    ) -> discord.Embed | None:
        title = embed_title.strip() if embed_title else None
        description = embed_description.strip() if embed_description else None
        color_text = embed_color.strip() if embed_color else None

        if not title and not description and not color_text:
            return None
        if not title and not description:
            raise ValueError("Embed 至少需要标题或描述。")

        color = discord.Color.blurple()
        if color_text:
            normalized = color_text.lstrip("#")
            if len(normalized) != 6 or any(ch not in "0123456789abcdefABCDEF" for ch in normalized):
                raise ValueError("Embed 颜色必须是 6 位十六进制，例如 #5865F2。")
            color = discord.Color(int(normalized, 16))

        return discord.Embed(title=title, description=description, color=color)

    @app_commands.command(name='send', description='[仅管理员] 发送消息或回复指定消息')
    @app_commands.describe(
        content='（可选）要发送的文字内容',
        message_link='（可选）要回复的消息链接',
        embed_title='（可选）单个 Embed 标题',
        embed_description='（可选）单个 Embed 描述',
        embed_color='（可选）Embed 颜色，6 位十六进制，例如 #5865F2',
    )
    async def send_message(
        self,
        interaction: discord.Interaction,
        content: str | None = None,
        message_link: str | None = None,
        embed_title: str | None = None,
        embed_description: str | None = None,
        embed_color: str | None = None,
    ):
        """
        发送消息或回复指定消息的斜杠指令
        仅限管理员使用
        """
        # 先延迟响应，避免超时与“使用了 /send”横幅
        await _safe_defer(interaction)

        # 检查管理员权限
        if not check_admin(interaction):
            await interaction.followup.send('❌ 此命令仅限管理员使用。', ephemeral=True)
            log_slash_command(interaction, False)
            return

        try:
            normalized_content = self._normalize_send_content(content)
            embed = self._build_embed(embed_title, embed_description, embed_color)
            if normalized_content is None and embed is None:
                await interaction.followup.send('❌ 请至少提供文字内容，或填写一个 Embed。', ephemeral=True)
                log_slash_command(interaction, False)
                return

            # 如果没有提供消息链接，直接在当前频道发送消息（普通消息，无斜杠横幅）
            if not message_link:
                # 检查发送权限（避免因缺少权限而失败）
                try:
                    me = interaction.guild.me if interaction.guild else None
                    if hasattr(interaction.channel, "permissions_for") and me and not interaction.channel.permissions_for(me).send_messages:
                        await interaction.followup.send('❌ 机器人在当前频道没有发送消息的权限。', ephemeral=True)
                        log_slash_command(interaction, False)
                        return
                except Exception:
                    # 权限检查异常不应阻止消息发送，继续尝试发送
                    pass

                await interaction.channel.send(content=normalized_content, embed=embed)
                await interaction.followup.send('✅ 已在当前频道发送消息。', ephemeral=True)
                log_slash_command(interaction, True)
                print(f"👑 管理员 {interaction.user.name} ({interaction.user.id}) 在频道 {interaction.channel.name} 发送了消息")
                return

            # 解析消息链接
            guild_id, channel_id, message_id = self.parse_message_link(message_link.strip())
            
            if not all([guild_id, channel_id, message_id]):
                await interaction.followup.send(
                    '❌ 无效的消息链接格式。请提供有效的Discord消息链接。\n'
                    '格式示例: `https://discord.com/channels/服务器ID/频道ID/消息ID`',
                    ephemeral=True
                )
                log_slash_command(interaction, False)
                return

            # 获取目标服务器
            target_guild = self.bot.get_guild(guild_id)
            if not target_guild:
                await interaction.followup.send('❌ 无法找到指定的服务器。', ephemeral=True)
                log_slash_command(interaction, False)
                return

            # 获取目标频道
            target_channel = await self.resolve_target_channel(target_guild, channel_id)
            if not target_channel:
                await interaction.followup.send('❌ 无法找到指定的频道。', ephemeral=True)
                log_slash_command(interaction, False)
                return

            # 检查机器人是否有发送消息的权限
            if not target_channel.permissions_for(target_guild.me).send_messages:
                await interaction.followup.send('❌ 机器人在目标频道没有发送消息的权限。', ephemeral=True)
                log_slash_command(interaction, False)
                return

            # 获取目标消息
            try:
                target_message = await target_channel.fetch_message(message_id)
            except discord.NotFound:
                await interaction.followup.send('❌ 无法找到指定的消息。', ephemeral=True)
                log_slash_command(interaction, False)
                return
            except discord.Forbidden:
                await interaction.followup.send('❌ 机器人没有权限访问该消息。', ephemeral=True)
                log_slash_command(interaction, False)
                return

            # 回复目标消息
            await target_message.reply(content=normalized_content, embed=embed)
            
            # 发送成功确认（仅管理员可见）
            summary_parts: list[str] = []
            if normalized_content:
                summary_parts.append(f'**回复内容**: {normalized_content[:100]}{"..." if len(normalized_content) > 100 else ""}')
            if embed:
                embed_summary = embed.title or embed.description or "单 Embed"
                summary_parts.append(f'**Embed**: {embed_summary[:100]}{"..." if len(embed_summary) > 100 else ""}')
            await interaction.followup.send(
                f'✅ 已成功回复消息！\n'
                f'**目标服务器**: {target_guild.name}\n'
                f'**目标频道**: {target_channel.mention}\n'
                + '\n'.join(summary_parts),
                ephemeral=True
            )
            log_slash_command(interaction, True)
            print(f"👑 管理员 {interaction.user.name} 回复了消息 {message_link}")

        except ValueError as e:
            await interaction.followup.send(f'❌ {e}', ephemeral=True)
            log_slash_command(interaction, False)
        except discord.HTTPException as e:
            await interaction.followup.send(f'❌ 发送消息时发生错误: {e}', ephemeral=True)
            log_slash_command(interaction, False)
        except Exception as e:
            print(f"[错误] /send 命令执行时发生错误: {e}")
            await interaction.followup.send('❌ 执行命令时发生未知错误。', ephemeral=True)
            log_slash_command(interaction, False)

    @app_commands.command(name='撤回', description='[仅管理员] 删除机器人消息')
    @app_commands.describe(
        message_link='（可选）要删除的消息链接，留空则删除机器人在当前频道的最后一条消息'
    )
    async def delete_message(self, interaction: discord.Interaction, message_link: str = None):
        """
        删除机器人消息的斜杠指令
        仅限管理员使用
        """
        # 先延迟响应，避免超时
        await _safe_defer(interaction)
        
        # 检查管理员权限
        if not check_admin(interaction):
            await interaction.followup.send('❌ 此命令仅限管理员使用。', ephemeral=True)
            log_slash_command(interaction, False)
            return

        try:
            # 如果没有提供消息链接，删除机器人在当前频道的最后一条消息
            if not message_link:
                # 获取当前频道
                channel = interaction.channel
                
                # 搜索机器人在当前频道的最后一条消息
                bot_message = None
                async for message in channel.history(limit=100):
                    if message.author.id == self.bot.user.id:
                        bot_message = message
                        break
                
                if not bot_message:
                    await interaction.followup.send(
                        '❌ 在当前频道未找到机器人的消息（搜索了最近100条消息）。',
                        ephemeral=True
                    )
                    log_slash_command(interaction, False)
                    return
                
                # 删除找到的消息
                try:
                    await bot_message.delete()
                    await interaction.followup.send(
                        f'✅ 已成功删除机器人在 {channel.mention} 的最后一条消息。',
                        ephemeral=True
                    )
                    log_slash_command(interaction, True)
                    print(f"👑 管理员 {interaction.user.name} ({interaction.user.id}) 删除了机器人在频道 {channel.name} 的最后一条消息")
                except discord.Forbidden:
                    await interaction.followup.send('❌ 机器人没有删除该消息的权限。', ephemeral=True)
                    log_slash_command(interaction, False)
                except discord.NotFound:
                    await interaction.followup.send('❌ 消息已经被删除或不存在。', ephemeral=True)
                    log_slash_command(interaction, False)
                
                return

            # 解析消息链接
            guild_id, channel_id, message_id = self.parse_message_link(message_link.strip())
            
            if not all([guild_id, channel_id, message_id]):
                await interaction.followup.send(
                    '❌ 无效的消息链接格式。请提供有效的Discord消息链接。\n'
                    '格式示例: `https://discord.com/channels/服务器ID/频道ID/消息ID`',
                    ephemeral=True
                )
                log_slash_command(interaction, False)
                return

            # 获取目标服务器
            target_guild = self.bot.get_guild(guild_id)
            if not target_guild:
                await interaction.followup.send('❌ 无法找到指定的服务器。', ephemeral=True)
                log_slash_command(interaction, False)
                return

            # 获取目标频道
            target_channel = await self.resolve_target_channel(target_guild, channel_id)
            if not target_channel:
                await interaction.followup.send('❌ 无法找到指定的频道。', ephemeral=True)
                log_slash_command(interaction, False)
                return

            # 获取目标消息
            try:
                target_message = await target_channel.fetch_message(message_id)
            except discord.NotFound:
                await interaction.followup.send('❌ 无法找到指定的消息。', ephemeral=True)
                log_slash_command(interaction, False)
                return
            except discord.Forbidden:
                await interaction.followup.send('❌ 机器人没有权限访问该消息。', ephemeral=True)
                log_slash_command(interaction, False)
                return

            # 检查消息是否是机器人发送的
            if target_message.author.id != self.bot.user.id:
                await interaction.followup.send(
                    '❌ 只能删除机器人自己发送的消息。',
                    ephemeral=True
                )
                log_slash_command(interaction, False)
                return

            # 删除目标消息
            try:
                await target_message.delete()
                await interaction.followup.send(
                    f'✅ 已成功删除消息！\n'
                    f'**所在服务器**: {target_guild.name}\n'
                    f'**所在频道**: {target_channel.mention}',
                    ephemeral=True
                )
                log_slash_command(interaction, True)
                print(f"👑 管理员 {interaction.user.name} 删除了消息 {message_link}")
            except discord.Forbidden:
                await interaction.followup.send('❌ 机器人没有删除该消息的权限。', ephemeral=True)
                log_slash_command(interaction, False)
            except discord.NotFound:
                await interaction.followup.send('❌ 消息已经被删除或不存在。', ephemeral=True)
                log_slash_command(interaction, False)

        except discord.HTTPException as e:
            await interaction.followup.send(f'❌ 删除消息时发生错误: {e}', ephemeral=True)
            log_slash_command(interaction, False)
        except Exception as e:
            print(f"[错误] /撤回 命令执行时发生错误: {e}")
            await interaction.followup.send('❌ 执行命令时发生未知错误。', ephemeral=True)
            log_slash_command(interaction, False)

async def setup(bot: commands.Bot):
    await bot.add_cog(SendCog(bot))
