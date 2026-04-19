from __future__ import annotations

import os
import asyncio
import sqlite3
from datetime import datetime, timezone
from typing import Any

from paths import TAGGER_DB

DB_DIR = str(TAGGER_DB.parent)
DB_PATH = str(TAGGER_DB)

RECORD_SELECT_COLUMNS = (
    "id, status, guild_id, target_user_id, message_link, reason, "
    "tagged_at, tagger_id, tagger_name, expire_at_epoch, expire_input, scope_id"
)

def _ensure_dirs_and_db():
    if not os.path.exists(DB_DIR):
        os.makedirs(DB_DIR, exist_ok=True)

class TaggerDBMixin:
    def _get_conn(self):
        return sqlite3.connect(DB_PATH)

    def _init_database(self):
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute('''
                CREATE TABLE IF NOT EXISTS tag_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    status TEXT NOT NULL,
                    guild_id TEXT NOT NULL,
                    target_user_id TEXT NOT NULL,
                    message_link TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    tagged_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    tagger_id TEXT NOT NULL,
                    tagger_name TEXT NOT NULL,
                    expire_at_epoch INTEGER NOT NULL,
                    expire_input TEXT NOT NULL,
                    scope_id INTEGER NOT NULL DEFAULT -1
                )
            ''')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_tag_records_guild_status ON tag_records (guild_id, status, id DESC)')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_tag_records_expiry_scan ON tag_records (status, expire_at_epoch)')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_tag_records_scope ON tag_records (guild_id, target_user_id, status, expire_at_epoch, scope_id, id DESC)')

    def _row_to_record(self, row: tuple[Any, ...]) -> dict[str, Any]:
        keys = ['id', 'status', 'guild_id', 'target_user_id', 'message_link', 'reason',
                'tagged_at', 'tagger_id', 'tagger_name', 'expire_at_epoch', 'expire_input', 'scope_id']
        return dict(zip(keys, row, strict=False))

    async def _insert_record(self,
                             guild_id: int,
                             target_user_id: int,
                             message_link: str,
                             reason: str,
                             tagger_id: int,
                             tagger_name: str,
                             expire_at_epoch: int,
                             expire_input: str,
                             scope_id: int) -> int:
        def _write() -> int:
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute('''
                    INSERT INTO tag_records
                    (status, guild_id, target_user_id, message_link, reason, tagger_id, tagger_name, expire_at_epoch, expire_input, scope_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    '正常',
                    str(guild_id),
                    str(target_user_id),
                    message_link,
                    reason,
                    str(tagger_id),
                    tagger_name,
                    int(expire_at_epoch),
                    expire_input,
                    int(scope_id)
                ))
                return cur.lastrowid

        return await asyncio.to_thread(_write)

    async def _fetch_record_by_id(self, record_id: int) -> dict[str, Any] | None:
        def _read() -> dict[str, Any] | None:
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute('''
                    SELECT
                    ''' + RECORD_SELECT_COLUMNS + '''
                    FROM tag_records
                    WHERE id = ?
                ''', (record_id,))
                row = cur.fetchone()
                return self._row_to_record(row) if row else None

        return await asyncio.to_thread(_read)

    async def _clear_record_by_id(self, record_id: int) -> bool:
        def _clear() -> bool:
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute('UPDATE tag_records SET status = ? WHERE id = ? AND status = ?', ('已清除', record_id, '正常'))
                return cur.rowcount > 0

        return await asyncio.to_thread(_clear)

    async def _list_recent_normal_records(self, guild_id: int, limit: int = 10) -> list[dict[str, Any]]:
        def _read() -> list[dict[str, Any]]:
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute('''
                    SELECT
                    ''' + RECORD_SELECT_COLUMNS + '''
                    FROM tag_records
                    WHERE guild_id = ? AND status = '正常'
                    ORDER BY id DESC
                    LIMIT ?
                ''', (str(guild_id), limit))
                return [self._row_to_record(row) for row in cur.fetchall()]

        return await asyncio.to_thread(_read)

    async def _list_user_normal_records(self, guild_id: int, user_id: int) -> list[dict[str, Any]]:
        def _read() -> list[dict[str, Any]]:
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute('''
                    SELECT
                    ''' + RECORD_SELECT_COLUMNS + '''
                    FROM tag_records
                    WHERE guild_id = ? AND target_user_id = ? AND status = '正常'
                    ORDER BY id DESC
                ''', (str(guild_id), str(user_id)))
                return [self._row_to_record(row) for row in cur.fetchall()]

        return await asyncio.to_thread(_read)

    async def _list_all_records_of_guild(self, guild_id: int) -> list[dict[str, Any]]:
        def _read() -> list[dict[str, Any]]:
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute('''
                    SELECT
                    ''' + RECORD_SELECT_COLUMNS + '''
                    FROM tag_records
                    WHERE guild_id = ?
                    ORDER BY id DESC
                ''', (str(guild_id),))
                return [self._row_to_record(row) for row in cur.fetchall()]

        return await asyncio.to_thread(_read)

    async def _expiry_scan_once(self) -> int:
        """
        过期扫描：将 status='正常' 且 expire_at_epoch!=-1 且 expire_at_epoch<=当前 的记录批量更新为 '已清除'
        返回受影响行数
        """
        def _scan() -> int:
            now_epoch = int(datetime.now(timezone.utc).timestamp())
            with self._get_conn() as conn:
                cur = conn.cursor()
                cur.execute('''
                    UPDATE tag_records
                    SET status = '已清除'
                    WHERE status = '正常'
                      AND expire_at_epoch != -1
                      AND expire_at_epoch <= ?
                ''', (now_epoch,))
                return cur.rowcount

        return await asyncio.to_thread(_scan)
