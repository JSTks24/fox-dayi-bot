from __future__ import annotations

import asyncio
import contextlib
import json
import random
import re
import time
import uuid
from typing import Any
import discord

from paths import ROOT_DIR

PUBLIC_ALLOWED_MENTIONS = discord.AllowedMentions.none()

PUBLIC_MESSAGE_LIMIT = 1900

STREAM_EDIT_INTERVAL_SECONDS = 3.0

SPINNING_VERB_ROTATION_SECONDS = 8.0

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

_SPINNING_EMOJIS_PATH = ROOT_DIR / "data" / "spinning" / "emojis.json"
_SPINNING_VERBS_PATH = ROOT_DIR / "data" / "spinning" / "verbs.json"


def _load_json_array(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [str(item) for item in data]
    except Exception:
        pass
    return []


def _get_spinning_verbs() -> list[str]:
    """返回所有可用的 spinning verbs 列表。"""
    verbs = _load_json_array(_SPINNING_VERBS_PATH)
    return verbs if verbs else ["处理中"]


def _get_spinning_emojis() -> list[str]:
    """返回所有可用的 spinning emojis 列表。"""
    emojis = _load_json_array(_SPINNING_EMOJIS_PATH)
    return emojis if emojis else ["⏳"]


def pick_random_verb(*, exclude: str | None = None, exclude_set: set[str] | None = None) -> str:
    """随机选取一个 verb，尽量避免与已用过的 verb 重复。

    exclude_set 优先于 exclude；当 exclude_set 覆盖了所有可用 verb 时，
    清空已用列表并重新开始（保证始终能返回值）。
    """
    verbs = _get_spinning_verbs()
    used = exclude_set if exclude_set is not None else ({exclude} if exclude else set())
    if used and len(verbs) > 1:
        candidates = [v for v in verbs if v not in used]
        if not candidates:
            candidates = verbs
        return random.choice(candidates) if candidates else random.choice(verbs)
    return random.choice(verbs)


def pick_spinning_status() -> tuple[str, str, str, str]:
    """从 spinning data 中随机选取 emoji 与 verb，返回两个阶段的状态文本、emoji 和 verb。

    Returns:
        (phase1_status, phase2_status, emoji, verb)
        phase1: "{emoji} {verb}（正在整理上下文和图片...）"
        phase2: "{emoji} {verb}（正在等待AI回复...）"
        emoji: 选中的 emoji（供后续轮换 verb 时复用）
        verb: 选中的 verb（供后续轮换时作为初始值）
    """
    emoji = random.choice(_get_spinning_emojis())
    verb = pick_random_verb()

    phase1 = f"{emoji} {verb}（整理上下文...）"
    phase2 = f"{emoji} {verb}（等待AI回复...）"
    return phase1, phase2, emoji, verb

QD_META_LINE_REGEX = re.compile(r"^\s*-# <\|qd-meta\|>(?P<payload>.+?)<\|/qd-meta\|>\s*$", re.MULTILINE)

QD_AUXILIARY_LINE_REGEX = re.compile(r"^\s*-# <\|qd-(?:footer|meta)\|>.*?<\|/qd-(?:footer|meta)\|>\s*$", re.MULTILINE)

QD_HEADER_REGEX = re.compile(r"^🦊 AI 回复(?:（续 \d+）)?\n\n")

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
        self._verb_rotation_task: asyncio.Task | None = None
        self._spinning_emoji: str | None = None
        self._current_verb: str | None = None
        self._used_verbs: set[str] = set()

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

    def start_verb_rotation(self, emoji: str, initial_verb: str) -> None:
        """启动等待 AI 回复期间的 verb 轮换后台任务。

        每 SPINNING_VERB_ROTATION_SECONDS 秒随机换一个不同的 verb，emoji 保持不变。
        一旦有流式内容到达（has_content 为 True）或会话关闭，任务自动停止。
        """
        self._spinning_emoji = emoji
        self._current_verb = initial_verb
        self._used_verbs.add(initial_verb)
        if self._verb_rotation_task is None:
            self._verb_rotation_task = asyncio.create_task(self._verb_rotation_loop())

    def stop_verb_rotation(self) -> None:
        """手动停止 verb 轮换任务。"""
        if self._verb_rotation_task:
            self._verb_rotation_task.cancel()
            self._verb_rotation_task = None

    async def _verb_rotation_loop(self) -> None:
        """后台循环：每 SPINNING_VERB_ROTATION_SECONDS 秒更换一次等待状态的 verb，同一次回答不重复。"""
        try:
            while not self._closed:
                await asyncio.sleep(SPINNING_VERB_ROTATION_SECONDS)

                # 一旦有流式内容到达，停止轮换
                if self.has_content or self._closed:
                    break

                new_verb = pick_random_verb(exclude_set=self._used_verbs)
                self._used_verbs.add(new_verb)
                self._current_verb = new_verb
                new_status = f"{self._spinning_emoji} {new_verb}（正在等待AI回复...）"

                try:
                    await self.set_status(new_status)
                except Exception as e:
                    print(f"⚠️ [快速答疑] verb 轮换编辑失败: {type(e).__name__}: {e}")
        except asyncio.CancelledError:
            pass
        finally:
            self._verb_rotation_task = None

    def append(self, delta_text: str) -> None:
        if self._closed or not delta_text:
            return
        self.full_text += delta_text
        self._dirty = True
        # 一旦收到流式内容，停止 verb 轮换
        if self._verb_rotation_task and self.has_content:
            self.stop_verb_rotation()

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
        if self._verb_rotation_task:
            self._verb_rotation_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._verb_rotation_task
            self._verb_rotation_task = None
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
