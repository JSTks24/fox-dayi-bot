"""
Discord 自动广播 Cog
支持间隔模式和定时模式的消息广播功能
"""

import discord
from discord.ext import commands, tasks
from discord import app_commands
import json
import asyncio
import os
from datetime import datetime, timedelta
import pytz
import logging
import traceback
from cogs.utils import check_admin

# 设置日志
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

class BroadcastCog(commands.Cog):
    """自动广播 Cog，支持定时和间隔两种模式"""
    
    def __init__(self, bot):
        self.bot = bot
        self.config: dict[str, dict] = {}
        self.stats: dict[str, dict] = {}
        self.active_tasks: dict[str, tasks.Loop] = {}
        self.config_path = 'broadcast/broadcast_threads.json'
        self.stats_path = 'broadcast/broadcast_stats.json'
        self.lock = asyncio.Lock()  # 防止并发修改
        self.tz_shanghai = pytz.timezone('Asia/Shanghai')
        
        # 确保目录存在
        os.makedirs('broadcast', exist_ok=True)
        
        # 加载配置和状态
        self.load_config()
        self.load_stats()

    async def cog_load(self) -> None:
        """Start background tasks after the cog is fully loaded."""
        if not self.auto_save.is_running():
            self.auto_save.start()
        await self.start_all_tasks()
    
    def cog_unload(self):
        """Cog 卸载时的清理工作"""
        logger.info("正在卸载 BroadcastCog...")
        
        # 停止所有任务
        for task_name, task_loop in self.active_tasks.items():
            if task_loop.is_running():
                task_loop.cancel()
                logger.info(f"已停止任务: {task_name}")
        
        # 停止自动保存
        if self.auto_save.is_running():
            self.auto_save.cancel()
        
        # 保存最终状态
        self.save_stats()
        logger.info("BroadcastCog 已卸载")
    
    def load_config(self) -> None:
        """加载任务配置文件"""
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, encoding='utf-8') as f:
                    self.config = json.load(f)
                logger.info(f"已加载 {len(self.config)} 个广播任务配置")
            else:
                logger.warning(f"配置文件不存在: {self.config_path}")
                self.config = {}
        except Exception as e:
            logger.error(f"加载配置文件失败: {e}")
            self.config = {}
    
    def load_stats(self) -> None:
        """加载统计数据文件"""
        try:
            if os.path.exists(self.stats_path):
                with open(self.stats_path, encoding='utf-8') as f:
                    self.stats = json.load(f)
                logger.info("已加载统计数据")
                
                # 检查并重置过期的每日计数
                self.reset_daily_counts()
            else:
                logger.info(f"统计文件不存在，创建新文件: {self.stats_path}")
                self.stats = {}
                self.save_stats()
        except Exception as e:
            logger.error(f"加载统计文件失败: {e}")
            self.stats = {}
    
    def save_stats(self) -> None:
        """保存统计数据到文件"""
        try:
            with open(self.stats_path, 'w', encoding='utf-8') as f:
                json.dump(self.stats, f, indent=4, ensure_ascii=False)
            logger.debug("统计数据已保存")
        except Exception as e:
            logger.error(f"保存统计文件失败: {e}")
    
    def reset_daily_counts(self) -> None:
        """重置过期的每日计数"""
        current_date = datetime.now(self.tz_shanghai).strftime("%Y%m%d")
        
        for task_id, stat in self.stats.items():
            last_time = stat.get('last_time_sent', '')
            if last_time:
                # 从 HHMMSS 格式解析日期
                try:
                    # 如果有日期信息，提取日期部分
                    if 'last_date' in stat:
                        last_date = stat['last_date']
                    else:
                        # 兼容旧格式，假设是今天
                        last_date = current_date
                    
                    if last_date < current_date:
                        stat['daily_count'] = 0
                        stat['last_date'] = current_date
                        logger.info(f"重置任务 {task_id} 的每日计数")
                except Exception as e:
                    logger.error(f"重置任务 {task_id} 计数失败: {e}")
    
    def validate_task(self, task_name: str, task_config: dict) -> tuple[bool, str | None]:
        """
        验证任务配置
        返回: (是否有效, 错误信息)
        """
        try:
            # 检查必需字段
            required_fields = ['id', 'status', 'author', 'thread_or_channel', 'content']
            for field in required_fields:
                if field not in task_config:
                    return False, f"缺少必需字段: {field}"
            
            # 检查模式互斥
            has_interval = 'INTERVAL_MINUTES' in task_config
            has_daily = 'DAILY_TIMES' in task_config
            
            if has_interval and has_daily:
                return False, "不能同时设置 INTERVAL_MINUTES 和 DAILY_TIMES"
            
            if not has_interval and not has_daily:
                return False, "必须设置 INTERVAL_MINUTES 或 DAILY_TIMES 之一"
            
            # 验证间隔模式
            if has_interval:
                try:
                    interval = int(task_config['INTERVAL_MINUTES'])
                    if interval <= 0:
                        return False, "间隔分钟数必须大于0"
                except ValueError:
                    return False, "INTERVAL_MINUTES 必须是有效的整数"
            
            # 验证定时模式
            if has_daily:
                try:
                    times = [int(t.strip()) for t in task_config['DAILY_TIMES'].split(',')]
                    for t in times:
                        if t < 0 or t > 24:
                            return False, f"定时时间 {t} 必须在 0-24 之间"
                        if t == 24:
                            # 24点转换为0点
                            times[times.index(t)] = 0
                except ValueError:
                    return False, "DAILY_TIMES 格式错误，应为逗号分隔的整数"
            
            # 验证目标频道/子区
            targets = task_config['thread_or_channel'].split(',')
            if not targets:
                return False, "必须指定至少一个目标频道或子区"
            
            # 验证 ID 格式
            for target in targets:
                try:
                    int(target.strip())
                except ValueError:
                    return False, f"无效的频道/子区 ID: {target}"
            
            return True, None
            
        except Exception as e:
            return False, f"验证过程出错: {str(e)}"
    
    async def start_all_tasks(self) -> None:
        """启动所有活动的任务"""
        await self.bot.wait_until_ready()
        
        for task_name, task_config in self.config.items():
            if task_config.get('status') == 'active':
                is_valid, error_msg = self.validate_task(task_name, task_config)
                
                if is_valid:
                    await self.start_task(task_name, task_config)
                else:
                    logger.error(f"任务 {task_name} 配置无效: {error_msg}")
                    # 将无效任务设为 inactive
                    task_config['status'] = 'inactive'
                    self.save_config()
    
    async def start_task(self, task_name: str, task_config: dict) -> None:
        """启动单个任务"""
        try:
            # 如果任务已经在运行，先停止
            if task_name in self.active_tasks:
                if self.active_tasks[task_name].is_running():
                    self.active_tasks[task_name].cancel()
                    logger.info(f"停止旧任务: {task_name}")
            
            # 根据模式创建任务
            if 'INTERVAL_MINUTES' in task_config:
                await self.create_interval_task(task_name, task_config)
            elif 'DAILY_TIMES' in task_config:
                await self.create_daily_task(task_name, task_config)
            
            logger.info(f"已启动任务: {task_name}")
            
        except Exception as e:
            logger.error(f"启动任务 {task_name} 失败: {e}")
            logger.error(traceback.format_exc())
    
    async def create_interval_task(self, task_name: str, task_config: dict) -> None:
        """创建间隔模式任务"""
        interval_minutes = int(task_config['INTERVAL_MINUTES'])
        
        @tasks.loop(minutes=interval_minutes)
        async def interval_task():
            await self.execute_task(task_name, task_config)

        @interval_task.before_loop
        async def before_interval_task():
            await self.bot.wait_until_ready()
            delay = self.get_interval_first_delay_seconds(task_config)
            if delay > 0:
                logger.info(f"任务 {task_name} 将在 {delay:.0f} 秒后首次执行")
                await asyncio.sleep(delay)
        
        # 启动任务
        interval_task.start()
        self.active_tasks[task_name] = interval_task

    def get_interval_first_delay_seconds(
        self,
        task_config: dict,
        now: datetime | None = None,
    ) -> float:
        """Calculate the delay before the first interval run."""
        task_id = task_config['id']
        last_time_str = self.stats.get(task_id, {}).get('last_time_sent', '')
        if not last_time_str:
            return 0.0

        try:
            current_time = now or datetime.now(self.tz_shanghai)
            last_hour = int(last_time_str[:2])
            last_minute = int(last_time_str[2:4])
            last_second = int(last_time_str[4:6])
            last_time = current_time.replace(
                hour=last_hour,
                minute=last_minute,
                second=last_second,
                microsecond=0,
            )

            if last_time > current_time:
                last_time -= timedelta(days=1)

            interval_minutes = int(task_config['INTERVAL_MINUTES'])
            next_time = last_time + timedelta(minutes=interval_minutes)
            if next_time <= current_time:
                return 0.0

            return (next_time - current_time).total_seconds()
        except Exception as e:
            logger.error(f"解析上次发送时间失败: {e}")
            return 0.0
    
    async def create_daily_task(self, task_name: str, task_config: dict) -> None:
        """创建定时模式任务"""
        daily_times = [int(t.strip()) for t in task_config['DAILY_TIMES'].split(',')]
        timezone_str = task_config.get('tz', 'Asia/Shanghai')
        
        try:
            tz = pytz.timezone(timezone_str)
        except Exception:
            logger.warning(f"无效的时区 {timezone_str}，使用默认时区 Asia/Shanghai")
            tz = self.tz_shanghai
        
        # 处理24点转为0点
        daily_times = [0 if t == 24 else t for t in daily_times]
        daily_times.sort()
        
        @tasks.loop(seconds=60)  # 每分钟检查一次
        async def daily_task():
            now = datetime.now(tz)
            current_hour = now.hour
            current_minute = now.minute
            
            # 检查是否到达执行时间
            if current_hour in daily_times and current_minute == 0:
                # 检查是否在这个小时内已经执行过
                task_id = task_config['id']
                if task_id in self.stats:
                    last_time_str = self.stats[task_id].get('last_time_sent', '')
                    if last_time_str:
                        last_hour = int(last_time_str[:2])
                        # 如果这个小时已经执行过，跳过
                        if last_hour == current_hour:
                            return
                
                await self.execute_task(task_name, task_config)
        
        # 启动任务
        daily_task.start()
        self.active_tasks[task_name] = daily_task
    
    async def execute_task(self, task_name: str, task_config: dict) -> None:
        """执行广播任务"""
        async with self.lock:
            try:
                # 检查状态
                if task_config.get('status') != 'active':
                    logger.debug(f"任务 {task_name} 未激活，跳过执行")
                    return
                
                task_id = task_config['id']
                
                # 准备消息内容
                content = self.replace_macros(task_config['content'], task_id)
                
                # 获取目标列表
                targets = [t.strip() for t in task_config['thread_or_channel'].split(',')]
                
                # 发送消息
                success_count = 0
                failed_targets = []
                
                for target_id in targets:
                    try:
                        channel_id = int(target_id)
                        target = self.bot.get_channel(channel_id)

                        if target:
                            await target.send(content)
                            success_count += 1
                            logger.info(f"任务 {task_name} 成功发送到频道 {channel_id}")
                        else:
                            failed_targets.append(target_id)
                            logger.warning(f"找不到频道/线程: {channel_id}")
                        
                        # 轻微延迟避免限流
                        if len(targets) > 1:
                            await asyncio.sleep(0.5)
                            
                    except discord.Forbidden:
                        failed_targets.append(target_id)
                        logger.warning(f"没有权限发送消息到 {target_id}")
                    except Exception as e:
                        failed_targets.append(target_id)
                        logger.error(f"发送到 {target_id} 失败: {e}")
                
                # 更新统计
                self.update_stats(task_id)
                
                # 记录执行结果
                if failed_targets:
                    logger.warning(f"任务 {task_name} 部分失败，成功 {success_count}/{len(targets)}，失败目标: {failed_targets}")
                else:
                    logger.info(f"任务 {task_name} 执行完成，发送到 {success_count} 个目标")
                
            except Exception as e:
                logger.error(f"执行任务 {task_name} 时发生错误: {e}")
                logger.error(traceback.format_exc())
    
    def replace_macros(self, content: str, task_id: str) -> str:
        """替换消息中的宏变量"""
        try:
            # 替换换行符
            content = content.replace('\\n', '\n')
            
            # 替换时间宏
            current_time = datetime.now(self.tz_shanghai).strftime("%H:%M")
            content = content.replace("{{time}}", current_time)
            
            # 获取并更新计数
            if task_id not in self.stats:
                self.stats[task_id] = {
                    'daily_count': 0,
                    'last_time_sent': '',
                    'last_date': datetime.now(self.tz_shanghai).strftime("%Y%m%d")
                }
            
            daily_count = self.stats[task_id].get('daily_count', 0) + 1
            content = content.replace("{{count}}", str(daily_count))
            
            return content
            
        except Exception as e:
            logger.error(f"替换宏变量失败: {e}")
            return content
    
    def update_stats(self, task_id: str) -> None:
        """更新任务统计信息"""
        try:
            now = datetime.now(self.tz_shanghai)
            current_date = now.strftime("%Y%m%d")
            current_time = now.strftime("%H%M%S")
            
            if task_id not in self.stats:
                self.stats[task_id] = {
                    'daily_count': 0,
                    'last_time_sent': '',
                    'last_date': current_date
                }
            
            # 检查是否需要重置每日计数
            if self.stats[task_id].get('last_date', '') < current_date:
                self.stats[task_id]['daily_count'] = 1
                self.stats[task_id]['last_date'] = current_date
            else:
                self.stats[task_id]['daily_count'] = self.stats[task_id].get('daily_count', 0) + 1
            
            self.stats[task_id]['last_time_sent'] = current_time
            
            # 保存统计
            self.save_stats()
            
        except Exception as e:
            logger.error(f"更新统计信息失败: {e}")
    
    def save_config(self) -> None:
        """保存配置文件"""
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)
            logger.debug("配置文件已保存")
        except Exception as e:
            logger.error(f"保存配置文件失败: {e}")
    
    @tasks.loop(minutes=5)
    async def auto_save(self):
        """定期自动保存统计数据"""
        self.save_stats()
        logger.debug("自动保存统计数据")
    
    @auto_save.before_loop
    async def before_auto_save(self):
        """等待 bot 准备就绪"""
        await self.bot.wait_until_ready()

    def _has_admin_permission(self, interaction: discord.Interaction) -> bool:
        member = interaction.user
        return isinstance(member, discord.Member) and member.guild_permissions.administrator

    @app_commands.command(name='broadcast_reload', description='[仅管理员] 重新加载广播配置')
    @app_commands.default_permissions(administrator=True)
    async def reload_broadcast(self, interaction: discord.Interaction):
        """重新加载广播配置"""
        if not self._has_admin_permission(interaction):
            await interaction.response.send_message("❌ 此命令仅限服务器管理员使用。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            # 停止所有现有任务
            for _task_name, task_loop in self.active_tasks.items():
                if task_loop.is_running():
                    task_loop.cancel()
            self.active_tasks.clear()
            
            # 重新加载配置
            self.load_config()
            self.load_stats()
            
            # 重新启动任务
            await self.start_all_tasks()
            
            await interaction.followup.send("✅ 广播配置已重新加载", ephemeral=True)
            logger.info(f"用户 {interaction.user} 重新加载了广播配置")
            
        except Exception as e:
            await interaction.followup.send(f"❌ 重新加载失败: {str(e)}", ephemeral=True)
            logger.error(f"重新加载广播配置失败: {e}")

    @app_commands.command(name='broadcast_status', description='[仅管理员] 查看广播任务状态')
    @app_commands.default_permissions(administrator=True)
    async def broadcast_status(self, interaction: discord.Interaction):
        """查看广播任务状态"""
        if not self._has_admin_permission(interaction):
            await interaction.response.send_message("❌ 此命令仅限服务器管理员使用。", ephemeral=True)
            return

        try:
            if not self.config:
                await interaction.response.send_message("📭 当前没有配置任何广播任务", ephemeral=True)
                return
            
            embed = discord.Embed(
                title="📢 广播任务状态",
                color=discord.Color.blue(),
                timestamp=datetime.now()
            )
            
            for task_name, task_config in self.config.items():
                task_id = task_config['id']
                status = task_config.get('status', 'unknown')
                
                # 获取统计信息
                stat = self.stats.get(task_id, {})
                daily_count = stat.get('daily_count', 0)
                last_sent = stat.get('last_time_sent', '从未')
                
                # 判断模式
                if 'INTERVAL_MINUTES' in task_config:
                    mode = f"间隔 {task_config['INTERVAL_MINUTES']} 分钟"
                elif 'DAILY_TIMES' in task_config:
                    mode = f"定时 {task_config['DAILY_TIMES']}"
                else:
                    mode = "未知"
                
                # 运行状态
                is_running = task_name in self.active_tasks and self.active_tasks[task_name].is_running()
                run_status = "🟢 运行中" if is_running else "🔴 已停止"
                
                field_value = (
                    f"状态: {status} {run_status}\n"
                    f"模式: {mode}\n"
                    f"今日执行: {daily_count} 次\n"
                    f"最后发送: {last_sent}"
                )
                
                embed.add_field(
                    name=f"📝 {task_name}",
                    value=field_value,
                    inline=False
                )
            
            embed.set_footer(text=f"请求者: {interaction.user}")
            await interaction.response.send_message(embed=embed, ephemeral=True)
            
        except Exception as e:
            response = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
            await response(f"❌ 获取状态失败: {str(e)}", ephemeral=True)
            logger.error(f"获取广播状态失败: {e}")
    
    # ===== 斜杠命令：广播控制面板 =====
    
    @app_commands.command(name='召唤广播控制面板', description='[仅管理员] 管理广播任务')
    async def broadcast_panel(self, interaction: discord.Interaction):
        """显示广播控制面板"""
        # 检查管理员权限
        if not check_admin(interaction):
            await interaction.response.send_message('❌ 此命令仅限管理员使用。', ephemeral=True)
            return
        
        # 延迟响应
        await interaction.response.defer(ephemeral=True)
        
        try:
            # 创建控制面板embed
            embed = await self.create_panel_embed(interaction)
            
            # 创建按钮视图
            view = BroadcastControlView(self, interaction.user.id)
            
            # 发送面板
            await interaction.followup.send(embed=embed, view=view)
            logger.info(f"管理员 {interaction.user.name} 打开了广播控制面板")
            
        except Exception as e:
            logger.error(f"创建广播控制面板失败: {e}")
            await interaction.followup.send(f'❌ 创建控制面板失败: {str(e)}', ephemeral=True)
    
    async def create_panel_embed(self, interaction: discord.Interaction) -> discord.Embed:
        """创建控制面板的embed消息"""
        embed = discord.Embed(
            title="📢 广播任务控制面板",
            description="管理自动广播任务",
            color=discord.Color.blue(),
            timestamp=datetime.now()
        )
        
        # 获取最近5个任务
        tasks_list = list(self.config.items())[:5]
        
        if not tasks_list:
            embed.add_field(
                name="📭 暂无任务",
                value="当前没有配置任何广播任务",
                inline=False
            )
        else:
            for idx, (task_name, task_config) in enumerate(tasks_list, 1):
                task_id = task_config.get('id', 'N/A')
                description = task_config.get('description', '无描述')
                status = task_config.get('status', 'unknown')
                author_id = task_config.get('author', '')
                
                # 获取作者名称
                try:
                    author = await self.bot.fetch_user(int(author_id))
                    author_name = author.name
                except Exception:
                    author_name = f"用户ID: {author_id}"
                
                # 获取目标频道名称
                target_ids = task_config.get('thread_or_channel', '').split(',')
                target_names = []
                for tid in target_ids[:2]:  # 最多显示2个
                    try:
                        channel = self.bot.get_channel(int(tid.strip()))
                        if channel:
                            target_names.append(f"#{channel.name}")
                        else:
                            target_names.append(f"ID:{tid.strip()}")
                    except Exception:
                        target_names.append(f"ID:{tid.strip()}")
                
                if len(target_ids) > 2:
                    target_names.append(f"等{len(target_ids)}个")
                
                target_str = ', '.join(target_names) if target_names else '未知'
                
                # 获取最后发送时间
                stat = self.stats.get(task_id, {})
                last_sent = stat.get('last_time_sent', '')
                if last_sent:
                    try:
                        # 格式化时间 HHMMSS -> HH:MM:SS
                        last_sent_formatted = f"{last_sent[:2]}:{last_sent[2:4]}:{last_sent[4:6]}"
                    except Exception:
                        last_sent_formatted = '未知'
                else:
                    last_sent_formatted = '从未'
                
                # 判断模式
                if 'INTERVAL_MINUTES' in task_config:
                    mode = f"间隔 {task_config['INTERVAL_MINUTES']} 分钟"
                elif 'DAILY_TIMES' in task_config:
                    mode = f"定时 {task_config['DAILY_TIMES']}"
                else:
                    mode = "未知"
                
                # 运行状态
                is_running = task_name in self.active_tasks and self.active_tasks[task_name].is_running()
                status_emoji = "🟢" if status == 'active' else "🔴"
                run_status = "运行中" if is_running else "已停止"
                
                # 构建字段内容
                field_value = (
                    f"**ID:** {task_id}\n"
                    f"📝 **描述:** {description[:50]}{'...' if len(description) > 50 else ''}\n"
                    f"👤 **部署者:** {author_name}\n"
                    f"📍 **目标:** {target_str}\n"
                    f"⏰ **最后发送:** {last_sent_formatted}\n"
                    f"⚙️ **模式:** {mode}\n"
                    f"{status_emoji} **状态:** {status} ({run_status})"
                )
                
                embed.add_field(
                    name=f"{idx}️⃣ {task_name}",
                    value=field_value,
                    inline=False
                )
        
        embed.set_footer(text=f"请求者: {interaction.user}")
        return embed
    
    async def get_next_task_id(self) -> str:
        """获取下一个可用的任务ID"""
        existing_ids = [int(config.get('id', 0)) for config in self.config.values() if config.get('id', '').isdigit()]
        if existing_ids:
            return str(max(existing_ids) + 1)
        return "1"


# ===== UI组件类 =====

class BroadcastControlView(discord.ui.View):
    """广播控制面板的按钮视图"""
    
    def __init__(self, cog: BroadcastCog, user_id: int):
        super().__init__(timeout=300)  # 5分钟超时
        self.cog = cog
        self.user_id = user_id
    
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """检查交互用户是否为原始用户"""
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ 只有召唤面板的用户才能使用这些按钮。", ephemeral=True)
            return False
        return True
    
    @discord.ui.button(label='🔍 查询任务', style=discord.ButtonStyle.primary, row=0)
    async def search_task(self, interaction: discord.Interaction, button: discord.ui.Button):
        """查询任务按钮"""
        modal = SearchTaskModal(self.cog)
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label='➕ 新增任务', style=discord.ButtonStyle.success, row=0)
    async def add_task(self, interaction: discord.Interaction, button: discord.ui.Button):
        """新增任务按钮"""
        modal = AddTaskModal(self.cog)
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label='⏸️ 控制任务', style=discord.ButtonStyle.secondary, row=0)
    async def control_task(self, interaction: discord.Interaction, button: discord.ui.Button):
        """控制任务按钮"""
        modal = ControlTaskModal(self.cog)
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label='🗑️ 删除任务', style=discord.ButtonStyle.danger, row=0)
    async def delete_task(self, interaction: discord.Interaction, button: discord.ui.Button):
        """删除任务按钮"""
        modal = DeleteTaskModal(self.cog)
        await interaction.response.send_modal(modal)
    
    @discord.ui.button(label='🔄 刷新面板', style=discord.ButtonStyle.secondary, row=1)
    async def refresh_panel(self, interaction: discord.Interaction, button: discord.ui.Button):
        """刷新面板按钮"""
        try:
            embed = await self.cog.create_panel_embed(interaction)
            await interaction.response.edit_message(embed=embed, view=self)
        except Exception as e:
            await interaction.response.send_message(f"❌ 刷新失败: {str(e)}", ephemeral=True)


