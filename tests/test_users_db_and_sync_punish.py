import sqlite3
import unittest
from pathlib import Path

from cogs import sync_punish_data, users_db
from tests.conftest import DummyBot, create_users_db, temporary_workdir


class UsersDatabaseCogTests(unittest.TestCase):
    def test_apply_permission_changes_sync_adds_and_removes_trusted_users(self):
        cog = users_db.UsersDatabaseCog(DummyBot())

        with temporary_workdir() as temp_dir:
            original_path = users_db.USERS_DB_PATH
            users_db.USERS_DB_PATH = str(Path(temp_dir) / "data" / "db" / "users.db")
            db_path = Path(users_db.USERS_DB_PATH)
            try:
                create_users_db(db_path, admins=(1,), trusted_users=(2,))

                success, already_exists, not_exists = cog._apply_permission_changes_sync(
                    group="trusted_users",
                    action="add",
                    target_user_ids=[2, 3],
                    operator_id=1,
                    operator_name="admin",
                )
                self.assertEqual((success, already_exists, not_exists), (["3"], ["2"], []))

                success, already_exists, not_exists = cog._apply_permission_changes_sync(
                    group="trusted_users",
                    action="remove",
                    target_user_ids=[3, 9],
                    operator_id=1,
                    operator_name="admin",
                )

                with sqlite3.connect(db_path) as conn:
                    trusted_users_ids = sorted(row[0] for row in conn.execute("SELECT id FROM trusted_users"))
            finally:
                users_db.USERS_DB_PATH = original_path

        self.assertEqual((success, already_exists, not_exists), (["3"], [], ["9"]))
        self.assertEqual(trusted_users_ids, ["2"])

    def test_apply_permission_changes_sync_blocks_removing_other_admins(self):
        cog = users_db.UsersDatabaseCog(DummyBot())

        with temporary_workdir() as temp_dir:
            original_path = users_db.USERS_DB_PATH
            users_db.USERS_DB_PATH = str(Path(temp_dir) / "data" / "db" / "users.db")
            try:
                create_users_db(Path(users_db.USERS_DB_PATH), admins=(1, 2))

                with self.assertRaises(PermissionError):
                    cog._apply_permission_changes_sync(
                        group="admins",
                        action="remove",
                        target_user_ids=[2],
                        operator_id=1,
                        operator_name="admin",
                    )
            finally:
                users_db.USERS_DB_PATH = original_path


class SyncPunishDataTests(unittest.TestCase):
    def test_sync_interface_record_inserts_and_deduplicates(self):
        cog = sync_punish_data.SyncPunishDataCog(DummyBot())

        with temporary_workdir() as temp_dir:
            original_path = sync_punish_data.QUICK_PUNISH_DB_PATH
            sync_punish_data.QUICK_PUNISH_DB_PATH = str(Path(temp_dir) / "data" / "db" / "quick_punish.db")
            try:
                cog.init_database()

                first_count = cog._sync_interface_record("42", "1001", "7", "bot", "2026-04-19T00:00:00", "99")
                second_count = cog._sync_interface_record("42", "1001", "7", "bot", "2026-04-19T00:00:01", "99")

                with sqlite3.connect(sync_punish_data.QUICK_PUNISH_DB_PATH) as conn:
                    rows = list(
                        conn.execute(
                            "SELECT punish_count, source_type, status FROM quick_punish_records"
                        )
                    )
            finally:
                sync_punish_data.QUICK_PUNISH_DB_PATH = original_path

        self.assertEqual(first_count, 1)
        self.assertIsNone(second_count)
        self.assertEqual(rows, [(1, "sync", "executed")])

    def test_sync_interface_record_ignores_failed_history_for_next_count(self):
        cog = sync_punish_data.SyncPunishDataCog(DummyBot())

        with temporary_workdir() as temp_dir:
            original_path = sync_punish_data.QUICK_PUNISH_DB_PATH
            sync_punish_data.QUICK_PUNISH_DB_PATH = str(Path(temp_dir) / "data" / "db" / "quick_punish.db")
            try:
                cog.init_database()
                with sqlite3.connect(sync_punish_data.QUICK_PUNISH_DB_PATH) as conn:
                    conn.execute(
                        "INSERT INTO quick_punish_records (user_id, user_name, punish_count, executor_id, executor_name, status) VALUES ('42', 'u', 4, '1', 'bot', 'executed')"
                    )
                    conn.execute(
                        "INSERT INTO quick_punish_records (user_id, user_name, punish_count, executor_id, executor_name, status) VALUES ('42', 'u', 9, '1', 'bot', 'failed')"
                    )

                next_count = cog._sync_interface_record("42", "1002", "7", "bot", "2026-04-19T00:00:02", "99")
            finally:
                sync_punish_data.QUICK_PUNISH_DB_PATH = original_path

        self.assertEqual(next_count, 5)


if __name__ == "__main__":
    unittest.main()
