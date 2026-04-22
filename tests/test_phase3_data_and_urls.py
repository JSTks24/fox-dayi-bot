import json
import threading
import unittest
from pathlib import Path

from cogs import get_context, pending_questions_reviewer, recognize_url, role_sync
from tests.conftest import temporary_workdir


class RecognizeUrlTests(unittest.TestCase):
    def test_normalize_url_keeps_path_and_non_default_port(self):
        cog = object.__new__(recognize_url.RecognizeURL)

        self.assertEqual(
            cog._normalize_url("https://Example.com:443/api/v1/?q=1#frag"),
            ("example.com", "/api/v1"),
        )
        self.assertEqual(
            cog._normalize_url("http://Example.com:8080/path/"),
            ("example.com:8080", "/path"),
        )
        self.assertEqual(cog._normalize_url("Example.com/path/"), ("example.com", "/path"))

    def test_save_json_writes_expected_content(self):
        cog = object.__new__(recognize_url.RecognizeURL)
        cog._json_write_lock = threading.Lock()

        with temporary_workdir() as temp_dir:
            file_path = Path(temp_dir) / "api_table" / "good.json"
            data = {"good": {"example.com/api": ["name", "desc"]}}

            self.assertTrue(cog._save_json(str(file_path), data))

            self.assertEqual(json.loads(file_path.read_text(encoding="utf-8")), data)


class PendingReviewerTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_and_prepare_batch_returns_four_tuple_on_early_exit(self):
        cog = object.__new__(pending_questions_reviewer.UnansweredFilter)
        original_target = pending_questions_reviewer.TARGET_FORUM_ID

        try:
            pending_questions_reviewer.TARGET_FORUM_ID = 0
            self.assertEqual(await cog._fetch_and_prepare_batch(), (None, None, [], []))

            pending_questions_reviewer.TARGET_FORUM_ID = 123
            cog.bot = type("Bot", (), {"get_channel": lambda self, _channel_id: object()})()
            self.assertEqual(await cog._fetch_and_prepare_batch(), (None, None, [], []))
        finally:
            pending_questions_reviewer.TARGET_FORUM_ID = original_target


class GetContextFileHelperTests(unittest.TestCase):
    def test_temp_file_helpers_round_trip(self):
        cog = object.__new__(get_context.GetContextCog)

        with temporary_workdir() as temp_dir:
            original_temp_path = get_context.CONTEXT_TEMP_PATH
            get_context.CONTEXT_TEMP_PATH = str(Path(temp_dir) / "runtime" / "temp" / "context")
            try:
                filepath = Path(
                    cog._create_temp_file(
                        [
                            {
                                "username": "Tester",
                                "content": "hello",
                                "timestamp": None,
                                "author_id": 1,
                            }
                        ],
                        42,
                    )
                )

                content = cog._read_file_bytes(str(filepath)).decode("utf-8")
                self.assertIn("Tester: hello", content)

                cog._delete_file_if_exists(str(filepath))
                self.assertFalse(filepath.exists())
            finally:
                get_context.CONTEXT_TEMP_PATH = original_temp_path


class RoleSyncConfigTests(unittest.TestCase):
    def test_save_config_overwrites_atomically(self):
        cog = object.__new__(role_sync.RoleSyncCog)
        cog._config_write_lock = threading.Lock()

        with temporary_workdir() as temp_dir:
            config_path = Path(temp_dir) / "cogs" / "config" / "role_sync_config.json"
            cog._config_path = str(config_path)
            cog.config = {"guild_id": "1", "role_ids": ["2"], "enabled": True}
            cog._save_config()

            cog.config["role_ids"] = ["2", "3"]
            cog._save_config()

            saved = json.loads(config_path.read_text(encoding="utf-8"))
            temp_files = list(config_path.parent.glob("*.tmp"))

        self.assertEqual(saved["role_ids"], ["2", "3"])
        self.assertEqual(temp_files, [])


if __name__ == "__main__":
    unittest.main()
