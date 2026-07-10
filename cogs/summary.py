import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import os
import re
from cogs.shared.interactions import safe_defer as _safe_defer
from paths import SUMMARY_PROMPT_DIR


def _summary_prompt_path(filename: str) -> str:
    return os.fspath(SUMMARY_PROMPT_DIR / filename)

TEMPLATE_CONFIG: dict[str, dict[str, str]] = {
    "judge": {
        "display": "判决",
        "head_path": _summary_prompt_path("judge_head.txt"),
        "end_path": _summary_prompt_path("judge_end.txt"),
    },
    "debate": {
        "display": "辩论",
        "head_path": _summary_prompt_path("summary_head.txt"),
        "end_path": _summary_prompt_path("summary_end_debate.txt"),
    },
    "chat": {
        "display": "聊天",
        "head_path": _summary_prompt_path("summary_head.txt"),
        "end_path": _summary_prompt_path("summary_end_chat.txt"),
    },
    "aar": {
        "display": "复盘",
        "head_path": _summary_prompt_path("summary_head.txt"),
        "end_path": _summary_prompt_path("summary_end_aar.txt"),
    },
    "question": {
        "display": "提问",
        "head_path": _summary_prompt_path("summary_head.txt"),
        "end_path": _summary_prompt_path("summary_end_question.txt"),
    },
    "auto": {
        "display": "自动",
        "head_path": _summary_prompt_path("summary_head.txt"),
        "end_path": _summary_prompt_path("summary_end_auto.txt"),
    },
}

DEFAULT_TEMPLATE_KEY = "auto"
MAX_MESSAGE_LENGTH = 2000
CHUNK_SAFE_LENGTH = 1800
SUMMARY_TIMEOUT_SECONDS = 240.0

def chunk_text(text: str, limit: int = CHUNK_SAFE_LENGTH) -> list[str]:
    """按行优先切分长文本，确保不超过Discord消息长度限制"""
    if not text:
        return []

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        split_idx = remaining.rfind("\n", 0, limit)
        if split_idx == -1 or split_idx < limit // 2:
            split_idx = limit

        chunk = remaining[:split_idx].rstrip()
        if not chunk:
            chunk = remaining[:limit]
            split_idx = limit

        chunks.append(chunk)
        remaining = remaining[split_idx:].lstrip()

    return chunks

