import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from cogs import utils


class DummyCommand:
    def __init__(self, name: str = "dummy"):
        self.name = name


class DummyUser:
    def __init__(self, user_id: int = 1, name: str = "tester"):
        self.id = user_id
        self.name = name


class DummyClient:
    def __init__(self, admins=None, trusted_users=None):
        self.admins = admins or []
        self.trusted_users = trusted_users or []


class DummyResponse:
    def __init__(self):
        self._done = False
        self.defer_calls = []

    def is_done(self) -> bool:
        return self._done

    async def defer(self, *, ephemeral: bool):
        self._done = True
        self.defer_calls.append(ephemeral)


class DummyInteraction:
    def __init__(self, *, user_id: int = 1, admins=None, trusted_users=None, command_name: str = "dummy"):
        self.user = DummyUser(user_id=user_id)
        self.client = DummyClient(admins=admins, trusted_users=trusted_users)
        self.response = DummyResponse()
        self.command = DummyCommand(command_name)


class UtilsTests(unittest.TestCase):
    def test_check_admin(self):
        interaction = DummyInteraction(user_id=42, admins=[42])
        self.assertTrue(utils.check_admin(interaction))
        self.assertFalse(utils.check_admin(DummyInteraction(user_id=7, admins=[42])))

    def test_check_admin_or_trusted(self):
        self.assertTrue(utils.check_admin_or_trusted(DummyInteraction(user_id=1, admins=[1])))
        self.assertTrue(utils.check_admin_or_trusted(DummyInteraction(user_id=2, trusted_users=[2])))
        self.assertFalse(utils.check_admin_or_trusted(DummyInteraction(user_id=3)))

    def test_get_user_tier(self):
        self.assertEqual(utils.get_user_tier(DummyClient(admins=[1]), 1), "admin")
        self.assertEqual(utils.get_user_tier(DummyClient(trusted_users=[2]), 2), "trusted")
        self.assertEqual(utils.get_user_tier(DummyClient(), 3), "other")

    def test_safe_defer_is_idempotent(self):
        interaction = DummyInteraction()

        asyncio.run(utils.safe_defer(interaction))
        asyncio.run(utils.safe_defer(interaction))

        self.assertEqual(interaction.response.defer_calls, [True])

    def test_safe_defer_respects_ephemeral_flag_and_done_state(self):
        interaction = DummyInteraction()

        asyncio.run(utils.safe_defer(interaction, ephemeral=False))
        asyncio.run(utils.safe_defer(interaction, ephemeral=True))

        self.assertEqual(interaction.response.defer_calls, [False])

    def test_encode_image_to_base64(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "tiny.png"
            Image.new("RGB", (1, 1), color=(255, 0, 0)).save(image_path)

            encoded = utils.encode_image_to_base64(str(image_path))

            self.assertTrue(encoded.startswith("data:image/png;base64,"))

    def test_encode_image_to_base64_falls_back_to_octet_stream(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = Path(temp_dir) / "payload.unknownext"
            file_path.write_bytes(b"abc123")

            encoded = utils.encode_image_to_base64(str(file_path))

            self.assertTrue(encoded.startswith("data:application/octet-stream;base64,"))

    def test_compress_image_keeps_small_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "tiny.jpg"
            Image.new("RGB", (10, 10), color=(255, 255, 255)).save(image_path, format="JPEG")

            result = asyncio.run(utils.compress_image(str(image_path), max_size_kb=250))

            self.assertEqual(result, str(image_path))

    def test_compress_image_creates_compressed_file_for_large_image(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "large.bmp"
            Image.new("RGB", (1024, 1024), color=(255, 255, 255)).save(image_path, format="BMP")

            result = asyncio.run(utils.compress_image(str(image_path), max_size_kb=50))

            self.assertTrue(result.endswith("_compressed.jpg"))
            self.assertTrue(Path(result).exists())
            self.assertLess(utils.get_file_size_kb(result), utils.get_file_size_kb(str(image_path)))

    def test_compress_image_returns_original_path_on_exception(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            image_path = Path(temp_dir) / "broken.jpg"
            image_path.write_bytes(b"not-an-image")

            with mock.patch("cogs.utils.Image.open", side_effect=OSError("broken image")):
                result = asyncio.run(utils.compress_image(str(image_path), max_size_kb=1))

            self.assertEqual(result, str(image_path))

    def test_log_slash_command_writes_file(self):
        interaction = DummyInteraction(user_id=123, command_name="ping")

        with tempfile.TemporaryDirectory() as temp_dir:
            original_log_file = utils.COMMAND_LOG_FILE
            utils.COMMAND_LOG_FILE = Path(temp_dir) / "runtime" / "logs" / "log.txt"
            try:
                utils.log_slash_command(interaction, True)
                log_path = Path(utils.COMMAND_LOG_FILE)
                self.assertTrue(log_path.exists())
                content = log_path.read_text(encoding="utf-8")
            finally:
                utils.COMMAND_LOG_FILE = original_log_file

        self.assertIn("123", content)
        self.assertIn("/ping", content)
        self.assertIn("成功", content)

    def test_log_slash_command_writes_failure_status_for_unknown_command(self):
        interaction = DummyInteraction(user_id=456)
        interaction.command = None

        with tempfile.TemporaryDirectory() as temp_dir:
            original_log_file = utils.COMMAND_LOG_FILE
            utils.COMMAND_LOG_FILE = Path(temp_dir) / "runtime" / "logs" / "log.txt"
            try:
                utils.log_slash_command(interaction, False)
                content = Path(utils.COMMAND_LOG_FILE).read_text(encoding="utf-8")
            finally:
                utils.COMMAND_LOG_FILE = original_log_file

        self.assertIn("456", content)
        self.assertIn("/Unknown", content)
        self.assertIn("失败", content)

    def test_ttl_cache_expires_entry(self):
        cache = utils.TTLCache[str]()
        cache.set("alpha", "value", ttl_seconds=0.01)

        self.assertEqual(cache.get("alpha"), "value")

        time.sleep(0.03)

        self.assertIsNone(cache.get("alpha"))
        self.assertNotIn("alpha", cache)

    def test_cooldown_manager_check_and_update(self):
        manager = utils.CooldownManager(default_seconds=0.05)

        first_hit = manager.check_and_update("message-1")
        second_hit = manager.check("message-1")

        self.assertEqual(first_hit, (False, 0))
        self.assertTrue(second_hit[0])
        self.assertGreaterEqual(second_hit[1], 1)

    def test_cooldown_manager_cleans_expired_entries(self):
        manager = utils.CooldownManager(default_seconds=0.01)
        manager.set_cooldown("user-1")

        time.sleep(0.03)

        self.assertEqual(manager.check("user-1"), (False, 0))


if __name__ == "__main__":
    unittest.main()
