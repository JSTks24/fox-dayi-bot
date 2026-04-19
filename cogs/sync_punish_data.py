import discord
from discord.ext import commands
import asyncio
import os
import json
import sqlite3
from datetime import datetime

from paths import QUICK_PUNISH_DB

QUICK_PUNISH_DB_PATH = str(QUICK_PUNISH_DB)

class SyncPunishDataCog(commands.Cog):
    """监听对接频道的Bot消息以同步处罚记录"""

    def __init__(self, bot):
        self.bot = bot
        self.interface_channel_id = self._parse_int(os.getenv("QUICK_PUNISH_INTERFACE_CHANNEL"))
        self.interface_bot_id = self._parse_int(os.getenv("QUICK_PUNISH_INTERFACE_BOT_ID"))

    async def cog_load(self):
        await asyncio.to_thread(self.init_database)

    def _parse_int(self, s: str | None) -> int | None:
        try:
            if s is None:
                return None
            return int(str(s).strip())
        except Exception:
            return None

    def init_database(self):
        """幂等建表 + 幂等迁移，保证跨模块顺序加载安全"""
        os.makedirs(os.path.dirname(QUICK_PUNISH_DB_PATH), exist_ok=True)
        with sqlite3.connect(QUICK_PUNISH_DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS quick_punish_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    user_name TEXT NOT NULL,
                    punish_count INTEGER DEFAULT 1,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    original_message_id TEXT,
                    original_message_link TEXT,
                    channel_id TEXT,
                    channel_name TEXT,
                    executor_id TEXT NOT NULL,
                    executor_name TEXT NOT NULL,
                    reason TEXT,
                    removed_roles TEXT,
                    status TEXT DEFAULT 'executed',
                    source_type TEXT DEFAULT 'local',
                    removed_roles_by_guild TEXT DEFAULT '{}',
                    source_guild_id TEXT
                )
            ''')

            cursor.execute("PRAGMA table_info(quick_punish_records)")
            cols = {row[1] for row in cursor.fetchall()}
            if "source_type" not in cols:
                cursor.execute("ALTER TABLE quick_punish_records ADD COLUMN source_type TEXT DEFAULT 'local'")
            if "removed_roles_by_guild" not in cols:
                cursor.execute("ALTER TABLE quick_punish_records ADD COLUMN removed_roles_by_guild TEXT DEFAULT '{}'")
            if "source_guild_id" not in cols:
                cursor.execute("ALTER TABLE quick_punish_records ADD COLUMN source_guild_id TEXT")

            cursor.execute("""
                UPDATE quick_punish_records
                SET source_type = 'local'
                WHERE source_type IS NULL OR TRIM(source_type) = ''
            """)
            cursor.execute("""
                UPDATE quick_punish_records
                SET source_type = 'sync'
                WHERE status = 'executed' AND (original_message_link IS NULL OR TRIM(original_message_link) = '')
                  AND (removed_roles IS NULL OR TRIM(removed_roles) = '' OR TRIM(removed_roles) = '[]')
            """)
            cursor.execute("""
                UPDATE quick_punish_records
                SET removed_roles_by_guild = '{}'
                WHERE removed_roles_by_guild IS NULL OR TRIM(removed_roles_by_guild) = ''
            """)

    def _is_main_text_channel(self, channel) -> bool:
        # 仅监听主层文本频道，排除子区/线程
        return isinstance(channel, discord.TextChannel)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        try:
            # 过滤：仅目标频道、指定Bot、主层文本频道
            if message.guild is None:
                return
            if not self._is_main_text_channel(message.channel):
                return
            if self.interface_channel_id and message.channel.id != self.interface_channel_id:
                return
            if self.interface_bot_id and message.author.id != self.interface_bot_id:
                return
            if self.bot.user and message.author.id == self.bot.user.id:
                return
            if not message.author.bot:
                return

            content = (message.content or "").strip()
            if not content:
                return

            # 严格JSON校验：仅 {"punish": <int>}
            try:
                payload = json.loads(content)
            except json.JSONDecodeError:
                return

            if not isinstance(payload, dict):
                return
            if set(payload.keys()) != {"punish"}:
                return

            punish_value = payload.get("punish")
            if not isinstance(punish_value, int):
                return

            punished_user_id_str = str(punish_value)

            # 执行者信息：使用接口Bot（即消息作者）
            executor_id = str(self.interface_bot_id) if self.interface_bot_id else str(message.author.id)
            executor_name = getattr(message.author, "name", "同步")

            # 时间戳：采用接口消息时间
            ts = message.created_at.isoformat() if message.created_at else datetime.now().isoformat()

            # 写库（source_type=sync）
            next_count = await asyncio.to_thread(
                self._sync_interface_record,
                user_id=punished_user_id_str,
                interface_message_id=str(message.id),
                executor_id=executor_id,
                executor_name=executor_name,
                timestamp=ts,
                source_guild_id=str(message.guild.id)
            )
            if next_count is None:
                return

            print(f"[sync_punish] synced: user_id={punished_user_id_str}, count={next_count}, msg={message.id}")

        except Exception as e:
            print(f"[sync_punish] error: {e}")

    def _sync_interface_record(
        self,
        user_id: str,
        interface_message_id: str,
        executor_id: str,
        executor_name: str,
        timestamp: str,
        source_guild_id: str
    ) -> int | None:
        with sqlite3.connect(QUICK_PUNISH_DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM quick_punish_records WHERE original_message_id = ? AND status = 'executed' LIMIT 1",
                (interface_message_id,)
            )
            if cursor.fetchone() is not None:
                return None

            cursor.execute(
                "SELECT MAX(COALESCE(punish_count, 0)) FROM quick_punish_records WHERE user_id = ? AND status != 'failed'",
                (user_id,)
            )
            row = cursor.fetchone()
            next_count = max(1, int(row[0]) + 1) if row and row[0] is not None else 1

            cursor.execute(
                '''
                INSERT INTO quick_punish_records
                (user_id, user_name, punish_count, timestamp, original_message_id, original_message_link,
                 channel_id, channel_name, executor_id, executor_name, reason, removed_roles, status,
                 source_type, removed_roles_by_guild, source_guild_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (
                    user_id,
                    "不明",
                    next_count,
                    timestamp,
                    interface_message_id,
                    None,
                    None,
                    None,
                    executor_id,
                    executor_name,
                    "同步",
                    "[]",
                    "executed",
                    "sync",
                    "{}",
                    source_guild_id
                )
            )
            return next_count


async def setup(bot):
    await bot.add_cog(SyncPunishDataCog(bot))
