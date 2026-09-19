import asyncio
import contextlib
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import discord

from cogs import quick_punish
from cogs.quick_punish import core as quick_punish_core
from cogs.quick_punish.vote import PunishDeleteVoteView, VOTE_REQUIRED_APPROVALS
from tests.conftest import temporary_workdir


VOTE_CHANNEL_ID = 777
TARGET_CHANNEL_ID = 10
GUILD_ID = 1
VOTE_ROLE_ID = 5
TARGET_MESSAGE_ID = 555
PANEL_MESSAGE_ID = 999
TARGET_MESSAGE_LINK = (
    f"https://discord.com/channels/{GUILD_ID}/{TARGET_CHANNEL_ID}/{TARGET_MESSAGE_ID}"
)


class DummyRole:
    def __init__(self, role_id: int):
        self.id = role_id
        self.name = f"role-{role_id}"


def make_member(user_id: int, role_ids: list[int]):
    member = mock.Mock(spec=discord.Member)
    member.id = user_id
    member.roles = [DummyRole(role_id) for role_id in role_ids]
    return member


class DummyVoteInteraction:
    def __init__(self, user, message_id: int = PANEL_MESSAGE_ID):
        self.user = user
        self.guild = SimpleNamespace(id=GUILD_ID)
        self.message = SimpleNamespace(id=message_id)
        self.response = SimpleNamespace(
            is_done=lambda: False,
            defer=mock.AsyncMock(),
            send_message=mock.AsyncMock(),
        )
        self.followup = SimpleNamespace(send=mock.AsyncMock())
        self.edit_original_response = mock.AsyncMock()


class DummySendChannel:
    def __init__(self, channel_id: int):
        self.id = channel_id
        self.send = mock.AsyncMock(return_value=SimpleNamespace(id=PANEL_MESSAGE_ID))


class DummyFetchChannel:
    def __init__(self, channel_id: int, message=None):
        self.id = channel_id
        self._message = message

    async def fetch_message(self, message_id: int):
        if self._message is None or self._message.id != message_id:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "missing")
        return self._message


class DummyVoteBot:
    def __init__(self, channels):
        self._channels = {channel.id: channel for channel in channels}
        self.added_views = []

    def get_channel(self, channel_id: int):
        return self._channels.get(channel_id)

    async def fetch_channel(self, channel_id: int):
        raise AssertionError(f"unexpected fetch_channel call for {channel_id}")

    def add_view(self, view):
        self.added_views.append(view)


@contextlib.contextmanager
def swapped_vote_db(temp_dir: str):
    original = quick_punish.db.QUICK_PUNISH_DB_PATH
    quick_punish.db.QUICK_PUNISH_DB_PATH = str(Path(temp_dir) / "quick_punish.db")
    try:
        yield
    finally:
        quick_punish.db.QUICK_PUNISH_DB_PATH = original


class PunishVoteTestBase(unittest.TestCase):
    def make_vote_cog(self, bot, *, configured: bool = True):
        cog = object.__new__(quick_punish.QuickPunishCog)
        cog.bot = bot
        cog.enabled = True
        cog.allowed_roles = [VOTE_ROLE_ID]
        cog.sync_config = {"version": 1, "sync_guild_ids": [], "guilds": {}, "policy": {"mode": "best_effort"}}
        cog.vote_channel_id = VOTE_CHANNEL_ID if configured else None
        cog._vote_locks = {}
        cog._vote_tasks = set()
        return cog

    async def seed_vote(self, cog, *, record_id: int = 1, executor_id: str = "2",
                        approver_ids: list[str] | None = None,
                        rejecter_ids: list[str] | None = None,
                        vote_message_id: int = PANEL_MESSAGE_ID):
        await cog.create_vote(
            record_id=record_id,
            vote_message_id=str(vote_message_id),
            target_message_link=TARGET_MESSAGE_LINK,
            target_user_id="42",
            executor_id=executor_id,
            reason="刷屏",
        )
        if approver_ids or rejecter_ids:
            await cog.update_vote_progress(
                record_id,
                approver_ids=approver_ids or [],
                rejecter_ids=rejecter_ids or [],
            )


class PunishVoteInteractionCheckTests(PunishVoteTestBase):
    def test_interaction_check_gates_by_punish_permission(self):
        bot = DummyVoteBot([])
        cog = self.make_vote_cog(bot)

        async def _scenario():
            view = PunishDeleteVoteView(cog)
            allowed_member = make_member(100, [VOTE_ROLE_ID])
            allowed = await view.interaction_check(DummyVoteInteraction(allowed_member))

            denied_member = make_member(101, [VOTE_ROLE_ID + 1])
            denied = DummyVoteInteraction(denied_member)
            not_allowed = await view.interaction_check(denied)
            return allowed, not_allowed, denied

        allowed, not_allowed, denied = asyncio.run(_scenario())
        self.assertTrue(allowed)
        self.assertFalse(not_allowed)
        denied.response.send_message.assert_awaited_once()
        self.assertTrue(denied.response.send_message.await_args.kwargs.get("ephemeral"))


