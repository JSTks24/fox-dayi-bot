import sqlite3
import unittest
from pathlib import Path

from cogs import role_sync
from cogs import pending_reviewer as pending_questions_reviewer
from tests.conftest import temporary_workdir


class RoleSyncDatabaseTests(unittest.TestCase):
    def test_load_trusted_users_sync_creates_table_and_returns_ints(self):
        cog = object.__new__(role_sync.RoleSyncCog)

        with temporary_workdir() as temp_dir:
            original_path = role_sync.USERS_DB_PATH
            role_sync.USERS_DB_PATH = str(Path(temp_dir) / "data" / "db" / "users.db")
            db_path = Path(role_sync.USERS_DB_PATH)
            try:
                self.assertEqual(cog._load_trusted_users_sync(), [])

                with sqlite3.connect(db_path) as conn:
                    conn.execute("INSERT INTO trusted_users (id) VALUES ('7')")
                    conn.execute("INSERT INTO trusted_users (id) VALUES ('9')")

                result = cog._load_trusted_users_sync()
            finally:
                role_sync.USERS_DB_PATH = original_path

        self.assertEqual(sorted(result), [7, 9])
        self.assertTrue(all(isinstance(user_id, int) for user_id in result))

    def test_sync_trusted_users_sync_only_inserts_missing_ids(self):
        cog = object.__new__(role_sync.RoleSyncCog)

        with temporary_workdir() as temp_dir:
            original_path = role_sync.USERS_DB_PATH
            role_sync.USERS_DB_PATH = str(Path(temp_dir) / "data" / "db" / "users.db")
            db_path = Path(role_sync.USERS_DB_PATH)
            try:
                db_path.parent.mkdir(parents=True, exist_ok=True)
                with sqlite3.connect(db_path) as conn:
                    conn.execute("CREATE TABLE trusted_users (id TEXT PRIMARY KEY)")
                    conn.execute("INSERT INTO trusted_users (id) VALUES ('2')")

                new_ids, existed_count = cog._sync_trusted_users_sync({"10", "3", "2"})
                second_new_ids, second_existed_count = cog._sync_trusted_users_sync({"10", "3", "2"})

                with sqlite3.connect(db_path) as conn:
                    stored_ids = sorted(row[0] for row in conn.execute("SELECT id FROM trusted_users"))
            finally:
                role_sync.USERS_DB_PATH = original_path

        self.assertEqual(new_ids, ["3", "10"])
        self.assertEqual(existed_count, 1)
        self.assertEqual(second_new_ids, [])
        self.assertEqual(second_existed_count, 3)
        self.assertEqual(stored_ids, ["10", "2", "3"])


class ReviewerCacheDatabaseTests(unittest.TestCase):
    def test_thread_cache_sync_helpers_round_trip_and_delete(self):
        cog = object.__new__(pending_questions_reviewer.UnansweredFilter)

        with temporary_workdir() as temp_dir:
            original_dir = pending_questions_reviewer.DB_DIR
            original_path = pending_questions_reviewer.DB_PATH
            pending_questions_reviewer.DB_DIR = str(Path(temp_dir) / "data" / "db")
            pending_questions_reviewer.DB_PATH = str(Path(temp_dir) / "data" / "db" / "unanswered.db")
            try:
                cog._ensure_db_ready()
                self.assertIsNone(cog._get_cached_thread_sync(1))

                cog._update_thread_cache_sync(1, 100, 2, "pending", "first")
                self.assertEqual(cog._get_cached_thread_sync(1), (100, 2, "pending", "first"))

                cog._update_thread_cache_sync(1, 101, 3, "resolved", "updated")
                self.assertEqual(cog._get_cached_thread_sync(1), (101, 3, "resolved", "updated"))

                cog._delete_thread_cache_sync(1)
                cog._delete_thread_cache_sync(999)
                self.assertIsNone(cog._get_cached_thread_sync(1))
            finally:
                pending_questions_reviewer.DB_DIR = original_dir
                pending_questions_reviewer.DB_PATH = original_path


if __name__ == "__main__":
    unittest.main()
