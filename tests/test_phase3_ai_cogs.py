import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from cogs.appdayi import AppDayi
from cogs.summary import SUMMARY_TIMEOUT_SECONDS, Summary


class DummyTree:
    def add_command(self, _command):
        return None

    def remove_command(self, _name, type=None):
        return None


class DummyBot:
    def __init__(self):
        self.tree = DummyTree()


class AppDayiPhase3Tests(unittest.TestCase):
    def setUp(self):
        self.cog = AppDayi(DummyBot())

    def test_banlist_cache_refreshes_when_mtime_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            banlist_path = os.path.join(temp_dir, "banlist.json")
            first_payload = {"banlist": [{"ID": "42", "reason": "first", "unbanned_at": int(time.time()) + 3600}]}
            second_payload = {"banlist": [{"ID": "42", "reason": "second", "unbanned_at": int(time.time()) + 3600}]}

            with open(banlist_path, "w", encoding="utf-8") as fh:
                json.dump(first_payload, fh)

            with mock.patch.object(self.cog, "_get_banlist_path", return_value=banlist_path):
                self.assertEqual(self.cog._get_active_ban_entry("42")["reason"], "first")

                with open(banlist_path, "w", encoding="utf-8") as fh:
                    json.dump(second_payload, fh)
                updated_mtime = os.path.getmtime(banlist_path) + 5
                os.utime(banlist_path, (updated_mtime, updated_mtime))

                self.assertEqual(self.cog._get_active_ban_entry("42")["reason"], "second")

    def test_default_prompt_cache_refreshes_when_mtime_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            prompt_path = os.path.join(temp_dir, "ALL.txt")
            with open(prompt_path, "w", encoding="utf-8") as fh:
                fh.write("first prompt")

            with mock.patch.object(self.cog, "_get_default_prompt_path", return_value=prompt_path):
                self.assertEqual(self.cog._load_default_prompt(), "first prompt")

                with open(prompt_path, "w", encoding="utf-8") as fh:
                    fh.write("second prompt")
                updated_mtime = os.path.getmtime(prompt_path) + 5
                os.utime(prompt_path, (updated_mtime, updated_mtime))

                self.assertEqual(self.cog._load_default_prompt(), "second prompt")

    def test_archive_prompt_uses_asyncio_to_thread(self):
        turns = [{"role": "user", "message": mock.Mock(content="hello", attachments=[]), "is_current": True, "image_paths": []}]
        write_mock = mock.Mock(return_value="runtime/save/mock.txt")
        self.cog._write_prompt_archive = write_mock
        captured = {}

        async def fake_to_thread(func, *args):
            captured["func"] = func
            captured["args"] = args
            return func(*args)

        with mock.patch("cogs.appdayi.asyncio.to_thread", side_effect=fake_to_thread):
            asyncio.run(self.cog._archive_prompt(7, turns, "system prompt"))

        self.assertIs(captured["func"], write_mock)
        self.assertEqual(captured["args"][0], 7)
        write_mock.assert_called_once_with(7, turns, "system prompt")


class SummaryPhase3Tests(unittest.TestCase):
    def setUp(self):
        self.cog = Summary(DummyBot())

    def test_summary_model_prefers_summary_model_then_openai_model(self):
        with mock.patch.dict(os.environ, {"OPENAI_MODEL": "base-model"}, clear=False):
            self.assertEqual(self.cog._get_summary_model(), "base-model")

        with mock.patch.dict(os.environ, {"OPENAI_MODEL": "base-model", "SUMMARY_MODEL": "summary-model"}, clear=False):
            self.assertEqual(self.cog._get_summary_model(), "summary-model")

    def test_summary_display_name_uses_env_override(self):
        with mock.patch.dict(
            os.environ,
            {"OPENAI_MODEL": "base-model", "SUMMARY_MODEL": "summary-model", "SUMMARY_DISPLAY_NAME": "Friendly Summary"},
            clear=False,
        ):
            self.assertEqual(self.cog._get_summary_display_name(), "Friendly Summary")

    def test_summary_timeout_message_matches_timeout_constant(self):
        expected_minutes = int(SUMMARY_TIMEOUT_SECONDS // 60)
        self.assertEqual(
            self.cog._get_summary_timeout_message(),
            f"⏱️ AI分析超时（超过{expected_minutes}分钟），请减少消息数量后重试。",
        )


if __name__ == "__main__":
    unittest.main()
