import asyncio
import base64
import contextlib
import io
import json
import mimetypes
import os
import random
import time
import traceback
from datetime import datetime
from typing import Any, Optional

import discord
import openai
from PIL import Image
from discord import app_commands
from discord.ext import commands

# --- 从 bot.py 引入的辅助函数和类 ---

PUBLIC_ALLOWED_MENTIONS = discord.AllowedMentions.none()
PUBLIC_MESSAGE_LIMIT = 1900
STREAM_EDIT_INTERVAL_SECONDS = 3.0
STREAM_POLL_INTERVAL_SECONDS = 0.5
STREAM_TIMEOUT_SECONDS = 180.0
MAX_IMAGE_ATTACHMENTS = 3
DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."
STATUS_RECEIVED = "⏳ 收到请求，正在处理中，请稍候..."
STATUS_PROCESSING_IMAGES = "🖼️ 正在处理图片，请稍候..."
STATUS_REQUESTING_AI = "🤖 正在向 AI 请求回复，请稍候..."


class QuotaError(app_commands.AppCommandError):
    """自定义异常，用于表示用户配额不足"""


class ParallelLimitError(app_commands.AppCommandError):
    """自定义异常，用于表示并发达到上限"""


class EmptyAIResponseError(RuntimeError):
    """AI 没有返回可用文本内容。"""


def encode_image_to_base64(image_path: str) -> str:
    """将图片文件编码为 Base64 数据 URI。"""
    mime_type, _ = mimetypes.guess_type(image_path)
    if mime_type is None:
        mime_type = "application/octet-stream"
    with open(image_path, "rb") as image_file:
        base64_encoded_data = base64.b64encode(image_file.read()).decode("utf-8")
    return f"data:{mime_type};base64,{base64_encoded_data}"


async def safe_defer(interaction: discord.Interaction):
    """
    一个绝对安全的“占坑”函数。
    它会检查交互是否已被响应，如果没有，就立即以“仅自己可见”的方式延迟响应，
    这能解决超时和重复响应问题。
    """
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)


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
    ):
        self.source_message = source_message
        self.display_model_name = (display_model_name or "未知模型")[:80]
        self.requester_name = (requester_name or "未知用户")[:80]
        self.message_limit = message_limit
        self.edit_interval = edit_interval

        self.messages: list[discord.Message] = []
        self.full_text = ""
        self.last_edit_at = 0.0

        self._dirty = False
        self._closed = False
        self._flush_task: Optional[asyncio.Task] = None
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

            desired_contents = self._split_response_text(response_text)
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
                    if index == 0:
                        new_message = await self.source_message.reply(
                            desired_content,
                            mention_author=False,
                            allowed_mentions=PUBLIC_ALLOWED_MENTIONS,
                        )
                    else:
                        new_message = await self.source_message.channel.send(
                            desired_content,
                            allowed_mentions=PUBLIC_ALLOWED_MENTIONS,
                        )
                    self.messages.append(new_message)
                    any_updated = True

            self._dirty = False
            if any_updated:
                self.last_edit_at = time.monotonic()

    async def finalize(self) -> None:
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

    def _split_response_text(self, text: str) -> list[str]:
        chunks: list[str] = []
        remaining = text
        chunk_index = 0

        while remaining:
            header = self._build_chunk_header(chunk_index)
            available_length = max(200, self.message_limit - len(header))

            if len(remaining) <= available_length:
                chunks.append(header + remaining)
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

            chunks.append(header + chunk_text)
            remaining = remaining[split_at:].lstrip("\n ")
            chunk_index += 1

        return chunks

    def _build_chunk_header(self, chunk_index: int) -> str:
        if chunk_index == 0:
            return (
                "🦊 AI 回复\n"
                f"由 {self.display_model_name} 提供支持 | {self.requester_name} 问的。\n\n"
            )
        return f"🦊 AI 回复（续 {chunk_index + 1}）\n\n"


