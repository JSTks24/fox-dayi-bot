from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

class TaggerAlertMixin:
    async def _seconds_until_next_beijing_midnight(self) -> int:
        """
        计算距离下一次北京时间 0 点的秒数（无外部时区库，按UTC+8）
        """
        now_utc = datetime.now(timezone.utc)
        bj_now = now_utc + timedelta(hours=8)
        bj_midnight_next = datetime(
            bj_now.year,
            bj_now.month,
            bj_now.day,
            tzinfo=timezone.utc,
        ) + timedelta(days=1)
        delta = bj_midnight_next - bj_now
        seconds = int(delta.total_seconds())
        # 容错，至少为1秒
        return max(1, seconds)

    async def _expiry_scheduler(self):
        """
        每日北京时间0点执行一次过期扫描
        """
        try:
            while True:
                delay = await self._seconds_until_next_beijing_midnight()
                await asyncio.sleep(delay)
                try:
                    await self._expiry_scan_once()
                except Exception as e:
                    print(f"[tagger] 过期扫描出错: {e}")
                # 下一轮继续
        except asyncio.CancelledError:
            # 任务取消时静默退出
            pass
        except Exception as e:
            print(f"[tagger] 调度任务异常退出: {e}")
