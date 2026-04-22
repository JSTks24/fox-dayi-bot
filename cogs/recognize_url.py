import discord
from discord.ext import commands
from discord import app_commands
import os
import asyncio
import json
import tempfile
from datetime import datetime
from threading import Lock
from urllib.parse import urlparse
from cogs.utils import CooldownManager, safe_defer, encode_image_to_base64, compress_image
from paths import (
    API_TABLE_BAD_FILE,
    API_TABLE_GOOD_FILE,
    API_TABLE_HISTORY_FILE,
    API_TABLE_PROMPT_FILE,
    APP_TEMP_DIR,
)

API_TABLE_PROMPT_PATH = os.fspath(API_TABLE_PROMPT_FILE)
API_TABLE_GOOD_PATH = os.fspath(API_TABLE_GOOD_FILE)
API_TABLE_BAD_PATH = os.fspath(API_TABLE_BAD_FILE)
API_TABLE_HISTORY_PATH = os.fspath(API_TABLE_HISTORY_FILE)
APP_TEMP_PATH = os.fspath(APP_TEMP_DIR)

# --- Cog 主体 ---

class RecognizeURL(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.cooldowns = CooldownManager(30)
        self._json_write_lock = Lock()
        
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
        temp_path = None
        try:
            directory = os.path.dirname(file_path) or '.'
            os.makedirs(directory, exist_ok=True)

            with self._json_write_lock:
                with tempfile.NamedTemporaryFile(
                    'w',
                    encoding='utf-8',
                    dir=directory,
                    delete=False,
                    suffix='.tmp'
                ) as f:
                    json.dump(data, f, ensure_ascii=False, indent=4)
                    temp_path = f.name

                os.replace(temp_path, file_path)
            return True
        except Exception as e:
            print(f"❌ 保存JSON文件失败 {file_path}: {e}")
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            return False
    
    def _build_prompt(self) -> str:
        """读取精简版提示词（仅提取URL，不包含列表）"""
        try:
            with open(API_TABLE_PROMPT_PATH, encoding='utf-8') as f:
                return f.read().strip()
        except Exception as e:
            print(f"❌ 构建提示词失败: {e}")
            return "Extract all URLs, IP addresses, and domain names visible in this screenshot. Output JSON: {\"urls\": [\"url1\", \"url2\"]}"

    def _parse_llm_urls(self, llm_response: str) -> list[str]:
        """从 LLM 响应中解析 URL 列表。

        尝试解析 JSON，失败则用正则回退提取。
        """
        import re

        text = llm_response.strip()

        # 尝试解析 JSON
        # 处理被 markdown 代码块包裹的情况
        json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
        if json_match:
            text = json_match.group(1)

        try:
            data = json.loads(text)
            if isinstance(data, dict) and 'urls' in data:
                return [u for u in data['urls'] if isinstance(u, str) and u.strip()]
        except (json.JSONDecodeError, TypeError):
            pass

        # 回退：用正则提取URL-like字符串
        urls = re.findall(
            r'(?:https?://)?(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}(?::\d+)?(?:/[^\s\'")\]},]*)?'
            r'|\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(?::\d+)?(?:/[^\s\'")\]},]*)?',
            llm_response,
        )
        return urls
    
    def _normalize_url(self, url: str) -> tuple[str, str]:
        """标准化URL格式，返回 (domain, path)。

        domain 包含主机名和非默认端口，path 为去掉尾部斜杠的路径部分。
        向后兼容：str(_normalize_url(url)) 不再有效，调用方需适配。
        """
        raw_url = (url or '').strip()
        if not raw_url:
            return ('', '')

        parsed = urlparse(raw_url if '://' in raw_url else f'//{raw_url}')
        hostname = (parsed.hostname or '').lower()

        if not hostname:
            fallback = parsed.path.strip().rstrip('/')
            return (fallback.lower(), '')

        domain = hostname
        try:
            port = parsed.port
        except ValueError:
            port = None
        default_ports = {'http': 80, 'https': 443}
        if port and port != default_ports.get(parsed.scheme):
            domain += f':{port}'

        path = (parsed.path or '').rstrip('/')
        if path == '/':
            path = ''

        return (domain, path)

    def _match_url(self, url: str) -> tuple[str, dict | None]:
        """分层匹配URL，返回 (status, entry_data_or_None)。

        三层匹配优先级：
        1. 精确匹配：domain + path 完整匹配
        2. 纯域名匹配：只匹配 domain（去掉 path）
        3. 后缀匹配：用户 URL 的域名 endswith("." + listed_domain)

        status: "good" / "bad" / "unknown"
        entry_data: {"name": str, "description": str, "matched_key": str} 或 None
        """
        good_data = self._load_json(API_TABLE_GOOD_PATH)
        bad_data = self._load_json(API_TABLE_BAD_PATH)

        good_list = good_data.get('good', {})
        bad_list = bad_data.get('bad', {})

        domain, path = self._normalize_url(url)
        if not domain:
            return ('unknown', None)

        full_key = domain + path if path else domain

        def _make_entry(info: list, key: str) -> dict:
            return {
                'name': info[0] if len(info) > 0 else '',
                'description': info[1] if len(info) > 1 else '',
                'matched_key': key,
            }

        # --- 第 1 层：精确匹配 (domain + path) ---
        if full_key in bad_list:
            return ('bad', _make_entry(bad_list[full_key], full_key))
        if full_key in good_list:
            return ('good', _make_entry(good_list[full_key], full_key))

        # --- 第 2 层：纯域名匹配 (仅 domain，忽略 path) ---
        if path:
            if domain in bad_list:
                return ('bad', _make_entry(bad_list[domain], domain))
            if domain in good_list:
                return ('good', _make_entry(good_list[domain], domain))

        # --- 第 3 层：后缀匹配 (子域名场景) ---
        for key, info in bad_list.items():
            # 去掉 key 中可能残留的 path 部分，只取域名
            key_domain = key.split('/')[0]
            if domain.endswith('.' + key_domain):
                return ('bad', _make_entry(info, key))
        for key, info in good_list.items():
            key_domain = key.split('/')[0]
            if domain.endswith('.' + key_domain):
                return ('good', _make_entry(info, key))

        return ('unknown', None)
    
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
            history_file = API_TABLE_HISTORY_PATH
            
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
            thread_ids_raw = os.getenv('QUICK_PUNISH_LOG_THREAD')
            if not thread_ids_raw:
                print("⚠️ 未配置QUICK_PUNISH_LOG_THREAD，跳过日志记录")
                return

            # 支持逗号分隔的多个子区ID，取第一个有效的
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
            
            # 构建日志消息
            log_message = (
                f"**URL合规性检查日志**\n"
                f"消息作者: {message.author.mention} ({message.author.id})\n"
                f"消息链接: [跳转]({message.jump_url})\n"
                f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            )
            
            # 发送日志消息和图片附件
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
        """处理单张图片：保存→压缩→LLM提取URL→本地匹配。

        返回 [(raw_url, normalized, status, entry), ...] 或空列表。
        """
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
            system_prompt = self._build_prompt()
            base64_image = encode_image_to_base64(compressed_path)

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": base64_image}}
                ]}
            ]

            client = self.bot.openai_client
            model = os.getenv("URL_CHECK_MODEL", os.getenv("OPENAI_MODEL"))

            response = await asyncio.wait_for(
                client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.1,
                    max_tokens=300
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

            extracted_urls = self._parse_llm_urls(ai_response)
            results = []
            for raw_url in extracted_urls:
                status, entry = self._match_url(raw_url)
                domain, path = self._normalize_url(raw_url)
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
        """
        APP命令：查成分
        检查消息中图片的URL合规性（支持多图，最多3张）
        """
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

        # 限制最多3张
        if len(image_attachments) > 3:
            image_attachments = image_attachments[:3]

        # 记录日志（第一张图）
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
            # 逐张处理图片，合并结果
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

            # 计算最严重状态
            status_priority = {'good': 0, 'unknown': 1, 'bad': 2}
            worst_status = 'good'
            for _, _, status, _ in all_results:
                if status_priority.get(status, 1) > status_priority.get(worst_status, 0):
                    worst_status = status

            # 构建 Embed
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
        domain, path = self._normalize_url(url)
        normalized_url = domain + path if path else domain

        operation = 操作.value
        
        try:
            if operation == 'delete':
                # 删除操作：从两个文件中查找并删除
                good_data = self._load_json(API_TABLE_GOOD_PATH)
                bad_data = self._load_json(API_TABLE_BAD_PATH)
                
                deleted_from = []
                
                # 从good.json中删除
                if 'good' in good_data and normalized_url in good_data['good']:
                    del good_data['good'][normalized_url]
                    self._save_json(API_TABLE_GOOD_PATH, good_data)
                    deleted_from.append('好API列表')
                
                # 从bad.json中删除
                if 'bad' in bad_data and normalized_url in bad_data['bad']:
                    del bad_data['bad'][normalized_url]
                    self._save_json(API_TABLE_BAD_PATH, bad_data)
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
                good_data = self._load_json(API_TABLE_GOOD_PATH)
                
                if 'good' not in good_data:
                    good_data['good'] = {}
                
                # 构建值列表 [名称, 描述]
                value = [名称 or "", 描述 or ""]
                good_data['good'][normalized_url] = value
                
                if self._save_json(API_TABLE_GOOD_PATH, good_data):
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
                bad_data = self._load_json(API_TABLE_BAD_PATH)
                
                if 'bad' not in bad_data:
                    bad_data['bad'] = {}
                
                # 构建值列表 [名称, 描述]
                value = [名称 or "", 描述 or ""]
                bad_data['bad'][normalized_url] = value
                
                if self._save_json(API_TABLE_BAD_PATH, bad_data):
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

        try:
            status, entry = self._match_url(url)
            domain, path = self._normalize_url(url)
            normalized_url = domain + path if path else domain

            if status == 'good':
                result = (
                    f"**状态:** ✅ 合规\n"
                    f"**URL:** `{normalized_url}`\n"
                    f"**命中规则:** `{entry['matched_key']}`\n"
                    f"**名称:** {entry['name'] or '(无)'}\n"
                    f"**描述:** {entry['description'] or '(无)'}"
                )
            elif status == 'bad':
                result = (
                    f"**状态:** 🚫 违规\n"
                    f"**URL:** `{normalized_url}`\n"
                    f"**命中规则:** `{entry['matched_key']}`\n"
                    f"**名称:** {entry['name'] or '(无)'}\n"
                    f"**描述:** {entry['description'] or '(无)'}"
                )
            else:
                result = (
                    f"**状态:** ❓ 未知\n"
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
