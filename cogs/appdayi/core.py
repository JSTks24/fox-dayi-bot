from __future__ import annotations

import asyncio
import json
import os
import random
import traceback
from datetime import datetime
from typing import Any
import discord
import openai
from discord import app_commands
from discord.ext import commands
from cogs.shared.cache import CooldownManager
from cogs.shared.images import compress_image, encode_image_to_base64, get_file_size_kb
from cogs.shared.interactions import safe_defer
from paths import APP_TEMP_DIR, PROMPT_DIR, SAVE_DIR

from .stream import DEFAULT_SYSTEM_PROMPT, MAX_IMAGE_ATTACHMENTS, PUBLIC_ALLOWED_MENTIONS, REPLY_CHAIN_CONTEXT_SYSTEM_PROMPT, STREAM_TIMEOUT_SECONDS, PublicStreamReply, pick_spinning_status

APPDAYI_ARCHIVE_LOG_KEEP_COUNT = 5


class EmptyAIResponseError(RuntimeError):
    """AI 没有返回可用文本内容。"""

class AppDayiCoreMixin:
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.message_cooldowns = CooldownManager(30)
        self.cooldown_duration = 30
        self._default_prompt_cache: str | None = None
        self._default_prompt_cache_mtime: float | None = None

        self.ctx_menu = app_commands.ContextMenu(
            name="快速答疑",
            callback=self.quick_dayi,
        )
        self.bot.tree.add_command(self.ctx_menu)

        self.ctx_menu_search = app_commands.ContextMenu(
            name="联网答疑",
            callback=self.quick_dayi_search,
        )
        self.bot.tree.add_command(self.ctx_menu_search)

    async def cog_unload(self):
        """Cog 卸载时移除命令"""
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)
        self.bot.tree.remove_command(self.ctx_menu_search.name, type=self.ctx_menu_search.type)

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

    def _build_archive_prompt_content(self, turns: list[dict[str, Any]], system_prompt: str) -> str:
        sections = ["=== 系统提示词 ===\n", system_prompt]
        history_user_index = 0
        history_ai_index = 0

        for turn in turns:
            if turn.get("role") == "assistant":
                history_ai_index += 1
                sections.append(f"\n\n=== 历史 AI 回复 #{history_ai_index} ===\n")
                sections.append(turn.get("text", ""))
                continue

            label = "当前用户消息" if turn.get("is_current") else "历史用户消息"
            if not turn.get("is_current"):
                history_user_index += 1
                label = f"{label} #{history_user_index}"

            sections.append(f"\n\n=== {label} ===\n")
            sections.append(self._build_user_turn_prompt_text(turn))

            included_image_count = len(turn.get("image_paths", []))
            omitted_image_count = self._safe_int(turn.get("omitted_image_count"), 0)
            if included_image_count:
                sections.append(f"\n[已附带图片 {included_image_count} 张]")
            if omitted_image_count:
                sections.append(f"\n[未附带原图 {omitted_image_count} 张]")

        return "".join(sections)

    def _cleanup_prompt_archives(self, save_dir: str, keep_count: int = APPDAYI_ARCHIVE_LOG_KEEP_COUNT) -> list[str]:
        keep_count = max(0, keep_count)
        try:
            candidates: list[tuple[int, str, str]] = []
            with os.scandir(save_dir) as entries:
                for entry in entries:
                    try:
                        if not entry.is_file() or not entry.name.lower().endswith(".txt"):
                            continue
                        candidates.append((entry.stat().st_mtime_ns, entry.name, entry.path))
                    except OSError as e:
                        print(f"⚠️ [快速答疑] 读取日志文件信息失败: {entry.path} -> {e}")
        except FileNotFoundError:
            return []
        except OSError as e:
            print(f"⚠️ [快速答疑] 扫描日志存档目录失败: {save_dir} -> {e}")
            return []

        if len(candidates) <= keep_count:
            return []

        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        deleted_paths: list[str] = []
        for _, _, path in candidates[keep_count:]:
            try:
                os.remove(path)
                deleted_paths.append(path)
            except FileNotFoundError:
                continue
            except OSError as e:
                print(f"⚠️ [快速答疑] 删除旧日志存档失败: {path} -> {e}")

        if deleted_paths:
            print(f"🧹 [快速答疑] 已清理旧日志存档 {len(deleted_paths)} 个，仅保留最近 {keep_count} 个 txt")
        return deleted_paths

    def _write_prompt_archive(self, user_id: int, turns: list[dict[str, Any]], system_prompt: str) -> str:
        try:
            save_dir = os.fspath(SAVE_DIR)
            os.makedirs(save_dir, exist_ok=True)

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            save_filename = f"{timestamp}_{user_id}.txt"
            save_path = os.path.join(save_dir, save_filename)
            archive_content = self._build_archive_prompt_content(turns, system_prompt)
            with open(save_path, "w", encoding="utf-8") as f:
                f.write(archive_content)
            self._cleanup_prompt_archives(save_dir)
            return save_path
        except Exception as e:
            print(f"❌ 存档提示词失败: {e}")
            raise

    async def _archive_prompt(self, user_id: int, turns: list[dict[str, Any]], system_prompt: str) -> None:
        try:
            save_path = await asyncio.to_thread(self._write_prompt_archive, user_id, turns, system_prompt)
            print(f"✅ 已存档提示词到 {save_path}")
        except Exception:
            return

    async def _stream_ai_response(
        self,
        client: openai.AsyncOpenAI,
        messages: list[dict[str, Any]],
        public_session: PublicStreamReply,
        *,
        model: str | None = None,
    ) -> bool:
        stream = await client.chat.completions.create(
            model=model or os.getenv("OPENAI_MODEL"),
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
        *,
        model: str | None = None,
    ) -> str:
        response = await client.chat.completions.create(
            model=model or os.getenv("OPENAI_MODEL"),
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
        *,
        model: str | None = None,
    ) -> str:
        try:
            received_stream_text = await self._stream_ai_response(client, messages, public_session, model=model)
        except Exception as e:
            if public_session.has_content:
                raise
            print(f"⚠️ [快速答疑] 流式请求失败，回退到非流式: {type(e).__name__}: {e}")
            fallback_text = await self._create_non_stream_completion(client, messages, model=model)
            if fallback_text:
                public_session.append(fallback_text)
            return fallback_text

        if received_stream_text or public_session.has_content:
            return public_session.full_text.strip()

        print("⚠️ [快速答疑] 流式响应未返回正文，回退到非流式。")
        fallback_text = await self._create_non_stream_completion(client, messages, model=model)
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
        await self._run_dayi(interaction, message, model_env_key="OPENAI_MODEL")

    async def quick_dayi_search(self, interaction: discord.Interaction, message: discord.Message):
        """对消息使用联网答疑，使用联网搜索模型并通过公开普通消息流式回复。"""
        await self._run_dayi(interaction, message, model_env_key="OPENAI_SEARCH_MODEL")

    async def _run_dayi(self, interaction: discord.Interaction, message: discord.Message, *, model_env_key: str):
        """快速答疑与联网答疑的共享实现。"""
        await safe_defer(interaction)

        model_name = os.getenv(model_env_key)
        if not model_name:
            await self._send_public_error(
                interaction,
                message,
                f"❌ 环境变量 {model_env_key} 未配置，请联系管理员。",
            )
            return

        user_id = interaction.user.id
        current_image_attachments = self._get_message_image_attachments(message)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_filename = f"{timestamp}_{user_id}"
        temp_dir = os.fspath(APP_TEMP_DIR)
        temp_files: set[str] = set()
        conversation_turns: list[dict[str, Any]] = []
        history_pairs_newest_first: list[dict[str, Any]] = []
        public_session: PublicStreamReply | None = None
        parallel_slot_acquired = False
        display_model_name = self._get_display_model_name()
        client = getattr(self.bot, "openai_client", None)

        try:
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

            phase1_status, phase2_status, spinning_emoji, initial_verb = pick_spinning_status()
            public_session = PublicStreamReply(
                source_message=message,
                display_model_name=display_model_name,
                requester_name=interaction.user.display_name,
            )
            await public_session.start(phase1_status)
            await self._acknowledge_public_result(interaction, "⏳ 正在频道公开生成回复，请留意下方消息。")

            self.bot.current_parallel_dayi_tasks += 1
            parallel_slot_acquired = True

            os.makedirs(temp_dir, exist_ok=True)

            # Phase 1: phase1_status 已通过 start() 设置，期间整理上下文 & 处理图片
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
                prepared_image_count = await self._prepare_turn_images(
                    conversation_turns,
                    temp_dir=temp_dir,
                    base_filename=base_filename,
                    temp_files=temp_files,
                )
                print(f"✅ 图片处理完成，实际附带 {prepared_image_count} 张")

            system_prompt = self._load_default_prompt()
            messages = self._build_openai_messages(conversation_turns, system_prompt)
            await self._archive_prompt(user_id, conversation_turns, system_prompt)

            total_image_paths = [
                image_path
                for turn in conversation_turns
                if turn.get("role") == "user"
                for image_path in turn.get("image_paths", [])
            ]
            current_text = self._get_base_user_message_text(message, is_current=True)

            print("📤 [API请求] 准备发送请求:")
            print(f"   - 模型: {model_name}")
            print(f"   - 当前消息文本长度: {len(current_text)} 字符")
            print(f"   - 回复链历史轮数: {len(history_pairs_newest_first)}")
            print(f"   - 上下文消息数: {len(conversation_turns)}")
            print(f"   - 图片数量: {len(total_image_paths)} 张")
            if total_image_paths:
                print(f"   - 图片总大小: {sum(get_file_size_kb(path) for path in total_image_paths):.2f} KB")

            await public_session.set_status(phase2_status)
            public_session.start_verb_rotation(spinning_emoji, initial_verb)
            ai_response = await asyncio.wait_for(
                self._generate_ai_response(client, messages, public_session, model=model_name),
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

    def _get_default_prompt_path(self) -> str:
        return os.fspath(PROMPT_DIR / "ALL.txt")

    def _load_default_prompt(self) -> str:
        """Load the default knowledge-base prompt with mtime-based invalidation."""
        prompt_file = self._get_default_prompt_path()
        try:
            current_mtime = os.path.getmtime(prompt_file)
        except FileNotFoundError:
            current_mtime = None
        except Exception as e:
            print(f"⚠️ 读取知识库文件修改时间失败，回退到缓存或默认提示词: {e}")
            return self._default_prompt_cache or DEFAULT_SYSTEM_PROMPT

        if self._default_prompt_cache is not None and current_mtime == self._default_prompt_cache_mtime:
            return self._default_prompt_cache

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
        self._default_prompt_cache_mtime = current_mtime
        return system_prompt
