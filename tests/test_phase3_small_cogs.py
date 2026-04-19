import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

import pytz

from cogs import broadcast, debug_role, guild_guard, slashsend


class BroadcastPhase3Tests(unittest.TestCase):
    def test_interval_first_delay_uses_remaining_time(self):
        cog = object.__new__(broadcast.BroadcastCog)
        cog.stats = {"task-1": {"last_time_sent": "100000"}}
        cog.tz_shanghai = pytz.timezone("Asia/Shanghai")

        now = cog.tz_shanghai.localize(datetime(2026, 4, 19, 10, 5, 0))
        delay = cog.get_interval_first_delay_seconds(
            {"id": "task-1", "INTERVAL_MINUTES": "10"},
            now=now,
        )

        self.assertEqual(delay, 300.0)

    def test_broadcast_commands_are_slash_commands(self):
        self.assertEqual(broadcast.BroadcastCog.reload_broadcast.name, "broadcast_reload")
        self.assertEqual(broadcast.BroadcastCog.broadcast_status.name, "broadcast_status")

    def test_debug_role_is_guild_only(self):
        self.assertTrue(debug_role.DebugRole.debug_role.guild_only)


class BroadcastLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_cog_load_starts_auto_save_and_tasks(self):
        cog = object.__new__(broadcast.BroadcastCog)
        cog.auto_save = mock.Mock()
        cog.auto_save.is_running.return_value = False
        cog.start_all_tasks = mock.AsyncMock()

        await broadcast.BroadcastCog.cog_load(cog)

        cog.auto_save.start.assert_called_once_with()
        cog.start_all_tasks.assert_awaited_once_with()


class GuildGuardPhase3Tests(unittest.IsolatedAsyncioTestCase):
    async def test_on_guild_join_leaves_unexpected_guild(self):
        cog = guild_guard.GuildGuardCog(SimpleNamespace())
        guild = SimpleNamespace(id=222, name="unexpected", leave=mock.AsyncMock())

        with mock.patch.dict(os.environ, {"BOT_SHOULD_IN_GUILD_IDS": "111,333"}, clear=False):
            await cog.on_guild_join(guild)

        guild.leave.assert_awaited_once_with()

    async def test_on_guild_join_skips_when_whitelist_missing(self):
        cog = guild_guard.GuildGuardCog(SimpleNamespace())
        guild = SimpleNamespace(id=222, name="unexpected", leave=mock.AsyncMock())

        with mock.patch.dict(os.environ, {}, clear=True):
            await cog.on_guild_join(guild)

        guild.leave.assert_not_awaited()


class SlashSendPhase3Tests(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_target_channel_falls_back_to_fetch_channel(self):
        fetched_channel = SimpleNamespace(id=456, guild=SimpleNamespace(id=123))
        bot = SimpleNamespace(fetch_channel=mock.AsyncMock(return_value=fetched_channel))
        cog = slashsend.SlashSend(bot)
        guild = SimpleNamespace(id=123, get_channel=mock.Mock(return_value=None))

        resolved = await cog.resolve_target_channel(guild, 456)

        self.assertIs(resolved, fetched_channel)
        bot.fetch_channel.assert_awaited_once_with(456)


if __name__ == "__main__":
    unittest.main()
