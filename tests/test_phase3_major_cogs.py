import asyncio
import os
import time
from unittest import mock
import unittest
from pathlib import Path
from types import SimpleNamespace

import discord

from cogs import fox14_tagger, mention, quick_punish
from tests.conftest import temporary_workdir


class DummyTree:
    def __init__(self):
        self.removed = []

    def remove_command(self, name, *, guild=None, type):
        self.removed.append((name, type))


class DummyRole:
    def __init__(self, role_id: int, name: str):
        self.id = role_id
        self.name = name


class DummyGuild:
    def __init__(self, roles=None, guild_id: int = 1, channels=None, threads=None):
        self.id = guild_id
        self._roles = {role.id: role for role in roles or []}
        self._channels = {channel.id: channel for channel in channels or []}
        self._threads = {thread.id: thread for thread in threads or []}

    def get_role(self, role_id: int):
        return self._roles.get(role_id)

    def get_channel(self, channel_id: int):
        return self._channels.get(channel_id)

    def get_thread(self, channel_id: int):
        return self._threads.get(channel_id)


class DummyBot:
    def __init__(self, guild=None):
        self.tree = DummyTree()
        self._guild = guild

    def get_guild(self, guild_id: int):
        return self._guild


class DummyResponse:
    def __init__(self):
        self.message = None
        self.modal = None

    async def send_message(self, content: str, ephemeral: bool = False):
        self.message = (content, ephemeral)

    async def send_modal(self, modal):
        self.modal = modal


class DummyClient:
    def __init__(self, cog=None, channels=None):
        self._cog = cog
        self._channels = {channel.id: channel for channel in channels or []}

    def get_cog(self, name: str):
        if name == "QuickPunishCog":
            return self._cog
        return None

    def get_channel(self, channel_id: int):
        return self._channels.get(channel_id)

    async def fetch_channel(self, channel_id: int):
        raise AssertionError(f"unexpected fetch_channel call for {channel_id}")


class DummyInteraction:
    def __init__(self, guild: DummyGuild | None, client: DummyClient, user=None):
        self.guild = guild
        self.guild_id = guild.id if guild else None
        self.client = client
        self.user = user or SimpleNamespace(id=99, roles=[])
        self.response = DummyResponse()


class DummyChannel:
    def __init__(self, channel_id: int, guild: DummyGuild, *, message=None, fetch_error: Exception | None = None):
        self.id = channel_id
        self.guild = guild
        self._message = message
        self._fetch_error = fetch_error

    async def fetch_message(self, message_id: int):
        if self._fetch_error is not None:
            raise self._fetch_error
        if self._message is None or self._message.id != message_id:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "missing")
        return self._message


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
        self.assertNotIn("999999999999999999", content)

    def test_cog_unload_removes_context_menus(self):
        cog = object.__new__(quick_punish.QuickPunishCog)
        cog.bot = DummyBot()

        with mock.patch.dict(os.environ, {"BOT_SHOULD_IN_GUILD_IDS": "111"}, clear=False):
            cog.cog_unload()

        self.assertEqual(
            cog.bot.tree.removed,
            [
                (quick_punish.quick_punish_context.name, quick_punish.quick_punish_context.type),
                (quick_punish.remote_quick_punish_context.name, quick_punish.remote_quick_punish_context.type),
            ],
        )

    def test_log_embed_forwards_original_message_to_each_destination(self):
        cog = object.__new__(quick_punish.QuickPunishCog)
        snapshot = SimpleNamespace(jump_url="https://discord.com/snapshot")
        original_message = SimpleNamespace(
            forward=mock.AsyncMock(return_value=snapshot),
            attachments=[SimpleNamespace(filename="evidence.png", url="https://cdn.example/expired")],
        )
        destinations = [
            SimpleNamespace(send=mock.AsyncMock()),
            SimpleNamespace(send=mock.AsyncMock()),
        ]
        user = SimpleNamespace(id=1, mention="<@1>")
        executor = SimpleNamespace(mention="<@2>")

        async def send_logs():
            return [
                await cog.send_log_embed(
                    channel=destination,
                    user=user,
                    executor=executor,
                    reason="测试",
                    message_link="https://discord.com/original",
                    removed_roles=[],
                    record_id=3,
                    original_message=original_message,
                )
                for destination in destinations
            ]

        self.assertEqual(asyncio.run(send_logs()), [snapshot, snapshot])
        self.assertEqual(original_message.forward.await_count, 2)
        self.assertEqual(
            [call.args[0] for call in original_message.forward.await_args_list],
            destinations,
        )
        for destination in destinations:
            destination.send.assert_awaited_once()
            self.assertEqual(destination.send.await_args.kwargs["embed"].title, "⚠️ 答题处罚执行")
            self.assertNotIn("cdn.example/expired", str(destination.send.await_args.kwargs["embed"].to_dict()))

    def test_snapshot_failure_keeps_log_and_sends_warning(self):
        cog = object.__new__(quick_punish.QuickPunishCog)
        original_message = SimpleNamespace(forward=mock.AsyncMock(side_effect=RuntimeError("forward failed")))
        destination = SimpleNamespace(send=mock.AsyncMock())

        result = asyncio.run(
            cog.send_log_embed(
                channel=destination,
                user=SimpleNamespace(id=1, mention="<@1>"),
                executor=SimpleNamespace(mention="<@2>"),
                reason="测试",
                message_link="https://discord.com/original",
                removed_roles=[],
                record_id=3,
                original_message=original_message,
            )
        )

        self.assertIsNone(result)
        self.assertEqual(destination.send.await_count, 2)
        self.assertEqual(destination.send.await_args_list[0].kwargs["embed"].title, "⚠️ 答题处罚执行")
        self.assertIn("无法创建原消息快照", destination.send.await_args_list[1].args[0])


