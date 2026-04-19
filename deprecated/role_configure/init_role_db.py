"""Initialize SQLite database for timed role memberships.

Usage:
  python deprecated/role_configure/init_role_db.py

This creates/updates: data/db/timed_role_members.db
Schema (final, includes duration_days for audit log reasons):
  timed_role_members(
    user_id TEXT NOT NULL,
    role_id TEXT NOT NULL,
    expire_ts INTEGER NOT NULL,          -- UTC seconds
    restore_role_id TEXT,                -- nullable
    duration_days INTEGER NOT NULL,       -- original duration in days
    PRIMARY KEY (user_id, role_id)
  )

Index:
  idx_timed_role_members_expire_ts(expire_ts)

Notes:
- Uses standard library sqlite3 only.
- Safe to run multiple times.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from paths import TIMED_ROLE_DB  # noqa: E402

DB_PATH = TIMED_ROLE_DB


def ensure_timed_role_db(db_path: Path = DB_PATH) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS timed_role_members (
              user_id TEXT NOT NULL,
              role_id TEXT NOT NULL,
              expire_ts INTEGER NOT NULL,
              restore_role_id TEXT,
              duration_days INTEGER NOT NULL,
              PRIMARY KEY (user_id, role_id)
            )
            """
        )

        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_timed_role_members_expire_ts
            ON timed_role_members(expire_ts)
            """
        )

        conn.commit()
    finally:
        conn.close()


def main() -> None:
    ensure_timed_role_db(DB_PATH)
    size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    print(f"OK: initialized timed role db: {DB_PATH} (size={size} bytes)")


if __name__ == "__main__":
    # On Windows, ensure relative path operations are stable even if launched elsewhere.
    os.chdir(ROOT_DIR)
    main()