class PunishVotePanelTests(PunishVoteTestBase):
    def test_create_punish_vote_posts_panel_and_persists_row(self):
        with temporary_workdir() as temp_dir, swapped_vote_db(temp_dir):
            vote_channel = DummySendChannel(VOTE_CHANNEL_ID)
            bot = DummyVoteBot([vote_channel])
            cog = self.make_vote_cog(bot)
            cog.init_database()

            target_user = SimpleNamespace(id=42, name="Target")
            executor = SimpleNamespace(id=2, name="Admin")
            target_message = SimpleNamespace(id=TARGET_MESSAGE_ID, channel=SimpleNamespace(id=TARGET_CHANNEL_ID))
            trigger_guild = SimpleNamespace(id=GUILD_ID)

            async def _scenario():
                await cog.create_punish_vote(
                    trigger_guild=trigger_guild,
                    target_user=target_user,
                    target_message=target_message,
                    reason="刷屏",
                    executor=executor,
                    record_id=1,
                )
                await cog.create_punish_vote(
                    trigger_guild=trigger_guild,
                    target_user=target_user,
                    target_message=target_message,
                    reason="刷屏",
                    executor=executor,
                    record_id=1,
                )
                return await cog.get_vote_by_record_id(1)

            vote = asyncio.run(_scenario())

            vote_channel.send.assert_awaited_once()
            kwargs = vote_channel.send.await_args.kwargs
            self.assertIsInstance(kwargs["view"], PunishDeleteVoteView)
            self.assertEqual(kwargs["embed"].footer.text, "记录ID: 1")
            self.assertIsNotNone(vote)
            self.assertEqual(vote["vote_message_id"], str(PANEL_MESSAGE_ID))
            self.assertEqual(vote["target_message_link"], TARGET_MESSAGE_LINK)
            self.assertEqual(vote["status"], "pending")

    def test_pending_panel_states_permission_requirement(self):
        cog = self.make_vote_cog(DummyVoteBot([]))
        embed = cog.build_vote_panel_embed(
            record_id=1,
            target_user_id="42",
            executor_id="2",
            reason="刷屏",
            target_message_link=TARGET_MESSAGE_LINK,
        )

        progress_field = next(f for f in embed.fields if f.name == "投票进度")
        self.assertIn("拥有快速处罚权限的成员同意", progress_field.value)
        self.assertNotIn("投票组成员", progress_field.value)

    def test_cog_load_registers_persistent_view_only_when_configured(self):
        with temporary_workdir() as temp_dir, swapped_vote_db(temp_dir):
            configured_bot = DummyVoteBot([])
            configured_cog = self.make_vote_cog(configured_bot)
            asyncio.run(configured_cog.cog_load())
            self.assertEqual(len(configured_bot.added_views), 1)
            self.assertIsInstance(configured_bot.added_views[0], PunishDeleteVoteView)

            unconfigured_bot = DummyVoteBot([])
            unconfigured_cog = self.make_vote_cog(unconfigured_bot, configured=False)
            asyncio.run(unconfigured_cog.cog_load())
            self.assertEqual(unconfigured_bot.added_views, [])


