import asyncio
import os
import time
import unittest
from pathlib import Path

from cogs import fox14_tagger, mention, quick_punish
from tests.conftest import temporary_workdir


class DummyTree:
    def __init__(self):
        self.removed = []

    def remove_command(self, name, *, type):
        self.removed.append((name, type))


class DummyRole:
    def __init__(self, role_id: int, name: str):
        self.id = role_id
        self.name = name


class DummyGuild:
    def __init__(self, roles=None):
        self._roles = {role.id: role for role in roles or []}

    def get_role(self, role_id: int):
        return self._roles.get(role_id)


class DummyBot:
    def __init__(self, guild=None):
        self.tree = DummyTree()
        self._guild = guild

    def get_guild(self, guild_id: int):
        return self._guild


class MentionMetadataTests(unittest.TestCase):
    def test_get_thread_metadata_returns_fresh_cache(self):
        cog = object.__new__(mention.MentionCog)

        with temporary_workdir() as temp_dir:
            metadata_dir = Path(temp_dir) / "mention" / "threadsMetadata"
            metadata_dir.mkdir(parents=True)
            metadata_path = metadata_dir / "123.txt"
            metadata_path.write_text("cached metadata", encoding="utf-8")

            cog.thread_metadata_path = str(metadata_dir)

            result = asyncio.run(cog.get_thread_metadata("123"))

        self.assertEqual(result, "cached metadata")

    def test_get_thread_metadata_returns_stale_cache_when_refresh_fails(self):
        cog = object.__new__(mention.MentionCog)

        class MetadataBot:
            def get_channel(self, _channel_id: int):
                return None

            async def fetch_channel(self, _channel_id: int):
                raise RuntimeError("fetch failed")

        with temporary_workdir() as temp_dir:
            metadata_dir = Path(temp_dir) / "mention" / "threadsMetadata"
            metadata_dir.mkdir(parents=True)
            metadata_path = metadata_dir / "456.txt"
            metadata_path.write_text("stale metadata", encoding="utf-8")
            stale_time = time.time() - mention.THREAD_METADATA_CACHE_TTL_SECONDS - 10
            os.utime(metadata_path, (stale_time, stale_time))

            cog.thread_metadata_path = str(metadata_dir)
            cog.bot = MetadataBot()
            cog.threads = {}

            result = asyncio.run(cog.get_thread_metadata("456"))

        self.assertEqual(result, "stale metadata")


class QuickPunishTests(unittest.TestCase):
    def test_build_dm_content_uses_configured_links(self):
        cog = object.__new__(quick_punish.QuickPunishCog)
        cog.bot = DummyBot(guild=DummyGuild([DummyRole(7, "违规身份组")]))
        cog.dm_templates = {}
        cog.reverify_link = "https://example.com/reverify"
        cog.rules_link = "https://example.com/rules"
        cog.appeal_channel_id = 42

        executor = type("Executor", (), {"name": "admin"})()
        content = asyncio.run(
            cog._build_dm_content(
                target_message=None,
                reason="测试原因",
                executor=executor,
                punish_count=1,
                removal_results=[
                    {"success": True, "guild_id": "1", "guild_name": "测试服", "removed_roles": [7]},
                ],
            )
        )

        self.assertIn("https://example.com/reverify", content)
        self.assertIn("[社区规则](https://example.com/rules)", content)
        self.assertIn("<#42>", content)
        self.assertNotIn("1338036166221365339", content)

    def test_cog_unload_removes_context_menus(self):
        cog = object.__new__(quick_punish.QuickPunishCog)
        cog.bot = DummyBot()

        cog.cog_unload()

        self.assertEqual(
            cog.bot.tree.removed,
            [
                (quick_punish.quick_punish_context.name, quick_punish.quick_punish_context.type),
                (quick_punish.remote_quick_punish_context.name, quick_punish.remote_quick_punish_context.type),
            ],
        )


class Fox14TaggerTests(unittest.TestCase):
    def test_cog_unload_cancels_task_and_removes_context_menu(self):
        task = type("Task", (), {"done": lambda self: False, "cancel": lambda self: setattr(self, "cancelled", True)})()
        task.cancelled = False

        cog = object.__new__(fox14_tagger.Fox14Tagger)
        cog.bot = DummyBot()
        cog._expiry_task = task

        cog.cog_unload()

        self.assertTrue(task.cancelled)
        self.assertEqual(cog.bot.tree.removed, [(fox14_tagger.fox14_tag_context.name, fox14_tagger.fox14_tag_context.type)])


if __name__ == "__main__":
    unittest.main()
