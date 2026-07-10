"""瘦身后的 UnansweredFilter Cog —— 编排、命令、定时任务、报告。"""

import asyncio
import datetime
import io
import os
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from paths import REVIEWER_DB

from .thread_cache import ThreadCache
from .thread_scanner import ThreadScanner

# ================= 配置映射 =================
# 新手开帖 论坛频道 ID
try:
    TARGET_FORUM_ID = int(os.getenv("TARGET_FORUM_ID", 0))
except (TypeError, ValueError):
    TARGET_FORUM_ID = 0

# 新手答疑 汇报频道 ID
try:
    REPORT_CHANNEL_ID = int(os.getenv("REPORT_CHANNEL_ID", 0))
except (TypeError, ValueError):
    REPORT_CHANNEL_ID = 0

# 已解决标签 ID
try:
    RESOLVED_TAG_ID = int(os.getenv("RESOLVED_TAG_ID", 0))
except (TypeError, ValueError):
    RESOLVED_TAG_ID = 0

# 待解决标签 ID
try:
    UNSOLVED_TAG_ID = int(os.getenv("UNSOLVED_TAG_ID", 0))
except (TypeError, ValueError):
    UNSOLVED_TAG_ID = 0

# 优先使用通用模型，如果没有则回退到图片描述模型
AI_MODEL_NAME = os.getenv("OPENAI_MODEL") or os.getenv("IMAGE_DESCRIBE_MODEL")
RESOLVED_TAG_NAME = os.getenv("RESOLVED_TAG_NAME", "已解决")
DB_DIR = str(REVIEWER_DB.parent)
DB_PATH = str(REVIEWER_DB)
REVIEWER_DISPLAY_TIMEZONE_NAME = "Asia/Shanghai"
REVIEWER_DISPLAY_TIMEZONE_FILE_LABEL = "Asia-Shanghai"
try:
    REVIEWER_DISPLAY_TIMEZONE = ZoneInfo(REVIEWER_DISPLAY_TIMEZONE_NAME)
except ZoneInfoNotFoundError:
    REVIEWER_DISPLAY_TIMEZONE = datetime.timezone(
        datetime.timedelta(hours=8),
        name=REVIEWER_DISPLAY_TIMEZONE_NAME,
    )


