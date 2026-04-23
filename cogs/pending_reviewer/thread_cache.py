"""帖子状态数据库缓存层 —— 独立于 Discord / bot 的纯 SQLite 封装。"""

import asyncio
import datetime
import os
import sqlite3


class ThreadCache:
    """管理 reviewer 帖子状态的 SQLite 缓存。"""

    def __init__(self, db_dir: str, db_path: str):
        self._db_dir = db_dir
        self._db_path = db_path

    # ---------- 公共同步方法 ----------

    def ensure_ready(self):
        if not os.path.exists(self._db_dir):
            os.makedirs(self._db_dir, exist_ok=True)

        with sqlite3.connect(self._db_path) as conn:
            c = conn.cursor()
            c.execute('''CREATE TABLE IF NOT EXISTS thread_cache (
                thread_id INTEGER PRIMARY KEY,
                last_message_id INTEGER,
                reply_count INTEGER,
                status TEXT,
                reason TEXT,
                last_analyzed_at TEXT
            )''')

    def get_sync(self, thread_id: int):
        with sqlite3.connect(self._db_path) as conn:
            c = conn.cursor()
            c.execute(
                "SELECT last_message_id, reply_count, status, reason FROM thread_cache WHERE thread_id=?",
                (thread_id,),
            )
            return c.fetchone()

    def update_sync(
        self,
        thread_id: int,
        last_msg_id: int,
        reply_count: int,
        status: str,
        reason: str,
    ):
        analyzed_at = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        with sqlite3.connect(self._db_path) as conn:
            c = conn.cursor()
            c.execute(
                "INSERT OR REPLACE INTO thread_cache VALUES (?,?,?,?,?,?)",
                (thread_id, last_msg_id, reply_count, status, reason, analyzed_at),
            )

    def delete_sync(self, thread_id: int):
        with sqlite3.connect(self._db_path) as conn:
            c = conn.cursor()
            c.execute("DELETE FROM thread_cache WHERE thread_id=?", (thread_id,))

    # ---------- 异步包装 ----------

    async def get(self, thread_id: int):
        return await asyncio.to_thread(self.get_sync, thread_id)

    async def update(self, thread_id: int, last_msg_id: int, reply_count: int, status: str, reason: str):
        await asyncio.to_thread(self.update_sync, thread_id, last_msg_id, reply_count, status, reason)

    async def delete(self, thread_id: int):
        await asyncio.to_thread(self.delete_sync, thread_id)
