from __future__ import annotations

import discord
import asyncio
import os
from datetime import datetime
import logging
import traceback
import tiktoken
import time
from cogs.utils import compress_image, encode_image_to_base64, get_file_size_kb
from paths import PROMPT_DIR

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

DEFAULT_KB_PROMPT_PATH = os.fspath(PROMPT_DIR / "ALL.txt")
RAW_KB_PROMPT_PATH = os.fspath(PROMPT_DIR / "raw.txt")

class MentionAIMixin:
    async def generate_ai_response(self, message: discord.Message, thread_id: str):
        """生成AI回复（流式）"""
        temp_files = []  # 用于跟踪需要清理的临时文件
        
        try:
            # 发送处理中的消息
            processing_msg = await message.reply("🤔 Vula 思考中...")
            
            # 获取消息内容、上下文和图片
            user_message_content, context_messages, image_paths = await self.extract_message_context(message, thread_id)
            temp_files.extend(image_paths)  # 记录原始图片路径
            
            # 如果有图片，进行压缩
            compressed_image_paths = []
            if image_paths:
                logger.info(f"📸 检测到 {len(image_paths)} 张图片，开始压缩...")
                for img_path in image_paths:
                    compressed_path = await compress_image(img_path)
                    compressed_image_paths.append(compressed_path)
                    if compressed_path != img_path:
                        temp_files.append(compressed_path)  # 记录压缩后的图片路径
                logger.info("✅ 图片压缩完成")
            
            # 构建提示词
            system_prompt = await self.build_prompt(thread_id, context_messages)
            
            # 调用OpenAI API
            client = self.bot.openai_client
            if not client:
                await processing_msg.edit(content="❌ OpenAI客户端未初始化")
                return
            
            # 构建用户消息内容（支持多模态）
            if compressed_image_paths:
                # 有图片：构建多模态消息
                user_content = [{"type": "text", "text": user_message_content}]
                for img_path in compressed_image_paths:
                    size_kb = get_file_size_kb(img_path)
                    logger.info(f"📎 添加图片到API请求: {os.path.basename(img_path)} ({size_kb:.2f}KB)")
                    base64_image = encode_image_to_base64(img_path)
                    user_content.append({
                        "type": "image_url",
                        "image_url": {"url": base64_image}
                    })
                
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content}
                ]
            else:
                # 纯文本消息
                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message_content}
                ]
            
            # 获取模型名称和编码器
            model_name = os.getenv("OPENAI_MODEL", "gpt-4")
            try:
                encoding = tiktoken.encoding_for_model(model_name)
            except KeyError:
                # 如果模型不支持，使用默认编码器
                encoding = tiktoken.get_encoding("cl100k_base")
            
            # 计算输入token数
            input_tokens = 0
            for msg in messages:
                if isinstance(msg["content"], str):
                    input_tokens += len(encoding.encode(msg["content"]))
                elif isinstance(msg["content"], list):
                    # 多模态消息，只计算文本部分
                    for item in msg["content"]:
                        if item.get("type") == "text":
                            input_tokens += len(encoding.encode(item["text"]))
            
            # 记录开始时间
            start_time = time.time()
            
            # 流式调用 API
            stream = await client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=1.0,
                stream=True
            )
            
            # 处理流式响应
            ai_response = ""
            loop = asyncio.get_running_loop()
            last_update_time = loop.time()
            has_started_output = False
            
            async for chunk in stream:
                if chunk.choices and len(chunk.choices) > 0:
                    delta = chunk.choices[0].delta
                    if delta.content:
                        ai_response += delta.content
                        
                        # 第一次收到内容时，标记已开始输出
                        if not has_started_output:
                            has_started_output = True
                            last_update_time = loop.time()
                        
                        # 根据配置的间隔更新消息
                        stream_interval = self.settings.get('stream_interval', 5)
                        current_time = loop.time()
                        if current_time - last_update_time >= stream_interval:
                            try:
                                # 限制显示长度，避免超过Discord消息限制
                                display_text = ai_response[:2000] if len(ai_response) <= 2000 else ai_response[:2000]
                                await processing_msg.edit(content=display_text)
                                last_update_time = current_time
                            except discord.errors.HTTPException as e:
                                # 如果编辑失败（比如内容太长），记录但继续
                                logger.warning(f"更新流式消息失败: {e}")
            
            # 计算结束时间和统计信息
            end_time = time.time()
            elapsed_time = end_time - start_time
            
            # 计算输出token数
            output_tokens = len(encoding.encode(ai_response))
            
            # 计算平均每秒输出token量
            tokens_per_second = output_tokens / elapsed_time if elapsed_time > 0 else 0
            
            # 构建统计信息（使用Discord的-#语法显示小字）
            stats_text = f"\n\n-# 输入: {input_tokens} | 输出: {output_tokens} | 用时: {elapsed_time:.2f}s | 速度: {tokens_per_second:.1f}"
            
            # 流式响应完成后，编辑成最终版本
            if not ai_response:
                await processing_msg.edit(content="❌ API返回空响应")
                return
            
            # 编辑成最终完整回复（包含统计信息）
            # 如果回复太长，需要分段发送
            final_content = ai_response + stats_text
            if len(final_content) <= 2000:
                await processing_msg.edit(content=final_content)
            else:
                # 如果加上统计信息后超长，尝试只在最后一段加统计信息
                if len(ai_response) <= 2000:
                    # AI回复本身不超长，但加上统计信息后超长
                    # 尝试缩短统计信息或分段
                    await processing_msg.edit(content=ai_response[:2000])
                    remaining = ai_response[2000:] + stats_text
                    chunks = [remaining[i:i+2000] for i in range(0, len(remaining), 2000)]
                    for chunk in chunks:
                        await processing_msg.reply(chunk)
                else:
                    # AI回复本身就超长
                    # 第一条消息编辑为前2000字符
                    await processing_msg.edit(content=ai_response[:2000])
                    # 剩余内容作为回复发送，统计信息放在最后一段
                    remaining = ai_response[2000:]
                    chunks = [remaining[i:i+2000] for i in range(0, len(remaining), 2000)]
                    for i, chunk in enumerate(chunks):
                        if i == len(chunks) - 1:
                            # 最后一段，加上统计信息
                            final_chunk = chunk + stats_text
                            if len(final_chunk) <= 2000:
                                await processing_msg.reply(final_chunk)
                            else:
                                # 如果最后一段加上统计信息后还是超长，分成两段
                                await processing_msg.reply(chunk)
                                await processing_msg.reply(stats_text)
                        else:
                            await processing_msg.reply(chunk)
            
            logger.info(f"成功生成AI回复 (thread: {thread_id}, user: {message.author.id}, length: {len(ai_response)})")
            
        except asyncio.TimeoutError:
            await processing_msg.edit(content="⏱️ 处理超时，请稍后再试")
            logger.warning(f"AI生成超时 (thread: {thread_id})")
        except Exception as e:
            logger.error(f"AI生成失败: {e}")
            logger.error(traceback.format_exc())
            try:
                await processing_msg.edit(content=f"❌ 生成失败: {str(e)}")
            except Exception:
                pass
        finally:
            # 清理临时文件
            for temp_file in temp_files:
                try:
                    if temp_file and os.path.exists(temp_file):
                        os.remove(temp_file)
                        logger.debug(f"🗑️ 已删除临时文件: {os.path.basename(temp_file)}")
                except Exception as e:
                    logger.warning(f"删除临时文件失败 {temp_file}: {e}")

    async def extract_message_context(self, message: discord.Message, thread_id: str) -> tuple[str, list[str], list[str]]:
        """
        提取消息内容和上下文
        返回: (用户消息文本, 上下文消息列表, 图片路径列表)
        """
        # 移除@机器人的部分
        user_message = message.content
        for mention in message.mentions:
            user_message = user_message.replace(f'<@{mention.id}>', '').replace(f'<@!{mention.id}>', '')
        user_message = user_message.strip()
        
        # 如果没有文本内容，使用默认提示
        if not user_message:
            user_message = "请帮我看看这个问题"
        
        # 处理当前消息的图片附件
        image_paths = []
        image_attachments = [att for att in message.attachments if att.content_type and att.content_type.startswith('image/')]
        
        if image_attachments:
            logger.info(f"📸 检测到当前消息 {len(image_attachments)} 张图片附件")
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            user_id = message.author.id
            
            for idx, attachment in enumerate(image_attachments):
                try:
                    # 保存图片到临时目录
                    _, ext = os.path.splitext(attachment.filename)
                    temp_path = os.path.join(self.temp_dir, f"{timestamp}_{user_id}_{idx}{ext}")
                    await attachment.save(temp_path)
                    image_paths.append(temp_path)
                    logger.info(f"  保存当前消息图片 {idx+1}: {attachment.filename} ({attachment.size / 1024:.2f} KB)")
                except Exception as e:
                    logger.error(f"保存图片附件失败: {e}")
        
        context_messages = []
        
        # 获取被回复的消息
        if message.reference and message.reference.message_id:
            try:
                replied_message = await message.channel.fetch_message(message.reference.message_id)
                replied_content = replied_message.content if replied_message.content else "[无文字内容]"
                
                # 处理被回复消息的图片附件
                replied_image_attachments = [att for att in replied_message.attachments if att.content_type and att.content_type.startswith('image/')]
                if replied_image_attachments:
                    logger.info(f"📸 检测到被回复消息 {len(replied_image_attachments)} 张图片附件")
                    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                    replied_user_id = replied_message.author.id
                    
                    for idx, attachment in enumerate(replied_image_attachments):
                        try:
                            # 保存被回复消息的图片到临时目录
                            _, ext = os.path.splitext(attachment.filename)
                            temp_path = os.path.join(self.temp_dir, f"{timestamp}_replied_{replied_user_id}_{idx}{ext}")
                            await attachment.save(temp_path)
                            image_paths.append(temp_path)
                            logger.info(f"  保存被回复消息图片 {idx+1}: {attachment.filename} ({attachment.size / 1024:.2f} KB)")
                        except Exception as e:
                            logger.error(f"保存被回复消息图片附件失败: {e}")
                    
                    # 在上下文中标注图片数量
                    replied_content += f" [包含{len(replied_image_attachments)}张图片]" if replied_content else f"[包含{len(replied_image_attachments)}张图片]"
                
                #检查是否有其他附件
                other_attachments = [att for att in replied_message.attachments if not (att.content_type and att.content_type.startswith('image/'))]
                if other_attachments:
                    replied_content += f" [其他附件×{len(other_attachments)}]"
                
                context_messages.append(f"[被回复的消息] {replied_message.author.display_name}: {replied_content}")
            except Exception as e:
                logger.warning(f"获取被回复消息失败: {e}")
        
        # 获取历史消息
        thread_config = self.threads.get(thread_id, {})
        history_depth = thread_config.get('xSettings', {}).get('read_user_interaction_history', 
                                                                self.settings.get('read_reply_history_depth', 5))
        
        if history_depth > 0:
            try:
                # 获取当前消息之前的n条消息
                history = []
                async for hist_msg in message.channel.history(limit=history_depth + 1, before=message):
                    if not hist_msg.author.bot or hist_msg.author.id == self.bot.user.id:
                        msg_content = self._extract_message_text_with_attachments(hist_msg)
                        # 处理 embed 消息
                        if hist_msg.embeds:
                            embed_texts = self._extract_embed_content(hist_msg.embeds)
                            if embed_texts:
                                msg_content += " " + " ".join(embed_texts)
                        history.append(f"{hist_msg.author.display_name}: {msg_content}")
                
                # 反转顺序（从旧到新）
                history.reverse()
                context_messages.extend(history)
                
            except Exception as e:
                logger.warning(f"获取历史消息失败: {e}")
        
        return user_message, context_messages, image_paths

    def _extract_message_text_with_attachments(self, message: discord.Message) -> str:
        """
        提取消息的文本内容，如果有图片附件则标注
        
        Args:
            message: Discord消息对象
            
        Returns:
            处理后的文本内容
        """
        content = message.content if message.content else ""
        
        # 检查是否有图片附件
        image_attachments = [att for att in message.attachments if att.content_type and att.content_type.startswith('image/')]
        if image_attachments:
            if content:
                content += f" [图片附件×{len(image_attachments)}]"
            else:
                content = f"[图片附件×{len(image_attachments)}]"
        
        # 检查是否有其他附件
        other_attachments = [att for att in message.attachments if not (att.content_type and att.content_type.startswith('image/'))]
        if other_attachments:
            if content:
                content += f" [其他附件×{len(other_attachments)}]"
            else:
                content = f"[其他附件×{len(other_attachments)}]"
        
        return content if content else "[空消息]"

    def _extract_embed_content(self, embeds: list[discord.Embed]) -> list[str]:
        """
        提取 embed 消息的文本内容
        
        Args:
            embeds: Embed对象列表
            
        Returns:
            提取的文本内容列表
        """
        embed_texts = []
        
        for embed in embeds:
            parts = []
            
            # 提取标题
            if embed.title:
                parts.append(f"[Embed标题: {embed.title}]")
            
            # 提取描述
            if embed.description:
                # 限制描述长度，避免过长
                desc = embed.description[:200] + "..." if len(embed.description) > 200 else embed.description
                parts.append(f"[Embed内容: {desc}]")
            
            # 提取字段
            if embed.fields:
                field_texts = []
                for field in embed.fields[:3]:  # 最多提取3个字段
                    field_texts.append(f"{field.name}: {field.value[:100]}")
                if field_texts:
                    parts.append(f"[Embed字段: {'; '.join(field_texts)}]")
            
            # 提取URL
            if embed.url:
                parts.append(f"[Embed链接: {embed.url}]")
            
            if parts:
                embed_texts.append(" ".join(parts))
        
        return embed_texts

    def save_prompt_log(self, prompt: str) -> None:
        """
        保存提示词到日志文件，并自动清理超过数量限制的旧文件
        """
        try:
            # 获取配置的保存数量
            prompt_log_count = self.settings.get('prompt_log_count', 5)
            
            # 生成文件名（时间戳）
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
            log_file = os.path.join(self.prompt_log_path, f"prompt_{timestamp}.txt")
            
            # 保存提示词
            with open(log_file, 'w', encoding='utf-8') as f:
                f.write(prompt)
            logger.info(f"📝 已保存提示词日志: {os.path.basename(log_file)}")
            
            # 清理超过数量限制的旧文件
            log_files = sorted(
                [f for f in os.listdir(self.prompt_log_path) if f.startswith('prompt_') and f.endswith('.txt')],
                reverse=True  # 从新到旧排序
            )
            
            # 删除超出数量的旧文件
            if len(log_files) > prompt_log_count:
                files_to_delete = log_files[prompt_log_count:]
                for old_file in files_to_delete:
                    old_file_path = os.path.join(self.prompt_log_path, old_file)
                    try:
                        os.remove(old_file_path)
                        logger.debug(f"🗑️ 已删除旧提示词日志: {old_file}")
                    except Exception as e:
                        logger.warning(f"删除旧提示词日志失败 {old_file}: {e}")
                
                logger.info(f"🧹 已清理 {len(files_to_delete)} 个旧提示词日志文件")
        
        except Exception as e:
            logger.error(f"保存提示词日志失败: {e}")

    async def build_prompt(self, thread_id: str, context_messages: list[str]) -> str:
        """
        构建系统提示词
        """
        thread_config = self.threads.get(thread_id, {})
        use_default_kb = thread_config.get('xSettings', {}).get('use_default_knowledge_base', True)
        
        # 选择基础提示词
        if use_default_kb:
            base_prompt_path = DEFAULT_KB_PROMPT_PATH
        else:
            base_prompt_path = RAW_KB_PROMPT_PATH
        
        # 加载基础提示词
        try:
            with open(base_prompt_path, encoding='utf-8') as f:
                system_prompt = f.read().strip()
        except FileNotFoundError:
            logger.warning(f"提示词文件不存在: {base_prompt_path}")
            system_prompt = "You are a helpful assistant."
        
        # 插入子区元数据
        thread_metadata = await self.get_thread_metadata(thread_id)
        if thread_metadata:
            system_prompt += "\n\n[子区信息]\n" + thread_metadata
        
        # 加载子区专属知识库
        kb_file = os.path.join(self.kb_path, f"{thread_id}.txt")
        has_custom_kb = False
        if os.path.exists(kb_file):
            try:
                with open(kb_file, encoding='utf-8') as f:
                    kb_content = f.read().strip()
                    if kb_content:
                        system_prompt += "\n\n[专属知识库]\n" + kb_content
                        has_custom_kb = True
                        logger.info(f"已加载子区 {thread_id} 的知识库")
            except Exception as e:
                logger.warning(f"加载子区知识库失败: {e}")
        
        # ⚠️ 警告：如果禁用了默认知识库但没有上传自定义知识库
        if not use_default_kb and not has_custom_kb:
            logger.warning(f"⚠️ 子区 {thread_id} 禁用了默认知识库但没有上传自定义知识库，bot可能无法提供专业答疑")
        
        # 添加上下文消息
        if context_messages:
            context_text = "\n\n[近期对话]\n" + "\n".join(context_messages)
            system_prompt += context_text
        
        # 保存提示词日志
        self.save_prompt_log(system_prompt)
        
        return system_prompt
