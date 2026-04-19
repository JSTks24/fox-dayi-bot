import asyncio
import contextlib
import json
import mimetypes
import os
import random
import re
import time
import traceback
import uuid
from datetime import datetime
from typing import Any

import discord
import openai
from discord import app_commands
from discord.ext import commands

from cogs.utils import CooldownManager, safe_defer, encode_image_to_base64, compress_image, get_file_size_kb

# --- 从 bot.py 引入的辅助函数和类 ---

PUBLIC_ALLOWED_MENTIONS = discord.AllowedMentions.none()
PUBLIC_MESSAGE_LIMIT = 1900
STREAM_EDIT_INTERVAL_SECONDS = 3.0
STREAM_POLL_INTERVAL_SECONDS = 0.5
STREAM_TIMEOUT_SECONDS = 180.0
MAX_IMAGE_ATTACHMENTS = 3
MAX_REPLY_CHAIN_ROUNDS = 5
MAX_HISTORY_IMAGE_TURNS = 2
REPLY_CHAIN_SCAN_LIMIT = 120
PUBLIC_RENDER_OVERHEAD_BUFFER = 260
QD_META_VERSION = 1
DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."
REPLY_CHAIN_CONTEXT_SYSTEM_PROMPT = "以下消息来自 Discord 同一条回复链历史，请结合上下文并重点回答最后一条用户消息。"
STATUS_RECEIVED = "⏳ 收到请求，正在处理中，请稍候..."
STATUS_RESOLVING_CONTEXT = "🧵 正在整理回复链上下文，请稍候..."
STATUS_PROCESSING_IMAGES = "🖼️ 正在处理图片，请稍候..."
STATUS_REQUESTING_AI = "🤖 正在向 AI 请求回复，请稍候..."
QD_META_LINE_REGEX = re.compile(r"^\s*-# <\|qd-meta\|>(?P<payload>.+?)<\|/qd-meta\|>\s*$", re.MULTILINE)
QD_AUXILIARY_LINE_REGEX = re.compile(r"^\s*-# <\|qd-(?:footer|meta)\|>.*?<\|/qd-(?:footer|meta)\|>\s*$", re.MULTILINE)
QD_HEADER_REGEX = re.compile(r"^🦊 AI 回复(?:（续 \d+）)?\n\n")


class EmptyAIResponseError(RuntimeError):
    """AI 没有返回可用文本内容。"""


def build_qd_auxiliary_line(marker_name: str, content: str) -> str:
    return f"-# <|{marker_name}|>{content}<|/{marker_name}|>"