class PunishVoteClickTests(PunishVoteTestBase):
    @contextlib.contextmanager
    def click_env(self, *, target_deleted: bool = False):
        with temporary_workdir() as temp_dir, swapped_vote_db(temp_dir):
            target = SimpleNamespace(id=TARGET_MESSAGE_ID, delete=mock.AsyncMock())
            target_channel = DummyFetchChannel(TARGET_CHANNEL_ID, message=None if target_deleted else target)
            vote_channel = DummySendChannel(VOTE_CHANNEL_ID)
            bot = DummyVoteBot([vote_channel, target_channel])
            cog = self.make_vote_cog(bot)
            cog.init_database()
            yield cog, target

    def test_click_on_unknown_panel_replies_not_found(self):
        with self.click_env() as (cog, _target):
            async def _scenario():
                interaction = DummyVoteInteraction(make_member(100, [VOTE_ROLE_ID]), message_id=12345)
                await cog.handle_vote_click(interaction, "approve")
                return interaction

            interaction = asyncio.run(_scenario())
            interaction.followup.send.assert_awaited_once_with("❌ 未找到对应的投票记录。", ephemeral=True)

    def test_first_approval_updates_progress_without_deleting(self):
        with self.click_env() as (cog, target):
            async def _scenario():
                await self.seed_vote(cog)
                interaction = DummyVoteInteraction(make_member(100, [VOTE_ROLE_ID]))
                await cog.handle_vote_click(interaction, "approve")
                vote = await cog.get_vote_by_record_id(1)
                return vote, interaction

            vote, interaction = asyncio.run(_scenario())
            target.delete.assert_not_awaited()
            self.assertEqual(vote["status"], "pending")
            self.assertEqual(vote["approver_ids"], ["100"])

            kwargs = interaction.edit_original_response.await_args.kwargs
            self.assertNotIn("view", kwargs)
            progress_field = next(f for f in kwargs["embed"].fields if f.name == "投票进度")
            self.assertIn(f"1/{VOTE_REQUIRED_APPROVALS}", progress_field.value)

    def test_second_approval_deletes_target_message_once(self):
        with self.click_env() as (cog, target):
            async def _scenario():
                await self.seed_vote(cog, approver_ids=["100"])
                interaction = DummyVoteInteraction(make_member(101, [VOTE_ROLE_ID]))
                await cog.handle_vote_click(interaction, "approve")
                vote = await cog.get_vote_by_record_id(1)
                return vote, interaction

            vote, interaction = asyncio.run(_scenario())
            target.delete.assert_awaited_once()
            self.assertEqual(vote["status"], "executed")
            self.assertEqual(vote["approver_ids"], ["100", "101"])
            kwargs = interaction.edit_original_response.await_args.kwargs
            self.assertIsNone(kwargs["view"])
            self.assertEqual(kwargs["embed"].title, "✅ 已删除被处罚消息")

    def test_duplicate_approval_counts_once(self):
        with self.click_env() as (cog, target):
            async def _scenario():
                await self.seed_vote(cog)
                interaction = DummyVoteInteraction(make_member(100, [VOTE_ROLE_ID]))
                await cog.handle_vote_click(interaction, "approve")
                await cog.handle_vote_click(interaction, "approve")
                vote = await cog.get_vote_by_record_id(1)
                return vote, interaction

            vote, interaction = asyncio.run(_scenario())
            self.assertEqual(vote["status"], "pending")
            self.assertEqual(vote["approver_ids"], ["100"])
            target.delete.assert_not_awaited()
            interaction.followup.send.assert_awaited_once_with("❌ 你已经投过同意票了。", ephemeral=True)

    def test_switching_approval_to_reject_keeps_only_last_action(self):
        with self.click_env() as (cog, target):
            async def _scenario():
                await self.seed_vote(cog)
                interaction = DummyVoteInteraction(make_member(100, [VOTE_ROLE_ID]))
                await cog.handle_vote_click(interaction, "approve")
                await cog.handle_vote_click(interaction, "reject")
                return await cog.get_vote_by_record_id(1)

            vote = asyncio.run(_scenario())
            self.assertEqual(vote["status"], "rejected")
            self.assertEqual(vote["approver_ids"], [])
            self.assertEqual(vote["rejecter_ids"], ["100"])
            target.delete.assert_not_awaited()

    def test_executor_approval_counts_toward_required_votes(self):
        with self.click_env() as (cog, target):
            async def _scenario():
                await self.seed_vote(cog, executor_id="2")
                await cog.handle_vote_click(DummyVoteInteraction(make_member(2, [VOTE_ROLE_ID])), "approve")
                after_executor = await cog.get_vote_by_record_id(1)
                deletes_after_executor_click = target.delete.await_count
                await cog.handle_vote_click(DummyVoteInteraction(make_member(100, [VOTE_ROLE_ID])), "approve")
                after_one_member = await cog.get_vote_by_record_id(1)
                return after_executor, after_one_member, deletes_after_executor_click

            after_executor, after_one_member, deletes_after_executor_click = asyncio.run(_scenario())
            self.assertEqual(after_executor["status"], "pending")
            self.assertEqual(after_executor["approver_ids"], ["2"])
            self.assertEqual(deletes_after_executor_click, 0)
            self.assertEqual(after_one_member["status"], "executed")
            self.assertEqual(after_one_member["approver_ids"], ["2", "100"])
            target.delete.assert_awaited_once()

    def test_executor_reject_still_vetoes(self):
        with self.click_env() as (cog, target):
            async def _scenario():
                await self.seed_vote(cog, executor_id="2")
                await cog.handle_vote_click(DummyVoteInteraction(make_member(2, [VOTE_ROLE_ID])), "reject")
                return await cog.get_vote_by_record_id(1)

            vote = asyncio.run(_scenario())
            self.assertEqual(vote["status"], "rejected")
            self.assertEqual(vote["rejecter_ids"], ["2"])
            target.delete.assert_not_awaited()

    def test_reject_vetoes_after_one_approval(self):
        with self.click_env() as (cog, target):
            async def _scenario():
                await self.seed_vote(cog, approver_ids=["100"])
                interaction = DummyVoteInteraction(make_member(101, [VOTE_ROLE_ID]))
                await cog.handle_vote_click(interaction, "reject")
                vote = await cog.get_vote_by_record_id(1)
                return vote, interaction

            vote, interaction = asyncio.run(_scenario())
            target.delete.assert_not_awaited()
            self.assertEqual(vote["status"], "rejected")
            self.assertEqual(vote["approver_ids"], ["100"])
            self.assertEqual(vote["rejecter_ids"], ["101"])
            kwargs = interaction.edit_original_response.await_args.kwargs
            self.assertIsNone(kwargs["view"])
            self.assertEqual(kwargs["embed"].title, "🚫 已否决，保留原消息")

    def test_click_after_decision_replies_closed(self):
        with self.click_env() as (cog, _target):
            async def _scenario():
                await self.seed_vote(cog, approver_ids=["100"], rejecter_ids=["101"])
                await cog.decide_vote(1, status="rejected", approver_ids=["100"], rejecter_ids=["101"])
                interaction = DummyVoteInteraction(make_member(102, [VOTE_ROLE_ID]))
                await cog.handle_vote_click(interaction, "approve")
                return interaction

            interaction = asyncio.run(_scenario())
            interaction.followup.send.assert_awaited_once_with("❌ 本次投票已结束。", ephemeral=True)

    def test_vote_lock_is_kept_after_click_completes(self):
        with self.click_env() as (cog, _target):
            async def _scenario():
                await self.seed_vote(cog)
                interaction = DummyVoteInteraction(make_member(100, [VOTE_ROLE_ID]))
                await cog.handle_vote_click(interaction, "approve")
                return dict(cog._vote_locks)

            locks = asyncio.run(_scenario())
            self.assertIn(str(PANEL_MESSAGE_ID), locks)

    def test_late_click_serializes_behind_queued_click(self):
        with self.click_env() as (cog, target):
            original_progress = cog.update_vote_progress
            original_delete = cog._delete_punished_message
            holding = asyncio.Event()
            release = asyncio.Event()

            async def _blocked_progress(*args, **kwargs):
                holding.set()
                await release.wait()
                return await original_progress(*args, **kwargs)

            async def _slow_delete(vote):
                # Keep the queued click inside its critical section long enough for the late
                # click to reach the DB; a pruned lock would let it read the pre-decision state.
                await asyncio.sleep(0.05)
                return await original_delete(vote)

            async def _scenario():
                await self.seed_vote(cog)
                with mock.patch.object(
                    cog, "update_vote_progress", new=mock.AsyncMock(side_effect=_blocked_progress)
                ), mock.patch.object(
                    cog, "_delete_punished_message", new=mock.AsyncMock(side_effect=_slow_delete)
                ):
                    first = asyncio.create_task(cog.handle_vote_click(
                        DummyVoteInteraction(make_member(100, [VOTE_ROLE_ID])), "approve"))
                    await holding.wait()
                    queued = asyncio.create_task(cog.handle_vote_click(
                        DummyVoteInteraction(make_member(101, [VOTE_ROLE_ID])), "approve"))
                    await asyncio.sleep(0)
                    release.set()
                    await first

                    late_interaction = DummyVoteInteraction(make_member(102, [VOTE_ROLE_ID]))
                    late = asyncio.create_task(cog.handle_vote_click(late_interaction, "approve"))
                    await asyncio.gather(queued, late)
                return await cog.get_vote_by_record_id(1), late_interaction

            vote, late_interaction = asyncio.run(_scenario())
            self.assertEqual(vote["status"], "executed")
            self.assertEqual(vote["approver_ids"], ["100", "101"])
            target.delete.assert_awaited_once()
            late_interaction.followup.send.assert_awaited_once_with("❌ 本次投票已结束。", ephemeral=True)

    def test_deletion_of_missing_message_still_completes_with_note(self):
        with self.click_env(target_deleted=True) as (cog, _target):
            async def _scenario():
                await self.seed_vote(cog, approver_ids=["100"])
                interaction = DummyVoteInteraction(make_member(101, [VOTE_ROLE_ID]))
                await cog.handle_vote_click(interaction, "approve")
                vote = await cog.get_vote_by_record_id(1)
                return vote, interaction

            vote, interaction = asyncio.run(_scenario())
            self.assertEqual(vote["status"], "executed")
            kwargs = interaction.edit_original_response.await_args.kwargs
            result_field = next(f for f in kwargs["embed"].fields if f.name == "投票结果")
            self.assertIn("原消息已不存在", result_field.value)


