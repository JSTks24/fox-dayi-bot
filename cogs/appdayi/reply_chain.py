from __future__ import annotations

import mimetypes
from typing import Any
import discord

from .stream import MAX_HISTORY_IMAGE_TURNS, MAX_IMAGE_ATTACHMENTS, MAX_REPLY_CHAIN_ROUNDS, QD_META_VERSION, REPLY_CHAIN_SCAN_LIMIT, extract_qd_meta, strip_public_reply_markup

class AppDayiReplyChainMixin:
    def _is_image_attachment(self, attachment: discord.Attachment) -> bool:
        if attachment.content_type and attachment.content_type.startswith("image/"):
            return True
        guessed_type, _ = mimetypes.guess_type(attachment.filename)
        return bool(guessed_type and guessed_type.startswith("image/"))

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
