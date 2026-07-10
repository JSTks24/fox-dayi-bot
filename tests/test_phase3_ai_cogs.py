import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from cogs.appdayi import AppDayi
from cogs.appdayi.stream import MAX_IMAGE_ATTACHMENTS
from cogs.summary import SUMMARY_TIMEOUT_SECONDS, Summary


class DummyTree:
    def __init__(self):
        self.added = []
        self.removed = []

    def add_command(self, command, *, guild=None):
        self.added.append((command, guild))
        return None

    def remove_command(self, name, *, guild=None, type=None):
        self.removed.append((name, guild, type))
        return None


class DummyBot:
    def __init__(self):
        self.tree = DummyTree()
        self.admins = []
        self.trusted_users = []
        self.openai_client = None


class AppDayiPhase3Tests(unittest.TestCase):
    def setUp(self):
        self.cog = AppDayi(DummyBot())

    def _make_image_attachment(self, index: int) -> SimpleNamespace:
        return SimpleNamespace(
            content_type="image/png",
            filename=f"image_{index}.png",
            size=1024,
        )

    def test_context_menus_are_registered_and_removed_per_guild(self):
        bot = DummyBot()

        with mock.patch.dict(os.environ, {"BOT_SHOULD_IN_GUILD_IDS": "111,222"}, clear=False):
            cog = AppDayi(bot)
            asyncio.run(cog.cog_unload())

        self.assertEqual(
            [(command.name, guild.id) for command, guild in bot.tree.added],
            [("快速答疑", 111), ("联网答疑", 111), ("快速答疑", 222), ("联网答疑", 222)],
        )
        self.assertEqual(
            [(name, guild.id) for name, guild, _command_type in bot.tree.removed],
            [("快速答疑", 111), ("联网答疑", 111), ("快速答疑", 222), ("联网答疑", 222)],
        )

    def test_quick_dayi_no_longer_blocks_target_user_with_banlist(self):
        bot = DummyBot()
        bot.trusted_users = [7]
        cog = AppDayi(bot)
        interaction = SimpleNamespace(user=SimpleNamespace(id=7))
        message = SimpleNamespace(author=SimpleNamespace(id=42, name="target"), attachments=[])
        send_public_error = mock.AsyncMock()
        cog._send_public_error = send_public_error

        with (
            mock.patch.dict(os.environ, {"OPENAI_MODEL": "test-model"}, clear=False),
            mock.patch("cogs.appdayi.core.safe_defer", new=mock.AsyncMock()),
        ):
            asyncio.run(cog.quick_dayi(interaction, message))

        send_public_error.assert_awaited_once()
        error_call = send_public_error.await_args
        assert error_call is not None
        self.assertEqual(error_call.args[1], message)
        self.assertIn("AI 服务尚未正确初始化", error_call.args[2])

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

    def test_cleanup_prompt_archives_keeps_newest_five_txt_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            old_txt_paths = []
            for index in range(7):
                path = os.path.join(temp_dir, f"archive_{index}.txt")
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(str(index))
                os.utime(path, (index, index))
                old_txt_paths.append(path)

            keep_file = os.path.join(temp_dir, "notes.json")
            Path(keep_file).write_text("{}", encoding="utf-8")

            deleted_paths = self.cog._cleanup_prompt_archives(temp_dir)

            self.assertEqual({os.path.basename(path) for path in deleted_paths}, {"archive_0.txt", "archive_1.txt"})
            self.assertEqual(
                sorted(path.name for path in Path(temp_dir).glob("*.txt")),
                [f"archive_{index}.txt" for index in range(2, 7)],
            )
            self.assertTrue(os.path.exists(keep_file))

    def test_quick_dayi_allows_five_images_before_service_check(self):
        bot = DummyBot()
        bot.trusted_users = [7]
        cog = AppDayi(bot)
        interaction = SimpleNamespace(user=SimpleNamespace(id=7))
        message = SimpleNamespace(
            id=99,
            author=SimpleNamespace(id=42, name="target"),
            attachments=[self._make_image_attachment(index) for index in range(MAX_IMAGE_ATTACHMENTS)],
        )
        send_public_error = mock.AsyncMock()
        cog._send_public_error = send_public_error

        with (
            mock.patch.dict(os.environ, {"OPENAI_MODEL": "test-model"}, clear=False),
            mock.patch("cogs.appdayi.core.safe_defer", new=mock.AsyncMock()),
        ):
            asyncio.run(cog.quick_dayi(interaction, message))

        send_public_error.assert_awaited_once()
        error_call = send_public_error.await_args
        assert error_call is not None
        self.assertIn("AI 服务尚未正确初始化", error_call.args[2])

    def test_quick_dayi_blocks_more_than_five_images(self):
        bot = DummyBot()
        bot.trusted_users = [7]
        cog = AppDayi(bot)
        interaction = SimpleNamespace(user=SimpleNamespace(id=7))
        message = SimpleNamespace(
            id=99,
            author=SimpleNamespace(id=42, name="target"),
            attachments=[self._make_image_attachment(index) for index in range(MAX_IMAGE_ATTACHMENTS + 1)],
        )
        send_public_error = mock.AsyncMock()
        cog._send_public_error = send_public_error

        with (
            mock.patch.dict(os.environ, {"OPENAI_MODEL": "test-model"}, clear=False),
            mock.patch("cogs.appdayi.core.safe_defer", new=mock.AsyncMock()),
        ):
            asyncio.run(cog.quick_dayi(interaction, message))

        send_public_error.assert_awaited_once()
        error_call = send_public_error.await_args
        assert error_call is not None
        self.assertIn(f"系统最多支持 {MAX_IMAGE_ATTACHMENTS} 张图片", error_call.args[2])


class SummaryPhase3Tests(unittest.TestCase):
    def setUp(self):
        self.cog = Summary(DummyBot())

    def test_summary_model_prefers_summary_model_then_openai_model(self):
        with mock.patch.dict(os.environ, {"OPENAI_MODEL": "base-model", "SUMMARY_MODEL": ""}, clear=False):
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
