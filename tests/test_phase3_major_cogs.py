import asyncio
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
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
        self.removed.append((name, guild, type))


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
        self.deferred = False

    def is_done(self):
        return self.deferred or self.message is not None or self.modal is not None

    async def defer(self, ephemeral: bool = False):
        self.deferred = ephemeral

    async def send_message(self, content: str, ephemeral: bool = False):
        self.message = (content, ephemeral)

    async def send_modal(self, modal):
        self.modal = modal


class DummyClient:
    def __init__(self, cog=None, channels=None, *, admins=None, trusted_users=None):
        self._cog = cog
        self._channels = {channel.id: channel for channel in channels or []}
        self.admins = admins or []
        self.owner_ids = []
        self.trusted_users = trusted_users or []

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
        self.followup = SimpleNamespace(send=mock.AsyncMock())


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
    def test_user_punishment_query_returns_all_statuses_in_descending_order(self):
        cog = object.__new__(quick_punish.QuickPunishCog)

        with temporary_workdir() as temp_dir:
            original_path = quick_punish.db.QUICK_PUNISH_DB_PATH
            quick_punish.db.QUICK_PUNISH_DB_PATH = str(Path(temp_dir) / "quick_punish.db")
            try:
                cog.init_database()
                with sqlite3.connect(quick_punish.db.QUICK_PUNISH_DB_PATH) as connection:
                    rows = [
                        ("42", "Target", 1, "2026-01-02T00:00:00", "Admin", "first", "executed", "local", None),
                        ("42", "Target", 2, "2026-01-02T00:00:00", "Admin", "second", "revoked", "sync", "https://discord.com/message"),
                        ("42", "Target", 3, "2026-01-01T00:00:00", "", None, "failed", "local", None),
                        ("99", "Other", 1, "2026-02-01T00:00:00", "Admin", "other", "executed", "local", None),
                    ]
                    connection.executemany(
                        """
                        INSERT INTO quick_punish_records
                        (user_id, user_name, punish_count, timestamp, executor_id, executor_name,
                         reason, status, source_type, original_message_link)
                        VALUES (?, ?, ?, ?, '1', ?, ?, ?, ?, ?)
                        """,
                        rows,
                    )
                    expected_ids = [row[0] for row in connection.execute(
                        "SELECT id FROM quick_punish_records WHERE user_id = '42' ORDER BY timestamp DESC, id DESC"
                    )]

                records = asyncio.run(cog.get_punishments_for_user("42"))
            finally:
                quick_punish.db.QUICK_PUNISH_DB_PATH = original_path

        self.assertEqual([record["id"] for record in records], expected_ids)
        self.assertEqual([record["status"] for record in records], ["revoked", "executed", "failed"])
        self.assertIsNone(records[-1]["original_message_link"])

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
        self.assertIn("`📪|意见与投诉投稿` 频道", content)
        self.assertNotIn("<#42>", content)
        self.assertNotIn("999999999999999999", content)

    def test_cog_unload_removes_context_menus(self):
        cog = object.__new__(quick_punish.QuickPunishCog)
        cog.bot = DummyBot()
        scheduled_task = SimpleNamespace(done=lambda: False, cancel=mock.Mock())
        retained_message = SimpleNamespace(delete=mock.AsyncMock())
        cog._scheduled_punishments = {
            1: {"task": scheduled_task, "evidence": [(retained_message, retained_message)]},
        }
        cog._scheduling_message_ids = {2}
        cog._scheduling_user_ids = {3}
        cog._schedule_creation_tasks = set()

        with mock.patch.dict(os.environ, {"BOT_SHOULD_IN_GUILD_IDS": "111"}, clear=False):
            cog.cog_unload()

        self.assertEqual(
            [(name, guild.id, command_type) for name, guild, command_type in cog.bot.tree.removed],
            [
                (quick_punish.quick_punish_context.name, 111, quick_punish.quick_punish_context.type),
                (quick_punish.remote_quick_punish_context.name, 111, quick_punish.remote_quick_punish_context.type),
                (quick_punish.scheduled_quick_punish_context.name, 111, quick_punish.scheduled_quick_punish_context.type),
            ],
        )
        scheduled_task.cancel.assert_called_once()
        retained_message.delete.assert_not_awaited()
        self.assertEqual(cog._scheduled_punishments, {})
        self.assertEqual(cog._scheduling_message_ids, set())
        self.assertEqual(cog._scheduling_user_ids, set())

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

    def test_parse_scheduled_delay_accepts_only_confirmed_ranges(self):
        self.assertEqual(quick_punish.commands.parse_scheduled_delay("1m"), 60)
        self.assertEqual(quick_punish.commands.parse_scheduled_delay("120m"), 7200)
        self.assertEqual(quick_punish.commands.parse_scheduled_delay("1h"), 3600)
        self.assertEqual(quick_punish.commands.parse_scheduled_delay("2h"), 7200)

        for invalid in ("0m", "121m", "3h", "1.5h", " 1m", "1m ", "1s", "1d", ""):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                quick_punish.commands.parse_scheduled_delay(invalid)

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

    async def test_active_schedule_blocks_all_immediate_punishment_entry_points(self):
        cog = self._build_quick_punish_cog()
        guild = DummyGuild(guild_id=1)
        channel = DummyChannel(10, guild)
        message = self._build_message(guild, channel)
        channel._message = message
        cog._scheduled_punishments = {
            888: {
                "target_user": message.author,
                "status": "pending",
            }
        }
        client = DummyClient(cog=cog, channels=[channel])

        for command in (quick_punish.quick_punish_context, quick_punish.remote_quick_punish_context):
            with self.subTest(command=command.name):
                interaction = DummyInteraction(guild, client)
                await command.callback(interaction, message)
                self.assertIn("已有倒计时中的预约送走", interaction.response.message[0])
                self.assertIsNone(interaction.response.modal)

        slash_interaction = DummyInteraction(guild, client)
        await quick_punish.QuickPunishCommandsMixin.quick_punish_slash.callback(
            cog,
            slash_interaction,
            "https://discord.com/channels/1/10/777",
            discord.app_commands.Choice(name="普通", value="normal"),
        )
        self.assertIn("已有倒计时中的预约送走", slash_interaction.response.message[0])
        self.assertIsNone(slash_interaction.response.modal)

    async def test_scheduled_context_menu_uses_role_based_permission(self):
        cog = self._build_quick_punish_cog()
        guild = DummyGuild(guild_id=1)
        message = self._build_message(guild, DummyChannel(10, guild))

        allowed = DummyInteraction(
            guild,
            DummyClient(cog=cog),
            user=SimpleNamespace(id=99),
        )
        await quick_punish.scheduled_quick_punish_context.callback(allowed, message)
        self.assertIsInstance(allowed.response.modal, quick_punish.commands.ScheduledQuickPunishModal)

        cog.has_permission = lambda interaction: False
        denied = DummyInteraction(
            guild,
            DummyClient(cog=cog),
            user=SimpleNamespace(id=99),
        )
        await quick_punish.scheduled_quick_punish_context.callback(denied, message)
        self.assertIn("没权", denied.response.message[0])

    async def test_scheduled_modal_rechecks_permission_when_creating(self):
        cog = self._build_quick_punish_cog()
        cog.schedule_punishment = mock.AsyncMock()
        guild = DummyGuild(guild_id=1)
        message = self._build_message(guild, DummyChannel(10, guild))

        permission_calls: list[bool] = []

        def permission_once(_interaction):
            permission_calls.append(True)
            return len(permission_calls) == 1

        cog.has_permission = permission_once
        interaction = DummyInteraction(
            guild,
            DummyClient(cog=cog),
            user=SimpleNamespace(id=99),
        )
        await quick_punish.scheduled_quick_punish_context.callback(interaction, message)
        modal = interaction.response.modal
        modal.delay._value = "1m"
        modal.reason._value = "测试"
        modal.template_select._values = ["__none__"]

        await modal.on_submit(interaction)

        cog.schedule_punishment.assert_not_awaited()
        self.assertIn("没权", interaction.followup.send.await_args.args[0])

    async def test_scheduled_modal_sends_immediate_success_embed(self):
        cog = self._build_quick_punish_cog()
        cog.dm_templates = {}
        cog.schedule_punishment = mock.AsyncMock(
            return_value=(True, "将在 <t:1893456000:F> 执行")
        )
        guild = DummyGuild(guild_id=1)
        message = self._build_message(guild, DummyChannel(10, guild))
        message.author.mention = "<@123456>"
        operator = SimpleNamespace(id=99, mention="<@99>")
        interaction = DummyInteraction(
            guild,
            DummyClient(cog=cog, trusted_users=[99]),
            user=operator,
        )
        modal = quick_punish.commands.ScheduledQuickPunishModal(message, cog)
        modal.delay._value = "1m"
        modal.reason._value = "测试"
        modal.template_select._values = ["__none__"]

        with mock.patch.object(
            quick_punish.commands,
            "safe_defer",
            new=mock.AsyncMock(),
        ) as defer:
            await modal.on_submit(interaction)

        send_kwargs = interaction.followup.send.await_args.kwargs
        embed = send_kwargs["embed"]
        defer.assert_awaited_once_with(interaction, ephemeral=False)
        self.assertFalse(send_kwargs["ephemeral"])
        self.assertEqual(embed.title, "预约成功")
        self.assertIn("<@123456> 被 <@99> 执行了预约送走", embed.description)
        self.assertIn("<t:1893456000:F>", embed.description)
        self.assertIn("/预约送走-取消", embed.footer.text)

    async def test_cog_unload_cancels_evidence_creation_before_it_can_schedule(self):
        cog = object.__new__(quick_punish.QuickPunishCog)
        cog.bot = DummyBot()
        cog._scheduled_punishments = {}
        cog._scheduling_message_ids = set()
        cog._scheduling_user_ids = set()
        cog._schedule_creation_tasks = set()
        started = asyncio.Event()

        async def create_evidence(_schedule):
            started.set()
            await asyncio.Future()

        cog._create_scheduled_evidence = create_evidence
        creation_task = asyncio.create_task(
            cog.schedule_punishment(
                target_message=SimpleNamespace(
                    id=777,
                    author=SimpleNamespace(id=7),
                ),
                trigger_guild=SimpleNamespace(id=1),
                creator=SimpleNamespace(id=9),
                reason="测试",
                dm_template_filename="default.txt",
                delay_seconds=60,
            )
        )
        await started.wait()

        with mock.patch.dict(os.environ, {"BOT_SHOULD_IN_GUILD_IDS": "111"}, clear=False):
            cog.cog_unload()

        with self.assertRaises(asyncio.CancelledError):
            await creation_task
        self.assertEqual(cog._scheduled_punishments, {})
        self.assertEqual(cog._schedule_creation_tasks, set())

    async def test_schedule_requires_complete_evidence_in_at_least_one_destination(self):
        cog = object.__new__(quick_punish.QuickPunishCog)
        cog._scheduled_punishments = {}
        cog._scheduling_message_ids = set()
        cog._scheduling_user_ids = set()
        cog._schedule_creation_tasks = set()
        status_message = SimpleNamespace(delete=mock.AsyncMock(), jump_url="https://discord.com/status")
        destination = SimpleNamespace(send=mock.AsyncMock(return_value=status_message))
        cog._get_log_destinations = mock.AsyncMock(return_value=[destination])
        target_message = SimpleNamespace(
            id=777,
            jump_url="https://discord.com/original",
            author=SimpleNamespace(id=7, mention="<@7>"),
            forward=mock.AsyncMock(side_effect=RuntimeError("snapshot failed")),
        )

        success, message = await cog.schedule_punishment(
            target_message=target_message,
            trigger_guild=SimpleNamespace(id=1, name="Guild"),
            creator=SimpleNamespace(id=9, mention="<@9>"),
            reason="测试",
            dm_template_filename="default.txt",
            delay_seconds=60,
        )

        self.assertFalse(success)
        self.assertIn("留存失败", message)
        self.assertEqual(cog._scheduled_punishments, {})
        self.assertEqual(cog._scheduling_user_ids, set())
        status_message.delete.assert_awaited_once()

    async def test_schedule_keeps_complete_destinations_and_rejects_duplicates(self):
        cog = object.__new__(quick_punish.QuickPunishCog)
        cog._scheduled_punishments = {}
        cog._scheduling_message_ids = set()
        cog._scheduling_user_ids = set()
        cog._schedule_creation_tasks = set()
        good_status = SimpleNamespace(
            edit=mock.AsyncMock(),
            delete=mock.AsyncMock(),
            jump_url="https://discord.com/status-good",
        )
        failed_status = SimpleNamespace(
            edit=mock.AsyncMock(),
            delete=mock.AsyncMock(),
            jump_url="https://discord.com/status-failed",
        )
        snapshot = SimpleNamespace(delete=mock.AsyncMock(), jump_url="https://discord.com/snapshot")
        destinations = [
            SimpleNamespace(send=mock.AsyncMock(return_value=good_status)),
            SimpleNamespace(send=mock.AsyncMock(return_value=failed_status)),
        ]
        cog._get_log_destinations = mock.AsyncMock(return_value=destinations)

        async def forward(destination):
            if destination is destinations[0]:
                return snapshot
            raise RuntimeError("snapshot failed")

        target_message = SimpleNamespace(
            id=777,
            jump_url="https://discord.com/original",
            author=SimpleNamespace(id=7, mention="<@7>"),
            forward=mock.AsyncMock(side_effect=forward),
        )
        creator = SimpleNamespace(id=9, mention="<@9>")
        guild = SimpleNamespace(id=1, name="Guild")

        success, _message = await cog.schedule_punishment(
            target_message=target_message,
            trigger_guild=guild,
            creator=creator,
            reason="测试",
            dm_template_filename="default.txt",
            delay_seconds=7200,
        )
        duplicate_target_message = SimpleNamespace(
            id=778,
            jump_url="https://discord.com/original-2",
            author=target_message.author,
            forward=mock.AsyncMock(side_effect=AssertionError("must reject before preserving evidence")),
        )
        duplicate_success, duplicate_message = await cog.schedule_punishment(
            target_message=duplicate_target_message,
            trigger_guild=guild,
            creator=creator,
            reason="测试",
            dm_template_filename="default.txt",
            delay_seconds=7200,
        )

        self.assertTrue(success)
        self.assertFalse(duplicate_success)
        self.assertIn("该用户已有未结束的预约", duplicate_message)
        self.assertEqual(len(cog._scheduled_punishments), 1)
        self.assertEqual(cog._scheduled_punishments[777]["evidence"], [(good_status, snapshot)])
        good_status.edit.assert_awaited_once()
        self.assertIn("计时中", str(good_status.edit.await_args.kwargs["embed"].to_dict()))
        self.assertIn(snapshot.jump_url, str(good_status.edit.await_args.kwargs["embed"].to_dict()))
        failed_status.delete.assert_awaited_once()
        self.assertEqual(target_message.forward.await_count, 2)

        task = cog._scheduled_punishments.pop(777)["task"]
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_due_schedule_executes_saved_message_and_records_terminal_state(self):
        for success in (True, False):
            with self.subTest(success=success):
                cog = object.__new__(quick_punish.QuickPunishCog)
                status_message = SimpleNamespace(edit=mock.AsyncMock(), delete=mock.AsyncMock())
                snapshot = SimpleNamespace(jump_url="https://discord.com/snapshot", delete=mock.AsyncMock())
                target_message = SimpleNamespace(
                    id=777,
                    jump_url="https://discord.com/original",
                    channel=SimpleNamespace(fetch_message=mock.AsyncMock(side_effect=AssertionError("must not refetch"))),
                )
                creator = SimpleNamespace(id=9, mention="<@9>")
                target_user = SimpleNamespace(id=7, mention="<@7>")
                guild = SimpleNamespace(id=1, name="Guild")
                schedule = {
                    "original_message": target_message,
                    "target_user": target_user,
                    "trigger_guild": guild,
                    "creator": creator,
                    "reason": "测试",
                    "dm_template_filename": "default.txt",
                    "created_at": datetime.now(timezone.utc) - timedelta(minutes=1),
                    "execute_at": datetime.now(timezone.utc) - timedelta(seconds=1),
                    "status": "pending",
                    "task": None,
                    "evidence": [(status_message, snapshot)],
                }
                cog._scheduled_punishments = {777: schedule}
                cog.execute_punishment = mock.AsyncMock(return_value=(success, "执行结果", []))

                await cog._run_scheduled_punishment(777)

                cog.execute_punishment.assert_awaited_once_with(
                    trigger_guild=guild,
                    target_user=target_user,
                    target_message=target_message,
                    reason="测试",
                    executor=creator,
                    dm_template_filename="default.txt",
                    original_message_preserved=True,
                    scheduled_execution=True,
                )
                target_message.channel.fetch_message.assert_not_awaited()
                self.assertEqual(status_message.edit.await_count, 2)
                final_embed = status_message.edit.await_args.kwargs["embed"]
                self.assertIn("执行成功" if success else "执行失败", str(final_embed.to_dict()))
                self.assertIn("执行结果", str(final_embed.to_dict()))
                snapshot.delete.assert_not_awaited()
                self.assertNotIn(777, cog._scheduled_punishments)

    async def test_execute_punishment_rechecks_active_schedule_before_role_removal(self):
        cog = object.__new__(quick_punish.QuickPunishCog)
        target_user = SimpleNamespace(id=7, mention="<@7>")
        cog._scheduled_punishments = {
            777: {
                "target_user": target_user,
                "status": "pending",
            }
        }
        cog._scheduling_user_ids = set()
        cog._punish_locks = {}
        cog._punish_locks_guard = asyncio.Lock()
        cog._execute_role_removal_in_guild = mock.AsyncMock()

        success, message, history = await cog.execute_punishment(
            trigger_guild=SimpleNamespace(id=1),
            target_user=target_user,
            target_message=SimpleNamespace(id=777),
            reason="测试",
            executor=SimpleNamespace(id=9),
        )

        self.assertFalse(success)
        self.assertEqual(history, [])
        self.assertIn("已有倒计时中的预约送走", message)
        cog._execute_role_removal_in_guild.assert_not_awaited()

    async def test_cancel_removes_only_creators_pending_schedules_and_reports_residue(self):
        cog = object.__new__(quick_punish.QuickPunishCog)
        own_task = SimpleNamespace(done=lambda: False, cancel=mock.Mock())
        other_task = SimpleNamespace(done=lambda: False, cancel=mock.Mock())
        running_task = SimpleNamespace(done=lambda: False, cancel=mock.Mock())
        deleted_status = SimpleNamespace(delete=mock.AsyncMock(), jump_url="https://discord.com/status")
        failed_snapshot = SimpleNamespace(
            delete=mock.AsyncMock(side_effect=RuntimeError("delete failed")),
            jump_url="https://discord.com/residue",
        )
        cog._scheduled_punishments = {
            1: {
                "creator": SimpleNamespace(id=9),
                "status": "pending",
                "task": own_task,
                "evidence": [(deleted_status, failed_snapshot)],
            },
            2: {
                "creator": SimpleNamespace(id=10),
                "status": "pending",
                "task": other_task,
                "evidence": [],
            },
            3: {
                "creator": SimpleNamespace(id=9),
                "status": "running",
                "task": running_task,
                "evidence": [],
            },
        }

        cancelled, skipped_running, residue = await cog.cancel_scheduled_punishments(9)

        self.assertEqual((cancelled, skipped_running), (1, 1))
        self.assertEqual(residue, ["https://discord.com/residue"])
        self.assertEqual(set(cog._scheduled_punishments), {2, 3})
        own_task.cancel.assert_called_once()
        other_task.cancel.assert_not_called()
        running_task.cancel.assert_not_called()
        deleted_status.delete.assert_awaited_once()
        failed_snapshot.delete.assert_awaited_once()

    def test_schedule_list_contains_all_creators_sorted_by_execution_time(self):
        cog = object.__new__(quick_punish.QuickPunishCog)
        now = datetime.now(timezone.utc)

        def schedule(message_id, creator_id, execute_at, status):
            return {
                "creator": SimpleNamespace(id=creator_id, mention=f"<@{creator_id}>"),
                "target_user": SimpleNamespace(id=message_id, mention=f"<@{message_id}>"),
                "original_message": SimpleNamespace(jump_url=f"https://discord.com/original/{message_id}"),
                "execute_at": execute_at,
                "status": status,
                "evidence": [
                    (SimpleNamespace(), SimpleNamespace(jump_url=f"https://discord.com/snapshot/{message_id}"))
                ],
            }

        cog._scheduled_punishments = {
            2: schedule(2, 20, now + timedelta(minutes=2), "running"),
            1: schedule(1, 10, now + timedelta(minutes=1), "pending"),
            3: schedule(3, 30, now, "succeeded"),
        }

        result = cog.format_scheduled_punishments()

        self.assertLess(result.index("<@10>"), result.index("<@20>"))
        self.assertIn("计时中", result)
        self.assertIn("正在执行", result)
        self.assertIn("https://discord.com/original/1", result)
        self.assertIn("https://discord.com/snapshot/2", result)
        self.assertNotIn("<@30>", result)

    async def test_schedule_management_commands_use_role_based_permission(self):
        cog = object.__new__(quick_punish.QuickPunishCog)
        cog.enabled = True
        cog.has_permission = lambda interaction: True
        cog._scheduled_punishments = {}
        cog.cancel_scheduled_punishments = mock.AsyncMock(return_value=(0, 0, []))
        guild = DummyGuild(guild_id=1)

        operator = DummyInteraction(
            guild,
            DummyClient(cog=cog),
            user=SimpleNamespace(id=99),
        )
        await quick_punish.QuickPunishCommandsMixin.scheduled_quick_punish_cancel.callback(cog, operator)
        cog.cancel_scheduled_punishments.assert_awaited_once_with(99)

        reviewer = DummyInteraction(
            guild,
            DummyClient(cog=cog),
            user=SimpleNamespace(id=99),
        )
        await quick_punish.QuickPunishCommandsMixin.scheduled_quick_punish_list.callback(cog, reviewer)
        self.assertIn("当前没有预约", reviewer.followup.send.await_args.args[0])

        cog.has_permission = lambda interaction: False
        denied = DummyInteraction(
            guild,
            DummyClient(cog=cog),
            user=SimpleNamespace(id=99),
        )
        await quick_punish.QuickPunishCommandsMixin.scheduled_quick_punish_cancel.callback(cog, denied)
        cog.cancel_scheduled_punishments.assert_awaited_once_with(99)
        self.assertIn("没有权限", denied.followup.send.await_args.args[0])

    async def test_query_resolves_user_selector_and_user_id_with_id_precedence(self):
        cog = object.__new__(quick_punish.QuickPunishCog)
        cog.enabled = True
        cog.has_permission = lambda interaction: True
        cog.get_punishments_for_user = mock.AsyncMock(return_value=[])
        selected_user = SimpleNamespace(id=42, display_name="Selected", name="selected")

        async def query(user=None, user_id=None):
            interaction = DummyInteraction(
                DummyGuild(guild_id=1),
                DummyClient(cog=cog),
                user=SimpleNamespace(id=99, name="admin"),
            )
            interaction.client.fetch_user = mock.AsyncMock(side_effect=AssertionError("must not fetch user"))
            await quick_punish.QuickPunishCommandsMixin.quick_punish_query.callback(
                cog,
                interaction,
                user,
                user_id,
            )
            return interaction

        await query(selected_user, None)
        cog.get_punishments_for_user.assert_awaited_once_with("42")

        cog.get_punishments_for_user.reset_mock()
        await query(None, " 43 ")
        cog.get_punishments_for_user.assert_awaited_once_with("43")

        cog.get_punishments_for_user.reset_mock()
        await query(selected_user, "44")
        cog.get_punishments_for_user.assert_awaited_once_with("44")

        cog.get_punishments_for_user.reset_mock()
        invalid = await query(selected_user, "bad")
        cog.get_punishments_for_user.assert_not_awaited()
        self.assertIn("无效的用户ID", invalid.followup.send.await_args.args[0])

        missing = await query(None, None)
        self.assertIn("至少填写一个", missing.followup.send.await_args.args[0])

    async def test_query_uses_embed_with_all_fields_and_nullable_fallbacks(self):
        cog = object.__new__(quick_punish.QuickPunishCog)
        cog.enabled = True
        cog.has_permission = lambda interaction: True
        records = [
            {
                "id": index,
                "user_id": "42",
                "user_name": "Target",
                "punish_count": index,
                "timestamp": f"2026-01-0{index}T00:00:00",
                "reason": None if index == 3 else f"reason-{index}",
                "executor_name": "" if index == 3 else "Admin",
                "status": status,
                "source_type": "sync" if index == 2 else "local",
                "original_message_link": "https://discord.com/message" if index == 1 else None,
            }
            for index, status in enumerate(("executed", "revoked", "failed"), 1)
        ]
        cog.get_punishments_for_user = mock.AsyncMock(return_value=records)
        interaction = DummyInteraction(
            DummyGuild(guild_id=1),
            DummyClient(cog=cog),
            user=SimpleNamespace(id=99, name="admin"),
        )

        await quick_punish.QuickPunishCommandsMixin.quick_punish_query.callback(
            cog,
            interaction,
            SimpleNamespace(id=42, display_name="Target", name="target"),
            None,
        )

        embed = interaction.followup.send.await_args.kwargs["embed"]
        rendered = str(embed.to_dict())
        self.assertEqual(len(embed.fields), 3)
        for expected in ("executed", "revoked", "failed", "全局处罚序号", "时间", "原因", "执行者", "来源"):
            self.assertIn(expected, rendered)
        self.assertIn("[跳转](https://discord.com/message)", rendered)
        self.assertIn("无（同步/旧记录未保存）", rendered)
        self.assertIn("未记录", rendered)
        self.assertNotIn("移除身份组", rendered)

    async def test_query_uses_utf8_file_for_more_than_ten_records(self):
        cog = object.__new__(quick_punish.QuickPunishCog)
        cog.enabled = True
        cog.has_permission = lambda interaction: True
        records = [
            {
                "id": index,
                "user_id": "42",
                "user_name": "Target",
                "punish_count": index,
                "timestamp": f"2026-01-{index:02d}T00:00:00",
                "reason": f"原因-{index}",
                "executor_name": "Admin",
                "status": "executed",
                "source_type": "local",
                "original_message_link": None,
            }
            for index in range(1, 12)
        ]
        cog.get_punishments_for_user = mock.AsyncMock(return_value=records)
        interaction = DummyInteraction(
            DummyGuild(guild_id=1),
            DummyClient(cog=cog),
            user=SimpleNamespace(id=99, name="admin"),
        )

        await quick_punish.QuickPunishCommandsMixin.quick_punish_query.callback(
            cog,
            interaction,
            None,
            "42",
        )

        file = interaction.followup.send.await_args.kwargs["file"]
        file.fp.seek(0)
        content = file.fp.read().decode("utf-8")
        self.assertEqual(content.count("全局处罚序号："), 11)
        self.assertIn("原因-11", content)
        self.assertNotIn("移除身份组", content)

    async def test_query_keeps_existing_quick_punish_permission(self):
        cog = object.__new__(quick_punish.QuickPunishCog)
        cog.enabled = True
        cog.has_permission = lambda interaction: False
        cog.get_punishments_for_user = mock.AsyncMock()
        interaction = DummyInteraction(
            DummyGuild(guild_id=1),
            DummyClient(cog=cog, admins=[99], trusted_users=[99]),
            user=SimpleNamespace(id=99, name="admin"),
        )

        await quick_punish.QuickPunishCommandsMixin.quick_punish_query.callback(
            cog,
            interaction,
            None,
            "42",
        )

        cog.get_punishments_for_user.assert_not_awaited()
        self.assertIn("没有权限", interaction.followup.send.await_args.args[0])

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
    def test_cog_unload_cancels_task_and_removes_global_context_menu(self):
        task = type("Task", (), {"done": lambda self: False, "cancel": lambda self: setattr(self, "cancelled", True)})()
        task.cancelled = False

        cog = object.__new__(fox14_tagger.Fox14Tagger)
        cog.bot = DummyBot()
        cog._expiry_task = task

        cog.cog_unload()

        self.assertTrue(task.cancelled)
        self.assertEqual(
            cog.bot.tree.removed,
            [(fox14_tagger.fox14_tag_context.name, None, fox14_tagger.fox14_tag_context.type)],
        )


if __name__ == "__main__":
    unittest.main()
