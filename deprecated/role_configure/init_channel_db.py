"""Initialize per-channel SQLite database for RoleConfigure stats.

Usage:
  python deprecated/role_configure/init_channel_db.py <channel_id>

This creates/updates: data/role_configure/<channel_id>.db
Schema:
  - daily_user_stats(user_id, date, msg_count, mention_count)  PK(user_id,date)
  - meta(key,value)  (optional, for incremental backfill/update commands)

Notes:
- Uses standard library sqlite3 only.
- Safe to run multiple times (CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS).
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from paths import ROLE_CONFIGURE_DIR as DATA_ROLE_CONFIGURE_DIR  # noqa: E402


def ensure_channel_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()

        # Main table: per-day aggregation
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_user_stats (
              user_id TEXT NOT NULL,
              date TEXT NOT NULL,
              msg_count INTEGER NOT NULL,
              mention_count INTEGER NOT NULL,
              PRIMARY KEY (user_id, date)
            )
            """
        )

        # Index for range queries / cleanup by date
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_daily_user_stats_date
            ON daily_user_stats(date)
            """
        )

        # Meta table for incremental update/backfill
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
              key TEXT PRIMARY KEY,
              value TEXT
            )
            """
        )

        conn.commit()
    finally:
        conn.close()


def _parse_channel_id(argv: list[str]) -> int:
    if len(argv) != 2:
        raise SystemExit("Usage: python deprecated/role_configure/init_channel_db.py <channel_id>")

    try:
        channel_id = int(argv[1])
    except ValueError as e:
        raise SystemExit(f"Invalid channel_id: {argv[1]}") from e

    if channel_id <= 0:
        raise SystemExit("channel_id must be positive")

    return channel_id


def main() -> None:
    channel_id = _parse_channel_id(sys.argv)
    db_path = DATA_ROLE_CONFIGURE_DIR / f"{channel_id}.db"

    ensure_channel_db(db_path)

    size = db_path.stat().st_size if db_path.exists() else 0
    print(f"OK: initialized channel db: {db_path} (size={size} bytes)")


if __name__ == "__main__":
    # On Windows, ensure relative path operations are stable even if launched elsewhere.
    os.chdir(ROOT_DIR)
    main()