class AppDayi(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.message_cooldowns: dict[int, datetime] = {}
        self.cooldown_duration = 30
        self._default_prompt_cache: Optional[str] = None

        self.ctx_menu = app_commands.ContextMenu(
            name="快速答疑",
            callback=self.quick_dayi,
        )
        self.bot.tree.add_command(self.ctx_menu)

    async def cog_unload(self):
        """Cog 卸载时移除命令"""
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)

    def _get_file_size_kb(self, file_path: str) -> float:
        """获取文件大小（KB）。"""
        if os.path.exists(file_path):
            return os.path.getsize(file_path) / 1024
        return 0

    def _is_image_attachment(self, attachment: discord.Attachment) -> bool:
        if attachment.content_type and attachment.content_type.startswith("image/"):
            return True
        guessed_type, _ = mimetypes.guess_type(attachment.filename)
        return bool(guessed_type and guessed_type.startswith("image/"))

    async def _compress_image(self, image_path: str, max_size_kb: int = 250) -> str:
        """压缩图片到指定大小以下。"""
        try:
            original_size_kb = self._get_file_size_kb(image_path)
            print(f"🖼️ 原始图片大小: {original_size_kb:.2f}KB")

            if original_size_kb <= max_size_kb:
                print("✅ 图片大小符合要求，无需压缩")
                return image_path

            print(f"🔧 开始压缩图片 (目标: <{max_size_kb}KB)")

            with Image.open(image_path) as img:
                if img.mode in ("RGBA", "LA", "P"):
                    background = Image.new("RGB", img.size, (255, 255, 255))
                    if img.mode in ("RGBA", "LA"):
                        background.paste(img, mask=img.split()[-1])
                    else:
                        background.paste(img)
                    img = background
                elif img.mode != "RGB":
                    img = img.convert("RGB")

                base_name = os.path.splitext(image_path)[0]
                compressed_path = f"{base_name}_compressed.jpg"

                quality = 85
                max_dimension = 1920
                buffer = io.BytesIO()

                for attempt in range(5):
                    width, height = img.size
                    if width > max_dimension or height > max_dimension:
                        ratio = min(max_dimension / width, max_dimension / height)
                        new_width = int(width * ratio)
                        new_height = int(height * ratio)
                        resized_img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
                        print(f"  调整尺寸: {width}x{height} → {new_width}x{new_height}")
                    else:
                        resized_img = img

                    buffer = io.BytesIO()
                    resized_img.save(buffer, format="JPEG", quality=quality, optimize=True)
                    buffer_size_kb = buffer.tell() / 1024

                    print(f"  尝试 {attempt + 1}: 质量={quality}, 大小={buffer_size_kb:.2f}KB")

                    if buffer_size_kb <= max_size_kb:
                        buffer.seek(0)
                        with open(compressed_path, "wb") as f:
                            f.write(buffer.read())
                        print(f"✅ 压缩成功: {original_size_kb:.2f}KB → {buffer_size_kb:.2f}KB")
                        print(f"   压缩率: {(1 - buffer_size_kb / original_size_kb) * 100:.1f}%")
                        return compressed_path

                    if attempt < 2:
                        quality -= 10
                    else:
                        max_dimension = int(max_dimension * 0.8)
                        quality = 75

                print(f"⚠️ 无法压缩到{max_size_kb}KB以下，使用最佳尝试结果")
                buffer.seek(0)
                with open(compressed_path, "wb") as f:
                    f.write(buffer.read())
                return compressed_path

        except Exception as e:
            print(f"❌ 图片压缩失败: {e}")
            return image_path

    def _clean_expired_cooldowns(self) -> None:
        """清理过期的冷却记录。"""
        current_time = datetime.now()
        expired_messages = [
            msg_id
            for msg_id, last_used in self.message_cooldowns.items()
            if (current_time - last_used).total_seconds() > self.cooldown_duration
        ]
        for msg_id in expired_messages:
            del self.message_cooldowns[msg_id]

    def _check_and_update_cooldown(self, message_id: int) -> tuple[bool, int]:
        """
        检查消息是否在冷却中，如果不在则更新冷却时间。

        Returns:
            (is_on_cooldown, remaining_seconds)
        """
        self._clean_expired_cooldowns()
        current_time = datetime.now()

        if message_id in self.message_cooldowns:
            last_used = self.message_cooldowns[message_id]
            elapsed = (current_time - last_used).total_seconds()
            if elapsed < self.cooldown_duration:
                remaining = int(self.cooldown_duration - elapsed)
                return True, remaining

        self.message_cooldowns[message_id] = current_time
        return False, 0

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
        public_session: Optional[PublicStreamReply],
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

    def _get_active_ban_entry(self, target_user_id: str) -> Optional[dict[str, Any]]:
        banlist_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "banlist.json")
        try:
            with open(banlist_path, "r", encoding="utf-8") as f:
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

    def _build_openai_messages(self, text: str, image_paths: list[str], system_prompt: str) -> list[dict[str, Any]]:
        user_content: list[dict[str, Any]] = [{"type": "text", "text": text}]

        for image_path in image_paths:
            size_kb = self._get_file_size_kb(image_path)
            print(f"📎 添加图片到 API 请求: {os.path.basename(image_path)} ({size_kb:.2f}KB)")
            base64_image = encode_image_to_base64(image_path)
            user_content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": base64_image},
                }
            )

        if image_paths:
            total_size_kb = sum(self._get_file_size_kb(path) for path in image_paths)
            print(f"📊 API 请求图片总大小: {total_size_kb:.2f}KB")

        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

    def _archive_prompt(self, user_id: int, text: str, image_paths: list[str], system_prompt: str) -> None:
        try:
            save_dir = "app_save"
            os.makedirs(save_dir, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            save_filename = f"{timestamp}_{user_id}.txt"
            save_path = os.path.join(save_dir, save_filename)

            with open(save_path, "w", encoding="utf-8") as f:
                f.write("=== 系统提示词 ===\n")
                f.write(system_prompt)
                f.write("\n\n=== 用户提问 ===\n")
                f.write(text)
                if image_paths:
                    f.write(f"\n[包含 {len(image_paths)} 张图片附件]\n")

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

    def _cleanup_temp_files(
        self,
        *,
        text_path: Optional[str],
        image_paths: list[str],
        image_attachments: list[discord.Attachment],
        temp_dir: str,
        base_filename: str,
    ) -> None:
        if os.getenv("DELETE_TEMP_FILES", "false").lower() != "true":
            return

        if text_path and os.path.exists(text_path):
            try:
                os.remove(text_path)
                print(f"🗑️ 已删除临时文件: {os.path.basename(text_path)}")
            except Exception as e:
                print(f" [33m[警告] [0m 删除临时文件 {text_path} 时出错: {e}")

        all_image_paths = set(path for path in image_paths if path)
        for idx, attachment in enumerate(image_attachments):
            _, image_extension = os.path.splitext(attachment.filename)
            original_path = os.path.join(temp_dir, f"{base_filename}_{idx}{image_extension}")
            compressed_path = f"{os.path.splitext(original_path)[0]}_compressed.jpg"
            all_image_paths.add(original_path)
            all_image_paths.add(compressed_path)

        for image_path in all_image_paths:
            if image_path and os.path.exists(image_path):
                try:
                    os.remove(image_path)
                    print(f"🗑️ 已删除临时文件: {os.path.basename(image_path)}")
                except Exception as e:
                    print(f" [33m[警告] [0m 删除临时文件 {image_path} 时出错: {e}")

    async def quick_dayi(self, interaction: discord.Interaction, message: discord.Message):
        """对消息使用快速答疑，并通过公开普通消息流式回复。"""
        await safe_defer(interaction)

        user_id = interaction.user.id
        target_user = message.author
        target_user_id = str(target_user.id)
        text = message.content if message.content else "这是什么问题，怎么解决"
        image_attachments = [att for att in message.attachments if self._is_image_attachment(att)]

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_filename = f"{timestamp}_{user_id}"
        temp_dir = "app_temp"
        text_path: Optional[str] = None
        image_paths: list[str] = []
        public_session: Optional[PublicStreamReply] = None
        parallel_slot_acquired = False
        display_model_name = self._get_display_model_name()
        async_client = getattr(self.bot, "openai_async_client", None)

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

            if len(image_attachments) > MAX_IMAGE_ATTACHMENTS:
                print(f"⚠️ [快速答疑] 图片数量超限: {len(image_attachments)} > {MAX_IMAGE_ATTACHMENTS}")
                await self._send_public_error(
                    interaction,
                    message,
                    (
                        "❌ 图片数量超出限制！\n"
                        f"当前消息包含 {len(image_attachments)} 张图片，系统最多支持 {MAX_IMAGE_ATTACHMENTS} 张图片。\n"
                        "请减少图片数量后重试。"
                    ),
                )
                return

            if image_attachments:
                print(f"📸 [快速答疑] 检测到 {len(image_attachments)} 张图片附件")
                for idx, attachment in enumerate(image_attachments, start=1):
                    print(f"   图片{idx}: {attachment.filename} ({attachment.size / 1024:.2f} KB)")

            if not async_client:
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

            text_path = os.path.join(temp_dir, f"{base_filename}.txt")
            with open(text_path, "w", encoding="utf-8") as f:
                f.write(text)

            for idx, image_attachment in enumerate(image_attachments):
                _, image_extension = os.path.splitext(image_attachment.filename)
                image_path = os.path.join(temp_dir, f"{base_filename}_{idx}{image_extension}")
                await image_attachment.save(image_path)
                image_paths.append(image_path)

            if image_paths:
                print(f"📸 保存了 {len(image_paths)} 张图片")
                await public_session.set_status(STATUS_PROCESSING_IMAGES)
                image_paths = await asyncio.gather(*[self._compress_image(path) for path in image_paths])
                print("✅ 图片压缩完成")

            system_prompt = self._load_default_prompt()
            messages = self._build_openai_messages(text, image_paths, system_prompt)
            self._archive_prompt(user_id, text, image_paths, system_prompt)

            print("📤 [API请求] 准备发送请求:")
            print(f"   - 模型: {os.getenv('OPENAI_MODEL')}")
            print(f"   - 文本长度: {len(text)} 字符")
            print(f"   - 图片数量: {len(image_paths)} 张")
            if image_paths:
                print(f"   - 图片总大小: {sum(self._get_file_size_kb(path) for path in image_paths):.2f} KB")

            await public_session.set_status(STATUS_REQUESTING_AI)
            ai_response = await asyncio.wait_for(
                self._generate_ai_response(async_client, messages, public_session),
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
                    "建议：\n"
                    "• 简化问题描述\n"
                    "• 减小图片尺寸\n"
                    "• 稍后重试"
                ),
            )

        except EmptyAIResponseError:
            await self._publish_failure(
                interaction,
                message,
                public_session,
                "❌ AI 没有返回可用内容，请稍后重试。",
            )

        except openai.APIConnectionError as e:
            print(f"❌ [快速答疑] 连接 AI 服务失败: {e}")
            await self._publish_failure(
                interaction,
                message,
                public_session,
                "❌ 无法连接到 AI 服务，请稍后重试。",
            )

        except openai.RateLimitError as e:
            print(f"❌ [快速答疑] AI 服务限流: {e}")
            await self._publish_failure(
                interaction,
                message,
                public_session,
                "❌ AI 服务当前较忙，请稍后再试。",
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
                f"❌ AI 服务返回异常状态（HTTP {e.status_code}），请稍后再试。",
            )

        except json.JSONDecodeError as e:
            print(f"❌ [快速答疑] JSON 解析失败: {e}")
            await self._publish_failure(
                interaction,
                message,
                public_session,
                "❌ AI 服务返回了无效响应，请稍后重试。",
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

            self._cleanup_temp_files(
                text_path=text_path,
                image_paths=image_paths,
                image_attachments=image_attachments,
                temp_dir=temp_dir,
                base_filename=base_filename,
            )

    def _load_default_prompt(self) -> str:
        """加载默认的完整知识库提示词，并进行缓存。"""
        if self._default_prompt_cache is not None:
            return self._default_prompt_cache

        prompt_file = "prompt/ALL.txt"
        try:
            with open(prompt_file, "r", encoding="utf-8") as f:
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
    """注册 Cog，并确保同步/异步 OpenAI 客户端都可用。"""
    openai_api_key = os.getenv("OPENAI_API_KEY")
    openai_api_base_url = os.getenv("OPENAI_API_BASE_URL")
    openai_model = os.getenv("OPENAI_MODEL")

    if not all([openai_api_key, openai_api_base_url, openai_model]):
        print(" [错误](来自App) 缺少必要的 OpenAI 环境变量。")
        bot.openai_client = None
        bot.openai_async_client = None
    else:
        if not hasattr(bot, "openai_client") or bot.openai_client is None:
            bot.openai_client = openai.OpenAI(
                api_key=openai_api_key,
                base_url=openai_api_base_url,
            )

        if not hasattr(bot, "openai_async_client") or bot.openai_async_client is None:
            bot.openai_async_client = openai.AsyncOpenAI(
                api_key=openai_api_key,
                base_url=openai_api_base_url,
            )

    await bot.add_cog(AppDayi(bot))