# ===== Modal类 =====

class SearchTaskModal(discord.ui.Modal, title='查询广播任务'):
    """查询任务的Modal"""
    
    keyword = discord.ui.TextInput(
        label='搜索关键词',
        placeholder='输入要搜索的内容（在任务内容中匹配）',
        required=True,
        max_length=100
    )
    
    def __init__(self, cog: BroadcastCog):
        super().__init__()
        self.cog = cog
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        keyword = self.keyword.value.lower()
        found_tasks = []
        
        # 搜索匹配的任务
        for task_name, task_config in self.cog.config.items():
            content = task_config.get('content', '').lower()
            if keyword in content:
                found_tasks.append((task_name, task_config))
        
        if not found_tasks:
            await interaction.followup.send(f"❌ 没有找到包含 '{self.keyword.value}' 的任务", ephemeral=True)
            return
        
        # 创建结果embed
        embed = discord.Embed(
            title=f"🔍 搜索结果（关键词: {self.keyword.value}）",
            color=discord.Color.green(),
            timestamp=datetime.now()
        )
        
        for task_name, task_config in found_tasks[:3]:  # 最多显示3个
            task_id = task_config.get('id', 'N/A')
            
            # 收集所有信息
            info_lines = [
                f"**ID:** {task_id}",
                f"**状态:** {task_config.get('status', 'unknown')}",
                f"**描述:** {task_config.get('description', '无')}",
                f"**作者:** <@{task_config.get('author', 'unknown')}>",
                f"**目标:** {task_config.get('thread_or_channel', 'unknown')}",
            ]
            
            # 模式信息
            if 'INTERVAL_MINUTES' in task_config:
                info_lines.append(f"**模式:** 间隔 {task_config['INTERVAL_MINUTES']} 分钟")
            elif 'DAILY_TIMES' in task_config:
                info_lines.append(f"**模式:** 定时 {task_config['DAILY_TIMES']}")
                if 'tz' in task_config:
                    info_lines.append(f"**时区:** {task_config['tz']}")
            
            # 内容预览
            content = task_config.get('content', '')
            content_preview = content[:200] + '...' if len(content) > 200 else content
            info_lines.append(f"**内容预览:**\n```\n{content_preview}\n```")
            
            embed.add_field(
                name=f"📝 {task_name}",
                value='\n'.join(info_lines),
                inline=False
            )
        
        if len(found_tasks) > 3:
            embed.add_field(
                name="ℹ️ 提示",
                value=f"共找到 {len(found_tasks)} 个任务，仅显示前3个",
                inline=False
            )
        
        await interaction.followup.send(embed=embed, ephemeral=True)


