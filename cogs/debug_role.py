import discord
from discord import app_commands
from discord.ext import commands
from typing import Literal
from cogs.utils import check_admin, log_slash_command, safe_defer as _safe_defer


class DebugRole(commands.Cog):
    """调试身份组管理命令"""
    
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name='调试身份组', description='[仅管理员] 为指定用户添加或删除身份组')
    @app_commands.guild_only()
    @app_commands.describe(
        user='要操作的Discord用户',
        role='要添加或删除的身份组',
        action='选择是增加还是删除身份组'
    )
    async def debug_role(
        self, 
        interaction: discord.Interaction,
        user: discord.Member,  # 使用Member而不是User，这样才能操作服务器身份组
        role: discord.Role,
        action: Literal['增加', '删除']
    ):
        """为指定用户添加或删除身份组"""
        
        # 立即defer响应
        await _safe_defer(interaction)
        
        try:
            # 检查是否为管理员
            if not check_admin(interaction):
                await interaction.followup.send(
                    '❌ 您没有权限使用此命令。只有管理员可以使用此功能。',
                    ephemeral=True
                )
                log_slash_command(interaction, False)
                return
            
            # 检查机器人是否有足够的权限
            if not interaction.guild.me.guild_permissions.manage_roles:
                await interaction.followup.send(
                    '❌ 机器人没有管理身份组的权限。请确保机器人有 "管理身份组" 权限。',
                    ephemeral=True
                )
                log_slash_command(interaction, False)
                return
            
            # 检查身份组等级是否高于机器人
            if role.position >= interaction.guild.me.top_role.position:
                await interaction.followup.send(
                    f'❌ 无法操作身份组 {role.mention}，因为它的等级高于或等于机器人的最高身份组。',
                    ephemeral=True
                )
                log_slash_command(interaction, False)
                return
            
            # 检查身份组是否为管理身份组（@everyone除外）
            if role.is_default():
                await interaction.followup.send(
                    '❌ 无法操作 @everyone 身份组。',
                    ephemeral=True
                )
                log_slash_command(interaction, False)
                return
            
            # 检查身份组是否为集成身份组（如Nitro Booster等）
            if role.is_integration():
                await interaction.followup.send(
                    f'❌ 无法操作集成身份组 {role.mention}（如Nitro Booster等）。',
                    ephemeral=True
                )
                log_slash_command(interaction, False)
                return
            
            # 检查身份组是否为机器人身份组
            if role.is_bot_managed():
                await interaction.followup.send(
                    f'❌ 无法操作机器人管理的身份组 {role.mention}。',
                    ephemeral=True
                )
                log_slash_command(interaction, False)
                return
            
            # 执行操作
            if action == '增加':
                # 检查用户是否已经拥有该身份组
                if role in user.roles:
                    await interaction.followup.send(
                        f'⚠️ 用户 {user.mention} 已经拥有身份组 {role.mention}。',
                        ephemeral=True
                    )
                    log_slash_command(interaction, True)
                    return
                
                # 添加身份组
                try:
                    await user.add_roles(role, reason=f'由管理员 {interaction.user} 通过调试命令添加')
                    
                    # 创建成功嵌入消息
                    embed = discord.Embed(
                        title='✅ 身份组添加成功',
                        description=f'已成功为用户 {user.mention} 添加身份组。',
                        color=discord.Color.green()
                    )
                    embed.add_field(name='目标用户', value=f'{user.mention}\n({user.id})', inline=True)
                    embed.add_field(name='添加的身份组', value=f'{role.mention}\n({role.id})', inline=True)
                    embed.add_field(name='操作者', value=f'{interaction.user.mention}', inline=True)
                    embed.set_footer(text='操作时间')
                    embed.timestamp = discord.utils.utcnow()
                    
                    await interaction.followup.send(embed=embed, ephemeral=True)
                    log_slash_command(interaction, True)
                    print(f'👑 管理员 {interaction.user} ({interaction.user.id}) 为用户 {user} ({user.id}) 添加了身份组 {role.name} ({role.id})')
                    
                except discord.Forbidden:
                    await interaction.followup.send(
                        '❌ 无法为用户添加身份组。可能是权限不足或其他限制。',
                        ephemeral=True
                    )
                    log_slash_command(interaction, False)
                except discord.HTTPException as e:
                    await interaction.followup.send(
                        f'❌ 添加身份组时发生网络错误：{str(e)}',
                        ephemeral=True
                    )
                    log_slash_command(interaction, False)
                    
            else:  # action == '删除'
                # 检查用户是否拥有该身份组
                if role not in user.roles:
                    await interaction.followup.send(
                        f'⚠️ 用户 {user.mention} 并没有身份组 {role.mention}。',
                        ephemeral=True
                    )
                    log_slash_command(interaction, True)
                    return
                
                # 删除身份组
                try:
                    await user.remove_roles(role, reason=f'由管理员 {interaction.user} 通过调试命令删除')
                    
                    # 创建成功嵌入消息
                    embed = discord.Embed(
                        title='✅ 身份组删除成功',
                        description=f'已成功从用户 {user.mention} 删除身份组。',
                        color=discord.Color.orange()
                    )
                    embed.add_field(name='目标用户', value=f'{user.mention}\n({user.id})', inline=True)
                    embed.add_field(name='删除的身份组', value=f'{role.mention}\n({role.id})', inline=True)
                    embed.add_field(name='操作者', value=f'{interaction.user.mention}', inline=True)
                    embed.set_footer(text='操作时间')
                    embed.timestamp = discord.utils.utcnow()
                    
                    await interaction.followup.send(embed=embed, ephemeral=True)
                    log_slash_command(interaction, True)
                    print(f'👑 管理员 {interaction.user} ({interaction.user.id}) 从用户 {user} ({user.id}) 删除了身份组 {role.name} ({role.id})')
                    
                except discord.Forbidden:
                    await interaction.followup.send(
                        '❌ 无法从用户删除身份组。可能是权限不足或其他限制。',
                        ephemeral=True
                    )
                    log_slash_command(interaction, False)
                except discord.HTTPException as e:
                    await interaction.followup.send(
                        f'❌ 删除身份组时发生网络错误：{str(e)}',
                        ephemeral=True
                    )
                    log_slash_command(interaction, False)
                    
        except Exception as e:
            # 处理未预期的错误
            print(f'❌ 调试身份组命令发生错误：{e}')
            await interaction.followup.send(
                f'❌ 执行命令时发生未知错误：{str(e)}',
                ephemeral=True
            )
            log_slash_command(interaction, False)

    @debug_role.error
    async def debug_role_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError):
        """处理命令错误"""
        # 确保响应已经defer
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        
        if isinstance(error, app_commands.CheckFailure):
            await interaction.followup.send(
                '❌ 您没有权限使用此命令。',
                ephemeral=True
            )
        else:
            print(f'❌ 调试身份组命令错误：{error}')
            await interaction.followup.send(
                f'❌ 执行命令时发生错误：{str(error)}',
                ephemeral=True
            )
        
        log_slash_command(interaction, False)


async def setup(bot):
    """设置函数，用于加载 Cog"""
    await bot.add_cog(DebugRole(bot))