def build_qd_meta_line(*, session_id: str, source_message_id: int, chunk_index: int, total_chunks: int, kind: str) -> str:
    payload = json.dumps(
        {
            "v": QD_META_VERSION,
            "sid": session_id,
            "src": source_message_id,
            "idx": chunk_index,
            "tot": total_chunks,
            "kind": kind,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return build_qd_auxiliary_line("qd-meta", payload)


def extract_qd_meta(content: str) -> dict[str, Any] | None:
    match = QD_META_LINE_REGEX.search(content)
    if not match:
        return None

    try:
        payload = json.loads(match.group("payload"))
    except json.JSONDecodeError:
        return None

    if not isinstance(payload, dict):
        return None
    return payload


def strip_public_reply_markup(content: str) -> str:
    cleaned = QD_AUXILIARY_LINE_REGEX.sub("", content)
    cleaned = QD_HEADER_REGEX.sub("", cleaned, count=1)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


class PublicStreamReply:
    """管理公开普通消息的流式输出、节流编辑与自动分片。"""

    def __init__(
        self,
        source_message: discord.Message,
        display_model_name: str,
        requester_name: str,
        *,
        message_limit: int = PUBLIC_MESSAGE_LIMIT,
        edit_interval: float = STREAM_EDIT_INTERVAL_SECONDS,
        reply_session_id: str | None = None,
    ):
        self.source_message = source_message
        self.display_model_name = (display_model_name or "未知模型")[:80]
        self.requester_name = (requester_name or "未知用户")[:80]
        self.message_limit = message_limit
        self.edit_interval = edit_interval
        self.started_at = time.monotonic()
        self.reply_session_id = reply_session_id or uuid.uuid4().hex[:12]
        self.reply_kind = "partial"
        self.context_user_input_count = 1

        self.messages: list[discord.Message] = []
        self.full_text = ""
        self.last_edit_at = 0.0

        self._dirty = False
        self._show_footer = False
        self._closed = False
        self._flush_task: asyncio.Task | None = None
        self._render_lock = asyncio.Lock()

    @property
    def has_content(self) -> bool:
        return bool(self.full_text.strip())

    async def start(self, initial_text: str) -> discord.Message:
        async with self._render_lock:
            if self.messages:
                return self.messages[0]

            message = await self.source_message.reply(
                initial_text,
                mention_author=False,
                allowed_mentions=PUBLIC_ALLOWED_MENTIONS,
            )
            self.messages.append(message)
            self.last_edit_at = time.monotonic()
            self._flush_task = asyncio.create_task(self._flush_loop())
            return message

    async def set_status(self, status_text: str) -> None:
        if self.has_content:
            return

        if not self.messages:
            await self.start(status_text)
            return

        async with self._render_lock:
            if self._closed:
                return

            current_message = self.messages[0]
            if current_message.content != status_text:
                await current_message.edit(
                    content=status_text,
                    allowed_mentions=PUBLIC_ALLOWED_MENTIONS,
                )
                self.last_edit_at = time.monotonic()

    def append(self, delta_text: str) -> None:
        if self._closed or not delta_text:
            return
        self.full_text += delta_text
        self._dirty = True

    async def flush(self, *, force: bool = False) -> None:
        async with self._render_lock:
            if self._closed and not force:
                return

            response_text = self.full_text.rstrip()
            if not response_text:
                return
            if not force and not self._dirty:
                return

            desired_contents = self._split_response_text(response_text, include_footer=self._show_footer)
            any_updated = False

            for index, desired_content in enumerate(desired_contents):
                if index < len(self.messages):
                    target_message = self.messages[index]
                    if target_message.content != desired_content:
                        await target_message.edit(
                            content=desired_content,
                            allowed_mentions=PUBLIC_ALLOWED_MENTIONS,
                        )
                        any_updated = True
                else:
                    new_message = await self.source_message.reply(
                        desired_content,
                        mention_author=False,
                        allowed_mentions=PUBLIC_ALLOWED_MENTIONS,
                    )
                    self.messages.append(new_message)
                    any_updated = True

            self._dirty = False
            if any_updated:
                self.last_edit_at = time.monotonic()

    async def finalize(self) -> None:
        self.reply_kind = "answer"
        self._show_footer = True
        await self.flush(force=True)
        await self.close()

    async def publish_error(self, error_text: str) -> None:
        await self.flush(force=True)

        async with self._render_lock:
            if self.has_content:
                await self.source_message.reply(
                    error_text,
                    mention_author=False,
                    allowed_mentions=PUBLIC_ALLOWED_MENTIONS,
                )
            elif self.messages:
                if self.messages[0].content != error_text:
                    await self.messages[0].edit(
                        content=error_text,
                        allowed_mentions=PUBLIC_ALLOWED_MENTIONS,
                    )
                    self.last_edit_at = time.monotonic()
            else:
                await self.source_message.reply(
                    error_text,
                    mention_author=False,
                    allowed_mentions=PUBLIC_ALLOWED_MENTIONS,
                )

        await self.close()

    async def close(self) -> None:
        if self._closed:
            return

        self._closed = True
        if self._flush_task:
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task
            self._flush_task = None

    async def _flush_loop(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(STREAM_POLL_INTERVAL_SECONDS)
                should_flush = self._dirty and (time.monotonic() - self.last_edit_at >= self.edit_interval)
                if should_flush:
                    try:
                        await self.flush()
                    except Exception as e:
                        print(f"❌ [快速答疑] 流式消息刷新失败: {type(e).__name__}: {e}")
        except asyncio.CancelledError:
            pass

    def _split_response_text(self, text: str, *, include_footer: bool = False) -> list[str]:
        raw_chunks: list[str] = []
        remaining = text
        chunk_index = 0

        while remaining:
            header = self._build_chunk_header(chunk_index)
            available_length = max(200, self.message_limit - len(header) - PUBLIC_RENDER_OVERHEAD_BUFFER)

            if len(remaining) <= available_length:
                raw_chunks.append(remaining.rstrip())
                break

            split_at = remaining.rfind("\n", 0, available_length + 1)
            if split_at < int(available_length * 0.6):
                split_at = remaining.rfind(" ", 0, available_length + 1)
            if split_at < int(available_length * 0.6):
                split_at = available_length

            chunk_text = remaining[:split_at].rstrip()
            if not chunk_text:
                chunk_text = remaining[:available_length]
                split_at = len(chunk_text)

            raw_chunks.append(chunk_text)
            remaining = remaining[split_at:].lstrip("\n ")
            chunk_index += 1

        total_chunks = len(raw_chunks)
        rendered_chunks: list[str] = []
        for index, chunk_text in enumerate(raw_chunks):
            rendered_chunks.append(
                self._render_chunk_content(
                    chunk_text,
                    chunk_index=index,
                    total_chunks=total_chunks,
                    include_footer=include_footer and index == total_chunks - 1,
                )
            )
        return rendered_chunks

    def _render_chunk_content(self, text: str, *, chunk_index: int, total_chunks: int, include_footer: bool) -> str:
        content = f"{self._build_chunk_header(chunk_index)}{text}"
        if include_footer:
            content += f"\n\n{self._build_footer_line()}"
        content += f"\n{self._build_meta_line(chunk_index=chunk_index + 1, total_chunks=total_chunks)}"
        return content

    def _build_chunk_header(self, chunk_index: int) -> str:
        if chunk_index == 0:
            return "🦊 AI 回复\n\n"
        return f"🦊 AI 回复（续 {chunk_index + 1}）\n\n"

    def _build_footer_line(self) -> str:
        elapsed_seconds = max(1, int(round(time.monotonic() - self.started_at)))
        footer_text = (
            f"time: {elapsed_seconds} s | 由{self.display_model_name}提供支持 | ctx: {max(1, int(self.context_user_input_count))} turns | {self.requester_name} 问的"
        )
        return build_qd_auxiliary_line("qd-footer", footer_text)

    def _build_meta_line(self, *, chunk_index: int, total_chunks: int) -> str:
        return build_qd_meta_line(
            session_id=self.reply_session_id,
            source_message_id=self.source_message.id,
            chunk_index=chunk_index,
            total_chunks=total_chunks,
            kind=self.reply_kind,
        )


class AppDayi(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.message_cooldowns = CooldownManager(30)
        self.cooldown_duration = 30
        self._default_prompt_cache: str | None = None

        self.ctx_menu = app_commands.ContextMenu(
            name="快速答疑",
            callback=self.quick_dayi,
        )
        self.bot.tree.add_command(self.ctx_menu)

    async def cog_unload(self):
        """Cog 卸载时移除命令"""
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)

    def _is_image_attachment(self, attachment: discord.Attachment) -> bool:
        if attachment.content_type and attachment.content_type.startswith("image/"):
            return True
        guessed_type, _ = mimetypes.guess_type(attachment.filename)
        return bool(guessed_type and guessed_type.startswith("image/"))

    def _check_and_update_cooldown(self, message_id: int) -> tuple[bool, int]:
        """
        检查消息是否在冷却中，如果不在则更新冷却时间。

        Returns:
            (is_on_cooldown, remaining_seconds)
        """
        return self.message_cooldowns.check_and_update(message_id)

    async def _reply_public_text(self, message: discord.Message, content: str) -> discord.Message:
        return await message.reply(
            content,
            mention_author=False,
            allowed_mentions=PUBLIC_ALLOWED_MENTIONS,
        )

    async def _acknowledge_public_result(self, interaction: discord.Interaction, content: str) -> None:
        if not interaction.response.is_done():
            return
        try:
            await interaction.edit_original_response(content=content)
        except Exception as e:
            print(f"⚠️ [快速答疑] 更新交互占坑消息失败: {type(e).__name__}: {e}")

    async def _send_public_error(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
        content: str,
    ) -> None:
        await self._reply_public_text(message, content)
        await self._acknowledge_public_result(interaction, "ℹ️ 错误信息已公开发送到频道。")

    async def _publish_failure(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
        public_session: PublicStreamReply | None,
        content: str,
    ) -> None:
        if public_session:
            if public_session.has_content:
                error_content = f"⚠️ 回复生成中断，以上为已生成内容。\n{content}"
            else:
                error_content = content
            await public_session.publish_error(error_content)
        else:
            await self._reply_public_text(message, content)

        await self._acknowledge_public_result(interaction, "ℹ️ 错误信息已公开发送到频道。")

    def _get_active_ban_entry(self, target_user_id: str) -> dict[str, Any] | None:
        banlist_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "banlist.json")
        try:
            with open(banlist_path, encoding="utf-8") as f:
                banlist_data = json.load(f)
        except FileNotFoundError:
            print("⚠️ banlist.json 文件不存在，跳过封禁检查")
            return None
        except json.JSONDecodeError as e:
            print(f"❌ 解析 banlist.json 失败: {e}")
            return None
        except Exception as e:
            print(f"❌ 封禁检查出错: {e}")
            return None

        current_timestamp = datetime.now().timestamp()
        for ban_entry in banlist_data.get("banlist", []):
            if ban_entry.get("ID") != target_user_id:
                continue

            try:
                unbanned_at = int(ban_entry["unbanned_at"])
            except (KeyError, TypeError, ValueError):
                continue

            if current_timestamp < unbanned_at:
                return ban_entry

        return None

    def _format_ban_message(self, ban_entry: dict[str, Any]) -> str:
        unbanned_timestamp = int(ban_entry["unbanned_at"])
        formatted_date = datetime.fromtimestamp(unbanned_timestamp).strftime("%Y年%m月%d日 %H:%M:%S")
        return (
            "❌ 该用户已被开发者封禁\n\n"
            f"用户ID：{ban_entry['ID']}\n"
            f"封禁原因：{ban_entry['reason']}\n"
            f"解封时间：{formatted_date}"
        )

    def _get_display_model_name(self) -> str:
        random_model_names = os.getenv("RANDOM_MODEL_NAMES", "")
        if random_model_names:
            model_names = [name.strip() for name in random_model_names.split(",") if name.strip()]
            if model_names:
                return random.choice(model_names)
        return os.getenv("OPENAI_MODEL") or "未知模型"

    def _safe_int(self, value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _get_message_image_attachments(self, message: discord.Message) -> list[discord.Attachment]:
        return [attachment for attachment in message.attachments if self._is_image_attachment(attachment)]

    def _get_base_user_message_text(self, message: discord.Message, *, is_current: bool) -> str:
        content = (message.content or "").strip()
        if content:
            return content

        image_count = len(self._get_message_image_attachments(message))
        if image_count > 0:
            if is_current:
                return "请结合这条消息附带的图片内容进行分析并回答。"
            return "[该消息仅包含图片]"

        if is_current:
            return "这是什么问题，怎么解决"
        return "[该消息没有文本内容]"

    def _build_user_turn_prompt_text(self, turn: dict[str, Any]) -> str:
        message = turn["message"]
        text = self._get_base_user_message_text(message, is_current=bool(turn.get("is_current")))
        included_image_count = len(turn.get("image_paths", []))
        omitted_image_count = self._safe_int(turn.get("omitted_image_count"), 0)
        notes: list[str] = []

        if included_image_count > 0 and not (message.content or "").strip():
            notes.append(f"[已附带 {included_image_count} 张图片]")
        if omitted_image_count > 0:
            notes.append(f"[该消息另含 {omitted_image_count} 张图片，但因上下文限制未附带原图]")

        if notes:
            text = f"{text}\n\n" + "\n".join(notes)
        return text

    def _normalize_text_content(self, content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                    continue
                if isinstance(item, dict):
                    text_value = item.get("text")
                    if text_value:
                        parts.append(str(text_value))
                    continue
                text_value = getattr(item, "text", None)
                if text_value:
                    parts.append(str(text_value))
            return "".join(parts)
        return str(content)

    def _parse_quick_dayi_meta(self, message: discord.Message) -> dict[str, Any] | None:
        bot_user = self.bot.user
        if not bot_user or message.author.id != bot_user.id:
            return None

        meta = extract_qd_meta(message.content)
        if not meta:
            return None
        if self._safe_int(meta.get("v"), 0) != QD_META_VERSION:
            return None
        return meta

    async def _fetch_message_by_id(self, channel: Any, message_id: int) -> discord.Message | None:
        try:
            return await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            print(f"⚠️ [快速答疑] 获取消息 {message_id} 失败: {type(e).__name__}: {e}")
            return None

    async def _resolve_referenced_message(self, message: discord.Message) -> discord.Message | None:
        reference = message.reference
        if not reference or not reference.message_id:
            return None

        resolved = reference.resolved
        if isinstance(resolved, discord.Message):
            return resolved

        return await self._fetch_message_by_id(message.channel, reference.message_id)

    async def _reconstruct_quick_dayi_answer(
        self,
        bot_message: discord.Message,
    ) -> tuple[discord.Message, str] | None:
        meta = self._parse_quick_dayi_meta(bot_message)
        if not meta or str(meta.get("kind")) != "answer":
            return None

        session_id = str(meta.get("sid") or "")
        if not session_id:
            return None

        source_message_id = self._safe_int(meta.get("src"), 0)
        if source_message_id <= 0:
            return None

        source_message = await self._fetch_message_by_id(bot_message.channel, source_message_id)
        if not source_message:
            return None

        total_chunks = max(1, self._safe_int(meta.get("tot"), 1))
        initial_chunk_index = max(1, self._safe_int(meta.get("idx"), 1))
        collected_chunks: dict[int, discord.Message] = {initial_chunk_index: bot_message}
        scan_limit = max(REPLY_CHAIN_SCAN_LIMIT, total_chunks * 10)

        async for candidate in bot_message.channel.history(limit=scan_limit, after=source_message, oldest_first=True):
            if candidate.id == bot_message.id:
                continue
            if not self.bot.user or candidate.author.id != self.bot.user.id:
                continue
            if not candidate.reference or candidate.reference.message_id != source_message.id:
                continue

            candidate_meta = self._parse_quick_dayi_meta(candidate)
            if not candidate_meta or str(candidate_meta.get("kind")) != "answer":
                continue
            if str(candidate_meta.get("sid") or "") != session_id:
                continue

            chunk_index = max(1, self._safe_int(candidate_meta.get("idx"), len(collected_chunks) + 1))
            collected_chunks[chunk_index] = candidate
            if len(collected_chunks) >= total_chunks:
                break

        if len(collected_chunks) < total_chunks:
            print(
                f"⚠️ [快速答疑] 回复链重组不完整: sid={session_id}, "
                f"找到 {len(collected_chunks)}/{total_chunks} 段"
            )

        ordered_parts: list[str] = []
        for chunk_index in sorted(collected_chunks):
            chunk_text = strip_public_reply_markup(collected_chunks[chunk_index].content)
            if chunk_text:
                ordered_parts.append(chunk_text)

        assistant_text = "\n".join(ordered_parts).strip()
        if not assistant_text:
            return None

        return source_message, assistant_text

    async def _collect_reply_chain_history(self, current_message: discord.Message) -> list[dict[str, Any]]:
        history_pairs: list[dict[str, Any]] = []
        cursor_message = current_message
        visited_source_ids = {current_message.id}

        for _ in range(MAX_REPLY_CHAIN_ROUNDS):
            replied_message = await self._resolve_referenced_message(cursor_message)
            if not replied_message:
                break

            reconstructed = await self._reconstruct_quick_dayi_answer(replied_message)
            if not reconstructed:
                break

            source_message, assistant_text = reconstructed
            if source_message.id in visited_source_ids:
                print(f"⚠️ [快速答疑] 回复链出现循环，提前停止: {source_message.id}")
                break

            visited_source_ids.add(source_message.id)
            history_pairs.append(
                {
                    "user_message": source_message,
                    "assistant_text": assistant_text,
                }
            )
            cursor_message = source_message

        if history_pairs:
            print(f"🧵 [快速答疑] 命中回复链上下文: {len(history_pairs)} 轮")
        else:
            print("🧵 [快速答疑] 未命中可用的回复链上下文")

        return history_pairs

    def _select_context_image_counts(
        self,
        current_message: discord.Message,
        history_pairs_newest_first: list[dict[str, Any]],
    ) -> dict[int, int]:
        remaining_slots = MAX_IMAGE_ATTACHMENTS
        selected_counts: dict[int, int] = {}
        priority_messages = [current_message]
        priority_messages.extend(pair["user_message"] for pair in history_pairs_newest_first[:MAX_HISTORY_IMAGE_TURNS])

        for user_message in priority_messages:
            if remaining_slots <= 0:
                break

            image_attachments = self._get_message_image_attachments(user_message)
            if not image_attachments:
                continue

            selected_count = min(len(image_attachments), remaining_slots)
            selected_counts[user_message.id] = selected_count
            remaining_slots -= selected_count

        return selected_counts

    def _build_user_turn_spec(
        self,
        message: discord.Message,
        *,
        is_current: bool,
        selected_image_count: int,
    ) -> dict[str, Any]:
        image_attachments = self._get_message_image_attachments(message)
        selected_attachments = image_attachments[:selected_image_count]
        return {
            "role": "user",
            "message": message,
            "is_current": is_current,
            "selected_image_attachments": selected_attachments,
            "total_image_count": len(image_attachments),
            "image_paths": [],
            "omitted_image_count": max(0, len(image_attachments) - len(selected_attachments)),
        }

    def _build_conversation_turns(
        self,
        current_message: discord.Message,
        history_pairs_newest_first: list[dict[str, Any]],
        selected_image_counts: dict[int, int],
    ) -> list[dict[str, Any]]:
        conversation_turns: list[dict[str, Any]] = []

        for history_pair in reversed(history_pairs_newest_first):
            user_message = history_pair["user_message"]
            conversation_turns.append(
                self._build_user_turn_spec(
                    user_message,
                    is_current=False,
                    selected_image_count=selected_image_counts.get(user_message.id, 0),
                )
            )
            conversation_turns.append(
                {
                    "role": "assistant",
                    "text": history_pair["assistant_text"],
                }
            )

        conversation_turns.append(
            self._build_user_turn_spec(
                current_message,
                is_current=True,
                selected_image_count=selected_image_counts.get(current_message.id, 0),
            )
        )
        return conversation_turns

    async def _prepare_turn_images(
        self,
        turns: list[dict[str, Any]],
        *,
        temp_dir: str,
        base_filename: str,
        temp_files: set[str],
    ) -> int:
        prepared_image_count = 0

        for turn_index, turn in enumerate(turns):
            if turn.get("role") != "user":
                continue

            selected_attachments = list(turn.get("selected_image_attachments", []))
            total_image_count = self._safe_int(turn.get("total_image_count"), 0)
            image_paths: list[str] = []

            for image_index, attachment in enumerate(selected_attachments):
                _, image_extension = os.path.splitext(attachment.filename)
                if not image_extension:
                    image_extension = ".img"

                image_path = os.path.join(temp_dir, f"{base_filename}_turn{turn_index}_{image_index}{image_extension}")

                try:
                    await attachment.save(image_path)
                    temp_files.add(image_path)

                    final_path = await compress_image(image_path)
                    temp_files.add(final_path)
                    image_paths.append(final_path)
                except Exception as e:
                    print(f"⚠️ [快速答疑] 保存上下文图片失败: {attachment.filename} -> {type(e).__name__}: {e}")

            turn["image_paths"] = image_paths
            turn["omitted_image_count"] = max(0, total_image_count - len(image_paths))
            prepared_image_count += len(image_paths)

        return prepared_image_count

    def _build_openai_messages(self, turns: list[dict[str, Any]], system_prompt: str) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        if any(turn.get("role") == "assistant" for turn in turns):
            messages.append({"role": "system", "content": REPLY_CHAIN_CONTEXT_SYSTEM_PROMPT})

        total_size_kb = 0.0
        for turn_index, turn in enumerate(turns, start=1):
            if turn.get("role") == "assistant":
                messages.append({"role": "assistant", "content": turn.get("text", "")})
                continue

            user_text = self._build_user_turn_prompt_text(turn)
            user_content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]

            for image_path in turn.get("image_paths", []):
                size_kb = get_file_size_kb(image_path)
                total_size_kb += size_kb
                print(f"📎 添加图片到 API 请求: turn={turn_index} {os.path.basename(image_path)} ({size_kb:.2f}KB)")
                base64_image = encode_image_to_base64(image_path)
                user_content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": base64_image},
                    }
                )

            messages.append({"role": "user", "content": user_content})

        if total_size_kb > 0:
            print(f"📊 API 请求图片总大小: {total_size_kb:.2f}KB")

        return messages

    def _archive_prompt(self, user_id: int, turns: list[dict[str, Any]], system_prompt: str) -> None:
        try:
            save_dir = "app_save"
            os.makedirs(save_dir, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            save_filename = f"{timestamp}_{user_id}.txt"
            save_path = os.path.join(save_dir, save_filename)

            history_user_index = 0
            history_ai_index = 0
            with open(save_path, "w", encoding="utf-8") as f:
                f.write("=== 系统提示词 ===\n")
                f.write(system_prompt)

                for turn in turns:
                    if turn.get("role") == "assistant":
                        history_ai_index += 1
                        f.write(f"\n\n=== 历史 AI 回复 #{history_ai_index} ===\n")
                        f.write(turn.get("text", ""))
                        continue

                    label = "当前用户消息" if turn.get("is_current") else "历史用户消息"
                    if not turn.get("is_current"):
                        history_user_index += 1
                        label = f"{label} #{history_user_index}"

                    f.write(f"\n\n=== {label} ===\n")
                    f.write(self._build_user_turn_prompt_text(turn))

                    included_image_count = len(turn.get("image_paths", []))
                    omitted_image_count = self._safe_int(turn.get("omitted_image_count"), 0)
                    if included_image_count:
                        f.write(f"\n[已附带图片 {included_image_count} 张]")
                    if omitted_image_count:
                        f.write(f"\n[未附带原图 {omitted_image_count} 张]")

            print(f"✅ 已存档提示词到 {save_path}")
        except Exception as e:
            print(f"❌ 存档提示词失败: {e}")

    async def _stream_ai_response(
        self,
        client: openai.AsyncOpenAI,
        messages: list[dict[str, Any]],
        public_session: PublicStreamReply,
    ) -> bool:
        stream = await client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL"),
            messages=messages,
            temperature=1.0,
            stream=True,
        )

        received_any_text = False
        async for chunk in stream:
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue

            delta = getattr(choices[0], "delta", None)
            if not delta:
                continue

            delta_text = self._normalize_text_content(getattr(delta, "content", None))
            if not delta_text:
                continue

            public_session.append(delta_text)
            received_any_text = True

        return received_any_text

    async def _create_non_stream_completion(
        self,
        client: openai.AsyncOpenAI,
        messages: list[dict[str, Any]],
    ) -> str:
        response = await client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL"),
            messages=messages,
            temperature=1.0,
            stream=False,
        )

        if not response or not response.choices:
            return ""

        first_choice = response.choices[0]
        message = getattr(first_choice, "message", None)
        if not message:
            return ""

        return self._normalize_text_content(getattr(message, "content", None)).strip()

    async def _generate_ai_response(
        self,
        client: openai.AsyncOpenAI,
        messages: list[dict[str, Any]],
        public_session: PublicStreamReply,
    ) -> str:
        try:
            received_stream_text = await self._stream_ai_response(client, messages, public_session)
        except Exception as e:
            if public_session.has_content:
                raise
            print(f"⚠️ [快速答疑] 流式请求失败，回退到非流式: {type(e).__name__}: {e}")
            fallback_text = await self._create_non_stream_completion(client, messages)
            if fallback_text:
                public_session.append(fallback_text)
            return fallback_text

        if received_stream_text or public_session.has_content:
            return public_session.full_text.strip()

        print("⚠️ [快速答疑] 流式响应未返回正文，回退到非流式。")
        fallback_text = await self._create_non_stream_completion(client, messages)
        if fallback_text:
            public_session.append(fallback_text)
        return fallback_text

    def _cleanup_temp_files(self, *, temp_files: set[str]) -> None:
        if os.getenv("DELETE_TEMP_FILES", "false").lower() != "true":
            return

        for temp_path in sorted(path for path in temp_files if path):
            if not os.path.exists(temp_path):
                continue
            try:
                os.remove(temp_path)
                print(f"🗑️ 已删除临时文件: {os.path.basename(temp_path)}")
            except Exception as e:
                print(f" [33m[警告] [0m 删除临时文件 {temp_path} 时出错: {e}")

    async def quick_dayi(self, interaction: discord.Interaction, message: discord.Message):
        """对消息使用快速答疑，并通过公开普通消息流式回复。"""
        await safe_defer(interaction)

        user_id = interaction.user.id
        target_user = message.author
        target_user_id = str(target_user.id)
        current_image_attachments = self._get_message_image_attachments(message)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_filename = f"{timestamp}_{user_id}"
        temp_dir = "app_temp"
        temp_files: set[str] = set()
        conversation_turns: list[dict[str, Any]] = []
        history_pairs_newest_first: list[dict[str, Any]] = []
        public_session: PublicStreamReply | None = None
        parallel_slot_acquired = False
        display_model_name = self._get_display_model_name()
        client = getattr(self.bot, "openai_client", None)

        try:
            banned_user_info = self._get_active_ban_entry(target_user_id)
            if banned_user_info:
                ban_message = self._format_ban_message(banned_user_info)
                await self._send_public_error(interaction, message, ban_message)
                print(f"🚫 尝试对封禁用户 {target_user_id} ({target_user.name}) 的消息使用快速答疑")
                print(f"   封禁原因: {banned_user_info['reason']}")
                print(f"   解封时间: {datetime.fromtimestamp(int(banned_user_info['unbanned_at'])).strftime('%Y年%m月%d日 %H:%M:%S')}")
                return

            print(f"✅ 用户 {target_user_id} ({target_user.name}) 未被封禁")

            admins = getattr(self.bot, "admins", [])
            trusted_users = getattr(self.bot, "trusted_users", [])

            if not (user_id in admins or user_id in trusted_users):
                await self._send_public_error(interaction, message, "❌ 此命令仅限答疑组使用。")
                return

            if len(current_image_attachments) > MAX_IMAGE_ATTACHMENTS:
                print(f"⚠️ [快速答疑] 图片数量超限: {len(current_image_attachments)} > {MAX_IMAGE_ATTACHMENTS}")
                await self._send_public_error(
                    interaction,
                    message,
                    (
                        "❌ 图片数量超出限制！\n"
                        f"当前消息包含 {len(current_image_attachments)} 张图片，系统最多支持 {MAX_IMAGE_ATTACHMENTS} 张图片。\n"
                        "请减少图片数量后重试。"
                    ),
                )
                return

            if current_image_attachments:
                print(f"📸 [快速答疑] 检测到 {len(current_image_attachments)} 张当前消息图片附件")
                for idx, attachment in enumerate(current_image_attachments, start=1):
                    print(f"   图片{idx}: {attachment.filename} ({attachment.size / 1024:.2f} KB)")

            if not client:
                await self._send_public_error(
                    interaction,
                    message,
                    "❌ AI 服务尚未正确初始化，请联系管理员检查配置。",
                )
                return

            if not hasattr(self.bot, "current_parallel_dayi_tasks"):
                self.bot.current_parallel_dayi_tasks = 0

            max_parallel = int(os.getenv("MAX_PARALLEL", 5))
            if self.bot.current_parallel_dayi_tasks >= max_parallel:
                await self._send_public_error(
                    interaction,
                    message,
                    f"❌ 当前并发数已达上限（{max_parallel}），请稍后再试。",
                )
                return

            is_on_cooldown, remaining_seconds = self._check_and_update_cooldown(message.id)
            if is_on_cooldown:
                await self._send_public_error(
                    interaction,
                    message,
                    (
                        f"⏰ 该消息正在冷却中，请在 {remaining_seconds} 秒后再试。\n"
                        f"（每条消息在使用快速答疑后需要等待 {self.cooldown_duration} 秒才能再次使用）"
                    ),
                )
                return

            public_session = PublicStreamReply(
                source_message=message,
                display_model_name=display_model_name,
                requester_name=interaction.user.display_name,
            )
            await public_session.start(STATUS_RECEIVED)
            await self._acknowledge_public_result(interaction, "⏳ 正在频道公开生成回复，请留意下方消息。")

            self.bot.current_parallel_dayi_tasks += 1
            parallel_slot_acquired = True

            os.makedirs(temp_dir, exist_ok=True)

            await public_session.set_status(STATUS_RESOLVING_CONTEXT)
            history_pairs_newest_first = await self._collect_reply_chain_history(message)
            public_session.context_user_input_count = len(history_pairs_newest_first) + 1
            selected_image_counts = self._select_context_image_counts(message, history_pairs_newest_first)
            conversation_turns = self._build_conversation_turns(message, history_pairs_newest_first, selected_image_counts)

            planned_image_count = sum(
                len(turn.get("selected_image_attachments", []))
                for turn in conversation_turns
                if turn.get("role") == "user"
            )
            if planned_image_count:
                print(f"📸 [快速答疑] 计划附带上下文图片 {planned_image_count} 张（总上限 {MAX_IMAGE_ATTACHMENTS} 张）")
                await public_session.set_status(STATUS_PROCESSING_IMAGES)
                prepared_image_count = await self._prepare_turn_images(
                    conversation_turns,
                    temp_dir=temp_dir,
                    base_filename=base_filename,
                    temp_files=temp_files,
                )
                print(f"✅ 图片处理完成，实际附带 {prepared_image_count} 张")

            system_prompt = self._load_default_prompt()
            messages = self._build_openai_messages(conversation_turns, system_prompt)
            self._archive_prompt(user_id, conversation_turns, system_prompt)

            total_image_paths = [
                image_path
                for turn in conversation_turns
                if turn.get("role") == "user"
                for image_path in turn.get("image_paths", [])
            ]
            current_text = self._get_base_user_message_text(message, is_current=True)

            print("📤 [API请求] 准备发送请求:")
            print(f"   - 模型: {os.getenv('OPENAI_MODEL')}")
            print(f"   - 当前消息文本长度: {len(current_text)} 字符")
            print(f"   - 回复链历史轮数: {len(history_pairs_newest_first)}")
            print(f"   - 上下文消息数: {len(conversation_turns)}")
            print(f"   - 图片数量: {len(total_image_paths)} 张")
            if total_image_paths:
                print(f"   - 图片总大小: {sum(get_file_size_kb(path) for path in total_image_paths):.2f} KB")

            await public_session.set_status(STATUS_REQUESTING_AI)
            ai_response = await asyncio.wait_for(
                self._generate_ai_response(client, messages, public_session),
                timeout=STREAM_TIMEOUT_SECONDS,
            )

            if not ai_response.strip():
                raise EmptyAIResponseError("AI 没有返回可用内容")

            await public_session.finalize()
            await self._acknowledge_public_result(interaction, "✅ 已在频道公开发送回复。")

        except asyncio.TimeoutError:
            print(f"⚠️ [超时] 用户 {user_id} 的快速答疑请求超过3分钟被终止")
            await self._publish_failure(
                interaction,
                message,
                public_session,
                (
                    "⏱️ 答疑超时：处理时间超过 3 分钟，请求已被终止。\n"
                ),
            )

        except EmptyAIResponseError:
            await self._publish_failure(
                interaction,
                message,
                public_session,
                "❌ AI 没有返回可用内容，请重试。若问题连续发生，请联系BOT管理员。",
            )

        except openai.APIConnectionError as e:
            print(f"❌ [快速答疑] 连接 AI 服务失败: {e}")
            await self._publish_failure(
                interaction,
                message,
                public_session,
                "❌ 无法连接到 AI 服务，请重试。若问题连续发生，请联系BOT管理员。",
            )

        except openai.RateLimitError as e:
            print(f"❌ [快速答疑] AI 服务限流: {e}")
            await self._publish_failure(
                interaction,
                message,
                public_session,
                "❌ AI 服务当前较忙，请再试。若问题连续发生，请联系BOT管理员。",
            )

        except openai.AuthenticationError as e:
            print(f"❌ [快速答疑] AI 服务认证失败: {e}")
            await self._publish_failure(
                interaction,
                message,
                public_session,
                "❌ AI 服务配置异常，请联系管理员检查配置。",
            )

        except openai.APIStatusError as e:
            print(f"❌ [快速答疑] AI 服务状态异常: status={e.status_code}, response={e.response}")
            await self._publish_failure(
                interaction,
                message,
                public_session,
                f"❌ AI 服务返回异常状态（HTTP {e.status_code}），请再试。若问题连续发生，请联系BOT管理员。",
            )

        except json.JSONDecodeError as e:
            print(f"❌ [快速答疑] JSON 解析失败: {e}")
            await self._publish_failure(
                interaction,
                message,
                public_session,
                "❌ AI 服务返回了无效响应，请重试。若问题连续发生，请联系BOT管理员。",
            )

        except Exception as e:
            print(f"❌ [快速答疑] 捕获异常: {type(e).__name__}: {e}")
            traceback.print_exc()
            await self._publish_failure(
                interaction,
                message,
                public_session,
                "❌ 发生意外错误，请联系管理员。",
            )

        finally:
            if public_session:
                await public_session.close()

            if parallel_slot_acquired:
                self.bot.current_parallel_dayi_tasks = max(0, self.bot.current_parallel_dayi_tasks - 1)

            self._cleanup_temp_files(temp_files=temp_files)

    def _load_default_prompt(self) -> str:
        """加载默认的完整知识库提示词，并进行缓存。"""
        if self._default_prompt_cache is not None:
            return self._default_prompt_cache

        prompt_file = "prompt/ALL.txt"
        try:
            with open(prompt_file, encoding="utf-8") as f:
                system_prompt = f.read().strip()
            if not system_prompt:
                system_prompt = DEFAULT_SYSTEM_PROMPT
            print("📖 使用完整知识库作为提示词")
        except FileNotFoundError:
            print("⚠️ 知识库文件不存在，使用默认提示词")
            system_prompt = DEFAULT_SYSTEM_PROMPT

        self._default_prompt_cache = system_prompt
        return system_prompt


async def setup(bot: commands.Bot):
    """注册 Cog，依赖主入口预先初始化异步 OpenAI 客户端。"""
    if not getattr(bot, "openai_client", None):
        print("⚠️ [AppDayi] bot.openai_client 未初始化，相关功能将不可用。")
    await bot.add_cog(AppDayi(bot))
