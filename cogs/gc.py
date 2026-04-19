import discord
from discord.ext import commands, tasks
from discord import app_commands
import os
from datetime import datetime, timedelta
import asyncio
import logging
import re

class AutoGarbageCollector(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        
        # 从环境变量加载配置 - 临时文件清理
        self.enabled = os.getenv("AUTO_GC", "false").lower() == "true"
        self.interval_hours = int(os.getenv("AUTO_GC_INTERVAL", "6"))  # 默认6小时
        self.grace_minutes = int(os.getenv("AUTO_GC_GRACE", "5"))  # 默认5分钟
        
        # 从环境变量加载配置 - 存档文件清理
        self.archive_enabled = os.getenv("AUTO_ARCHIVE_GC", "false").lower() == "true"
        self.archive_interval_hours = int(os.getenv("AUTO_ARCHIVE_GC_INTERVAL", "24"))  # 默认24小时
        self.archive_grace_hours = int(os.getenv("AUTO_ARCHIVE_GC_GRACE", "144"))  # 默认144小时（6天）
        self.archive_folder = os.getenv("AUTO_ARCHIVE_GC_FOLDER", "thread_save")  # 默认存档文件夹
        
        # 要清理的临时文件夹列表
        self.cleanup_folders = ["jmtktemp", "app_temp", "temp", "logs", "shieldlog", "thread_temp","app_save","agent_save"]
        
        self.first_run_time = None
        self.archive_first_run_time = None
        
        # 设置日志
        self.logger = logging.getLogger('AutoGC')
        self.logger.setLevel(logging.INFO)
        
        # 如果还没有处理器，添加一个
        if not self.logger.handlers:
            handler = logging.FileHandler('logs/gc.log', encoding='utf-8')
            formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
        
        print("🗑️ 自动清理功能初始化完成:")
        print("\n📁 临时文件清理:")
        print(f"   - 启用状态: {self.enabled}")
        print(f"   - 清理间隔: {self.interval_hours} 小时")
        print(f"   - 文件保留时间: {self.grace_minutes} 分钟")
        print(f"   - 清理文件夹: {', '.join(self.cleanup_folders)}")
        
        print("\n📚 存档文件清理:")
        print(f"   - 启用状态: {self.archive_enabled}")
        print(f"   - 清理间隔: {self.archive_interval_hours} 小时")
        print(f"   - 文件保留时间: {self.archive_grace_hours} 小时")
        print(f"   - 清理文件夹: {self.archive_folder}")
        
        # 如果启用了临时文件自动清理，启动定时任务
        if self.enabled:
            self.auto_cleanup_task.change_interval(hours=self.interval_hours)
            self.auto_cleanup_task.start()
            print("✅ 临时文件自动清理定时任务已启动")
        else:
            print("⚠️ 临时文件自动清理功能已禁用")
        
        # 如果启用了存档文件自动清理，启动定时任务
        if self.archive_enabled:
            self.auto_archive_cleanup_task.change_interval(hours=self.archive_interval_hours)
            self.auto_archive_cleanup_task.start()
            print("✅ 存档文件自动清理定时任务已启动")
        else:
            print("⚠️ 存档文件自动清理功能已禁用")
    
    def cog_unload(self):
        """当cog被卸载时停止定时任务"""
        if hasattr(self, 'auto_cleanup_task'):
            self.auto_cleanup_task.cancel()
        if hasattr(self, 'auto_archive_cleanup_task'):
            self.auto_archive_cleanup_task.cancel()
    
    @tasks.loop(hours=6)  # 默认间隔，会在初始化时根据配置调整
    async def auto_cleanup_task(self):
        """自动清理定时任务"""
        try:
            self.logger.info("开始执行自动清理任务")
            await self.perform_cleanup()
            self.logger.info("自动清理任务执行完成")
        except Exception as e:
            self.logger.error(f"自动清理任务执行失败: {str(e)}")
            print(f"❌ 自动清理任务执行失败: {str(e)}")
    
    @auto_cleanup_task.before_loop
    async def before_auto_cleanup(self):
        """等待bot准备就绪，并在第一次运行时延迟"""
        await self.bot.wait_until_ready()
        self.first_run_time = datetime.now() + timedelta(hours=self.interval_hours)
        self.logger.info(f"自动清理任务已启动，第一次清理将在 {self.interval_hours} 小时后执行。 (预计时间: {self.first_run_time.strftime('%Y-%m-%d %H:%M:%S')})")
        await asyncio.sleep(self.interval_hours * 3600)
        self.logger.info("初始延迟结束，即将开始第一次自动清理。")
    
    async def perform_cleanup(self):
        """执行清理操作"""
        total_deleted = 0
        total_size_freed = 0
        cleanup_summary = []
        
        # 计算截止时间
        cutoff_time = datetime.now() - timedelta(minutes=self.grace_minutes)
        
        for folder_name in self.cleanup_folders:
            try:
                folder_path = os.path.join(os.getcwd(), folder_name)
                
                # 检查文件夹是否存在
                if not os.path.exists(folder_path):
                    self.logger.warning(f"文件夹不存在，跳过: {folder_path}")
                    continue
                
                if not os.path.isdir(folder_path):
                    self.logger.warning(f"路径不是文件夹，跳过: {folder_path}")
                    continue
                
                folder_deleted = 0
                folder_size_freed = 0
                
                # 遍历文件夹中的所有文件
                for filename in os.listdir(folder_path):
                    file_path = os.path.join(folder_path, filename)
                    
                    # 只处理文件，跳过子文件夹
                    if not os.path.isfile(file_path):
                        continue
                    
                    try:
                        # 获取文件修改时间
                        file_mtime = datetime.fromtimestamp(os.path.getmtime(file_path))
                        
                        # 如果文件修改时间早于截止时间，删除文件
                        if file_mtime < cutoff_time:
                            file_size = os.path.getsize(file_path)
                            os.remove(file_path)
                            
                            folder_deleted += 1
                            folder_size_freed += file_size
                            
                            self.logger.info(f"已删除文件: {file_path} (大小: {file_size} 字节, 修改时间: {file_mtime})")
                    
                    except OSError as e:
                        self.logger.error(f"删除文件失败: {file_path}, 错误: {str(e)}")
                        continue
                    except Exception as e:
                        self.logger.error(f"处理文件时出错: {file_path}, 错误: {str(e)}")
                        continue
                
                total_deleted += folder_deleted
                total_size_freed += folder_size_freed
                
                if folder_deleted > 0:
                    cleanup_summary.append(f"{folder_name}: {folder_deleted} 个文件 ({self.format_size(folder_size_freed)})")
                    self.logger.info(f"文件夹 {folder_name} 清理完成: 删除 {folder_deleted} 个文件，释放 {self.format_size(folder_size_freed)}")
                else:
                    self.logger.info(f"文件夹 {folder_name} 无需清理")
            
            except Exception as e:
                self.logger.error(f"清理文件夹 {folder_name} 时出错: {str(e)}")
                continue
        
        # 记录总结信息
        if total_deleted > 0:
            summary_msg = f"清理完成: 共删除 {total_deleted} 个文件，释放 {self.format_size(total_size_freed)}"
            details_msg = "详细信息: " + ", ".join(cleanup_summary) if cleanup_summary else "无文件被删除"
            
            self.logger.info(summary_msg)
            self.logger.info(details_msg)
            print(f"🗑️ {summary_msg}")
            print(f"📊 {details_msg}")
        else:
            self.logger.info("清理完成: 无文件需要删除")
            print("🗑️ 清理完成: 无文件需要删除")
    
    @tasks.loop(hours=24)  # 默认间隔，会在初始化时根据配置调整
    async def auto_archive_cleanup_task(self):
        """存档文件自动清理定时任务"""
        try:
            self.logger.info("开始执行存档文件自动清理任务")
            await self.perform_archive_cleanup()
            self.logger.info("存档文件自动清理任务执行完成")
        except Exception as e:
            self.logger.error(f"存档文件自动清理任务执行失败: {str(e)}")
            print(f"❌ 存档文件自动清理任务执行失败: {str(e)}")
    
    @auto_archive_cleanup_task.before_loop
    async def before_auto_archive_cleanup(self):
        """等待bot准备就绪，并在第一次运行时延迟"""
        await self.bot.wait_until_ready()
        self.archive_first_run_time = datetime.now() + timedelta(hours=self.archive_interval_hours)
        self.logger.info(f"存档文件自动清理任务已启动，第一次清理将在 {self.archive_interval_hours} 小时后执行。 (预计时间: {self.archive_first_run_time.strftime('%Y-%m-%d %H:%M:%S')})")
        await asyncio.sleep(self.archive_interval_hours * 3600)
        self.logger.info("初始延迟结束，即将开始第一次存档文件自动清理。")
    
    async def perform_archive_cleanup(self):
        """执行存档文件清理操作"""
        total_deleted = 0
        total_size_freed = 0
        
        # 计算截止时间（144小时前）
        cutoff_time = datetime.now() - timedelta(hours=self.archive_grace_hours)
        
        try:
            folder_path = os.path.join(os.getcwd(), self.archive_folder)
            
            # 检查文件夹是否存在
            if not os.path.exists(folder_path):
                self.logger.warning(f"存档文件夹不存在，跳过: {folder_path}")
                return
            
            if not os.path.isdir(folder_path):
                self.logger.warning(f"路径不是文件夹，跳过: {folder_path}")
                return
            
            # 正则表达式匹配文件名格式: 时间戳_子区ID_子区名称.txt
            pattern = re.compile(r'^(\d{8}_\d{6})_(\d+)_(.+)\.txt$')
            
            # 遍历文件夹中的所有文件
            for filename in os.listdir(folder_path):
                file_path = os.path.join(folder_path, filename)
                
                # 只处理文件，跳过子文件夹
                if not os.path.isfile(file_path):
                    continue
                
                # 检查文件名是否匹配格式
                match = pattern.match(filename)
                if not match:
                    self.logger.debug(f"文件名格式不匹配，跳过: {filename}")
                    continue
                
                try:
                    # 从文件名中提取时间戳
                    timestamp_str = match.group(1)
                    file_datetime = datetime.strptime(timestamp_str, "%Y%m%d_%H%M%S")
                    
                    # 如果文件时间早于截止时间，删除文件
                    if file_datetime < cutoff_time:
                        file_size = os.path.getsize(file_path)
                        os.remove(file_path)
                        
                        total_deleted += 1
                        total_size_freed += file_size
                        
                        self.logger.info(f"已删除存档文件: {file_path} (大小: {file_size} 字节, 时间戳: {file_datetime})")
                
                except ValueError as e:
                    self.logger.error(f"解析文件时间戳失败: {filename}, 错误: {str(e)}")
                    continue
                except OSError as e:
                    self.logger.error(f"删除存档文件失败: {file_path}, 错误: {str(e)}")
                    continue
                except Exception as e:
                    self.logger.error(f"处理存档文件时出错: {file_path}, 错误: {str(e)}")
                    continue
            
            # 记录总结信息
            if total_deleted > 0:
                summary_msg = f"存档清理完成: 共删除 {total_deleted} 个文件，释放 {self.format_size(total_size_freed)}"
                self.logger.info(summary_msg)
                print(f"📚 {summary_msg}")
            else:
                self.logger.info("存档清理完成: 无文件需要删除")
                print("📚 存档清理完成: 无文件需要删除")
        
        except Exception as e:
            self.logger.error(f"清理存档文件夹 {self.archive_folder} 时出错: {str(e)}")
            print(f"❌ 清理存档文件夹时出错: {str(e)}")
    
    def format_size(self, size_bytes):
        """格式化文件大小显示"""
        if size_bytes == 0:
            return "0 B"
        
        size_names = ["B", "KB", "MB", "GB"]
        i = 0
        while size_bytes >= 1024 and i < len(size_names) - 1:
            size_bytes /= 1024.0
            i += 1
        
        return f"{size_bytes:.1f} {size_names[i]}"
    
    @app_commands.command(name='gc_status', description='[仅管理员] 查看自动清理功能状态')
    async def gc_status(self, interaction: discord.Interaction):
        """查看自动清理功能状态"""
        # 检查是否为管理员
        if interaction.user.id not in getattr(self.bot, 'admins', []):
            await interaction.response.send_message("❌ 此命令仅限管理员使用。", ephemeral=True)
            return
        
        embed = discord.Embed(
            title="🗑️ 自动清理功能状态",
            color=0x00ff00 if (self.enabled or self.archive_enabled) else 0xff0000
        )
        
        # 临时文件清理状态
        embed.add_field(name="📁 **临时文件清理**", value="━━━━━━━━━━━━━", inline=False)
        embed.add_field(name="启用状态", value="✅ 已启用" if self.enabled else "❌ 已禁用", inline=True)
        embed.add_field(name="清理间隔", value=f"{self.interval_hours} 小时", inline=True)
        embed.add_field(name="文件保留时间", value=f"{self.grace_minutes} 分钟", inline=True)
        embed.add_field(name="清理文件夹", value="\n".join([f"• {folder}" for folder in self.cleanup_folders]), inline=False)
        
        if self.enabled and hasattr(self, 'auto_cleanup_task'):
            if self.auto_cleanup_task.is_running():
                next_run = self.auto_cleanup_task.next_iteration
                if next_run:
                    embed.add_field(name="下次清理时间", value=f"<t:{int(next_run.timestamp())}:R>", inline=True)
                elif self.first_run_time:
                    embed.add_field(name="首次清理时间", value=f"<t:{int(self.first_run_time.timestamp())}:R>", inline=True)
                embed.add_field(name="任务状态", value="🟢 运行中", inline=True)
            else:
                embed.add_field(name="任务状态", value="🔴 已停止", inline=True)
        else:
            embed.add_field(name="任务状态", value="⏸️ 未启用", inline=True)
        
        # 存档文件清理状态
        embed.add_field(name="\n📚 **存档文件清理**", value="━━━━━━━━━━━━━", inline=False)
        embed.add_field(name="启用状态", value="✅ 已启用" if self.archive_enabled else "❌ 已禁用", inline=True)
        embed.add_field(name="清理间隔", value=f"{self.archive_interval_hours} 小时", inline=True)
        embed.add_field(name="文件保留时间", value=f"{self.archive_grace_hours} 小时", inline=True)
        embed.add_field(name="清理文件夹", value=f"• {self.archive_folder}", inline=False)
        embed.add_field(name="文件格式", value="时间戳_子区ID_子区名称.txt", inline=False)
        
        if self.archive_enabled and hasattr(self, 'auto_archive_cleanup_task'):
            if self.auto_archive_cleanup_task.is_running():
                next_run = self.auto_archive_cleanup_task.next_iteration
                if next_run:
                    embed.add_field(name="下次清理时间", value=f"<t:{int(next_run.timestamp())}:R>", inline=True)
                elif self.archive_first_run_time:
                    embed.add_field(name="首次清理时间", value=f"<t:{int(self.archive_first_run_time.timestamp())}:R>", inline=True)
                embed.add_field(name="任务状态", value="🟢 运行中", inline=True)
            else:
                embed.add_field(name="任务状态", value="🔴 已停止", inline=True)
        else:
            embed.add_field(name="任务状态", value="⏸️ 未启用", inline=True)
        
        embed.set_footer(text="配置可在 .env 文件中修改")
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
    
    @app_commands.command(name='gc_run', description='[仅管理员] 立即执行一次清理任务')
    @app_commands.choices(
        task_type=[
            app_commands.Choice(name="临时文件清理", value="temp"),
            app_commands.Choice(name="存档文件清理", value="archive"),
            app_commands.Choice(name="全部清理", value="all")
        ]
    )
    async def gc_run(self, interaction: discord.Interaction, task_type: str = "all"):
        """手动执行清理任务"""
        # 检查是否为管理员
        if interaction.user.id not in getattr(self.bot, 'admins', []):
            await interaction.response.send_message("❌ 此命令仅限管理员使用。", ephemeral=True)
            return
        
        await interaction.response.defer(ephemeral=True)
        
        try:
            if task_type == "temp" or task_type == "all":
                self.logger.info(f"管理员 {interaction.user} 手动触发临时文件清理任务")
                await self.perform_cleanup()
            
            if task_type == "archive" or task_type == "all":
                self.logger.info(f"管理员 {interaction.user} 手动触发存档文件清理任务")
                await self.perform_archive_cleanup()
            
            task_name = {
                "temp": "临时文件清理",
                "archive": "存档文件清理",
                "all": "全部清理"
            }.get(task_type, "清理")
            
            await interaction.followup.send(f"✅ {task_name}任务执行完成！请查看控制台输出获取详细信息。", ephemeral=True)
        except Exception as e:
            error_msg = f"清理任务执行失败: {str(e)}"
            self.logger.error(error_msg)
            await interaction.followup.send(f"❌ {error_msg}", ephemeral=True)
    
    @app_commands.command(name='gc_toggle', description='[仅管理员] 切换自动清理功能的启用状态')
    @app_commands.choices(
        task_type=[
            app_commands.Choice(name="临时文件清理", value="temp"),
            app_commands.Choice(name="存档文件清理", value="archive"),
            app_commands.Choice(name="全部", value="all")
        ]
    )
    async def gc_toggle(self, interaction: discord.Interaction, task_type: str = "all"):
        """切换自动清理功能"""
        # 检查是否为管理员
        if interaction.user.id not in getattr(self.bot, 'admins', []):
            await interaction.response.send_message("❌ 此命令仅限管理员使用。", ephemeral=True)
            return
        
        messages = []
        
        # 处理临时文件清理
        if task_type == "temp" or task_type == "all":
            if self.enabled:
                # 禁用临时文件自动清理
                self.enabled = False
                if hasattr(self, 'auto_cleanup_task'):
                    self.auto_cleanup_task.cancel()
                
                self.logger.info(f"管理员 {interaction.user} 禁用了临时文件自动清理功能")
                messages.append("🔴 临时文件自动清理功能已禁用")
            else:
                # 启用临时文件自动清理
                self.enabled = True
                if hasattr(self, 'auto_cleanup_task'):
                    self.auto_cleanup_task.change_interval(hours=self.interval_hours)
                    self.auto_cleanup_task.start()
                
                self.logger.info(f"管理员 {interaction.user} 启用了临时文件自动清理功能")
                messages.append("🟢 临时文件自动清理功能已启用")
        
        # 处理存档文件清理
        if task_type == "archive" or task_type == "all":
            if self.archive_enabled:
                # 禁用存档文件自动清理
                self.archive_enabled = False
                if hasattr(self, 'auto_archive_cleanup_task'):
                    self.auto_archive_cleanup_task.cancel()
                
                self.logger.info(f"管理员 {interaction.user} 禁用了存档文件自动清理功能")
                messages.append("🔴 存档文件自动清理功能已禁用")
            else:
                # 启用存档文件自动清理
                self.archive_enabled = True
                if hasattr(self, 'auto_archive_cleanup_task'):
                    self.auto_archive_cleanup_task.change_interval(hours=self.archive_interval_hours)
                    self.auto_archive_cleanup_task.start()
                
                self.logger.info(f"管理员 {interaction.user} 启用了存档文件自动清理功能")
                messages.append("🟢 存档文件自动清理功能已启用")
        
        response_msg = "\n".join(messages)
        response_msg += "\n\n⚠️ 注意：这只是临时更改，重启bot后会恢复.env文件中的设置。"
        
        await interaction.response.send_message(response_msg, ephemeral=True)

async def setup(bot):
    await bot.add_cog(AutoGarbageCollector(bot))