class AddTaskModal(discord.ui.Modal, title='新增广播任务'):
    """新增任务的Modal - 基本信息"""
    
    task_name = discord.ui.TextInput(
        label='任务名称',
        placeholder='例如: daily_announcement',
        required=True,
        max_length=50
    )
    
    
    channels = discord.ui.TextInput(
        label='目标频道ID（逗号分隔）',
        placeholder='例如: 123456789,987654321',
        required=True,
        max_length=200
    )
    
    interval = discord.ui.TextInput(
        label='间隔分钟数（与定时互斥，留空则使用定时）',
        placeholder='例如: 60 表示每60分钟',
        required=False,
        max_length=10
    )
    
    daily_times = discord.ui.TextInput(
        label='定时时间（逗号分隔，24小时制，UTC+8）',
        placeholder='例如: 6,12,18,0 表示6点、12点、18点、0点',
        required=False,
        max_length=100
    )

    content = discord.ui.TextInput(
        label='广播内容',
        placeholder='支持宏：{{time}}=当前时间，{{count}}=今日次数，\\n=换行',
        required=True,
        style=discord.TextStyle.paragraph,
        max_length=2000
    )
    
    def __init__(self, cog: BroadcastCog):
        super().__init__()
        self.cog = cog
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            task_name = self.task_name.value

            # 检查任务名是否已存在
            if task_name in self.cog.config:
                await interaction.followup.send(f"❌ 任务名 '{task_name}' 已存在", ephemeral=True)
                return

            # 获取新的任务ID
            task_id = await self.cog.get_next_task_id()

            # 构建配置（描述默认为'未提供'）
            new_config = {
                'id': task_id,
                'status': 'active',
                'author': str(interaction.user.id),
                'description': '未提供',
                'thread_or_channel': self.channels.value,
                'content': self.content.value
            }

            # 处理模式
            if self.interval.value:
                try:
                    interval = int(self.interval.value)
                    if interval <= 0:
                        raise ValueError("间隔必须大于0")
                    new_config['INTERVAL_MINUTES'] = str(interval)
                except ValueError as e:
                    await interaction.followup.send(f"❌ 间隔分钟数无效: {e}", ephemeral=True)
                    return
            elif self.daily_times.value:
                try:
                    times = [int(t.strip()) for t in self.daily_times.value.split(',')]
                    for t in times:
                        if t < 0 or t > 24:
                            raise ValueError(f"时间 {t} 必须在 0-24 之间")
                    new_config['DAILY_TIMES'] = self.daily_times.value
                    new_config['tz'] = 'Asia/Shanghai'
                except ValueError as e:
                    await interaction.followup.send(f"❌ 定时时间格式无效: {e}", ephemeral=True)
                    return
            else:
                await interaction.followup.send("❌ 必须指定间隔分钟数或定时时间之一", ephemeral=True)
                return

            # 验证配置
            is_valid, error_msg = self.cog.validate_task(task_name, new_config)
            if not is_valid:
                await interaction.followup.send(f"❌ 任务配置无效: {error_msg}", ephemeral=True)
                return

            # 保存配置
            async with self.cog.lock:
                self.cog.config[task_name] = new_config
                self.cog.save_config()

            # 启动任务
            await self.cog.start_task(task_name, new_config)

            # 成功消息
            embed = discord.Embed(
                title="✅ 任务创建成功",
                description=f"任务 **{task_name}** 已成功创建并启动",
                color=discord.Color.green(),
                timestamp=datetime.now()
            )
            embed.add_field(name="任务ID", value=task_id, inline=True)
            embed.add_field(name="状态", value="🟢 运行中", inline=True)
            if 'INTERVAL_MINUTES' in new_config:
                embed.add_field(name="模式", value=f"间隔 {new_config['INTERVAL_MINUTES']} 分钟", inline=True)
            else:
                embed.add_field(name="模式", value=f"定时 {new_config['DAILY_TIMES']}", inline=True)

            await interaction.followup.send(embed=embed, ephemeral=True)
            logger.info(f"管理员 {interaction.user.name} 创建了新任务: {task_name}")

        except Exception as e:
            logger.error(f"创建任务失败: {e}")
            await interaction.followup.send(f"❌ 创建任务失败: {str(e)}", ephemeral=True)




