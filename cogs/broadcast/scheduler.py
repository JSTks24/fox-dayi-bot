from __future__ import annotations

import discord
from discord.ext import tasks
import asyncio
from datetime import datetime, timedelta
import pytz
import logging
import traceback

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

class BroadcastSchedulerMixin:
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

    @tasks.loop(minutes=5)
    async def auto_save(self):
        """定期自动保存统计数据"""
        self.save_stats()
        logger.debug("自动保存统计数据")

    @auto_save.before_loop
    async def before_auto_save(self):
        """等待 bot 准备就绪"""
        await self.bot.wait_until_ready()
