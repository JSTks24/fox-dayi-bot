from __future__ import annotations

import json
import os
from datetime import datetime
import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

class BroadcastStorageMixin:
    def load_config(self) -> None:
        """加载任务配置文件"""
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, encoding='utf-8') as f:
                    raw_config = json.load(f)

                # 提取白名单频道（顶层特殊字段）
                self.allowed_channels: list[int] = raw_config.pop('allowed_channels', [])

                self.config = {}
                for task_name, task_config in raw_config.items():
                    if not isinstance(task_config, dict):
                        continue
                    if 'DAILY_TIMES' in task_config:
                        logger.warning(
                            f"任务 {task_name} 使用已弃用的 DAILY_TIMES 模式，已跳过加载。"
                            f"请手动迁移为 INTERVAL_MINUTES 模式。"
                        )
                        continue
                    self.config[task_name] = task_config

                logger.info(f"已加载 {len(self.config)} 个广播任务配置")
            else:
                logger.warning(f"配置文件不存在: {self.config_path}")
                self.config = {}
                self.allowed_channels = []
        except Exception as e:
            logger.error(f"加载配置文件失败: {e}")
            self.config = {}
            self.allowed_channels = getattr(self, 'allowed_channels', [])

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

    def save_config(self) -> None:
        """保存配置文件（包含白名单频道）"""
        try:
            data = dict(self.config)
            data['allowed_channels'] = getattr(self, 'allowed_channels', [])
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
            logger.debug("配置文件已保存")
        except Exception as e:
            logger.error(f"保存配置文件失败: {e}")

    async def get_next_task_id(self) -> str:
        """获取下一个可用的任务ID"""
        existing_ids = [int(config.get('id', 0)) for config in self.config.values() if config.get('id', '').isdigit()]
        if existing_ids:
            return str(max(existing_ids) + 1)
        return "1"