class ControlTaskModal(discord.ui.Modal, title='控制广播任务'):
    """控制任务的Modal"""
    
    task_id = discord.ui.TextInput(
        label='任务ID',
        placeholder='输入要控制的任务ID',
        required=True,
        max_length=10
    )
    
    def __init__(self, cog: BroadcastCog):
        super().__init__()
        self.cog = cog
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        task_id = self.task_id.value
        
        # 查找任务
        task_name = None
        task_config = None
        for name, config in self.cog.config.items():
            if config.get('id') == task_id:
                task_name = name
                task_config = config
                break
        
        if not task_config:
            await interaction.followup.send(f"❌ 未找到ID为 {task_id} 的任务", ephemeral=True)
            return
        
        try:
            async with self.cog.lock:
                current_status = task_config.get('status', 'inactive')
                new_status = 'inactive' if current_status == 'active' else 'active'
                
                # 更新状态
                task_config['status'] = new_status
                self.cog.save_config()
                
                # 处理任务启停
                if new_status == 'active':
                    # 启动任务
                    await self.cog.start_task(task_name, task_config)
                    status_text = "✅ 已启动"
                    status_emoji = "🟢"
                else:
                    # 停止任务
                    if task_name in self.cog.active_tasks:
                        if self.cog.active_tasks[task_name].is_running():
                            self.cog.active_tasks[task_name].cancel()
                        del self.cog.active_tasks[task_name]
                    status_text = "⏸️ 已停止"
                    status_emoji = "🔴"
            
            # 发送成功消息
            embed = discord.Embed(
                title=f"{status_emoji} 任务状态已更新",
                description=f"任务 **{task_name}** (ID: {task_id})",
                color=discord.Color.green() if new_status == 'active' else discord.Color.orange(),
                timestamp=datetime.now()
            )
            
            embed.add_field(name="新状态", value=status_text, inline=True)
            embed.add_field(name="描述", value=task_config.get('description', '无'), inline=False)
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            logger.info(f"管理员 {interaction.user.name} 将任务 {task_name} 状态更改为 {new_status}")
            
        except Exception as e:
            logger.error(f"控制任务失败: {e}")
            await interaction.followup.send(f"❌ 控制任务失败: {str(e)}", ephemeral=True)