class Summary(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.default_head_prompt = "请总结以下Discord消息记录：\n"
        self.default_end_prompt = "\n请提供详细的总结和分析。"

    def _get_summary_model(self) -> str | None:
        return os.getenv("SUMMARY_MODEL") or os.getenv("OPENAI_MODEL")

    def _get_summary_display_name(self) -> str:
        return (
            os.getenv("SUMMARY_DISPLAY_NAME")
            or os.getenv("SUMMARY_MODEL_DISPLAY_NAME")
            or self._get_summary_model()
            or "未配置"
        )

    def _get_summary_timeout_message(self) -> str:
        timeout_minutes = int(SUMMARY_TIMEOUT_SECONDS // 60)
        return f"⏱️ AI分析超时（超过{timeout_minutes}分钟），请减少消息数量后重试。"
        
    def parse_discord_link(self, link: str) -> tuple[int, int, int]:
        """
        解析Discord消息链接，提取guild_id, channel_id, message_id
        
        Args:
            link: Discord消息链接
            
        Returns:
            (guild_id, channel_id, message_id)
            
        Raises:
            ValueError: 如果链接格式无效
        """
        # Discord消息链接格式: https://discord.com/channels/guild_id/channel_id/message_id
        pattern = r'https://discord\.com/channels/(\d+)/(\d+)/(\d+)'
        match = re.match(pattern, link.strip())
        
        if not match:
            # 尝试其他可能的格式
            pattern2 = r'https://discordapp\.com/channels/(\d+)/(\d+)/(\d+)'
            match = re.match(pattern2, link.strip())
            
        if not match:
            raise ValueError("无效的Discord消息链接格式")
            
        return int(match.group(1)), int(match.group(2)), int(match.group(3))
    
    async def resolve_channel(self, interaction: discord.Interaction, guild_id: int, channel_id: int):
        """
        兼容子区/线程/论坛贴子的频道解析：
        优先使用缓存，失败则回退到 API 拉取。
        """
        # 1) 通过 bot 全局缓存
        channel = self.bot.get_channel(channel_id)
        if channel:
            print(f"🔎 频道解析: 通过 bot 缓存拿到 {type(channel).__name__}({channel.id})")
            return channel

        guild = interaction.guild
        # 2) 通过 guild 缓存
        if guild:
            ch = guild.get_channel(channel_id)
            if ch:
                print(f"🔎 频道解析: 通过 guild 缓存拿到 {type(ch).__name__}({ch.id})")
                return ch
            # 3) 线程缓存
            try:
                th = guild.get_thread(channel_id)
            except AttributeError:
                th = None
            if th:
                print(f"🔎 频道解析: 通过线程缓存拿到 Thread({th.id})")
                return th

        # 4) API 拉取（覆盖 TextChannel/Thread/ForumThread）
        try:
            fetched = await self.bot.fetch_channel(channel_id)
            print(f"🔎 频道解析: 通过 API fetch 拿到 {type(fetched).__name__}({fetched.id})")
            return fetched
        except discord.NotFound:
            print(f"❌ 频道解析: API 返回 NotFound，channel_id={channel_id}")
            return None
        except discord.Forbidden:
            print(f"❌ 频道解析: API 返回 Forbidden，channel_id={channel_id}，可能机器人未加入私有线程")
            return None
        except discord.HTTPException as e:
            print(f"❌ 频道解析: API HTTPException: {e}")
            return None
    async def fetch_messages_batch(self, channel: discord.TextChannel,
                                  start_message: discord.Message,
                                  count: int) -> list[discord.Message]:
        """
        分批获取消息，每100条休息2秒
        
        Args:
            channel: 目标频道
            start_message: 起始消息
            count: 要获取的消息数量
            
        Returns:
            消息列表（按时间倒序，即最新的在前）
        """
        messages = [start_message]  # 🔥 关键修复：包含起始消息
        remaining = count - 1  # 已经包含了起始消息，所以减1
        before = start_message
        
        # 添加调试日志
        print(f"📍 开始获取消息，起始消息ID: {start_message.id}")
        print(f"📍 需要获取总数: {count} 条（包含起始消息）")
        
        while remaining > 0:
            batch_size = min(100, remaining)
            
            try:
                # 获取一批消息
                batch = []
                async for msg in channel.history(limit=batch_size, before=before):
                    batch.append(msg)
                
                if not batch:
                    print(f"📍 没有更多消息了，已获取 {len(messages)} 条")
                    break
                
                messages.extend(batch)
                remaining -= len(batch)
                before = batch[-1]  # 更新before为这批最后一条消息
                
                # 如果还有更多消息要获取，休息2秒
                if remaining > 0:
                    print(f"📥 已获取 {len(messages)} 条消息，休息2秒...")
                    await asyncio.sleep(2)
                    
            except discord.Forbidden:
                print(f"❌ 无权限获取频道 {channel.name} 的消息")
                break
            except discord.HTTPException as e:
                print(f"❌ 获取消息时发生HTTP错误: {e}")
                break
                
        return messages
    
    def format_messages_for_prompt(self, messages: list[discord.Message]) -> str:
        """
        格式化消息列表为提示词格式
        
        Args:
            messages: 消息列表
            
        Returns:
            格式化后的消息文本
        """
        formatted_lines = []
        
        # 消息是倒序的（最新的在前），我们需要反转以获得正确的时间顺序
        messages_reversed = list(reversed(messages))
        
        for idx, msg in enumerate(messages_reversed):
            # 每50条消息记录一次时间戳（第1条、第51条、第101条...）
            if idx == 0 or idx % 50 == 0:
                timestamp = msg.created_at.strftime('%Y-%m-%d %H:%M:%S')
                formatted_lines.append(f"\n--- 时间戳: {timestamp} ---\n")
            
            # 格式化消息内容
            author_name = msg.author.display_name
            content = msg.content if msg.content else "[无文本内容]"
            
            # 如果消息有附件，添加附件说明
            if msg.attachments:
                attachments_info = f" [附件: {', '.join([att.filename for att in msg.attachments])}]"
                content += attachments_info
            
            # 如果消息有嵌入（embed），添加说明
            if msg.embeds:
                content += f" [包含{len(msg.embeds)}个嵌入内容]"
            
            formatted_lines.append(f"[{author_name}]: {content}")
        
        return "\n".join(formatted_lines)
    
    def load_prompts(self, template_key: str) -> tuple[str, str]:
        """
        根据模板配置加载提示词头部和尾部

        Args:
            template_key: 模板标识

        Returns:
            (head_prompt, end_prompt)
        """
        config = TEMPLATE_CONFIG.get(template_key, TEMPLATE_CONFIG[DEFAULT_TEMPLATE_KEY])
        head_path = config.get("head_path", "")
        end_path = config.get("end_path", "")

        try:
            with open(head_path, encoding='utf-8') as f:
                head_prompt = f.read().strip()
        except FileNotFoundError:
            print(f"⚠️ 未找到 {head_path}，使用默认头部提示词")
            head_prompt = self.default_head_prompt

        try:
            with open(end_path, encoding='utf-8') as f:
                end_prompt = f.read().strip()
        except FileNotFoundError:
            print(f"⚠️ 未找到 {end_path}，使用默认尾部提示词")
            end_prompt = self.default_end_prompt

        return head_prompt, end_prompt
    
    async def _validate_and_parse_input(
        self,
        interaction: discord.Interaction,
        message_link: str,
        message_count: int,
        template: app_commands.Choice[str] | None,
    ) -> dict | None:
        """权限检查、参数校验、消息链接解析。失败时已回复 interaction，返回 None。"""
        await _safe_defer(interaction)

        user_id = interaction.user.id
        if not (user_id in self.bot.admins or user_id in self.bot.trusted_users):
            await interaction.edit_original_response(
                content='❌ 没有权限。此命令仅限答疑组使用。'
            )
            return None

        if message_count < 1:
            await interaction.edit_original_response(
                content='❌ 消息数量必须至少为1条。'
            )
            return None

        if message_count > 1000:
            await interaction.edit_original_response(
                content='❌ 消息数量不能超过1000条。'
            )
            return None

        template_key = template.value if template is not None else DEFAULT_TEMPLATE_KEY

        if template_key not in TEMPLATE_CONFIG:
            await interaction.edit_original_response(
                content='❌ 模板参数无效。'
            )
            return None

        try:
            guild_id, channel_id, message_id = self.parse_discord_link(message_link)
        except ValueError as e:
            await interaction.edit_original_response(
                content=f'❌ {str(e)}\n'
                       f'正确格式: https://discord.com/channels/服务器ID/频道ID/消息ID'
            )
            return None

        if interaction.guild_id != guild_id:
            await interaction.edit_original_response(
                content='❌ 只能总结当前服务器的消息。'
            )
            return None

        return {
            "template_key": template_key,
            "guild_id": guild_id,
            "channel_id": channel_id,
            "message_id": message_id,
        }

    async def _fetch_and_format_messages(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        channel_id: int,
        message_id: int,
        message_count: int,
    ) -> tuple[list[discord.Message], dict] | None:
        """频道解析、消息拉取、统计信息。失败时已回复 interaction，返回 None。"""
        channel = await self.resolve_channel(interaction, guild_id, channel_id)
        if not channel:
            await interaction.edit_original_response(
                content='❌ 找不到指定的频道。'
            )
            return None

        if isinstance(channel, discord.Thread):
            try:
                await channel.join()
                print(f"🧵 已尝试加入线程: {channel.id}")
            except discord.Forbidden:
                print(f"❌ 无法加入线程（Forbidden）: {channel.id}")
            except Exception as e:
                print(f"⚠️ 加入线程时发生异常: {e}")

        user_perms = channel.permissions_for(interaction.user)
        if not getattr(user_perms, "view_channel", False):
            await interaction.edit_original_response(
                content='❌ 你没有权限查看该频道的消息。'
            )
            return None

        bot_perms = channel.permissions_for(interaction.guild.me)
        if not getattr(bot_perms, "view_channel", False) or not getattr(bot_perms, "read_message_history", False):
            await interaction.edit_original_response(
                content='❌ 机器人没有查看或读取历史消息的权限。'
            )
            return None

        try:
            start_message = await channel.fetch_message(message_id)
        except discord.NotFound:
            await interaction.edit_original_response(
                content='❌ 找不到指定的消息，可能已被删除。'
            )
            return None
        except discord.Forbidden:
            await interaction.edit_original_response(
                content='❌ 没有权限获取该消息。'
            )
            return None

        await interaction.edit_original_response(
            content=f'⏳ 正在获取 {message_count} 条消息...\n'
                   f'起始消息: {start_message.author.display_name} - {start_message.created_at.strftime("%Y-%m-%d %H:%M")}'
        )

        try:
            messages = await self.fetch_messages_batch(channel, start_message, message_count)

            if not messages:
                await interaction.edit_original_response(
                    content='❌ 未能获取到任何消息。'
                )
                return None

            actual_count = len(messages)

            if messages:
                newest_time = start_message.created_at
                oldest_time = messages[-1].created_at
                time_span = newest_time - oldest_time

                days = time_span.days
                hours = time_span.seconds // 3600
                minutes = (time_span.seconds % 3600) // 60

                if days > 0:
                    time_span_str = f"{days}天{hours}小时{minutes}分钟"
                elif hours > 0:
                    time_span_str = f"{hours}小时{minutes}分钟"
                else:
                    time_span_str = f"{minutes}分钟"
            else:
                time_span_str = "未知"

            participants = set()
            for msg in messages:
                participants.add(msg.author.display_name)

            await interaction.edit_original_response(
                content=f'📊 已获取 {actual_count} 条消息\n'
                       f'⏱️ 时间跨度: {time_span_str}\n'
                       f'👥 参与者: {len(participants)} 人\n'
                       f'⏳ 正在进行AI分析...'
            )

        except Exception as e:
            await interaction.edit_original_response(
                content=f'❌ 获取消息时出错: {str(e)}'
            )
            return None

        return messages, {
            "actual_count": actual_count,
            "time_span_str": time_span_str,
            "participant_count": len(participants),
        }

    async def _call_ai_and_send_result(
        self,
        interaction: discord.Interaction,
        messages: list[discord.Message],
        template_key: str,
        channel_id: int,
        stats: dict,
    ) -> None:
        """提示词组装、AI 调用、分段发送结果。"""
        formatted_messages = self.format_messages_for_prompt(messages)

        head_prompt, end_prompt = self.load_prompts(template_key)
        full_prompt = f"{head_prompt}\n{formatted_messages}\n{end_prompt}"

        print("📊 准备发送给AI的消息统计:")
        print(f"  - 实际消息数: {len(messages)} 条")
        print(f"  - 格式化后文本长度: {len(formatted_messages)} 字符")
        print(f"  - 完整提示词长度: {len(full_prompt)} 字符")
        summary_model = self._get_summary_model()
        summary_display_name = self._get_summary_display_name()

        try:
            if not hasattr(self.bot, 'openai_client') or not self.bot.openai_client:
                await interaction.edit_original_response(
                    content='❌ OpenAI客户端未初始化。'
                )
                return

            messages_for_api = [
                {"role": "system", "content": "你是一个专业的对话分析助手，擅长总结和评判讨论内容。"},
                {"role": "user", "content": full_prompt}
            ]

            response = await asyncio.wait_for(
                self.bot.openai_client.chat.completions.create(
                    model=summary_model,
                    messages=messages_for_api,
                    temperature=1.0,
                    max_tokens=65535
                ),
                timeout=SUMMARY_TIMEOUT_SECONDS
            )

            if not response or not response.choices:
                await interaction.edit_original_response(
                    content='❌ AI返回了空响应。'
                )
                return

            ai_response = response.choices[0].message.content

        except asyncio.TimeoutError:
            await interaction.edit_original_response(
                content=self._get_summary_timeout_message()
            )
            return
        except Exception as e:
            await interaction.edit_original_response(
                content=f'❌ AI分析时出错: {str(e)}'
            )
            return

        actual_count = stats["actual_count"]
        time_span_str = stats["time_span_str"]
        participant_count = stats["participant_count"]

        template_display = TEMPLATE_CONFIG.get(template_key, {}).get("display", template_key)
        header_lines = [
            f"📝 消息总结与评判（模板：{template_display}）",
            f"📊 消息数量: {actual_count} 条",
            f"⏱️ 时间跨度: {time_span_str}",
            f"👥 参与人数: {participant_count} 人",
            f"📢 频道: <#{channel_id}>",
            f"模型: {summary_display_name}",
            "------------------------------",
        ]
        header_text = "\n".join(header_lines)

        for _idx, chunk in enumerate(chunk_text(header_text, MAX_MESSAGE_LENGTH), start=1):
            await interaction.channel.send(content=chunk)

        chunks = chunk_text(ai_response, CHUNK_SAFE_LENGTH)
        total_chunks = len(chunks) or 0

        for i, chunk in enumerate(chunks, start=1):
            if total_chunks > 1:
                prefix = f"[AI 总结第 {i}/{total_chunks} 段]\n"
            else:
                prefix = ""
            await interaction.channel.send(content=f"{prefix}{chunk}")

        await interaction.edit_original_response(
            content='✅ 总结已完成并发送到频道。'
        )

        print(f"✅ 用户 {interaction.user.id} 成功总结了 {actual_count} 条消息（模板: {template_key}）")
        print(f"📊 最终统计: 获取 {len(messages)} 条，格式化 {len(formatted_messages)} 字符")

    @app_commands.command(name="大法官开庭", description="对Discord消息进行AI总结和评判")
    @app_commands.describe(
        message_link="Discord消息链接（右键消息->复制消息链接）",
        message_count="要分析的消息数量（最多1000条）",
        template="选择总结模板"
    )
    @app_commands.choices(
        template=[
            app_commands.Choice(name=config["display"], value=key)
            for key, config in TEMPLATE_CONFIG.items()
        ]
    )
    async def summarize_messages(self,
                                interaction: discord.Interaction,
                                message_link: str,
                                message_count: int,
                                template: app_commands.Choice[str] | None = None):
        """AI快速总结并评判功能的斜杠命令"""
        params = await self._validate_and_parse_input(
            interaction, message_link, message_count, template,
        )
        if params is None:
            return

        result = await self._fetch_and_format_messages(
            interaction,
            params["guild_id"], params["channel_id"], params["message_id"],
            message_count,
        )
        if result is None:
            return

        messages, stats = result
        await self._call_ai_and_send_result(
            interaction, messages, params["template_key"], params["channel_id"], stats,
        )

async def setup(bot: commands.Bot):
    """设置Cog"""
    if not getattr(bot, 'openai_client', None):
        print("⚠️ [Summary] bot.openai_client 未初始化，相关功能将不可用")
    await bot.add_cog(Summary(bot))
    print("✅ Summary Cog 已加载")