class QuickPunishCommandTests(unittest.IsolatedAsyncioTestCase):
    def _build_quick_punish_cog(self):
        cog = object.__new__(quick_punish.QuickPunishCog)
        cog.enabled = True
        cog.has_permission = lambda interaction: True
        return cog

    def _build_message(self, guild: DummyGuild, channel: DummyChannel, *, author_bot: bool = False):
        author = SimpleNamespace(
            bot=author_bot,
            display_name="Target",
            name="target_user",
            id=123456,
        )
        return SimpleNamespace(id=777, author=author, channel=channel, guild=guild)

    def test_parse_message_link_supports_known_domains(self):
        self.assertEqual(
            quick_punish.commands._parse_quick_punish_message_link(
                "https://discord.com/channels/1/2/3"
            ),
            (1, 2, 3),
        )
        self.assertEqual(
            quick_punish.commands._parse_quick_punish_message_link(
                "https://discordapp.com/channels/4/5/6"
            ),
            (4, 5, 6),
        )
        self.assertIsNone(quick_punish.commands._parse_quick_punish_message_link("not-a-link"))

    async def test_resolve_message_from_link_rejects_cross_guild(self):
        guild = DummyGuild(guild_id=1)
        interaction = DummyInteraction(guild, DummyClient())

        message, error = await quick_punish.commands._resolve_quick_punish_message_from_link(
            interaction,
            "https://discord.com/channels/2/10/20",
        )

        self.assertIsNone(message)
        self.assertEqual(error, "❌ 只能处理当前服务器的消息链接，不支持跨服务器。")

    async def test_resolve_message_from_link_reports_missing_or_inaccessible_messages(self):
        guild = DummyGuild(guild_id=1)

        deleted_channel = DummyChannel(
            10,
            guild,
            fetch_error=discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "missing"),
        )
        deleted_interaction = DummyInteraction(guild, DummyClient(channels=[deleted_channel]))
        _, deleted_error = await quick_punish.commands._resolve_quick_punish_message_from_link(
            deleted_interaction,
            "https://discord.com/channels/1/10/20",
        )
        self.assertEqual(deleted_error, "❌ 找不到目标消息，可能已被删除。")

        forbidden_channel = DummyChannel(
            11,
            guild,
            fetch_error=discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "forbidden"),
        )
        forbidden_interaction = DummyInteraction(guild, DummyClient(channels=[forbidden_channel]))
        _, forbidden_error = await quick_punish.commands._resolve_quick_punish_message_from_link(
            forbidden_interaction,
            "https://discord.com/channels/1/11/21",
        )
        self.assertEqual(forbidden_error, "❌ 无法访问目标消息，可能是频道或线程不可见。")

    async def test_context_menus_still_open_existing_modals(self):
        cog = self._build_quick_punish_cog()
        guild = DummyGuild(guild_id=1)
        channel = DummyChannel(10, guild)
        message = self._build_message(guild, channel)
        client = DummyClient(cog=cog)
        interaction = DummyInteraction(guild, client)

        await quick_punish.quick_punish_context.callback(interaction, message)
        self.assertIsInstance(interaction.response.modal, quick_punish.commands.QuickPunishModal)

        remote_interaction = DummyInteraction(guild, client)
        await quick_punish.remote_quick_punish_context.callback(remote_interaction, message)
        self.assertIsInstance(remote_interaction.response.modal, quick_punish.commands.RemoteQuickPunishModal)

    async def test_slash_quick_punish_opens_remote_modal_from_message_link(self):
        cog = self._build_quick_punish_cog()
        guild = DummyGuild(guild_id=1)
        channel = DummyChannel(10, guild)
        message = self._build_message(guild, channel)
        channel._message = message
        interaction = DummyInteraction(guild, DummyClient(cog=cog, channels=[channel]))

        await quick_punish.QuickPunishCommandsMixin.quick_punish_slash.callback(
            cog,
            interaction,
            "https://discord.com/channels/1/10/777",
            discord.app_commands.Choice(name="远距", value="remote"),
        )

        self.assertIsInstance(interaction.response.modal, quick_punish.commands.RemoteQuickPunishModal)


class Fox14TaggerTests(unittest.TestCase):
    def test_cog_unload_cancels_task_and_removes_context_menu(self):
        task = type("Task", (), {"done": lambda self: False, "cancel": lambda self: setattr(self, "cancelled", True)})()
        task.cancelled = False

        cog = object.__new__(fox14_tagger.Fox14Tagger)
        cog.bot = DummyBot()
        cog._expiry_task = task

        with mock.patch.dict(os.environ, {"BOT_SHOULD_IN_GUILD_IDS": "111"}, clear=False):
            cog.cog_unload()

        self.assertTrue(task.cancelled)
        self.assertEqual(cog.bot.tree.removed, [(fox14_tagger.fox14_tag_context.name, fox14_tagger.fox14_tag_context.type)])


if __name__ == "__main__":
    unittest.main()