class PunishVoteHookTests(PunishVoteTestBase):
    def make_full_cog(self, bot, *, with_vote: bool):
        cog = self.make_vote_cog(bot, configured=with_vote)
        cog.enabled = True
        cog.sync_config = {"version": 1, "sync_guild_ids": [], "guilds": {}, "policy": {"mode": "best_effort"}}
        cog.allowed_roles = [1]
        cog.remove_roles = [7]
        cog.log_channel_ids = []
        cog.log_thread_ids = []
        cog.interface_channel_id = None
        cog.appeal_channel_id = None
        cog.reverify_link = ""
        cog.rules_link = ""
        cog.dm_templates = {}
        cog._punish_locks = {}
        cog._punish_locks_guard = asyncio.Lock()
        cog._scheduled_punishments = {}
        cog._scheduling_message_ids = set()
        cog._scheduling_user_ids = set()
        cog._schedule_creation_tasks = set()
        return cog

    def run_execute_punishment(self, cog):
        target_user = SimpleNamespace(id=42, name="Target", mention="<@42>")
        executor = SimpleNamespace(id=2, name="Admin", mention="<@2>")
        target_message = SimpleNamespace(
            id=TARGET_MESSAGE_ID,
            guild=SimpleNamespace(id=GUILD_ID),
            channel=SimpleNamespace(id=TARGET_CHANNEL_ID),
            author=target_user,
        )
        trigger_guild = SimpleNamespace(id=GUILD_ID)

        async def _driver():
            result = await cog.execute_punishment(
                trigger_guild=trigger_guild,
                target_user=target_user,
                target_message=target_message,
                reason="刷屏",
                executor=executor,
            )
            pending = [task for task in cog._vote_tasks if not task.done()]
            if pending:
                await asyncio.gather(*pending)
            return result

        return asyncio.run(_driver())

    @contextlib.contextmanager
    def punish_env(self, *, with_vote: bool):
        with temporary_workdir() as temp_dir, swapped_vote_db(temp_dir):
            vote_channel = DummySendChannel(VOTE_CHANNEL_ID)
            target_channel = DummyFetchChannel(TARGET_CHANNEL_ID)
            cog = self.make_full_cog(DummyVoteBot([vote_channel, target_channel]), with_vote=with_vote)
            cog.init_database()

            with mock.patch.object(cog, "_execute_role_removal_in_guild", new=mock.AsyncMock(
                return_value={"success": True, "guild_id": str(GUILD_ID), "guild_name": "测试服", "removed_roles": [7]}
            )), mock.patch.object(cog, "_build_dm_content", new=mock.AsyncMock(return_value="dm")), \
                mock.patch.object(cog, "send_dm", new=mock.AsyncMock(return_value=True)), \
                mock.patch.object(cog, "_send_channel_notification", new=mock.AsyncMock()):
                yield cog, vote_channel

    @mock.patch.object(quick_punish_core, "PUBLIC_NOTICE_PATH", Path("missing_public_notice.txt"))
    def test_successful_punishment_posts_vote_panel(self):
        with self.punish_env(with_vote=True) as (cog, vote_channel):
            success, _message, _history = self.run_execute_punishment(cog)

            self.assertTrue(success)
            vote_channel.send.assert_awaited_once()
            kwargs = vote_channel.send.await_args.kwargs
            self.assertIsInstance(kwargs["view"], PunishDeleteVoteView)
            vote = asyncio.run(cog.get_vote_by_record_id(1))
            self.assertIsNotNone(vote)
            self.assertEqual(vote["vote_message_id"], str(PANEL_MESSAGE_ID))

    @mock.patch.object(quick_punish_core, "PUBLIC_NOTICE_PATH", Path("missing_public_notice.txt"))
    def test_punishment_without_vote_config_skips_panel(self):
        with self.punish_env(with_vote=False) as (cog, vote_channel):
            success, _message, _history = self.run_execute_punishment(cog)

            self.assertTrue(success)
            vote_channel.send.assert_not_awaited()
            self.assertEqual(cog._vote_tasks, set())
            with sqlite3.connect(quick_punish.db.QUICK_PUNISH_DB_PATH) as conn:
                count = conn.execute("SELECT COUNT(*) FROM quick_punish_votes").fetchone()[0]
            self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
