from __future__ import annotations

import discord
import asyncio
import os
import sqlite3
from datetime import datetime
import json
from typing import Any

from paths import QUICK_PUNISH_DB

QUICK_PUNISH_DB_PATH = str(QUICK_PUNISH_DB)

class QuickPunishDBMixin:
    def init_database(self):
        """初始化数据库并执行幂等迁移"""
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
                WHERE status = 'executed'
                  AND (original_message_link IS NULL OR TRIM(original_message_link) = '')
                  AND (removed_roles IS NULL OR TRIM(removed_roles) = '' OR TRIM(removed_roles) = '[]')
            """)
            cursor.execute("""
                UPDATE quick_punish_records
                SET removed_roles_by_guild = '{}'
                WHERE removed_roles_by_guild IS NULL OR TRIM(removed_roles_by_guild) = ''
            """)

            cursor.execute('''
                CREATE TABLE IF NOT EXISTS quick_punish_votes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL UNIQUE,
                    vote_message_id TEXT,
                    target_message_link TEXT,
                    target_user_id TEXT,
                    executor_id TEXT NOT NULL,
                    reason TEXT,
                    approver_ids TEXT DEFAULT '[]',
                    rejecter_ids TEXT DEFAULT '[]',
                    status TEXT DEFAULT 'pending',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    decided_at DATETIME
                )
            ''')
            # Vote rows are never pruned, so the panel-message lookup needs its own index.
            cursor.execute('''
                CREATE INDEX IF NOT EXISTS idx_quick_punish_votes_message
                ON quick_punish_votes (vote_message_id)
            ''')

    def _parse_json_list(self, value: Any) -> list[int]:
        if isinstance(value, list):
            return [int(x) for x in value if str(x).strip().isdigit()]
        if value is None:
            return []
        try:
            loaded = json.loads(value) if isinstance(value, str) else value
            if isinstance(loaded, list):
                return [int(x) for x in loaded if str(x).strip().isdigit()]
        except Exception:
            pass
        return []

    def _parse_json_str_list(self, value: Any) -> list[str]:
        """Same JSON id-list contract as `_parse_json_list`, kept in string form."""
        return [str(x) for x in self._parse_json_list(value)]

    def _parse_json_roles_by_guild(self, value: Any) -> dict[str, list[int]]:
        if value is None:
            return {}
        try:
            loaded = json.loads(value) if isinstance(value, str) else value
        except Exception:
            loaded = {}

        if not isinstance(loaded, dict):
            return {}

        result: dict[str, list[int]] = {}
        for gid, roles in loaded.items():
            gid_str = str(gid).strip()
            if not gid_str:
                continue
            result[gid_str] = self._parse_json_list(roles)
        return result

    def _compute_next_punish_count_with_cursor(self, cursor: sqlite3.Cursor, user_id: str) -> int:
        cursor.execute(
            "SELECT MAX(COALESCE(punish_count, 0)) FROM quick_punish_records WHERE user_id = ? AND status != 'failed'",
            (user_id,)
        )
        row = cursor.fetchone()
        last = 0
        if row and row[0] is not None:
            try:
                last = int(row[0])
            except Exception:
                last = 0
        return max(1, last + 1)

    async def get_punish_count(self, user_id: str) -> int:
        """获取用户被处罚次数（executed）"""
        def _read_count() -> int:
            with sqlite3.connect(QUICK_PUNISH_DB_PATH) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT COUNT(*) FROM quick_punish_records WHERE user_id = ? AND status = 'executed'",
                    (user_id,)
                )
                row = cursor.fetchone()
                return int(row[0]) if row and row[0] is not None else 0

        return await asyncio.to_thread(_read_count)

    async def get_user_punishment_history(self, user_id: str, limit: int = 5) -> list[dict]:
        """获取用户的处罚历史记录"""
        def _read_history() -> list[dict]:
            with sqlite3.connect(QUICK_PUNISH_DB_PATH) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT id, punish_count, timestamp, reason, executor_name, status, source_type
                    FROM quick_punish_records
                    WHERE user_id = ?
                    ORDER BY timestamp DESC, id DESC
                    LIMIT ?
                ''', (user_id, limit))

                records = []
                for row in cursor.fetchall():
                    records.append({
                        'id': row[0],
                        'punish_count': row[1],
                        'timestamp': row[2],
                        'reason': row[3],
                        'executor_name': row[4],
                        'status': row[5],
                        'source_type': row[6] or 'local'
                    })
                return records

        return await asyncio.to_thread(_read_history)

    async def log_to_database_with_count(self, user: discord.User, message: discord.Message,
                                        executor: discord.User, reason: str, removed_roles: list[int],
                                        punish_count: int, status: str = "executed",
                                        source_type: str = "local",
                                        removed_roles_by_guild: dict[str, list[int]] | None = None,
                                        source_guild_id: str | None = None) -> tuple[int, int]:
        """记录处罚信息到数据库（使用事务确保原子性）"""
        def _write_record() -> tuple[int, int]:
            try:
                with sqlite3.connect(QUICK_PUNISH_DB_PATH) as conn:
                    cursor = conn.cursor()

                    resolved_count = punish_count
                    if resolved_count == 0:
                        resolved_count = self._compute_next_punish_count_with_cursor(cursor, str(user.id))

                    message_link = None
                    msg_id = None
                    channel_id = None
                    channel_name = None
                    if message:
                        msg_id = str(message.id)
                        channel_id = str(message.channel.id)
                        channel_name = getattr(message.channel, "name", None)
                        if message.guild and message.channel:
                            message_link = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message.id}"

                    rrbg = removed_roles_by_guild or {}
                    cursor.execute('''
                        INSERT INTO quick_punish_records
                        (user_id, user_name, punish_count, timestamp, original_message_id, original_message_link,
                         channel_id, channel_name, executor_id, executor_name, reason, removed_roles, status,
                         source_type, removed_roles_by_guild, source_guild_id)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        str(user.id),
                        user.name,
                        resolved_count,
                        datetime.now().isoformat(),
                        msg_id,
                        message_link,
                        channel_id,
                        channel_name,
                        str(executor.id),
                        executor.name,
                        reason,
                        json.dumps(removed_roles),
                        status,
                        source_type,
                        json.dumps(rrbg, ensure_ascii=False),
                        source_guild_id
                    ))
                    record_id = cursor.lastrowid
                    if record_id is None:
                        raise RuntimeError("数据库未返回处罚记录 ID")
                    return record_id, resolved_count
            except Exception as e:
                print(f"数据库事务错误: {e}")
                raise

        return await asyncio.to_thread(_write_record)

    async def get_punishments_for_user(self, user_id: str) -> list[dict]:
        """Return every stored status for one user, newest first."""
        def _read_records() -> list[dict]:
            with sqlite3.connect(QUICK_PUNISH_DB_PATH) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT id, user_id, user_name, punish_count, timestamp,
                           reason, executor_name, status, source_type, original_message_link
                    FROM quick_punish_records
                    WHERE user_id = ?
                    ORDER BY timestamp DESC, id DESC
                ''', (user_id,))

                records = []
                for row in cursor.fetchall():
                    records.append({
                        'id': row[0],
                        'user_id': row[1],
                        'user_name': row[2],
                        'punish_count': row[3],
                        'timestamp': row[4],
                        'reason': row[5],
                        'executor_name': row[6],
                        'status': row[7],
                        'source_type': row[8] or 'local',
                        'original_message_link': row[9],
                    })
                return records

        return await asyncio.to_thread(_read_records)

    def _row_to_record(self, row: tuple) -> dict[str, Any]:
        return {
            'id': row[0],
            'user_id': row[1],
            'user_name': row[2],
            'timestamp': row[3],
            'original_message_id': row[4],
            'original_message_link': row[5],
            'channel_id': row[6],
            'channel_name': row[7],
            'executor_id': row[8],
            'executor_name': row[9],
            'reason': row[10],
            'removed_roles': self._parse_json_list(row[11]),
            'status': row[12],
            'source_type': (row[13] or 'local') if len(row) > 13 else 'local',
            'removed_roles_by_guild': self._parse_json_roles_by_guild(row[14] if len(row) > 14 else {}),
            'source_guild_id': row[15] if len(row) > 15 else None
        }

    def _has_restore_basis(self, record: dict[str, Any]) -> bool:
        by_guild = record.get('removed_roles_by_guild', {})
        if isinstance(by_guild, dict) and any(v for v in by_guild.values()):
            return True
        return bool(record.get('removed_roles'))

    async def get_last_punishment_for_user(self, user_id: str) -> dict | None:
        """获取用户最近一次 executed 处罚记录"""
        def _read_last_record() -> dict[str, Any] | None:
            with sqlite3.connect(QUICK_PUNISH_DB_PATH) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT id, user_id, user_name, timestamp, original_message_id,
                           original_message_link, channel_id, channel_name,
                           executor_id, executor_name, reason, removed_roles, status,
                           source_type, removed_roles_by_guild, source_guild_id
                    FROM quick_punish_records
                    WHERE user_id = ? AND status = 'executed'
                    ORDER BY timestamp DESC, id DESC
                    LIMIT 1
                ''', (user_id,))
                row = cursor.fetchone()
                return self._row_to_record(row) if row else None

        return await asyncio.to_thread(_read_last_record)

    async def get_last_revocable_local_record_for_user(self, user_id: str) -> dict[str, Any] | None:
        """获取最近可撤销（有恢复依据）的 local executed 记录"""
        def _read_revocable_record() -> dict[str, Any] | None:
            with sqlite3.connect(QUICK_PUNISH_DB_PATH) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT id, user_id, user_name, timestamp, original_message_id,
                           original_message_link, channel_id, channel_name,
                           executor_id, executor_name, reason, removed_roles, status,
                           source_type, removed_roles_by_guild, source_guild_id
                    FROM quick_punish_records
                    WHERE user_id = ? AND status = 'executed' AND COALESCE(source_type, 'local') = 'local'
                    ORDER BY timestamp DESC, id DESC
                    LIMIT 30
                ''', (user_id,))
                rows = cursor.fetchall()

            for row in rows:
                record = self._row_to_record(row)
                if self._has_restore_basis(record):
                    return record
            return None

        return await asyncio.to_thread(_read_revocable_record)

    async def revoke_punishment(self, record_id: int) -> bool:
        """撤销处罚记录（更新状态为revoked）"""
        def _revoke_record() -> bool:
            with sqlite3.connect(QUICK_PUNISH_DB_PATH) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE quick_punish_records
                    SET status = 'revoked'
                    WHERE id = ? AND status = 'executed'
                ''', (record_id,))
                return cursor.rowcount > 0

        return await asyncio.to_thread(_revoke_record)

    # Single source for the vote column order shared by every SELECT and `_row_to_vote`.
    _VOTE_COLUMNS = (
        'id, record_id, vote_message_id, target_message_link, target_user_id, '
        'executor_id, reason, approver_ids, rejecter_ids, status, created_at, decided_at'
    )

    def _row_to_vote(self, row: tuple) -> dict[str, Any]:
        return {
            'id': row[0],
            'record_id': row[1],
            'vote_message_id': row[2],
            'target_message_link': row[3],
            'target_user_id': row[4],
            'executor_id': row[5],
            'reason': row[6],
            'approver_ids': self._parse_json_str_list(row[7]),
            'rejecter_ids': self._parse_json_str_list(row[8]),
            'status': row[9],
            'created_at': row[10],
            'decided_at': row[11],
        }

    async def create_vote(self, *, record_id: int, vote_message_id: str, target_message_link: str,
                          target_user_id: str, executor_id: str, reason: str) -> None:
        """写入一条投票记录（record_id 唯一约束防重复面板）"""
        def _insert_vote() -> None:
            with sqlite3.connect(QUICK_PUNISH_DB_PATH) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO quick_punish_votes
                    (record_id, vote_message_id, target_message_link, target_user_id,
                     executor_id, reason)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (record_id, vote_message_id, target_message_link, target_user_id,
                      executor_id, reason))

        await asyncio.to_thread(_insert_vote)

    async def _fetch_vote(self, where_clause: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
        """Fetch one vote row; `where_clause` is an internal literal, never user input."""
        def _read_vote() -> dict[str, Any] | None:
            with sqlite3.connect(QUICK_PUNISH_DB_PATH) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    f"SELECT {self._VOTE_COLUMNS} FROM quick_punish_votes WHERE {where_clause}",
                    params,
                )
                row = cursor.fetchone()
                return self._row_to_vote(row) if row else None

        return await asyncio.to_thread(_read_vote)

    async def get_vote_by_record_id(self, record_id: int) -> dict[str, Any] | None:
        return await self._fetch_vote("record_id = ?", (record_id,))

    async def get_vote_by_message_id(self, vote_message_id: str) -> dict[str, Any] | None:
        """按投票面板消息ID定位投票（持久视图回调的查表键）"""
        return await self._fetch_vote("vote_message_id = ?", (vote_message_id,))

    async def update_vote_progress(self, record_id: int, *,
                                   approver_ids: list[str], rejecter_ids: list[str]) -> bool:
        """更新仍在进行中的投票的票数明细"""
        def _update_progress() -> bool:
            with sqlite3.connect(QUICK_PUNISH_DB_PATH) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE quick_punish_votes
                    SET approver_ids = ?, rejecter_ids = ?
                    WHERE record_id = ? AND status = 'pending'
                ''', (json.dumps(approver_ids), json.dumps(rejecter_ids), record_id))
                return cursor.rowcount > 0

        return await asyncio.to_thread(_update_progress)

    async def decide_vote(self, record_id: int, *, status: str,
                          approver_ids: list[str], rejecter_ids: list[str]) -> bool:
        """终态落库（仅允许从 pending 迁移，防止并发下二次判定）"""
        def _decide_vote() -> bool:
            with sqlite3.connect(QUICK_PUNISH_DB_PATH) as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE quick_punish_votes
                    SET status = ?, approver_ids = ?, rejecter_ids = ?,
                        decided_at = CURRENT_TIMESTAMP
                    WHERE record_id = ? AND status = 'pending'
                ''', (status, json.dumps(approver_ids), json.dumps(rejecter_ids), record_id))
                return cursor.rowcount > 0

        return await asyncio.to_thread(_decide_vote)

    def _build_restore_targets(self, record: dict[str, Any], fallback_guild_id: int | None) -> dict[str, list[int]]:
        by_guild = record.get("removed_roles_by_guild", {}) or {}
        if by_guild:
            restored: dict[str, list[int]] = {}
            for gid, roles in by_guild.items():
                parsed_roles = self._parse_json_list(roles)
                if parsed_roles:
                    restored[str(gid)] = parsed_roles
            if restored:
                return restored

        legacy_roles = self._parse_json_list(record.get("removed_roles", []))
        if not legacy_roles:
            return {}

        source_gid = record.get("source_guild_id") or (str(fallback_guild_id) if fallback_guild_id else None)
        if not source_gid:
            return {}
        return {str(source_gid): legacy_roles}