class UnansweredFilter(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # AI 请求/响应跟踪信息（用于手动扫描后的管理员私信报告）
        self._last_ai_raw_response_text: str | None = None
        self._last_ai_response_note: str = "尚未调用 AI"
        self._ai_request_logs: list[dict[str, Any]] = []
        self._ai_expected_total_batches: int = 0
        self._ai_expected_total_threads: int = 0

        # 批处理配置
        self._ai_batch_size: int = 4
        self._ai_batch_interval_seconds: int = 10

        # 拆分出的模块
        self.cache = ThreadCache(DB_DIR, DB_PATH)
        self.scanner = ThreadScanner(
            bot,
            self.cache,
            ai_model_name=AI_MODEL_NAME,
            target_forum_id=TARGET_FORUM_ID,
            resolved_tag_id=RESOLVED_TAG_ID,
            unsolved_tag_id=UNSOLVED_TAG_ID,
            batch_size=self._ai_batch_size,
            batch_interval_seconds=self._ai_batch_interval_seconds,
        )

        # Run the scheduler at UTC 04:00, which is 12:00 in the reviewer business timezone.
        self.daily_check_task.start()

    async def cog_load(self):
        await asyncio.to_thread(self.cache.ensure_ready)

    def cog_unload(self):
        self.daily_check_task.cancel()

    # ================= 兼容层：保持旧方法签名供测试使用 =================
    # 测试通过 object.__new__ 创建裸 cog（跳过 __init__），直接调用这些方法。
    # _get_cache() 惰性创建 ThreadCache，读取当前模块级 DB_DIR / DB_PATH。

    def _get_cache(self) -> ThreadCache:
        if not hasattr(self, "cache"):
            self.cache = ThreadCache(DB_DIR, DB_PATH)
        return self.cache

    def _get_scanner(self) -> ThreadScanner:
        if not hasattr(self, "scanner"):
            batch_size = getattr(self, "_ai_batch_size", 4)
            batch_interval = getattr(self, "_ai_batch_interval_seconds", 10)
            self.scanner = ThreadScanner(
                self.bot,
                self._get_cache(),
                ai_model_name=AI_MODEL_NAME,
                target_forum_id=TARGET_FORUM_ID,
                resolved_tag_id=RESOLVED_TAG_ID,
                unsolved_tag_id=UNSOLVED_TAG_ID,
                batch_size=batch_size,
                batch_interval_seconds=batch_interval,
            )
        return self.scanner

    def _ensure_db_ready(self):
        self._get_cache().ensure_ready()

    def _get_cached_thread_sync(self, thread_id: int):
        return self._get_cache().get_sync(thread_id)

    async def _get_cached_thread(self, thread_id: int):
        return await self._get_cache().get(thread_id)

    def _update_thread_cache_sync(self, thread_id: int, last_msg_id: int, reply_count: int, status: str, reason: str):
        self._get_cache().update_sync(thread_id, last_msg_id, reply_count, status, reason)

    async def _update_thread_cache(self, thread_id: int, last_msg_id: int, reply_count: int, status: str, reason: str):
        await self._get_cache().update(thread_id, last_msg_id, reply_count, status, reason)

    def _delete_thread_cache_sync(self, thread_id: int):
        self._get_cache().delete_sync(thread_id)

    async def _delete_thread_cache(self, thread_id: int):
        await self._get_cache().delete(thread_id)

    async def _fetch_and_prepare_batch(self):
        # 兼容层：每次读取当前模块级常量，因为测试可能在调用之间修改它们。
        if not TARGET_FORUM_ID:
            print("❌ [Unanswered] 未配置 TARGET_CHANNEL_OR_THREAD")
            return None, None, [], []
        scanner = ThreadScanner(
            self.bot,
            self._get_cache(),
            ai_model_name=AI_MODEL_NAME,
            target_forum_id=TARGET_FORUM_ID,
            resolved_tag_id=RESOLVED_TAG_ID,
            unsolved_tag_id=UNSOLVED_TAG_ID,
            batch_size=getattr(self, "_ai_batch_size", 4),
            batch_interval_seconds=getattr(self, "_ai_batch_interval_seconds", 10),
        )
        return await scanner.fetch_and_prepare_batch()

    # ================= 定时任务与指令 =================

    @tasks.loop(time=datetime.time(hour=4, minute=0))  # UTC scheduler; user-facing reviewer time is Asia/Shanghai.
    async def daily_check_task(self):
        await self.bot.wait_until_ready()
        print("⏰ [Unanswered] 执行每日扫描...")
        await self.execute_check()

    async def _safe_defer(self, interaction: discord.Interaction):
        """一个绝对安全的"占坑"函数。"""
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

    def _to_reviewer_display_time(self, value: datetime.datetime | None = None) -> datetime.datetime:
        """Convert a timestamp to the reviewer business/display timezone."""
        current = value or datetime.datetime.now(datetime.timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=datetime.timezone.utc)
        return current.astimezone(REVIEWER_DISPLAY_TIMEZONE)

    def _build_daily_report_title(self, now: datetime.datetime | None = None) -> str:
        """Build the daily report title in the reviewer business timezone."""
        display_now = self._to_reviewer_display_time(now)
        return f"📅 {display_now.strftime('%Y-%m-%d')} 待解决问题汇总（{REVIEWER_DISPLAY_TIMEZONE_NAME}）"

    def _build_ai_report_filename(self, now: datetime.datetime | None = None) -> str:
        """Build the manual-report filename using the reviewer business timezone."""
        display_now = self._to_reviewer_display_time(now)
        timestamp = display_now.strftime("%Y%m%d_%H%M%S")
        return (
            f"unanswered_last_ai_response_{timestamp}_"
            f"{REVIEWER_DISPLAY_TIMEZONE_FILE_LABEL}.txt"
        )

    def _build_last_ai_response_txt(self, now: datetime.datetime | None = None) -> str:
        """生成用于私信附件的 AI 批处理执行报告（含每批原始响应）。"""
        display_now = self._to_reviewer_display_time(now)
        now_str = display_now.strftime("%Y-%m-%d %H:%M:%S %z")

        total_logs = len(self._ai_request_logs)
        api_success = sum(1 for x in self._ai_request_logs if x.get("ok"))
        json_success = sum(1 for x in self._ai_request_logs if x.get("json_ok"))
        api_failed = total_logs - api_success
        json_failed = api_success - json_success

        lines = [
            f"生成时间({REVIEWER_DISPLAY_TIMEZONE_NAME}): {now_str}",
            f"模型: {AI_MODEL_NAME or '未配置'}",
            f"状态: {self._last_ai_response_note}",
            f"待分析帖子数: {self._ai_expected_total_threads}",
            f"计划请求数: {self._ai_expected_total_batches}（每批最多{self._ai_batch_size}帖，间隔{self._ai_batch_interval_seconds}秒）",
            f"实际执行批次: {total_logs}",
            f"接口成功: {api_success} | 接口失败: {api_failed}",
            f"JSON可解析: {json_success} | JSON异常: {json_failed}",
            ""
        ]

        if not self._ai_request_logs:
            lines.append("(本次没有 AI 批次请求记录)\n")
            return "\n".join(lines)

        lines.append("===== AI_BATCH_SUMMARY_BEGIN =====")
        for log in self._ai_request_logs:
            batch_idx = log.get("batch_index", "?")
            total_batches = log.get("total_batches", "?")
            thread_count = log.get("thread_count", 0)
            thread_ids = log.get("thread_ids", [])
            thread_ids_text = ", ".join(str(x) for x in thread_ids) if thread_ids else "-"
            ok_text = "SUCCESS" if log.get("ok") else "FAILED"
            json_text = "YES" if log.get("json_ok") else "NO"
            duration_ms = log.get("duration_ms", 0)
            error_text = (log.get("error") or "").strip()

            lines.append(f"[Batch {batch_idx}/{total_batches}] status={ok_text}, json_ok={json_text}, results={log.get('results_count', 0)}, duration_ms={duration_ms}")
            lines.append(f"thread_count={thread_count}, thread_ids=[{thread_ids_text}]")
            if error_text:
                lines.append(f"error={error_text}")
            lines.append("")
        lines.append("===== AI_BATCH_SUMMARY_END =====")
        lines.append("")

        for log in self._ai_request_logs:
            batch_idx = log.get("batch_index", "?")
            total_batches = log.get("total_batches", "?")
            raw_text = (log.get("raw_text") or "").strip()
            if not raw_text:
                raw_text = "(空响应)"

            lines.append(f"===== BATCH_{batch_idx}_OF_{total_batches}_RAW_BEGIN =====")
            lines.append(raw_text)
            lines.append(f"===== BATCH_{batch_idx}_OF_{total_batches}_RAW_END =====")
            lines.append("")

        return "\n".join(lines)

    async def _notify_manual_check_result(self, user, result_msg: str, success: bool):
        """手动扫描结束后，私信管理员并附带 AI 批处理报告 txt。"""
        status = "成功" if success else "失败"
        txt_content = self._build_last_ai_response_txt()
        file_name = self._build_ai_report_filename()
        file_obj = discord.File(io.BytesIO(txt_content.encode("utf-8")), filename=file_name)

        await user.send(
            f"🔔 **待办清单扫描任务已结束（{status}）**\n{result_msg}\n\n"
            f"已附带本次 AI 批处理执行报告（txt）。",
            file=file_obj
        )

    @app_commands.command(name="待办清单", description="[管理员] 强制执行一次待解决帖子扫描")
    async def manual_check(self, interaction: discord.Interaction):
        # 权限检查：仅管理员
        user_id = interaction.user.id
        admins = getattr(self.bot, 'admins', [])

        if user_id not in admins:
            await interaction.response.send_message("❌ 权限不足，仅限管理员使用", ephemeral=True)
            return

        # 黄金法则：永远先 defer
        await self._safe_defer(interaction)

        # 每次手动扫描前重置 AI 报告状态，避免误发旧记录
        self._last_ai_raw_response_text = None
        self._last_ai_response_note = "本次手动扫描尚未调用 AI"
        self._ai_request_logs = []
        self._ai_expected_total_batches = 0
        self._ai_expected_total_threads = 0

        run_success = False
        try:
            stats = await self.execute_check()
            run_success = True
            result_msg = f"✅ 扫描完成。\n自动归档: {stats['solved']} 个\n待解决汇报: {stats['unsolved']} 个"
        except Exception as e:
            self._last_ai_response_note = f"扫描流程异常: {e}"
            result_msg = f"❌ 扫描失败：{e}"

        await interaction.edit_original_response(content=result_msg)

        try:
            await self._notify_manual_check_result(interaction.user, result_msg, run_success)
        except Exception as e:
            print(f"⚠️ 无法发送私信通知给 {interaction.user.name}: {e}")

    async def execute_check(self):
        # 每轮扫描都重置一次 AI 报告状态
        self._last_ai_raw_response_text = None
        self._last_ai_response_note = "本次扫描尚未调用 AI"
        self._ai_request_logs = []
        self._ai_expected_total_batches = 0
        self._ai_expected_total_threads = 0

        resolved_tag, unsolved_tag, threads_to_analyze, unchanged_results = await self.scanner.fetch_and_prepare_batch()

        if not resolved_tag:
            self._last_ai_response_note = "扫描提前结束：未找到目标论坛或已解决标签"
            return {"solved": 0, "unsolved": 0}

        # 1. AI 批次判定
        ai_results_map: dict[int, dict[str, Any]] = {}
        thread_batch_index_map: dict[int, int] = {}

        if threads_to_analyze:
            batches = ThreadScanner.chunk_threads(threads_to_analyze, self._ai_batch_size)
            total_batches = len(batches)
            self._ai_expected_total_threads = len(threads_to_analyze)
            self._ai_expected_total_batches = total_batches

            print(f"🧾 [Unanswered] 本次待 AI 判定 {self._ai_expected_total_threads} 帖，预计发送 {total_batches} 个请求（每批最多{self._ai_batch_size}帖，间隔{self._ai_batch_interval_seconds}s）。")

            for idx, batch in enumerate(batches, start=1):
                for item in batch:
                    t_id = int(item.get("data", {}).get("id", 0))
                    if t_id:
                        thread_batch_index_map[t_id] = idx

                batch_log = await self.scanner.call_gemini_batch(batch, idx, total_batches)
                self._ai_request_logs.append(batch_log)

                # 跟踪最近一次非空 AI 原始响应
                raw_text = batch_log.get("raw_text", "").strip()
                if raw_text:
                    self._last_ai_raw_response_text = raw_text
                elif not self._last_ai_raw_response_text:
                    self._last_ai_raw_response_text = ""

                parsed = batch_log.get("parsed", {})
                results_list = parsed.get("results", []) if isinstance(parsed, dict) else []
                for res in results_list:
                    t_id_raw = res.get("id")
                    try:
                        t_id = int(t_id_raw)
                    except (TypeError, ValueError):
                        continue
                    ai_results_map[t_id] = res

                if idx < total_batches:
                    print(f"⏳ [Unanswered] 批次 {idx}/{total_batches} 完成，等待 {self._ai_batch_interval_seconds} 秒后发送下一批...")
                    await asyncio.sleep(self._ai_batch_interval_seconds)

            total_logs = len(self._ai_request_logs)
            api_success = sum(1 for x in self._ai_request_logs if x.get("ok"))
            json_success = sum(1 for x in self._ai_request_logs if x.get("json_ok"))
            api_failed = total_logs - api_success
            json_failed = api_success - json_success
            self._last_ai_response_note = (
                f"AI批次完成：共{total_logs}包，接口成功{api_success}包，JSON可解析{json_success}包，"
                f"接口失败{api_failed}包，解析失败{json_failed}包"
            )
        else:
            self._last_ai_response_note = "本次扫描无需 AI 判定（无新增/变更帖子）"

        batch_logs_map: dict[int, dict[str, Any]] = {}
        for log in self._ai_request_logs:
            batch_index = log.get("batch_index")
            if batch_index is not None:
                batch_logs_map[int(batch_index)] = log

        # 2. 结果汇总
        final_solved = []
        final_unsolved = []

        for item in threads_to_analyze:
            t = item['thread_obj']
            res = ai_results_map.get(t.id)

            status = "unsolved"
            reason = ThreadScanner.build_ai_fallback_reason(t.id, thread_batch_index_map, batch_logs_map)
            reply_cnt = item['data']['true_reply_count']

            if res:
                status = str(res.get("status", "unsolved")).strip().lower()
                if status not in ("solved", "unsolved"):
                    status = "unsolved"
                reason = res.get("reason", "无理由")
                if not isinstance(reason, str):
                    reason = str(reason)

            reason = reason.strip() if isinstance(reason, str) else str(reason)
            if not reason:
                reason = "无理由"

            last_msg_id = t.last_message_id or 0
            await self.cache.update(t.id, last_msg_id, reply_cnt, status, reason)

            if status == "solved":
                final_solved.append((t, reason))
            else:
                final_unsolved.append((t, reply_cnt))

        for item in unchanged_results:
            if item['status'] == "solved":
                final_solved.append((item['thread_obj'], item['reason']))
            else:
                final_unsolved.append((item['thread_obj'], item['reply_count']))

        # 3. 执行操作：贴标签
        # 3.1 处理【已解决】的帖子
        for t, reason in final_solved:
            should_edit = False
            current_tags = list(t.applied_tags)
            new_tags = []

            if unsolved_tag and unsolved_tag in current_tags:
                new_tags = [tag for tag in current_tags if tag.id != unsolved_tag.id]
                should_edit = True
            else:
                new_tags = list(current_tags)

            if resolved_tag not in new_tags:
                if len(new_tags) >= 5:
                    new_tags.pop(0)
                new_tags.append(resolved_tag)
                should_edit = True

            if should_edit:
                try:
                    await ThreadScanner.edit_thread_tags(t, new_tags, "AI判定已解决(自动互斥)")
                except Exception as e:
                    print(f"❌ [Solved] 标签变更失败 {t.name}: {e}")
                    continue

                if resolved_tag not in current_tags and not t.archived:
                    try:
                        embed = discord.Embed(
                            description=f"✅ **检测到本帖已满足解决条件**\n理由：{reason}\n(如有异议，请回复本帖，系统将自动撤销标签)",
                            color=discord.Color.green()
                        )
                        await t.send(embed=embed)
                    except Exception as e:
                        print(f"⚠️ [Solved] {t.name} 标签已更新，但发送提示失败: {e}")

        # 3.2 处理【待解决】的帖子 (补全标签)
        if unsolved_tag:
            for t, _ in final_unsolved:
                if resolved_tag in t.applied_tags:
                    continue

                if unsolved_tag not in t.applied_tags:
                    try:
                        new_tags = list(t.applied_tags)
                        if len(new_tags) >= 5:
                            new_tags.pop(0)
                        new_tags.append(unsolved_tag)
                        await ThreadScanner.edit_thread_tags(t, new_tags, "AI判定待解决(补全标签)")
                        print(f"🔹 [Unanswered] 为 {t.name} 补全了待解决标签")
                    except Exception as e:
                        print(f"❌ [Unsolved] 标签补全失败 {t.name}: {e}")

        # 4. 执行操作：发送汇报
        if final_unsolved and REPORT_CHANNEL_ID:
            report_channel = self.bot.get_channel(REPORT_CHANNEL_ID)
            if isinstance(report_channel, discord.abc.Messageable):
                zero_replies = [x for x in final_unsolved if x[1] == 0]
                others = [x for x in final_unsolved if x[1] > 0]

                embed = discord.Embed(
                    title=self._build_daily_report_title(),
                    description="以下问题仍待解决，请大家看看是否能提供帮助！",
                    color=discord.Color.orange()
                )

                if zero_replies:
                    lines = []
                    for t, _cnt in zero_replies[:10]:
                        lines.append(f"🚨 **[{t.name}]({t.jump_url})** <t:{int(t.created_at.timestamp())}:R>")

                    if len(zero_replies) > 10:
                        lines.append(f"...还有 {len(zero_replies)-10} 个零回复帖子")

                    embed.add_field(name=f"🆘 零回复救援区 ({len(zero_replies)})", value="\n".join(lines), inline=False)

                if others:
                    lines = []
                    for t, cnt in others[:10]:
                        lines.append(f"• [{t.name}]({t.jump_url}) ({cnt}条他人回复)")

                    if len(others) > 10:
                        lines.append(f"...还有 {len(others)-10} 个讨论中帖子")

                    embed.add_field(name=f"💬 讨论进行中 ({len(others)})", value="\n".join(lines), inline=False)

                try:
                    await report_channel.send(embed=embed)
                except Exception as e:
                    print(f"❌ 发送汇报失败: {e}")

        return {"solved": len(final_solved), "unsolved": len(final_unsolved)}

    # ================= 反悔重开机制 =================
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """监听已解决帖子的新回复，自动重开"""
        if message.author.bot:
            return

        if not isinstance(message.channel, discord.Thread):
            return

        thread = message.channel
        if thread.parent_id != TARGET_FORUM_ID:
            return

        if not isinstance(thread.parent, discord.ForumChannel):
            return

        thread_created_at = thread.created_at
        if thread_created_at is None:
            return

        now = datetime.datetime.now(datetime.timezone.utc)
        thread_age = now - thread_created_at
        if thread_age.days >= 14:
            return

        resolved_tag = next((t for t in thread.parent.available_tags if t.id == RESOLVED_TAG_ID), None)
        unsolved_tag = next((t for t in thread.parent.available_tags if t.id == UNSOLVED_TAG_ID), None)

        if resolved_tag and resolved_tag in thread.applied_tags:
            try:
                new_tags = [t for t in thread.applied_tags if t.id != resolved_tag.id]

                if unsolved_tag and unsolved_tag not in new_tags:
                    if len(new_tags) >= 5:
                        new_tags.pop(0)
                    new_tags.append(unsolved_tag)

                await thread.edit(applied_tags=new_tags, reason=f"用户 {message.author.name} 新增回复，自动重开")

                await thread.send("🔓 **检测到新回复，已自动切换为「❓待解决」标签。**\n本帖将进入明日的自动扫描队列。")

                await self.cache.delete(thread.id)
                print(f"🔓 [Unanswered] 帖子 {thread.id} 已重开")

            except Exception as e:
                print(f"❌ 反悔重开失败: {e}")