class DeleteTaskModal(discord.ui.Modal, title='删除广播任务'):
    """删除任务的Modal"""
    
    task_id = discord.ui.TextInput(
        label='任务ID',
        placeholder='输入要删除的任务ID（此操作不可恢复）',
        required=True,
        max_length=10
    )
    
    def __init__(self, cog: BroadcastCog):
        super().__init__()
        self.cog = cog
    
    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        
        task_id = self.task_id.value
        
        # 查找任务
        task_name = None
        task_config = None
        for name, config in self.cog.config.items():
            if config.get('id') == task_id:
                task_name = name
                task_config = config
                break
        
        if not task_config:
            await interaction.followup.send(f"❌ 未找到ID为 {task_id} 的任务", ephemeral=True)
            return
        
        try:
            async with self.cog.lock:
                # 停止任务
                if task_name in self.cog.active_tasks:
                    if self.cog.active_tasks[task_name].is_running():
                        self.cog.active_tasks[task_name].cancel()
                    del self.cog.active_tasks[task_name]
                
                # 从配置中删除
                del self.cog.config[task_name]
                self.cog.save_config()
                
                # 删除统计数据
                if task_id in self.cog.stats:
                    del self.cog.stats[task_id]
                    self.cog.save_stats()
            
            # 发送成功消息
            embed = discord.Embed(
                title="🗑️ 任务已删除",
                description=f"任务 **{task_name}** (ID: {task_id}) 已被永久删除",
                color=discord.Color.red(),
                timestamp=datetime.now()
            )
            
            embed.add_field(name="描述", value=task_config.get('description', '无'), inline=False)
            embed.set_footer(text="此操作不可恢复")
            
            await interaction.followup.send(embed=embed, ephemeral=True)
            logger.info(f"管理员 {interaction.user.name} 删除了任务: {task_name}")
            
        except Exception as e:
            logger.error(f"删除任务失败: {e}")
            await interaction.followup.send(f"❌ 删除任务失败: {str(e)}", ephemeral=True)


async def setup(bot):
    """添加 Cog 到 bot"""
    await bot.add_cog(BroadcastCog(bot))
    logger.info("BroadcastCog 已加载")
