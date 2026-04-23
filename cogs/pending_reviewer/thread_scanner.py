"""帖子数据抓取 + AI 批处理调用。"""

import asyncio
import datetime
import json
from collections.abc import Sequence
from typing import Any

import discord

from .ai_parser import parse_ai_json_response, stringify_ai_content
from .thread_cache import ThreadCache


# ================= 配置常量（由 cog 模块级导入） =================
# 这些由 cog.py 中的模块级常量提供，这里通过构造参数或直接从上层 import。


class ThreadScanner:
    """数据抓取 + AI 批处理调用，不持有 Discord 命令或定时任务。"""

    def __init__(
        self,
        bot,
        cache: ThreadCache,
        *,
        ai_model_name: str | None,
        target_forum_id: int,
        resolved_tag_id: int,
        unsolved_tag_id: int,
        batch_size: int = 4,
        batch_interval_seconds: int = 10,
    ):
        self.bot = bot
        self.cache = cache
        self._ai_model_name = ai_model_name
        self._target_forum_id = target_forum_id
        self._resolved_tag_id = resolved_tag_id
        self._unsolved_tag_id = unsolved_tag_id
        self._ai_batch_size = batch_size
        self._ai_batch_interval_seconds = batch_interval_seconds

    # ================= 静态 / 纯函数方法 =================

    @staticmethod
    def summarize_attachments(attachments: Sequence[discord.Attachment]) -> str:
        """把附件转换为纯文本占位符，如 [图片附件x3] [文件附件x2]。"""
        if not attachments:
            return ""

        image_count = 0
        file_count = 0
        image_exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".heic"}

        for att in attachments:
            is_image = False
            if att.content_type and att.content_type.startswith("image/"):
                is_image = True
            else:
                lower_name = (att.filename or "").lower()
                for ext in image_exts:
                    if lower_name.endswith(ext):
                        is_image = True
                        break

            if is_image:
                image_count += 1
            else:
                file_count += 1

        parts = []
        if image_count > 0:
            parts.append(f"[图片附件x{image_count}]")
        if file_count > 0:
            parts.append(f"[文件附件x{file_count}]")
        return " ".join(parts)

    @staticmethod
    def chunk_threads(threads_data: list[dict[str, Any]], batch_size: int) -> list[list[dict[str, Any]]]:
        """按配置的批大小切分待分析帖子。"""
        if not threads_data:
            return []

        size = max(1, batch_size)
        return [threads_data[i:i + size] for i in range(0, len(threads_data), size)]

    @staticmethod
    def build_ai_fallback_reason(thread_id: int, batch_index_map: dict[int, int], batch_logs_map: dict[int, dict[str, Any]]) -> str:
        """当线程没有拿到有效 AI 结果时，生成可落库的失败原因。"""
        batch_idx = batch_index_map.get(thread_id)
        if not batch_idx:
            return "AI未命中批次/判定失败"

        log = batch_logs_map.get(batch_idx)
        if not log:
            return f"AI批次{batch_idx}日志缺失"

        if not log.get("ok"):
            err = (log.get("error") or "未知错误").strip()
            return f"AI请求失败(批次{batch_idx}): {err}"

        if not log.get("json_ok"):
            err = (log.get("error") or "响应无法解析JSON").strip()
            return f"AI响应异常(批次{batch_idx}): {err}"

        return f"AI结果缺失(批次{batch_idx})"

    # ================= 数据抓取 =================

    async def fetch_and_prepare_batch(self):
        """拉取帖子，计算真实回复数，构建发送给AI的数据包"""
        if not self._target_forum_id:
            print("❌ [Unanswered] 未配置 TARGET_CHANNEL_OR_THREAD")
            return None, None, [], []

        forum_channel = self.bot.get_channel(self._target_forum_id)
        if not forum_channel:
            try:
                forum_channel = await self.bot.fetch_channel(self._target_forum_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                print(f"❌ [Unanswered] 无法获取论坛频道 {self._target_forum_id}")
                return None, None, [], []

        if not isinstance(forum_channel, discord.ForumChannel):
            print(f"❌ [Unanswered] 频道 {self._target_forum_id} 不是论坛频道，无法使用标签功能")
            return None, None, [], []

        resolved_tag = next((t for t in forum_channel.available_tags if t.id == self._resolved_tag_id), None)
        if not resolved_tag:
            print(f"❌ [Unanswered] 找不到已解决标签")
            return None, None, [], []

        unsolved_tag = next((t for t in forum_channel.available_tags if t.id == self._unsolved_tag_id), None)

        target_date = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)

        threads_to_analyze = []
        unchanged_results = []

        all_threads = list(forum_channel.threads)
        try:
            async for t in forum_channel.archived_threads(limit=50):
                if t.created_at >= target_date:
                    all_threads.append(t)
        except discord.HTTPException as e:
            print(f"⚠️ [Unanswered] 获取归档帖子失败: {e}")

        print(f"🔍 [Unanswered] 开始扫描 {len(all_threads)} 个帖子...")

        for thread in all_threads:
            if resolved_tag in thread.applied_tags:
                continue
            if thread.created_at < target_date:
                continue

            try:
                recent_msgs = [m async for m in thread.history(limit=10, oldest_first=False)]
            except discord.HTTPException as e:
                print(f"⚠️ 无法读取帖子 {thread.id} 历史: {e}")
                continue

            if not recent_msgs:
                continue

            helper_replies = [m for m in recent_msgs if m.author.id != thread.owner_id]
            true_reply_count = len(helper_replies)

            last_msg = recent_msgs[0]
            last_msg_id = last_msg.id
            time_since_active = datetime.datetime.now(datetime.timezone.utc) - last_msg.created_at
            days_silent = time_since_active.days

            cached = await self.cache.get(thread.id)
            if cached and cached[0] == last_msg_id and cached[1] == true_reply_count:
                if days_silent < 7:
                    unchanged_results.append({
                        "thread_obj": thread,
                        "status": cached[2],
                        "reason": cached[3],
                        "reply_count": true_reply_count
                    })
                    continue

            starter_msg = None
            if len(recent_msgs) < 10:
                starter_msg = recent_msgs[-1]
            else:
                try:
                    async for m in thread.history(limit=1, oldest_first=True):
                        starter_msg = m
                        break
                except discord.HTTPException:
                    pass

            if not starter_msg:
                continue

            history_text = []
            for m in reversed(recent_msgs[:10]):
                role = "楼主" if m.author.id == thread.owner_id else f"用户{m.author.name}"
                attachment_hint = self.summarize_attachments(m.attachments)
                content_text = (m.content or "").strip()
                combined_text = " ".join([x for x in [content_text, attachment_hint] if x]).strip()
                if not combined_text:
                    combined_text = "(无文本内容)"
                history_text.append(f"[{m.created_at.strftime('%Y-%m-%d')}] {role}: {combined_text}")

            starter_content_text = (starter_msg.content or "").strip()
            starter_attachment_hint = self.summarize_attachments(starter_msg.attachments)

            threads_to_analyze.append({
                "thread_obj": thread,
                "data": {
                    "id": thread.id,
                    "title": thread.name,
                    "created_at": str(thread.created_at),
                    "days_silent": days_silent,
                    "true_reply_count": true_reply_count,
                    "starter_content": starter_content_text,
                    "starter_attachments": starter_attachment_hint,
                    "recent_history": history_text
                }
            })

        return resolved_tag, unsolved_tag, threads_to_analyze, unchanged_results

    # ================= AI 批处理 =================

    async def call_gemini_batch(self, threads_data: list[dict[str, Any]], batch_index: int, total_batches: int) -> dict[str, Any]:
        """发送单个批次的审计请求给 Gemini，返回结构化执行结果。"""
        started_at = datetime.datetime.now(datetime.timezone.utc)
        thread_ids = [int(item.get("data", {}).get("id", 0)) for item in threads_data]

        result: dict[str, Any] = {
            "batch_index": batch_index,
            "total_batches": total_batches,
            "thread_ids": [tid for tid in thread_ids if tid],
            "thread_count": len(threads_data),
            "started_at": started_at.isoformat(),
            "ended_at": None,
            "duration_ms": 0,
            "ok": False,
            "json_ok": False,
            "results_count": 0,
            "error": "",
            "raw_text": "",
            "parsed": {}
        }

        if not threads_data:
            result["error"] = "空批次，无需请求"
            ended_at = datetime.datetime.now(datetime.timezone.utc)
            result["ended_at"] = ended_at.isoformat()
            result["duration_ms"] = int((ended_at - started_at).total_seconds() * 1000)
            return result

        system_prompt = """
        你需要批量分析以下帖子数据，并判断其状态，以 JSON 格式返回判断结果。

        【判定标准】
        1. 已解决:
           - 楼主回复了"谢谢""已解决""ok"等明确确认。
           - 有人给出了可行方案，且帖子静默超过7天 (days_silent >= 7)。
           - 有人追问细节但楼主超过7天未回。

        2. 待解决:
           - 对话仍在进行、问题描述不足或无明确结论。
           - 零回复 (true_reply_count == 0)：这是最高优先级，表示只有楼主在自言自语或完全没人理。
           - 方案被楼主明确否定。

        【任务】
        帖子内容中的附件会使用占位符表示：
        - [图片附件xN]
        - [文件附件xN]
        请只依据文字和上下文判断，不要臆测附件里的具体内容。

        返回内容必须为纯JSON，正文必须仅包含JSON。

        JSON结构:
        {
            "results": [
                {
                    "id": 12345,
                    "status": "solved" | "unsolved",
                    "reason": "简短判定理由"
                }
            ]
        }
        """

        payload_lines = ["请分析以下帖子数据："]
        for item in threads_data:
            t_data = item["data"]
            payload_lines.append(f"\n--- Thread {t_data['id']} ---")
            payload_lines.append(json.dumps(t_data, ensure_ascii=False))
        user_content = "\n".join(payload_lines)

        try:
            print(f"📤 [Unanswered] 发送 Gemini 请求（批次 {batch_index}/{total_batches}），包含 {len(threads_data)} 个帖子...")

            if not self.bot.openai_client:
                raise RuntimeError("OpenAI 客户端未初始化")

            response = await self.bot.openai_client.chat.completions.create(
                model=self._ai_model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}
                ],
                response_format={"type": "json_object"},
                temperature=0.5,
                max_tokens=4096
            )

            if not response or not getattr(response, "choices", None):
                result["ok"] = False
                result["error"] = "AI 返回空响应对象"
            else:
                content = response.choices[0].message.content
                raw_text = stringify_ai_content(content)
                result["raw_text"] = raw_text

                parsed = parse_ai_json_response(content)
                if parsed is None:
                    result["ok"] = True
                    result["json_ok"] = False
                    result["error"] = "AI 请求成功，但响应无法解析为 JSON"
                    result["parsed"] = {}
                else:
                    results_list = parsed.get("results", []) if isinstance(parsed, dict) else []
                    result["ok"] = True
                    result["json_ok"] = True
                    result["results_count"] = len(results_list)
                    result["parsed"] = parsed
                    result["error"] = ""

        except Exception as e:
            result["ok"] = False
            result["json_ok"] = False
            result["error"] = f"AI 请求失败: {e}"
            print(f"❌ [Unanswered] 批次 {batch_index}/{total_batches} 请求失败: {e}")

        finally:
            ended_at = datetime.datetime.now(datetime.timezone.utc)
            result["ended_at"] = ended_at.isoformat()
            result["duration_ms"] = int((ended_at - started_at).total_seconds() * 1000)

        return result

    # ================= 标签编辑 =================

    @staticmethod
    async def edit_thread_tags(
        thread: discord.Thread,
        new_tags: list[discord.ForumTag],
        reason: str
    ):
        """更新帖子标签：若帖子已归档，则先解档，更新标签后再归档。"""
        was_archived = bool(getattr(thread, "archived", False))

        if not was_archived:
            await thread.edit(applied_tags=new_tags, reason=reason)
            return

        await thread.edit(archived=False, reason="自动解档以更新标签")

        tag_error = None
        try:
            await thread.edit(applied_tags=new_tags, reason=reason)
        except Exception as e:
            tag_error = e
        finally:
            try:
                await thread.edit(archived=True, reason="更新标签后恢复归档")
            except Exception as archive_err:
                print(f"⚠️ [Unanswered] 帖子 {thread.id} 恢复归档失败: {archive_err}")
                if tag_error is None:
                    raise

        if tag_error is not None:
            raise tag_error
