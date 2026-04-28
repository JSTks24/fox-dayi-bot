import os
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

from cogs import broadcast, guild_guard, send, wiki_search


class DummyWikiResponse:
    status = 200

    async def json(self):
        return {"results": [{"title": "结果", "url": "https://naoleiwiki.pages.dev/faq/recent/item"}]}

    async def text(self):
        return ""


class DummyWikiRequestContext:
    async def __aenter__(self):
        return DummyWikiResponse()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class DummyWikiClientSession:
    request_count = 0

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def get(self, url):
        type(self).request_count += 1
        return DummyWikiRequestContext()


class DummyWikiInteraction:
    def __init__(self, user_id: int):
        self.user = SimpleNamespace(id=user_id, name=f"user-{user_id}")
        self.command = SimpleNamespace(name="问题搜索")
        self.response = SimpleNamespace(is_done=lambda: False, defer=mock.AsyncMock())
        self.edit_original_response = mock.AsyncMock()


class BroadcastPhase3Tests(unittest.TestCase):
    def test_interval_first_delay_uses_remaining_time(self):
        cog = object.__new__(broadcast.BroadcastCog)
        cog.stats = {"task-1": {"last_time_sent": "100000"}}
        cog.tz_shanghai = timezone(timedelta(hours=8), name="Asia/Shanghai")

        now = datetime(2026, 4, 19, 10, 5, 0, tzinfo=cog.tz_shanghai)
        delay = cog.get_interval_first_delay_seconds(
            {"id": "task-1", "INTERVAL_MINUTES": "10"},
            now=now,
        )

        self.assertEqual(delay, 300.0)

    def test_broadcast_commands_are_slash_commands(self):
        self.assertEqual(broadcast.BroadcastCog.reload_broadcast.name, "broadcast_reload")
        self.assertEqual(broadcast.BroadcastCog.broadcast_status.name, "broadcast_status")

    def test_replace_macros_only_normalizes_newlines(self):
        cog = object.__new__(broadcast.BroadcastCog)
        normalized = broadcast.BroadcastCog.replace_macros(cog, r"第一行\n{{time}}-{{count}}", "task-1")
        self.assertEqual(normalized, "第一行\n{{time}}-{{count}}")


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


class SendPhase3Tests(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_target_channel_falls_back_to_fetch_channel(self):
        fetched_channel = SimpleNamespace(id=456, guild=SimpleNamespace(id=123))
        bot = SimpleNamespace(fetch_channel=mock.AsyncMock(return_value=fetched_channel))
        cog = send.SendCog(bot)
        guild = SimpleNamespace(id=123, get_channel=mock.Mock(return_value=None))

        resolved = await cog.resolve_target_channel(guild, 456)

        self.assertIs(resolved, fetched_channel)
        bot.fetch_channel.assert_awaited_once_with(456)

    def test_build_embed_supports_hex_color(self):
        embed = send.SendCog._build_embed("标题", "描述", "#112233")
        self.assertEqual(embed.title, "标题")
        self.assertEqual(embed.description, "描述")
        self.assertEqual(embed.colour.value, 0x112233)


class WikiSearchCooldownTests(unittest.IsolatedAsyncioTestCase):
    async def _invoke_twice(self, *, admins=None, trusted_users=None, user_id: int = 1):
        DummyWikiClientSession.request_count = 0
        bot = SimpleNamespace(admins=admins or [], trusted_users=trusted_users or [])
        cog = wiki_search.WikiSearch(bot)
        interaction = DummyWikiInteraction(user_id=user_id)

        with mock.patch("cogs.wiki_search.aiohttp.ClientSession", DummyWikiClientSession), mock.patch(
            "cogs.wiki_search.log_slash_command"
        ):
            await wiki_search.WikiSearch.wiki_search.callback(cog, interaction, 关键词="测试", 包含作品=False)
            await wiki_search.WikiSearch.wiki_search.callback(cog, interaction, 关键词="测试", 包含作品=False)

        return interaction

    async def test_admin_wiki_search_has_no_cooldown(self):
        interaction = await self._invoke_twice(admins=[1], user_id=1)

        self.assertEqual(DummyWikiClientSession.request_count, 2)
        self.assertEqual(interaction.edit_original_response.await_count, 2)
        second_call = interaction.edit_original_response.await_args_list[1].kwargs
        self.assertIn("embed", second_call)
        self.assertNotIn("content", second_call)

    async def test_trusted_wiki_search_uses_10_second_cooldown(self):
        interaction = await self._invoke_twice(trusted_users=[2], user_id=2)

        self.assertEqual(DummyWikiClientSession.request_count, 1)
        second_call = interaction.edit_original_response.await_args_list[1].kwargs
        self.assertEqual(
            second_call["content"],
            "⏳ 你当前为受信任用户，/问题搜索冷却为 10 秒，还需等待 10 秒后再试。",
        )

    async def test_regular_wiki_search_uses_30_second_cooldown(self):
        interaction = await self._invoke_twice(user_id=3)

        self.assertEqual(DummyWikiClientSession.request_count, 1)
        second_call = interaction.edit_original_response.await_args_list[1].kwargs
        self.assertEqual(
            second_call["content"],
            "⏳ 你当前为普通用户，/问题搜索冷却为 30 秒，还需等待 30 秒后再试。",
        )


if __name__ == "__main__":
    unittest.main()
