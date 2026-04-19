import discord
from discord.ext import commands
from discord import app_commands
import os
import asyncio
import json
from datetime import datetime
from cogs.utils import CooldownManager, safe_defer, encode_image_to_base64, compress_image

# --- Cog 主体 ---

class RecognizeURL(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cooldowns = CooldownManager(30)
        
        # 将上下文菜单命令添加到 bot 的 tree 中
        self.ctx_menu = app_commands.ContextMenu(
            name='查成分',
            callback=self.check_url_compliance,
        )
        self.bot.tree.add_command(self.ctx_menu)

    async def cog_unload(self):
        """Cog 卸载时移除命令"""
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)
    
    def _check_permission(self, user_id: int) -> bool:
        """检查用户是否有权限使用此功能"""
        return user_id in self.bot.admins or user_id in self.bot.trusted_users
    
    def _load_json(self, file_path: str) -> dict:
        """加载JSON文件"""
        try:
            with open(file_path, encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            print(f"⚠️ 文件不存在: {file_path}")
            return {}
        except json.JSONDecodeError as e:
            print(f"❌ JSON解析失败 {file_path}: {e}")
            return {}
        except Exception as e:
            print(f"❌ 加载JSON文件失败 {file_path}: {e}")
            return {}
    
    def _save_json(self, file_path: str, data: dict) -> bool:
        """保存JSON文件"""
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
            return True
        except Exception as e:
            print(f"❌ 保存JSON文件失败 {file_path}: {e}")
            return False
    
    def _build_prompt(self) -> str:
        """组合完整提示词"""
        try:
            # 读取基础提示词
            with open('api_table/prompt.txt', encoding='utf-8') as f:
                base_prompt = f.read().strip()
            
            # 读取good.json和bad.json
            good_data = self._load_json('api_table/good.json')
            bad_data = self._load_json('api_table/bad.json')
            
            # 组合提示词
            full_prompt = base_prompt + "\n\n"
            full_prompt += json.dumps(good_data, ensure_ascii=False, indent=2)
            full_prompt += "\n\n以下是bad_json的内容：\n\n"
            full_prompt += json.dumps(bad_data, ensure_ascii=False, indent=2)
            
            return full_prompt
        except Exception as e:
            print(f"❌ 构建提示词失败: {e}")
            return "你是一个URL识别助手。"
    
    def _normalize_url(self, url: str) -> str:
        """标准化URL格式"""
        # 移除协议前缀
        url = url.replace('https://', '').replace('http://', '')
        # 移除尾部斜杠
        url = url.rstrip('/')
        # 移除端口号（如果有）
        if ':' in url:
            url = url.split(':')[0]
        return url.lower()
    
    def _log_operation_to_history(
        self,
        user: discord.User,
        operation_type: str,
        url: str,
        name: str | None = None,
        description: str | None = None,
        success: bool = True
    ):
        """
        记录操作历史到 api_table/history.txt
        
        Args:
            user: 操作者
            operation_type: 操作类型（添加到好API/添加到坏API/删除）
            url: 操作的URL
            name: API站点名称（可选）
            description: API站点描述（可选）
            success: 操作是否成功
        """
        try:
            history_file = 'api_table/history.txt'
            
            # 确保目录存在
            os.makedirs(os.path.dirname(history_file), exist_ok=True)
            
            # 构建历史记录
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            result_status = "成功" if success else "失败"
            
            log_entry = (
                "=" * 80 + "\n"
                f"时间: {timestamp}\n"
                f"操作者: {user.name} ({user.id})\n"
                f"操作类型: {operation_type}\n"
                f"URL: {url}\n"
            )
            
            # 添加名称和描述（如果提供）
            if name:
                log_entry += f"名称: {name}\n"
            if description:
                log_entry += f"描述: {description}\n"
            
            log_entry += f"结果: {result_status}\n"
            log_entry += "=" * 80 + "\n\n"
            
            # 追加到历史文件
            with open(history_file, 'a', encoding='utf-8') as f:
                f.write(log_entry)
            
            print(f"✅ 已记录操作历史: {operation_type} - {url}")
        
        except Exception as e:
            # 历史记录失败不应影响主要功能，只打印警告
            print(f"⚠️ 记录操作历史失败: {e}")
    
    async def _log_to_thread(self, message: discord.Message, image_attachment: discord.Attachment):
        """记录日志到子区"""
        try:
            thread_id = os.getenv('QUICK_PUNISH_LOG_THREAD')
            if not thread_id:
                print("⚠️ 未配置QUICK_PUNISH_LOG_THREAD，跳过日志记录")
                return
            
            thread = self.bot.get_channel(int(thread_id))
            if not thread:
                print(f"❌ 无法找到子区: {thread_id}")
                return
            
            # 构建日志消息
            log_message = (
                f"**URL合规性检查日志**\n"
                f"消息作者: {message.author.mention} ({message.author.id})\n"
                f"消息链接: [跳转]({message.jump_url})\n"
                f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            )
            
            # 发送日志消息和图片附件
            await thread.send(log_message, file=await image_attachment.to_file())
            print(f"✅ 已记录日志到子区 {thread_id}")
        except Exception as e:
            print(f"❌ 记录日志失败: {e}")
    
    async def check_url_compliance(self, interaction: discord.Interaction, message: discord.Message):
        """
        APP命令：查成分
        检查消息中图片的URL合规性
        """
        # 🔥 黄金法则：永远先 defer！
        await safe_defer(interaction)
        
        user_id = interaction.user.id
        
        # --- 权限检查 ---
        if not self._check_permission(user_id):
            await interaction.edit_original_response(content='❌ 没权。此命令仅限答疑组使用。')
            return
        
        # --- 冷却时间检查 ---
        message_id = message.id
        is_on_cooldown, remaining_time = self.cooldowns.check_and_update(message_id)
        if is_on_cooldown:
            await interaction.edit_original_response(
                content=f'⏱️ 此消息的"查成分"命令正在冷却中，请等待 {remaining_time} 秒后再试。'
            )
            return
        
        # --- 提取图片附件 ---
        image_attachments = [att for att in message.attachments if att.content_type and att.content_type.startswith('image/')]
        
        if not image_attachments:
            await interaction.edit_original_response(content='❌ 该消息没有图片附件。')
            return
        
        if len(image_attachments) > 1:
            await interaction.edit_original_response(content='❌ 该消息包含多张图片，请确保只有一张图片。')
            return
        
        # 获取图片附件
        image_attachment = image_attachments[0]
        
        # 立即记录日志到子区（不等待API结果）
        await self._log_to_thread(message, image_attachment)
        
        # 更新状态消息
        await interaction.edit_original_response(content="⏳ 正在处理图片，请稍候...")
        
        # --- 文件处理 ---
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        base_filename = f"{timestamp}_{user_id}_url_check"
        temp_dir = 'app_temp'
        image_path = None
        
        try:
            if not os.path.exists(temp_dir):
                os.makedirs(temp_dir)
            
            # 保存图片
            _, image_extension = os.path.splitext(image_attachment.filename)
            image_path = os.path.join(temp_dir, f"{base_filename}{image_extension}")
            await image_attachment.save(image_path)
            
            print(f"📸 保存图片: {image_path}")
            
            # 压缩图片
            compressed_path = await compress_image(image_path)
            
            # 构建提示词
            system_prompt = self._build_prompt()
            
            # 编码图片
            base64_image = encode_image_to_base64(compressed_path)
            
            # 构建请求
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": base64_image}}
                ]}
            ]
            
            # 调用API
            client = self.bot.openai_client
            if not client:
                await interaction.edit_original_response(content="❌ OpenAI客户端未初始化。")
                return
            
            # 使用URL_CHECK_MODEL或默认OPENAI_MODEL
            model = os.getenv("URL_CHECK_MODEL", os.getenv("OPENAI_MODEL"))
            
            print(f"📤 调用API识别URL，模型: {model}")
            
            try:
                response = await asyncio.wait_for(
                    client.chat.completions.create(
                        model=model,
                        messages=messages,
                        temperature=0.3,
                        max_tokens=500
                    ),
                    timeout=60.0  # 60秒超时
                )
                
                # 处理响应
                if isinstance(response, list):
                    print("⚠️ 检测到列表响应，尝试提取第一个元素")
                    if response and len(response) > 0 and hasattr(response[0], 'choices'):
                        response = response[0]
                
                if not hasattr(response, 'choices') or not response.choices or len(response.choices) == 0:
                    await interaction.edit_original_response(content="❌ API返回空响应，请稍后重试。")
                    return
                
                ai_response = response.choices[0].message.content
                print(f"✅ API响应成功，长度: {len(ai_response)}")
                
                # 发送结果（私密消息，仅使用命令的人可见）
                result_message = f"**URL合规性检查结果**\n\n{ai_response}"
                
                # 以私密方式发送结果
                await interaction.followup.send(result_message, ephemeral=True)
                
                # 编辑原始响应
                await interaction.edit_original_response(content="✅ 检查完成。")
                
            except asyncio.TimeoutError:
                await interaction.edit_original_response(
                    content="⏱️ API请求超时（60秒），请稍后重试。"
                )
                print("⚠️ URL检查API请求超时")
                return
            
        except Exception as e:
            print(f"❌ 处理URL检查时出错: {e}")
            import traceback
            traceback.print_exc()
            await interaction.edit_original_response(content=f"❌ 处理时出错: {str(e)}")
        
        finally:
            # 清理临时文件
            if os.getenv("DELETE_TEMP_FILES", "false").lower() == "true":
                if image_path and os.path.exists(image_path):
                    try:
                        os.remove(image_path)
                        print(f"🗑️ 已删除临时文件: {os.path.basename(image_path)}")
                    except Exception as e:
                        print(f"⚠️ 删除临时文件失败: {e}")
                
                # 删除压缩文件
                if image_path:
                    compressed_path = f"{os.path.splitext(image_path)[0]}_compressed.jpg"
                    if os.path.exists(compressed_path):
                        try:
                            os.remove(compressed_path)
                            print(f"🗑️ 已删除压缩文件: {os.path.basename(compressed_path)}")
                        except Exception as e:
                            print(f"⚠️ 删除压缩文件失败: {e}")
    
    @app_commands.command(name='url速查表-编辑', description='编辑URL速查表（添加/删除URL）')
    @app_commands.describe(
        url='要操作的URL',
        操作='选择操作类型',
        名称='API站点名称（添加时可选）',
        描述='API站点描述（添加时可选）'
    )
    @app_commands.choices(操作=[
        app_commands.Choice(name='添加到好API', value='add_good'),
        app_commands.Choice(name='添加到坏API', value='add_bad'),
        app_commands.Choice(name='删除', value='delete')
    ])
    async def url_table_edit(
        self,
        interaction: discord.Interaction,
        url: str,
        操作: app_commands.Choice[str],
        名称: str | None = None,
        描述: str | None = None
    ):
        """编辑URL速查表"""
        # 🔥 黄金法则：永远先 defer！
        await safe_defer(interaction)
        
        user_id = interaction.user.id
        
        # --- 权限检查 ---
        if not self._check_permission(user_id):
            await interaction.followup.send('❌ 没权。此命令仅限答疑组使用。', ephemeral=True)
            return
        
        # 标准化URL
        normalized_url = self._normalize_url(url)
        
        operation = 操作.value
        
        try:
            if operation == 'delete':
                # 删除操作：从两个文件中查找并删除
                good_data = self._load_json('api_table/good.json')
                bad_data = self._load_json('api_table/bad.json')
                
                deleted_from = []
                
                # 从good.json中删除
                if 'good' in good_data and normalized_url in good_data['good']:
                    del good_data['good'][normalized_url]
                    self._save_json('api_table/good.json', good_data)
                    deleted_from.append('好API列表')
                
                # 从bad.json中删除
                if 'bad' in bad_data and normalized_url in bad_data['bad']:
                    del bad_data['bad'][normalized_url]
                    self._save_json('api_table/bad.json', bad_data)
                    deleted_from.append('坏API列表')
                
                if deleted_from:
                    # 记录操作历史
                    self._log_operation_to_history(
                        user=interaction.user,
                        operation_type="删除",
                        url=normalized_url,
                        success=True
                    )
                    
                    await interaction.followup.send(
                        f"✅ 已从 {' 和 '.join(deleted_from)} 中删除URL:\n`{normalized_url}`",
                        ephemeral=True
                    )
                else:
                    # 记录失败的删除操作
                    self._log_operation_to_history(
                        user=interaction.user,
                        operation_type="删除",
                        url=normalized_url,
                        success=False
                    )
                    
                    await interaction.followup.send(
                        f"⚠️ 未找到URL: `{normalized_url}`",
                        ephemeral=True
                    )
            
            elif operation == 'add_good':
                # 添加到好API
                good_data = self._load_json('api_table/good.json')
                
                if 'good' not in good_data:
                    good_data['good'] = {}
                
                # 构建值列表 [名称, 描述]
                value = [名称 or "", 描述 or ""]
                good_data['good'][normalized_url] = value
                
                if self._save_json('api_table/good.json', good_data):
                    # 记录操作历史
                    self._log_operation_to_history(
                        user=interaction.user,
                        operation_type="添加到好API",
                        url=normalized_url,
                        name=名称,
                        description=描述,
                        success=True
                    )
                    
                    await interaction.followup.send(
                        f"✅ 已添加到好API列表:\n"
                        f"URL: `{normalized_url}`\n"
                        f"名称: {名称 or '(未提供)'}\n"
                        f"描述: {描述 or '(未提供)'}",
                        ephemeral=True
                    )
                else:
                    # 记录失败的添加操作
                    self._log_operation_to_history(
                        user=interaction.user,
                        operation_type="添加到好API",
                        url=normalized_url,
                        name=名称,
                        description=描述,
                        success=False
                    )
                    
                    await interaction.followup.send("❌ 保存失败，请检查文件权限。", ephemeral=True)
            
            elif operation == 'add_bad':
                # 添加到坏API
                bad_data = self._load_json('api_table/bad.json')
                
                if 'bad' not in bad_data:
                    bad_data['bad'] = {}
                
                # 构建值列表 [名称, 描述]
                value = [名称 or "", 描述 or ""]
                bad_data['bad'][normalized_url] = value
                
                if self._save_json('api_table/bad.json', bad_data):
                    # 记录操作历史
                    self._log_operation_to_history(
                        user=interaction.user,
                        operation_type="添加到坏API",
                        url=normalized_url,
                        name=名称,
                        description=描述,
                        success=True
                    )
                    
                    await interaction.followup.send(
                        f"✅ 已添加到坏API列表:\n"
                        f"URL: `{normalized_url}`\n"
                        f"名称: {名称 or '(未提供)'}\n"
                        f"描述: {描述 or '(未提供)'}",
                        ephemeral=True
                    )
                else:
                    # 记录失败的添加操作
                    self._log_operation_to_history(
                        user=interaction.user,
                        operation_type="添加到坏API",
                        url=normalized_url,
                        name=名称,
                        description=描述,
                        success=False
                    )
                    
                    await interaction.followup.send("❌ 保存失败，请检查文件权限。", ephemeral=True)
        
        except Exception as e:
            print(f"❌ 编辑URL速查表时出错: {e}")
            import traceback
            traceback.print_exc()
            await interaction.followup.send(f"❌ 操作失败: {str(e)}", ephemeral=True)
    
    @app_commands.command(name='url速查表-查询', description='查询URL在速查表中的状态')
    @app_commands.describe(url='要查询的URL')
    async def url_table_query(self, interaction: discord.Interaction, url: str):
        """查询URL状态"""
        # 🔥 黄金法则：永远先 defer！
        await safe_defer(interaction)
        
        user_id = interaction.user.id
        
        # --- 权限检查 ---
        if not self._check_permission(user_id):
            await interaction.followup.send('❌ 没权。此命令仅限答疑组使用。', ephemeral=True)
            return
        
        # 标准化URL
        normalized_url = self._normalize_url(url)
        
        try:
            # 加载数据
            good_data = self._load_json('api_table/good.json')
            bad_data = self._load_json('api_table/bad.json')
            
            # 查询
            result = None
            status = "未知"
            
            if 'good' in good_data and normalized_url in good_data['good']:
                info = good_data['good'][normalized_url]
                status = "✅ 合规"
                result = (
                    f"**状态:** {status}\n"
                    f"**URL:** `{normalized_url}`\n"
                    f"**名称:** {info[0] if len(info) > 0 else '(无)'}\n"
                    f"**描述:** {info[1] if len(info) > 1 else '(无)'}"
                )
            elif 'bad' in bad_data and normalized_url in bad_data['bad']:
                info = bad_data['bad'][normalized_url]
                status = "🚫 违规"
                result = (
                    f"**状态:** {status}\n"
                    f"**URL:** `{normalized_url}`\n"
                    f"**名称:** {info[0] if len(info) > 0 else '(无)'}\n"
                    f"**描述:** {info[1] if len(info) > 1 else '(无)'}"
                )
            else:
                status = "❓ 未知"
                result = (
                    f"**状态:** {status}\n"
                    f"**URL:** `{normalized_url}`\n"
                    f"该URL不在速查表中。"
                )
            
            await interaction.followup.send(result, ephemeral=True)
        
        except Exception as e:
            print(f"❌ 查询URL时出错: {e}")
            import traceback
            traceback.print_exc()
            await interaction.followup.send(f"❌ 查询失败: {str(e)}", ephemeral=True)

async def setup(bot: commands.Bot):
    """加载Cog"""
    await bot.add_cog(RecognizeURL(bot))
