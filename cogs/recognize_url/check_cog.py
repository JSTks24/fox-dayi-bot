import asyncio
import os
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

from cogs.utils import CooldownManager, safe_defer, encode_image_to_base64, compress_image
from paths import APP_TEMP_DIR

from .url_matcher import URLMatcher

APP_TEMP_PATH = os.fspath(APP_TEMP_DIR)


class URLCheckCog(commands.Cog):
    """'查成分'上下文菜单命令：图片处理 + LLM 提取 URL + 本地匹配。"""

    def __init__(self, bot: commands.Bot, matcher: URLMatcher):
        self.bot = bot
        self.matcher = matcher
        self.cooldowns = CooldownManager(30)

        self.ctx_menu = app_commands.ContextMenu(
            name='查成分',
            callback=self.check_url_compliance,
        )
        self.bot.tree.add_command(self.ctx_menu)

    async def cog_unload(self):
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)

    def _check_permission(self, user_id: int) -> bool:
        return user_id in self.bot.admins or user_id in self.bot.trusted_users

    async def _log_to_thread(self, message: discord.Message, image_attachment: discord.Attachment):
        try:
            thread_ids_raw = os.getenv('QUICK_PUNISH_LOG_THREAD')
            if not thread_ids_raw:
                print("⚠️ 未配置QUICK_PUNISH_LOG_THREAD，跳过日志记录")
                return

            thread = None
            for tid in thread_ids_raw.split(','):
                tid = tid.strip()
                if not tid:
                    continue
                try:
                    thread = self.bot.get_channel(int(tid))
                    if thread:
                        break
                except ValueError:
                    continue

            if not thread:
                print(f"❌ 无法找到子区: {thread_ids_raw}")
                return

            log_message = (
                f"**URL合规性检查日志**\n"
                f"消息作者: {message.author.mention} ({message.author.id})\n"
                f"消息链接: [跳转]({message.jump_url})\n"
                f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            )

            await thread.send(log_message, file=await image_attachment.to_file())
            print(f"✅ 已记录日志到子区 {thread.id}")
        except Exception as e:
            print(f"❌ 记录日志失败: {e}")

    async def _process_single_image(
        self,
        image_attachment: discord.Attachment,
        user_id: int,
        idx: int,
    ) -> list[tuple[str, str, str, dict | None]]:
        """处理单张图片：保存→压缩→LLM提取URL→本地匹配。"""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        base_filename = f"{timestamp}_{user_id}_url_check_{idx}"
        temp_dir = APP_TEMP_PATH
        image_path = None

        try:
            if not os.path.exists(temp_dir):
                os.makedirs(temp_dir)

            _, image_extension = os.path.splitext(image_attachment.filename)
            image_path = os.path.join(temp_dir, f"{base_filename}{image_extension}")
            await image_attachment.save(image_path)

            compressed_path = await compress_image(image_path)
            system_prompt = self.matcher.build_prompt()
            base64_image = encode_image_to_base64(compressed_path)

            messages = [
                {"role": "user", "content": [
                    {"type": "text", "text": system_prompt},
                    {"type": "image_url", "image_url": {"url": base64_image}},
                ]}
            ]

            client = self.bot.openai_client
            model = os.getenv("URL_CHECK_MODEL", os.getenv("OPENAI_MODEL"))

            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.5,
                    max_tokens=8192,
                    reasoning_effort="none"
                ),
                timeout=60.0,
            )

            if isinstance(response, list):
                if response and len(response) > 0 and hasattr(response[0], 'choices'):
                    response = response[0]

            if not hasattr(response, 'choices') or not response.choices or len(response.choices) == 0:
                return []

            ai_response = response.choices[0].message.content
            print(f"✅ 图片{idx+1} API响应: {ai_response}")

            extracted_urls = self.matcher.parse_llm_urls(ai_response)
            results = []
            for raw_url in extracted_urls:
                status, entry = self.matcher.match(raw_url)
                domain, path = self.matcher.normalize(raw_url)
                normalized = domain + path if path else domain
                results.append((raw_url, normalized, status, entry))
            return results

        finally:
            if os.getenv("DELETE_TEMP_FILES", "false").lower() == "true":
                if image_path and os.path.exists(image_path):
                    try:
                        os.remove(image_path)
                    except OSError:
                        pass
                if image_path:
                    compressed_path = f"{os.path.splitext(image_path)[0]}_compressed.jpg"
                    if os.path.exists(compressed_path):
                        try:
                            os.remove(compressed_path)
                        except OSError:
                            pass

    async def check_url_compliance(self, interaction: discord.Interaction, message: discord.Message):
        """APP命令：查成分"""
        await safe_defer(interaction)

        user_id = interaction.user.id

        if not self._check_permission(user_id):
            await interaction.edit_original_response(content='❌ 没权。此命令仅限答疑组使用。')
            return

        message_id = message.id
        is_on_cooldown, remaining_time = self.cooldowns.check_and_update(message_id)
        if is_on_cooldown:
            await interaction.edit_original_response(
                content=f'⏱️ 此消息的"查成分"命令正在冷却中，请等待 {remaining_time} 秒后再试。'
            )
            return

        image_attachments = [att for att in message.attachments if att.content_type and att.content_type.startswith('image/')]

        if not image_attachments:
            await interaction.edit_original_response(content='❌ 该消息没有图片附件。')
            return

        if len(image_attachments) > 3:
            image_attachments = image_attachments[:3]

        await self._log_to_thread(message, image_attachments[0])

        img_count = len(image_attachments)
        await interaction.edit_original_response(
            content=f"⏳ 正在处理 {img_count} 张图片，请稍候..."
        )

        client = self.bot.openai_client
        if not client:
            await interaction.edit_original_response(content="❌ OpenAI客户端未初始化。")
            return

        try:
            all_results: list[tuple[str, str, str, dict | None]] = []
            for idx, att in enumerate(image_attachments):
                try:
                    results = await self._process_single_image(att, user_id, idx)
                    all_results.extend(results)
                except asyncio.TimeoutError:
                    all_results.append((f"[图片{idx+1}超时]", "", "unknown", None))
                except Exception as e:
                    print(f"❌ 处理图片{idx+1}出错: {e}")
                    all_results.append((f"[图片{idx+1}出错: {e}]", "", "unknown", None))

            if not all_results:
                await interaction.followup.send(
                    "**URL合规性检查结果**\n\n未在图片中检测到任何URL。",
                    ephemeral=True,
                )
                await interaction.edit_original_response(content="✅ 检查完成。")
                return

            status_priority = {'good': 0, 'unknown': 1, 'bad': 2}
            worst_status = 'good'
            for _, _, status, _ in all_results:
                if status_priority.get(status, 1) > status_priority.get(worst_status, 0):
                    worst_status = status

            status_config = {
                'good': {'title': '✅ 合规', 'color': 0x2ecc71},
                'bad': {'title': '❌ 违规', 'color': 0xe74c3c},
                'unknown': {'title': '❓ 未知', 'color': 0xf39c12},
            }
            cfg = status_config[worst_status]
            embed = discord.Embed(
                title=f"URL合规性检查 — {cfg['title']}",
                color=cfg['color'],
            )

            for raw_url, normalized, status, entry in all_results:
                if status == 'good':
                    icon = '✅'
                    value = f"域名: `{normalized}`\n状态: 合规"
                    if entry:
                        value += f"\n命中: `{entry['matched_key']}`"
                        if entry['name']:
                            value += f"\n名称: {entry['name']}"
                        if entry['description']:
                            value += f"\n描述: {entry['description']}"
                elif status == 'bad':
                    icon = '❌'
                    value = f"域名: `{normalized}`\n状态: 违规"
                    if entry:
                        value += f"\n命中: `{entry['matched_key']}`"
                        if entry['name']:
                            value += f"\n名称: {entry['name']}"
                        if entry['description']:
                            value += f"\n描述: {entry['description']}"
                else:
                    icon = '❓'
                    value = f"域名: `{normalized}`\n状态: 未收录" if normalized else "处理失败"

                embed.add_field(
                    name=f"{icon} {raw_url}",
                    value=value,
                    inline=False,
                )

            embed.set_footer(text=f"检测时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 图片数: {img_count}")

            await interaction.followup.send(embed=embed, ephemeral=True)
            await interaction.edit_original_response(content="✅ 检查完成。")

        except Exception as e:
            print(f"❌ 处理URL检查时出错: {e}")
            import traceback
            traceback.print_exc()
            await interaction.edit_original_response(content=f"❌ 处理时出错: {str(e)}")
