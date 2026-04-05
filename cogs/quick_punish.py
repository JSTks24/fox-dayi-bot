import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import os
import sqlite3
from datetime import datetime
import json
from typing import Optional, List, Dict, Tuple, Any
import aiofiles
from dotenv import load_dotenv
import io

# 加载环境变量
load_dotenv()

class QuickPunishModal(discord.ui.Modal):
    """快速处罚确认表单"""
    
    def __init__(self, target_message: discord.Message, cog):
        super().__init__(title=f"快速处罚 - {target_message.author.display_name}")
        self.target_message = target_message
        self.target_user = target_message.author
        self.cog = cog
        # 原因输入框（最多100字符）
        self.reason = discord.ui.TextInput(
            label="处罚原因",
            placeholder="请输入处罚原因（留空则使用默认值'付费违规第三方'）",
            required=False,
            max_length=100,
            style=discord.TextStyle.short
        )
        self.add_item(self.reason)
    
    # 已移除用户名/ID二次确认输入框及校验机制
    
    async def safe_defer(self, interaction: discord.Interaction):
        """安全的defer响应"""
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
    
    async def on_submit(self, interaction: discord.Interaction):
        """处理表单提交"""
        # 立即defer以避免超时
        await self.safe_defer(interaction)

        # 获取处罚原因
        reason = self.reason.value.strip() or "付费违规第三方"

        # 构建二次确认Embed（包含用户信息、原因、模板文件名占位）
        user = self.target_user
        embed = discord.Embed(
            title="二次确认 - 快速处罚",
            color=discord.Color.orange(),
            timestamp=datetime.now()
        )
        embed.set_author(
            name=f"{user.display_name} (@{user.name})",
            icon_url=user.avatar.url if getattr(user, "avatar", None) else None
        )
        if getattr(user, "avatar", None):
            embed.set_thumbnail(url=user.avatar.url)
        embed.add_field(name="显示名称", value=user.display_name, inline=True)
        embed.add_field(name="用户名", value=user.name, inline=True)
        embed.add_field(name="ID", value=str(user.id), inline=False)
        embed.add_field(name="处罚原因", value=reason, inline=False)
        embed.add_field(name="私信模板", value="未选择", inline=False)

        # 带有下拉选单与确认/取消按钮的视图
        view = QuickPunishConfirmView(
            cog=self.cog,
            target_message=self.target_message,
            target_user=self.target_user,
            reason=reason
        )

        # 在当前频道以临时消息发送二次确认
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
    
    async def on_error(self, interaction: discord.Interaction, error: Exception):
        """处理错误"""
        print(f"QuickPunishModal错误: {error}")
        try:
            await self.safe_defer(interaction)
            await interaction.followup.send(
                f"❌ 发生错误：{str(error)}", 
                ephemeral=True
            )
        except:
            pass


class QuickPunishConfirmView(discord.ui.View):
    """二次确认视图：包含模板选择下拉选单与确认/取消按钮"""
    def __init__(self, cog, target_message: discord.Message, target_user: discord.User, reason: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.target_message = target_message
        self.target_user = target_user
        self.reason = reason
        self.selected_template_filename: Optional[str] = None
        # 添加下拉选单（动态读取xiaozuowen目录的txt文件）
        self.add_item(TemplateSelect(cog=self.cog))

    async def safe_defer(self, interaction: discord.Interaction):
        """安全的defer响应"""
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

    def _disable_all(self):
        for child in self.children:
            try:
                child.disabled = True
            except:
                pass

    @discord.ui.button(label="确认执行", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 黄金法则：先defer
        await self.safe_defer(interaction)

        # 未选择模板时，回退到默认模板 xiaozuowen/default.txt
        chosen_template = self.selected_template_filename or "default.txt"

        # 执行处罚
        success, message, punishment_history = await self.cog.execute_punishment(
            interaction=interaction,
            target_user=self.target_user,
            target_message=self.target_message,
            reason=self.reason,
            executor=interaction.user,
            dm_template_filename=chosen_template
        )

        # 更新消息（移除交互视图）
        if success:
            embed = discord.Embed(
                title="✅ 处罚执行成功",
                description=message,
                color=discord.Color.green(),
                timestamp=datetime.now()
            )
            if punishment_history:
                history_lines = []
                for record in punishment_history[:5]:  # 最多显示5条历史记录
                    try:
                        timestamp_dt = datetime.fromisoformat(record['timestamp'])
                        time_str = timestamp_dt.strftime('%Y-%m-%d %H:%M')
                    except:
                        time_str = record['timestamp'][:16]

                    status_emoji = {
                        'executed': '✅',
                        'failed': '❌',
                        'revoked': '↩️'
                    }.get(record['status'], '❓')

                    source_tag = f"[{record.get('source_type', 'local')}]"

                    history_lines.append(
                        f"{status_emoji} {source_tag} **第{record['punish_count']}次** - {time_str}\n"
                        f"   原因: {record['reason'][:30]}{'...' if len(record['reason']) > 30 else ''}\n"
                        f"   执行者: {record['executor_name']}"
                    )

                embed.add_field(
                    name=f"📋 该用户的处罚历史（共{len(punishment_history)}条）",
                    value="\n".join(history_lines) if history_lines else "无历史记录",
                    inline=False
                )

            embed.set_footer(text=f"执行者: {interaction.user.name}")
            self._disable_all()
            await interaction.edit_original_response(embed=embed, view=None)
        else:
            self._disable_all()
            is_duplicate_prevented = self.cog._is_duplicate_punishment_message(message)
            embed = discord.Embed(
                title="⚠️ 本次未重复执行处罚" if is_duplicate_prevented else "❌ 处罚执行失败",
                description=message,
                color=discord.Color.orange() if is_duplicate_prevented else discord.Color.red(),
                timestamp=datetime.now()
            )
            embed.set_footer(text=f"操作人: {interaction.user.name}")
            await interaction.edit_original_response(embed=embed, view=None)

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 黄金法则：先defer
        await self.safe_defer(interaction)
        self._disable_all()
        embed = discord.Embed(
            title="操作已取消",
            description="本次快速处罚未执行。",
            color=discord.Color.greyple(),
            timestamp=datetime.now()
        )
        await interaction.edit_original_response(embed=embed, view=None)


class RevokeConfirmView(discord.ui.View):
    """撤销二次确认：当最近记录是sync时，确认是否回溯撤销local记录"""

    def __init__(self, cog, target_user_id: str, latest_record: Dict[str, Any], revoke_record: Dict[str, Any]):
        super().__init__(timeout=180)
        self.cog = cog
        self.target_user_id = target_user_id
        self.latest_record = latest_record
        self.revoke_record = revoke_record

    async def safe_defer(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

    def _disable_all(self):
        for child in self.children:
            try:
                child.disabled = True
            except Exception:
                pass

    @discord.ui.button(label="确认继续撤销", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.safe_defer(interaction)
        self._disable_all()

        success, message = await self.cog._execute_revoke_record(
            interaction=interaction,
            user_id=self.target_user_id,
            record=self.revoke_record
        )

        result_embed = discord.Embed(
            title="✅ 撤销完成" if success else "❌ 撤销失败",
            description=message,
            color=discord.Color.green() if success else discord.Color.red(),
            timestamp=datetime.now()
        )
        await interaction.edit_original_response(embed=result_embed, view=None)

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.safe_defer(interaction)
        self._disable_all()
        embed = discord.Embed(
            title="操作已取消",
            description="本次撤销未执行。",
            color=discord.Color.greyple(),
            timestamp=datetime.now()
        )
        await interaction.edit_original_response(embed=embed, view=None)

    async def on_timeout(self):
        self._disable_all()


class TemplateSelect(discord.ui.Select):
    """下拉选单：选择要发送的私信模板（文件名）"""
    def __init__(self, cog):
        self.cog = cog
        options = []
        try:
            for fn in sorted(self.cog.dm_templates.keys()):
                # 排除默认模板，仅在未选择时使用
                if fn.lower() == "default.txt":
                    continue
                options.append(discord.SelectOption(label=fn, value=fn))
        except Exception as e:
            print(f"构建模板选项失败: {e}")
        if not options:
            options = [discord.SelectOption(label="无可用模板", value="__none__", description="xiaozuowen目录下未找到txt文件")]
        super().__init__(placeholder="要发送的私信模板（不选默认为第三方API）", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        # 黄金法则：先defer
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        chosen = self.values[0]
        view: QuickPunishConfirmView = self.view
        if chosen == "__none__":
            view.selected_template_filename = None
        else:
            view.selected_template_filename = chosen

        # 更新确认Embed，显示已选择的模板文件名
        user = view.target_user
        embed = discord.Embed(
            title="二次确认 - 快速处罚",
            color=discord.Color.orange(),
            timestamp=datetime.now()
        )
        embed.set_author(
            name=f"{user.display_name} (@{user.name})",
            icon_url=user.avatar.url if getattr(user, "avatar", None) else None
        )
        if getattr(user, "avatar", None):
            embed.set_thumbnail(url=user.avatar.url)
        embed.add_field(name="显示名称", value=user.display_name, inline=True)
        embed.add_field(name="用户名", value=user.name, inline=True)
        embed.add_field(name="ID", value=str(user.id), inline=False)
        embed.add_field(name="处罚原因", value=view.reason or "付费违规第三方", inline=False)
        embed.add_field(name="私信模板", value=view.selected_template_filename or "默认（第三方API）", inline=False)

        await interaction.edit_original_response(embed=embed, view=view)


class QuickPunishCog(commands.Cog):
    """快速处罚功能Cog"""
    
    def __init__(self, bot):
        self.bot = bot
        self._punish_locks: Dict[int, asyncio.Lock] = {}

        # 从环境变量加载配置
        self.enabled = os.getenv("QUICK_PUNISH_ENABLED", "false").lower() == "true"
        self.sync_config_path = os.path.join("cogs", "config", "quick_punish_sync.json")
        self.allowed_roles = self._parse_role_ids(os.getenv("QUICK_PUNISH_ROLES", ""))
        self.remove_roles = self._parse_role_ids(os.getenv("QUICK_PUNISH_REMOVE_ROLES", ""))
        self.log_channel_id = self._parse_channel_id(os.getenv("QUICK_PUNISH_LOG_CHANNEL"))
        self.log_thread_id = self._parse_channel_id(os.getenv("QUICK_PUNISH_LOG_THREAD"))
        self.interface_channel_id = self._parse_channel_id(os.getenv("QUICK_PUNISH_INTERFACE_CHANNEL"))
        self.appeal_channel_id = self._parse_channel_id(os.getenv("QUICK_PUNISH_APPEAL_CHANNEL"))

        # 双服同步配置（JSON优先，env作为兼容fallback）
        self.sync_config = self._load_sync_config()

        # 数据库初始化与迁移
        self.init_database()

        # 加载xiaozuowen目录中的txt模板（不硬编码文件名）
        self.dm_templates: Dict[str, str] = {}
        self._load_dm_templates()

    
    def _parse_role_ids(self, role_str: str) -> List[int]:
        """解析身份组ID字符串"""
        if not role_str:
            return []
        try:
            return [int(role_id.strip()) for role_id in role_str.split(",") if role_id.strip()]
        except ValueError:
            print(f"警告：无法解析身份组ID: {role_str}")
            return []
    
    def _parse_channel_id(self, channel_str: str) -> Optional[int]:
        """解析频道ID字符串"""
        if not channel_str:
            return None
        try:
            return int(channel_str.strip())
        except ValueError:
            print(f"警告：无法解析频道ID: {channel_str}")
            return None

    def _load_sync_config(self) -> Dict[str, Any]:
        """加载并校验双服同步配置"""
        default_config: Dict[str, Any] = {
            "version": 1,
            "sync_guild_ids": [],
            "guilds": {},
            "policy": {"mode": "best_effort"}
        }

        if not os.path.exists(self.sync_config_path):
            print(f"警告：未找到同步配置文件 {self.sync_config_path}，将回退到env配置")
            return default_config

        try:
            with open(self.sync_config_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception as e:
            print(f"警告：读取同步配置失败: {e}")
            return default_config

        if not isinstance(raw, dict):
            print("警告：quick_punish_sync.json 顶层必须是对象")
            return default_config

        sync_ids: List[str] = []
        for gid in raw.get("sync_guild_ids", []):
            try:
                sync_ids.append(str(int(str(gid).strip())))
            except Exception:
                print(f"警告：sync_guild_ids 中存在无效guild id: {gid}")

        guild_cfg_raw = raw.get("guilds", {}) if isinstance(raw.get("guilds", {}), dict) else {}
        guilds: Dict[str, Dict[str, List[int]]] = {}
        for gid, cfg in guild_cfg_raw.items():
            gid_str = str(gid).strip()
            if not gid_str:
                continue
            if not isinstance(cfg, dict):
                cfg = {}
            allowed_raw = cfg.get("allowed_roles", [])
            remove_raw = cfg.get("punish_remove_roles", [])
            guilds[gid_str] = {
                "allowed_roles": [int(x) for x in allowed_raw if str(x).strip().isdigit()] if isinstance(allowed_raw, list) else [],
                "punish_remove_roles": [int(x) for x in remove_raw if str(x).strip().isdigit()] if isinstance(remove_raw, list) else []
            }

        if not sync_ids:
            print("警告：quick_punish_sync.json 的 sync_guild_ids 为空，将仅处理触发服务器")

        for gid in sync_ids:
            if gid not in guilds:
                guilds[gid] = {"allowed_roles": [], "punish_remove_roles": []}
                print(f"警告：同步服 {gid} 未配置 guilds 块，已按空配置处理")

        policy = raw.get("policy", {}) if isinstance(raw.get("policy", {}), dict) else {}
        mode = str(policy.get("mode", "best_effort")).strip().lower() or "best_effort"
        if mode != "best_effort":
            print(f"警告：当前仅支持 best_effort，收到 {mode}，将回退为 best_effort")
            mode = "best_effort"

        return {
            "version": int(raw.get("version", 1)) if str(raw.get("version", "1")).isdigit() else 1,
            "sync_guild_ids": sync_ids,
            "guilds": guilds,
            "policy": {"mode": mode}
        }

    def _get_sync_guild_ids(self, trigger_guild_id: Optional[int] = None) -> List[str]:
        ids = list(self.sync_config.get("sync_guild_ids", []))
        if trigger_guild_id is not None:
            gid = str(trigger_guild_id)
            if gid not in ids:
                ids.append(gid)
        if not ids and trigger_guild_id is not None:
            return [str(trigger_guild_id)]
        return ids

    def _get_guild_sync_config(self, guild_id: int) -> Dict[str, List[int]]:
        cfg = self.sync_config.get("guilds", {}).get(str(guild_id), {})
        if not isinstance(cfg, dict):
            cfg = {}
        return {"allowed_roles": cfg.get("allowed_roles", []), "punish_remove_roles": cfg.get("punish_remove_roles", [])}

    def _get_punish_lock(self, user_id: int) -> asyncio.Lock:
        lock = self._punish_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._punish_locks[user_id] = lock
        return lock

    def _release_punish_lock(self, user_id: int, lock: asyncio.Lock):
        if lock.locked():
            lock.release()
        if self._punish_locks.get(user_id) is lock and not lock.locked():
            self._punish_locks.pop(user_id, None)

    def _is_already_processed_result(self, result: Optional[Dict[str, Any]]) -> bool:
        already_processed_codes = {"no_removable_roles", "removal_conflict"}
        return bool(result) and result.get("code") in already_processed_codes

    def _all_sync_results_already_processed(self, results: List[Dict[str, Any]]) -> bool:
        return bool(results) and all(self._is_already_processed_result(item) for item in results)

    def _is_duplicate_punishment_message(self, message: str) -> bool:
        duplicate_markers = (
            "正在被其他管理员处理",
            "先一步处罚",
            "不会重复执行",
            "已不再拥有可移除的处罚身份组"
        )
        return any(marker in message for marker in duplicate_markers)
    
    def _load_dm_templates(self):
        """扫描xiaozuowen目录，加载所有txt模板文件名 -> 路径"""
        self.dm_templates = {}
        base_dir = 'xiaozuowen'
        try:
            for fn in os.listdir(base_dir):
                if fn.lower().endswith('.txt'):
                    self.dm_templates[fn] = os.path.join(base_dir, fn)
            print(f"已加载DM模板: {list(self.dm_templates.keys())}")
        except Exception as e:
            print(f"加载DM模板失败: {e}")

    def init_database(self):
        """初始化数据库并执行幂等迁移"""
        conn = sqlite3.connect('quick_punish.db')
        cursor = conn.cursor()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS quick_punish_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                user_name TEXT NOT NULL,
                punish_count INTEGER DEFAULT 1,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                original_message_id TEXT,
                original_message_link TEXT,
                channel_id TEXT,
                channel_name TEXT,
                executor_id TEXT NOT NULL,
                executor_name TEXT NOT NULL,
                reason TEXT,
                removed_roles TEXT,
                status TEXT DEFAULT 'executed',
                source_type TEXT DEFAULT 'local',
                removed_roles_by_guild TEXT DEFAULT '{}',
                source_guild_id TEXT
            )
        ''')

        cursor.execute("PRAGMA table_info(quick_punish_records)")
        cols = {row[1] for row in cursor.fetchall()}
        if "source_type" not in cols:
            cursor.execute("ALTER TABLE quick_punish_records ADD COLUMN source_type TEXT DEFAULT 'local'")
        if "removed_roles_by_guild" not in cols:
            cursor.execute("ALTER TABLE quick_punish_records ADD COLUMN removed_roles_by_guild TEXT DEFAULT '{}'")
        if "source_guild_id" not in cols:
            cursor.execute("ALTER TABLE quick_punish_records ADD COLUMN source_guild_id TEXT")

        cursor.execute("""
            UPDATE quick_punish_records
            SET source_type = 'local'
            WHERE source_type IS NULL OR TRIM(source_type) = ''
        """)
        cursor.execute("""
            UPDATE quick_punish_records
            SET source_type = 'sync'
            WHERE status = 'executed'
              AND (original_message_link IS NULL OR TRIM(original_message_link) = '')
              AND (removed_roles IS NULL OR TRIM(removed_roles) = '' OR TRIM(removed_roles) = '[]')
        """)
        cursor.execute("""
            UPDATE quick_punish_records
            SET removed_roles_by_guild = '{}'
            WHERE removed_roles_by_guild IS NULL OR TRIM(removed_roles_by_guild) = ''
        """)

        conn.commit()
        conn.close()

    def has_permission(self, interaction: discord.Interaction) -> bool:
        """检查用户是否有快速处罚权限（仅校验触发服allowed_roles）"""
        if not self.enabled:
            return False

        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return False

        guild_cfg = self._get_guild_sync_config(interaction.guild.id)
        allowed_roles = guild_cfg.get("allowed_roles", []) or self.allowed_roles
        if not allowed_roles:
            return False

        user_roles = [role.id for role in interaction.user.roles]
        return any(role_id in user_roles for role_id in allowed_roles)

    def _parse_json_list(self, value: Any) -> List[int]:
        if isinstance(value, list):
            return [int(x) for x in value if str(x).strip().isdigit()]
        if value is None:
            return []
        try:
            loaded = json.loads(value) if isinstance(value, str) else value
            if isinstance(loaded, list):
                return [int(x) for x in loaded if str(x).strip().isdigit()]
        except Exception:
            pass
        return []

    def _parse_json_roles_by_guild(self, value: Any) -> Dict[str, List[int]]:
        if value is None:
            return {}
        try:
            loaded = json.loads(value) if isinstance(value, str) else value
        except Exception:
            loaded = {}

        if not isinstance(loaded, dict):
            return {}

        result: Dict[str, List[int]] = {}
        for gid, roles in loaded.items():
            gid_str = str(gid).strip()
            if not gid_str:
                continue
            result[gid_str] = self._parse_json_list(roles)
        return result

    def _compute_next_punish_count_with_cursor(self, cursor: sqlite3.Cursor, user_id: str) -> int:
        cursor.execute(
            "SELECT MAX(COALESCE(punish_count, 0)) FROM quick_punish_records WHERE user_id = ? AND status != 'failed'",
            (user_id,)
        )
        row = cursor.fetchone()
        last = 0
        if row and row[0] is not None:
            try:
                last = int(row[0])
            except Exception:
                last = 0
        return max(1, last + 1)

    async def get_punish_count(self, user_id: str) -> int:
        """获取用户被处罚次数（executed）"""
        conn = sqlite3.connect('quick_punish.db')
        cursor = conn.cursor()
        cursor.execute(
            "SELECT COUNT(*) FROM quick_punish_records WHERE user_id = ? AND status = 'executed'",
            (user_id,)
        )
        count = cursor.fetchone()[0]
        conn.close()
        return count

    async def get_user_punishment_history(self, user_id: str, limit: int = 5) -> List[Dict]:
        """获取用户的处罚历史记录"""
        conn = sqlite3.connect('quick_punish.db')
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, punish_count, timestamp, reason, executor_name, status, source_type
            FROM quick_punish_records
            WHERE user_id = ?
            ORDER BY timestamp DESC, id DESC
            LIMIT ?
        ''', (user_id, limit))

        records = []
        for row in cursor.fetchall():
            records.append({
                'id': row[0],
                'punish_count': row[1],
                'timestamp': row[2],
                'reason': row[3],
                'executor_name': row[4],
                'status': row[5],
                'source_type': row[6] or 'local'
            })
        conn.close()
        return records

    async def send_dm(self, user: discord.User, message_content: str) -> bool:
        """发送私信给用户"""
        try:
            await user.send(message_content)
            return True
        except discord.Forbidden:
            print(f"无法发送私信给用户 {user.name} ({user.id})")
            return False
        except Exception as e:
            print(f"发送私信时出错: {e}")
            return False
    
    async def remove_user_roles(self, member: discord.Member, roles_to_remove: List[int]) -> Tuple[List[int], bool]:
        """移除用户的身份组
        返回: (实际被移除的身份组ID列表, 是否成功移除了至少一个身份组)
        """
        removed_roles = []
        roles_to_remove_objs = []
        
        # 在移除前的最后一刻再次检查用户是否拥有这些身份组
        for role_id in roles_to_remove:
            role = member.guild.get_role(role_id)
            if role and role in member.roles:  # 最终检查
                roles_to_remove_objs.append(role)
                removed_roles.append(role_id)
        
        # 如果没有任何身份组需要移除，返回空列表和False
        if not roles_to_remove_objs:
            return [], False
        
        try:
            await member.remove_roles(*roles_to_remove_objs, reason="快速处罚")
            return removed_roles, True
        except Exception as e:
            print(f"移除身份组时出错: {e}")
            raise
    
    async def log_to_database_with_count(self, user: discord.User, message: discord.Message,
                                        executor: discord.User, reason: str, removed_roles: List[int],
                                        punish_count: int, status: str = "executed",
                                        source_type: str = "local",
                                        removed_roles_by_guild: Optional[Dict[str, List[int]]] = None,
                                        source_guild_id: Optional[str] = None) -> Tuple[int, int]:
        """记录处罚信息到数据库（使用事务确保原子性）"""
        conn = sqlite3.connect('quick_punish.db')
        conn.isolation_level = None  # 自动提交模式
        cursor = conn.cursor()

        try:
            cursor.execute("BEGIN TRANSACTION")

            if punish_count == 0:
                punish_count = self._compute_next_punish_count_with_cursor(cursor, str(user.id))

            message_link = None
            msg_id = None
            channel_id = None
            channel_name = None
            if message:
                msg_id = str(message.id)
                channel_id = str(message.channel.id)
                channel_name = getattr(message.channel, "name", None)
                if message.guild and message.channel:
                    message_link = f"https://discord.com/channels/{message.guild.id}/{message.channel.id}/{message.id}"

            rrbg = removed_roles_by_guild or {}
            cursor.execute('''
                INSERT INTO quick_punish_records
                (user_id, user_name, punish_count, timestamp, original_message_id, original_message_link,
                 channel_id, channel_name, executor_id, executor_name, reason, removed_roles, status,
                 source_type, removed_roles_by_guild, source_guild_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                str(user.id),
                user.name,
                punish_count,
                datetime.now().isoformat(),
                msg_id,
                message_link,
                channel_id,
                channel_name,
                str(executor.id),
                executor.name,
                reason,
                json.dumps(removed_roles),
                status,
                source_type,
                json.dumps(rrbg, ensure_ascii=False),
                source_guild_id
            ))

            record_id = cursor.lastrowid
            cursor.execute("COMMIT")
            return record_id, punish_count

        except Exception as e:
            cursor.execute("ROLLBACK")
            print(f"数据库事务错误: {e}")
            raise
        finally:
            conn.close()

    async def send_log_embed(self, channel: discord.abc.Messageable, user: discord.User,
                            executor: discord.User, reason: str, message_link: str,
                            removed_roles: List[int], record_id: int,
                            trigger_guild: Optional[discord.Guild] = None,
                            sync_results: Optional[List[Dict[str, Any]]] = None,
                            original_message: discord.Message = None):
        """发送日志Embed到指定频道，并转发原消息"""
        embed = discord.Embed(
            title="⚠️ 快速处罚执行",
            color=discord.Color.red(),
            timestamp=datetime.now()
        )

        embed.add_field(name="处罚对象", value=f"{user.mention} ({user.id})", inline=False)
        embed.add_field(name="执行者", value=f"{executor.mention}", inline=True)
        embed.add_field(name="原因", value=reason, inline=True)
        if message_link:
            embed.add_field(name="原消息", value=f"[跳转到消息]({message_link})", inline=False)

        if trigger_guild:
            embed.add_field(
                name="触发服务器",
                value=f"{trigger_guild.name} ({trigger_guild.id})",
                inline=False
            )

        if removed_roles:
            roles_str = ", ".join([f"<@&{role_id}>" for role_id in removed_roles])
            embed.add_field(name="触发服移除身份组", value=roles_str, inline=False)

        if sync_results:
            detail_lines = []
            for result in sync_results:
                gname = result.get("guild_name", "未知服务器")
                gid = result.get("guild_id", "-")
                if result.get("success"):
                    role_text = ", ".join([f"<@&{rid}>" for rid in result.get("removed_roles", [])]) or "无"
                    detail_lines.append(f"✅ {gname} ({gid})\n移除: {role_text}")
                else:
                    detail_lines.append(f"❌ {gname} ({gid})\n原因: {result.get('error', '未知错误')}")

            if detail_lines:
                embed.add_field(name="双服执行明细", value="\n\n".join(detail_lines)[:1024], inline=False)

        embed.set_footer(text=f"记录ID: {record_id}")

        try:
            await channel.send(embed=embed)

            if original_message:
                await self._forward_original_message(channel, original_message, user)
        except Exception as e:
            print(f"发送日志Embed时出错: {e}")

    async def _forward_original_message(self, channel: discord.TextChannel,
                                       message: discord.Message,
                                       punished_user: discord.User):
        """转发被处罚的原消息到日志频道"""
        try:
            # 检查消息是否仍然存在
            try:
                # 尝试重新获取消息，确保它仍然存在
                fresh_message = await message.channel.fetch_message(message.id)
            except (discord.NotFound, discord.HTTPException):
                # 消息已被删除
                fallback_embed = discord.Embed(
                    title="📝 原消息内容（已删除）",
                    description="*消息已被删除，无法获取内容*",
                    color=discord.Color.greyple()
                )
                fallback_embed.add_field(
                    name="消息信息",
                    value=f"作者: {punished_user.mention}\n"
                          f"频道: <#{message.channel.id}>\n"
                          f"消息ID: {message.id}",
                    inline=False
                )
                await channel.send(embed=fallback_embed)
                return
            
            # 构建转发的Embed
            forward_embed = discord.Embed(
                title="📝 被处罚的原消息",
                color=discord.Color.dark_grey(),
                timestamp=fresh_message.created_at
            )
            
            # 添加作者信息
            forward_embed.set_author(
                name=f"{fresh_message.author.display_name} (@{fresh_message.author.name})",
                icon_url=fresh_message.author.avatar.url if fresh_message.author.avatar else None
            )
            
            # 添加消息内容
            content = fresh_message.content[:4000] if fresh_message.content else "*无文本内容*"
            if len(fresh_message.content) > 4000:
                content += "\n...*内容过长已截断*"
            forward_embed.add_field(name="消息内容", value=content, inline=False)
            
            # 添加频道和时间信息
            forward_embed.add_field(
                name="位置",
                value=f"频道: <#{fresh_message.channel.id}>\n"
                      f"[跳转到原消息](https://discord.com/channels/{fresh_message.guild.id}/{fresh_message.channel.id}/{fresh_message.id})",
                inline=False
            )
            
            # 如果有附件，添加附件信息
            if fresh_message.attachments:
                attachments_info = []
                for att in fresh_message.attachments[:5]:  # 最多显示5个附件
                    attachments_info.append(f"• [{att.filename}]({att.url})")
                if len(fresh_message.attachments) > 5:
                    attachments_info.append(f"*...还有 {len(fresh_message.attachments) - 5} 个附件*")
                forward_embed.add_field(
                    name=f"附件 ({len(fresh_message.attachments)})",
                    value="\n".join(attachments_info),
                    inline=False
                )
            
            # 如果有嵌入（Embeds），添加说明
            if fresh_message.embeds:
                forward_embed.add_field(
                    name="嵌入内容",
                    value=f"*包含 {len(fresh_message.embeds)} 个嵌入内容*",
                    inline=False
                )
            
            # 如果有贴纸（Stickers），添加贴纸信息
            if fresh_message.stickers:
                stickers_info = ", ".join([sticker.name for sticker in fresh_message.stickers])
                forward_embed.add_field(
                    name="贴纸",
                    value=stickers_info,
                    inline=False
                )
            
            await channel.send(embed=forward_embed)
            
        except Exception as e:
            print(f"转发原消息时出错: {e}")
            # 发送错误信息
            error_embed = discord.Embed(
                title="⚠️ 无法转发原消息",
                description=f"转发消息时发生错误：{str(e)}",
                color=discord.Color.orange()
            )
            await channel.send(embed=error_embed)
    
    async def _resolve_member_in_guild(self, guild: discord.Guild, user_id: int,
                                       force_fetch: bool = False) -> Optional[discord.Member]:
        cached_member = guild.get_member(user_id)
        if cached_member and not force_fetch:
            return cached_member
        try:
            return await guild.fetch_member(user_id)
        except Exception:
            return cached_member

    async def _execute_role_removal_in_guild(self, guild_id: str,
                                             target_user_id: int,
                                             trigger_guild_id: Optional[int] = None,
                                             force_fetch: bool = False) -> Dict[str, Any]:
        result = {
            "guild_id": guild_id,
            "guild_name": guild_id,
            "success": False,
            "removed_roles": [],
            "error": None,
            "code": None
        }

        if not str(guild_id).isdigit():
            result["error"] = "服务器配置中的 guild id 无效"
            result["code"] = "invalid_guild_id"
            return result

        guild = self.bot.get_guild(int(guild_id))
        if not guild:
            result["error"] = "机器人不在该服务器，或暂时无法获取服务器对象"
            result["code"] = "guild_unavailable"
            return result

        result["guild_name"] = guild.name
        member = await self._resolve_member_in_guild(guild, target_user_id, force_fetch=force_fetch)
        if not member:
            result["error"] = "用户当前不在该服务器，无法同步移除身份组"
            result["code"] = "member_not_found"
            return result

        guild_cfg = self._get_guild_sync_config(guild.id)
        configured_remove_roles = guild_cfg.get("punish_remove_roles", [])
        if not configured_remove_roles and trigger_guild_id is not None and str(guild.id) == str(trigger_guild_id):
            configured_remove_roles = self.remove_roles

        if not configured_remove_roles:
            result["error"] = "该服务器未配置可移除的处罚身份组"
            result["code"] = "no_configured_roles"
            return result

        user_role_ids = [role.id for role in member.roles]
        roles_to_remove = [role_id for role_id in configured_remove_roles if role_id in user_role_ids]
        if not roles_to_remove:
            result["error"] = "用户当前已不再拥有可移除的处罚身份组，可能已被其他管理员先一步处理"
            result["code"] = "no_removable_roles"
            return result

        try:
            removed_roles, removal_success = await self.remove_user_roles(member, roles_to_remove)
            if not removal_success:
                result["error"] = "执行时发现用户已不再拥有需要移除的身份组，可能已被其他管理员先一步处罚"
                result["code"] = "removal_conflict"
                return result
            result["success"] = True
            result["removed_roles"] = removed_roles
            result["code"] = "success"
            return result
        except Exception as e:
            result["error"] = f"移除身份组时发生错误：{str(e)}"
            result["code"] = "removal_error"
            return result

    def _format_sync_results(self, results: List[Dict[str, Any]]) -> str:
        lines = []
        for item in results:
            if item.get("success"):
                lines.append(f"✅ {item.get('guild_name')}：移除{len(item.get('removed_roles', []))}个身份组")
            else:
                lines.append(f"❌ {item.get('guild_name')}：{item.get('error', '未知错误')}")
        return "\n".join(lines) if lines else "无执行结果"

    async def execute_punishment(self, interaction: discord.Interaction,
                                target_user: discord.User,
                                target_message: discord.Message,
                                reason: str,
                                executor: discord.User,
                                dm_template_filename: Optional[str] = None) -> tuple[bool, str, List[Dict]]:
        """执行处罚的主要逻辑，返回(成功状态, 消息, 处罚历史)"""
        trigger_guild = interaction.guild
        if trigger_guild is None:
            return False, "无法识别触发服务器", []

        punish_lock = self._get_punish_lock(target_user.id)
        if punish_lock.locked():
            return False, (
                f"用户 {target_user.mention} 正在被其他管理员处理。\n"
                "为避免重复处罚，本次操作已被拦截；若对方已完成处罚，请稍后再查看记录。"
            ), []

        await punish_lock.acquire()
        try:
            sync_guild_ids = self._get_sync_guild_ids(trigger_guild.id)
            sync_results: List[Dict[str, Any]] = []

            for guild_id in sync_guild_ids:
                sync_results.append(
                    await self._execute_role_removal_in_guild(
                        guild_id,
                        target_user.id,
                        trigger_guild_id=trigger_guild.id,
                        force_fetch=True
                    )
                )

            success_results = [r for r in sync_results if r.get("success")]
            trigger_result = next(
                (r for r in sync_results if str(r.get("guild_id")) == str(trigger_guild.id)),
                None
            )
            if not success_results:
                if self._is_already_processed_result(trigger_result) or self._all_sync_results_already_processed(sync_results):
                    return False, (
                        f"用户 {target_user.mention} 当前已不再拥有可移除的处罚身份组，"
                        "很可能已被其他管理员先一步处罚，本次不会重复执行。\n"
                        f"{self._format_sync_results(sync_results)}"
                    ), []
                return False, (
                    "本次处罚未执行：未能在任何目标服务器完成身份组移除。\n"
                    f"{self._format_sync_results(sync_results)}"
                ), []

            removed_roles_by_guild = {
                str(r["guild_id"]): r.get("removed_roles", [])
                for r in success_results if r.get("removed_roles")
            }
            trigger_removed_roles = removed_roles_by_guild.get(str(trigger_guild.id), [])

            record_id, punish_count = await self.log_to_database_with_count(
                user=target_user,
                message=target_message,
                executor=executor,
                reason=reason,
                removed_roles=trigger_removed_roles,
                punish_count=0,
                status="executed",
                source_type="local",
                removed_roles_by_guild=removed_roles_by_guild,
                source_guild_id=str(trigger_guild.id)
            )

            dm_content = await self._build_dm_content(
                target_message=target_message,
                reason=reason,
                executor=executor,
                punish_count=punish_count,
                dm_template_filename=dm_template_filename,
                removal_results=success_results
            )
            dm_sent = await self.send_dm(target_user, dm_content)

            await self._send_channel_notification(
                channel=target_message.channel,
                user=target_user,
                executor=executor,
                reason=reason,
                removed_roles=trigger_removed_roles
            )

            try:
                async with aiofiles.open('xiaozuowen/public.txt', 'r', encoding='utf-8') as f:
                    public_content = await f.read()
                await target_message.channel.send(public_content.strip())
            except Exception as e:
                print(f"发送public.txt内容失败: {e}")

            log_destination = await self._get_log_destination()
            if log_destination:
                message_link = f"https://discord.com/channels/{trigger_guild.id}/{target_message.channel.id}/{target_message.id}"
                await self.send_log_embed(
                    channel=log_destination,
                    user=target_user,
                    executor=executor,
                    reason=reason,
                    message_link=message_link,
                    removed_roles=trigger_removed_roles,
                    record_id=record_id,
                    trigger_guild=trigger_guild,
                    sync_results=sync_results,
                    original_message=target_message
                )

            if self.interface_channel_id:
                try:
                    interface_channel = self.bot.get_channel(self.interface_channel_id)
                    if interface_channel:
                        await interface_channel.send(f'{{"punish": {target_user.id}}}')
                    else:
                        print("警告：未找到 QUICK_PUNISH_INTERFACE_CHANNEL，已跳过接口发送")
                except Exception as e:
                    print(f"警告：接口频道发送失败（不影响主流程）: {e}")

            punishment_history = await self.get_user_punishment_history(str(target_user.id))
            success_msg = f"用户 {target_user.mention} 已被处罚（第{punish_count}次，全局）\n{self._format_sync_results(sync_results)}"
            if not dm_sent:
                success_msg += "\n⚠️ 注意：私信发送失败（用户可能关闭了私信）"

            return True, success_msg, punishment_history

        except Exception as e:
            print(f"执行处罚时出错: {e}")
            try:
                await self.log_to_database_with_count(
                    user=target_user,
                    message=target_message,
                    executor=executor,
                    reason=reason,
                    removed_roles=[],
                    punish_count=0,
                    status="failed",
                    source_type="local",
                    removed_roles_by_guild={},
                    source_guild_id=str(trigger_guild.id)
                )
            except Exception:
                pass
            return False, f"执行处罚时出错：{str(e)}", []
        finally:
            self._release_punish_lock(target_user.id, punish_lock)
    
    async def _build_dm_content(self, target_message: discord.Message,
                               reason: str, executor: discord.User,
                               punish_count: int,
                               dm_template_filename: Optional[str] = None,
                               removal_results: Optional[List[Dict[str, Any]]] = None) -> str:
        """构建私信内容"""
        # 读取3rd.txt文件内容
        third_content = "请重新完成新人验证答题。"  # 默认内容

        # 构建“服务器 + 被移除身份组”说明
        server_role_parts: List[str] = []
        for item in (removal_results or []):
            if not item.get("success"):
                continue

            guild_id = str(item.get("guild_id", "")).strip()
            guild_name = item.get("guild_name", "未知服务器")
            removed_role_ids = self._parse_json_list(item.get("removed_roles", []))

            role_names: List[str] = []
            guild_obj = self.bot.get_guild(int(guild_id)) if guild_id.isdigit() else None
            for role_id in removed_role_ids:
                role_name = None
                if guild_obj:
                    role_obj = guild_obj.get_role(role_id)
                    if role_obj:
                        role_name = f"@{role_obj.name}"
                if not role_name:
                    role_name = f"ID:{role_id}"
                role_names.append(role_name)

            roles_text = "、".join(role_names) if role_names else "无可展示身份组"
            server_role_parts.append(f"{guild_name}（{roles_text}）")

        server_role_text = "，".join(server_role_parts) if server_role_parts else "未记录到具体服务器与身份组"
        confirm_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # 使用选择的模板文件（来自xiaozuowen目录）
        try:
            if dm_template_filename and dm_template_filename in getattr(self, "dm_templates", {}):
                template_path = self.dm_templates[dm_template_filename]
                async with aiofiles.open(template_path, 'r', encoding='utf-8') as f:
                    third_content = await f.read()
        except Exception as e:
            print(f"读取模板文件失败: {e}")
        
        # 构建完整私信
        dm_parts = [
            "# === 答题处罚通知 ===\n",
            f"你在以下服务器的一些身份组已被移除：{server_role_text}。原因：{reason}\n",
            f"此处罚在{confirm_time}由{executor.name}确认。\n",
            third_content.strip(),
            "\n请仔细阅读以上内容和社区规则，重新完成新人验证答题： https://discord.com/channels/1134557553011998840/1338036166221365339 "
        ]
        
        # 添加申诉信息
        if self.appeal_channel_id:
            dm_parts.append("\n## ⚠️ 请勿回复此消息，机器人不会读取或转发私信。\n\n在**重新阅读上方内容和[社区规则](https://discord.com/channels/1134557553011998840/1401896293181292715/1428266657045938206)之后**，如果你认为此处罚存在**事实性错误**（例如：处罚对象搞错了、你使用的API/云酒馆被误认为违规第三方提供等），请开 ticket 向管理组申诉。\n\n⛔ **以下无效申诉将不予回复：**\n - 不读完上方说明就开ticket，只反问「我做了什么」，「凭什么罚我」的\n- 申诉内容为「不理解相关规则」或「不知道相关规则」，主张无知者无罪的\n- 觉得规则不合理，想来找管理辩论，更改规则的\n")
        
        return "\n".join(dm_parts)
    
    async def _send_channel_notification(self, channel: discord.TextChannel,
                                        user: discord.User, executor: discord.User,
                                        reason: str, removed_roles: List[int]):
        """在原频道发送处罚通知"""
        embed = discord.Embed(
            title="⚠️ 快速处罚",
            color=discord.Color.orange(),
            timestamp=datetime.now()
        )
        
        embed.add_field(name="处罚对象", value=f"{user.mention}", inline=True)
        embed.add_field(name="执行者", value=f"{executor.mention}", inline=True)
        embed.add_field(name="原因", value=reason, inline=False)
        
        # 已移除：不再显示"已移除身份组"字段
        # if removed_roles:
        #     roles_str = ", ".join([f"<@&{role_id}>" for role_id in removed_roles])
        #     embed.add_field(name="已移除身份组", value=roles_str, inline=False)
        
        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"发送频道通知时出错: {e}")
    
    async def get_recent_punishments(self, count: int = 3, max_count: int = 1000) -> List[Dict]:
        """获取最近的处罚记录"""
        count = min(count, max_count)
        count = max(count, 1)

        conn = sqlite3.connect('quick_punish.db')
        cursor = conn.cursor()

        cursor.execute('''
            SELECT id, user_id, user_name, timestamp, channel_name,
                   executor_name, reason, removed_roles, status, source_type
            FROM quick_punish_records
            ORDER BY timestamp DESC, id DESC
            LIMIT ?
        ''', (count,))

        records = []
        for row in cursor.fetchall():
            records.append({
                'id': row[0],
                'user_id': row[1],
                'user_name': row[2],
                'timestamp': row[3],
                'channel_name': row[4],
                'executor_name': row[5],
                'reason': row[6],
                'removed_roles': self._parse_json_list(row[7]),
                'status': row[8],
                'source_type': row[9] or 'local'
            })

        conn.close()
        return records

    async def format_punishment_records(self, records: List[Dict], guild: discord.Guild) -> str:
        """格式化处罚记录为文本"""
        if not records:
            return "暂无处罚记录"

        lines = ["===== 快速处罚记录 =====\n"]

        for i, record in enumerate(records, 1):
            try:
                timestamp = datetime.fromisoformat(record['timestamp'])
                time_str = timestamp.strftime('%Y-%m-%d %H:%M:%S')
            except:
                time_str = record['timestamp']

            roles_str = "无"
            if record['removed_roles']:
                role_names = []
                for role_id in record['removed_roles']:
                    role = guild.get_role(role_id)
                    if role:
                        role_names.append(f"@{role.name}")
                    else:
                        role_names.append(f"ID:{role_id}")
                roles_str = ", ".join(role_names)

            lines.append(f"【记录 #{i}】")
            lines.append(f"记录ID: {record['id']}")
            lines.append(f"用户: {record['user_name']} (ID: {record['user_id']})")
            lines.append(f"时间: {time_str}")
            lines.append(f"频道: #{record['channel_name']}")
            lines.append(f"执行者: {record['executor_name']}")
            lines.append(f"来源: {record.get('source_type', 'local')}")
            lines.append(f"原因: {record['reason']}")
            lines.append(f"移除身份组: {roles_str}")
            lines.append(f"状态: {record['status']}")
            lines.append("-" * 50 + "\n")

        return "\n".join(lines)

    def _row_to_record(self, row: tuple) -> Dict[str, Any]:
        return {
            'id': row[0],
            'user_id': row[1],
            'user_name': row[2],
            'timestamp': row[3],
            'original_message_id': row[4],
            'original_message_link': row[5],
            'channel_id': row[6],
            'channel_name': row[7],
            'executor_id': row[8],
            'executor_name': row[9],
            'reason': row[10],
            'removed_roles': self._parse_json_list(row[11]),
            'status': row[12],
            'source_type': (row[13] or 'local') if len(row) > 13 else 'local',
            'removed_roles_by_guild': self._parse_json_roles_by_guild(row[14] if len(row) > 14 else {}),
            'source_guild_id': row[15] if len(row) > 15 else None
        }

    def _has_restore_basis(self, record: Dict[str, Any]) -> bool:
        by_guild = record.get('removed_roles_by_guild', {})
        if isinstance(by_guild, dict) and any(v for v in by_guild.values()):
            return True
        return bool(record.get('removed_roles'))

    async def get_last_punishment_for_user(self, user_id: str) -> Optional[Dict]:
        """获取用户最近一次 executed 处罚记录"""
        conn = sqlite3.connect('quick_punish.db')
        cursor = conn.cursor()

        cursor.execute('''
            SELECT id, user_id, user_name, timestamp, original_message_id,
                   original_message_link, channel_id, channel_name,
                   executor_id, executor_name, reason, removed_roles, status,
                   source_type, removed_roles_by_guild, source_guild_id
            FROM quick_punish_records
            WHERE user_id = ? AND status = 'executed'
            ORDER BY timestamp DESC, id DESC
            LIMIT 1
        ''', (user_id,))

        row = cursor.fetchone()
        conn.close()

        if not row:
            return None
        return self._row_to_record(row)

    async def get_last_revocable_local_record_for_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        """获取最近可撤销（有恢复依据）的 local executed 记录"""
        conn = sqlite3.connect('quick_punish.db')
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, user_id, user_name, timestamp, original_message_id,
                   original_message_link, channel_id, channel_name,
                   executor_id, executor_name, reason, removed_roles, status,
                   source_type, removed_roles_by_guild, source_guild_id
            FROM quick_punish_records
            WHERE user_id = ? AND status = 'executed' AND COALESCE(source_type, 'local') = 'local'
            ORDER BY timestamp DESC, id DESC
            LIMIT 30
        ''', (user_id,))
        rows = cursor.fetchall()
        conn.close()

        for row in rows:
            record = self._row_to_record(row)
            if self._has_restore_basis(record):
                return record
        return None

    async def revoke_punishment(self, record_id: int) -> bool:
        """撤销处罚记录（更新状态为revoked）"""
        conn = sqlite3.connect('quick_punish.db')
        cursor = conn.cursor()

        cursor.execute('''
            UPDATE quick_punish_records
            SET status = 'revoked'
            WHERE id = ? AND status = 'executed'
        ''', (record_id,))

        affected = cursor.rowcount
        conn.commit()
        conn.close()

        return affected > 0

    async def restore_user_roles(self, member: discord.Member, roles_to_restore: List[int]) -> Tuple[List[int], List[int]]:
        """恢复用户的身份组
        返回: (成功恢复的身份组ID列表, 失败的身份组ID列表)
        """
        restored_roles = []
        failed_roles = []
        roles_to_add = []
        
        for role_id in roles_to_restore:
            role = member.guild.get_role(role_id)
            if role:
                # 检查用户是否已有该身份组
                if role not in member.roles:
                    roles_to_add.append(role)
                    restored_roles.append(role_id)
                else:
                    # 用户已有该身份组，也算成功
                    restored_roles.append(role_id)
            else:
                # 身份组不存在
                failed_roles.append(role_id)
        
        if roles_to_add:
            try:
                await member.add_roles(*roles_to_add, reason="快速处罚撤销")
            except Exception as e:
                print(f"恢复身份组时出错: {e}")
                # 如果添加失败，将这些角色移到失败列表
                for role in roles_to_add:
                    restored_roles.remove(role.id)
                    failed_roles.append(role.id)
        
        return restored_roles, failed_roles
    
    async def _get_log_destination(self):
        """全局获取日志发送目标（优先子区，其次频道）"""
        log_destination = None

        if self.log_thread_id:
            log_destination = self.bot.get_channel(self.log_thread_id)
            if not log_destination:
                try:
                    log_destination = await self.bot.fetch_channel(self.log_thread_id)
                except Exception:
                    log_destination = None

            if isinstance(log_destination, discord.Thread) and log_destination.archived:
                try:
                    await log_destination.edit(archived=False)
                except Exception:
                    pass

        if not log_destination and self.log_channel_id:
            log_destination = self.bot.get_channel(self.log_channel_id)
            if not log_destination:
                try:
                    log_destination = await self.bot.fetch_channel(self.log_channel_id)
                except Exception:
                    log_destination = None

        return log_destination

    def _build_restore_targets(self, record: Dict[str, Any], fallback_guild_id: Optional[int]) -> Dict[str, List[int]]:
        by_guild = record.get("removed_roles_by_guild", {}) or {}
        if by_guild:
            restored: Dict[str, List[int]] = {}
            for gid, roles in by_guild.items():
                parsed_roles = self._parse_json_list(roles)
                if parsed_roles:
                    restored[str(gid)] = parsed_roles
            if restored:
                return restored

        legacy_roles = self._parse_json_list(record.get("removed_roles", []))
        if not legacy_roles:
            return {}

        source_gid = record.get("source_guild_id") or (str(fallback_guild_id) if fallback_guild_id else None)
        if not source_gid:
            return {}
        return {str(source_gid): legacy_roles}

    async def _execute_revoke_record(self, interaction: discord.Interaction, user_id: str, record: Dict[str, Any]) -> Tuple[bool, str]:
        restore_targets = self._build_restore_targets(record, interaction.guild.id if interaction.guild else None)
        if not restore_targets:
            return False, f"在数据库中找不到（{user_id}）的上次处罚移除了什么身份组，可能是由于上次处罚来源于同步，请检查日志频道。"

        restored_roles: List[int] = []
        failed_roles: List[int] = []
        detail_lines: List[str] = []

        for guild_id, roles in restore_targets.items():
            guild = self.bot.get_guild(int(guild_id)) if str(guild_id).isdigit() else None
            if not guild:
                failed_roles.extend(roles)
                detail_lines.append(f"❌ {guild_id}: 机器人不在该服务器")
                continue

            member = await self._resolve_member_in_guild(guild, int(user_id))
            if not member:
                failed_roles.extend(roles)
                detail_lines.append(f"❌ {guild.name}: 用户不在服务器")
                continue

            restored, failed = await self.restore_user_roles(member, roles)
            restored_roles.extend(restored)
            failed_roles.extend(failed)
            detail_lines.append(f"✅ {guild.name}: 恢复 {len(restored)} 个，失败 {len(failed)} 个")

        success = await self.revoke_punishment(record['id'])
        if not success:
            return False, "❌ 撤销处罚失败，可能记录已被修改"

        log_destination = await self._get_log_destination()
        if log_destination:
            await self.send_revoke_log_embed(
                channel=log_destination,
                record=record,
                revoker=interaction.user,
                restored_roles=restored_roles,
                failed_roles=failed_roles,
                restore_targets=restore_targets
            )

        message = (
            f"✅ 成功撤销对用户 **{record['user_name']}** (ID: {user_id}) 的处罚\n"
            f"记录ID: {record['id']}\n"
            f"原处罚时间: {record['timestamp']}\n"
            f"原处罚原因: {record['reason']}\n"
            f"来源: {record.get('source_type', 'local')}\n"
            + "\n".join(detail_lines)
        )
        return True, message

    async def send_revoke_log_embed(self, channel: discord.abc.Messageable,
                                   record: Dict, revoker: discord.User,
                                   restored_roles: List[int], failed_roles: List[int],
                                   restore_targets: Dict[str, List[int]]):
        """发送撤销日志Embed到指定频道"""
        embed = discord.Embed(
            title="↩️ 快速处罚撤销",
            color=discord.Color.green(),
            timestamp=datetime.now()
        )

        embed.add_field(
            name="撤销对象",
            value=f"{record['user_name']} (ID: {record['user_id']})",
            inline=False
        )
        embed.add_field(name="撤销者", value=f"{revoker.mention}", inline=True)
        embed.add_field(name="原执行者", value=record['executor_name'], inline=True)
        embed.add_field(name="来源", value=record.get('source_type', 'local'), inline=True)

        embed.add_field(name="原处罚原因", value=record['reason'], inline=False)
        embed.add_field(name="原处罚时间", value=record['timestamp'], inline=False)

        target_lines = [f"{gid}: {len(roles)}个" for gid, roles in restore_targets.items()]
        if target_lines:
            embed.add_field(name="恢复目标服务器", value="\n".join(target_lines), inline=False)

        if restored_roles:
            roles_str = ", ".join([f"<@&{role_id}>" for role_id in restored_roles])
            embed.add_field(name="✅ 已恢复身份组", value=roles_str, inline=False)

        if failed_roles:
            failed_str = ", ".join([f"ID:{role_id}" for role_id in failed_roles])
            embed.add_field(name="❌ 恢复失败的身份组", value=failed_str, inline=False)

        embed.set_footer(text=f"撤销的记录ID: {record['id']}")

        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"发送撤销日志Embed时出错: {e}")
    
    @app_commands.command(name="快速处罚-查询", description="查询最近的快速处罚记录")
    @app_commands.describe(count="要查询的记录数量（默认3条，最多1000条）")
    @app_commands.guild_only()
    async def quick_punish_query(self, interaction: discord.Interaction, count: Optional[int] = 3):
        """查询快速处罚记录命令"""
        # 立即defer响应
        await interaction.response.defer(ephemeral=True)
        
        # 检查功能是否启用
        if not self.enabled:
            await interaction.followup.send(
                "❌ 快速处罚功能未启用",
                ephemeral=True
            )
            return
        
        # 检查权限
        if not self.has_permission(interaction):
            await interaction.followup.send(
                "❌ 您没有权限使用此命令",
                ephemeral=True
            )
            return
        
        # 处理默认值和范围限制
        if count is None:
            count = 3
        count = min(count, 1000)
        count = max(count, 1)
        
        # 获取记录
        records = await self.get_recent_punishments(count)
        
        if not records:
            await interaction.followup.send(
                "📝 暂无快速处罚记录",
                ephemeral=True
            )
            return
        
        # 格式化记录
        formatted_text = await self.format_punishment_records(records, interaction.guild)
        
        # 根据记录数量决定发送方式
        if len(records) <= 10:
            # 10条以内，使用Embed显示
            embed = discord.Embed(
                title=f"📋 最近 {len(records)} 条快速处罚记录",
                description="",
                color=discord.Color.blue(),
                timestamp=datetime.now()
            )
            
            for i, record in enumerate(records, 1):
                # 解析时间
                try:
                    timestamp = datetime.fromisoformat(record['timestamp'])
                    time_str = timestamp.strftime('%m-%d %H:%M')
                except:
                    time_str = record['timestamp'][:16]
                
                # 状态标记
                status_emoji = {
                    'executed': '✅',
                    'failed': '❌',
                    'revoked': '↩️'
                }.get(record['status'], '❓')
                
                field_name = f"{status_emoji} #{record['id']} - {record['user_name']}"
                field_value = (
                    f"时间: {time_str}\n"
                    f"来源: {record.get('source_type', 'local')}\n"
                    f"原因: {record['reason'][:50]}{'...' if len(record['reason']) > 50 else ''}\n"
                    f"执行者: {record['executor_name']}"
                )

                embed.add_field(name=field_name, value=field_value, inline=False)
            
            embed.set_footer(text=f"查询者: {interaction.user.name}")
            
            await interaction.followup.send(
                embed=embed,
                ephemeral=True
            )
        else:
            # 超过10条，生成txt文件
            file_content = formatted_text.encode('utf-8')
            file = discord.File(
                io.BytesIO(file_content),
                filename=f"punish_records_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            )
            
            await interaction.followup.send(
                f"📋 找到 {len(records)} 条快速处罚记录，已生成文件：",
                file=file,
                ephemeral=True
            )
    
    @app_commands.command(name="快速处罚-撤销", description="撤销最近一次的快速处罚")
    @app_commands.describe(user_id="要撤销处罚的用户ID")
    @app_commands.guild_only()
    async def quick_punish_revoke(self, interaction: discord.Interaction, user_id: str):
        """撤销快速处罚命令"""
        await interaction.response.defer(ephemeral=True)

        if not self.enabled:
            await interaction.followup.send(
                "❌ 快速处罚功能未启用",
                ephemeral=True
            )
            return

        if not self.has_permission(interaction):
            await interaction.followup.send(
                "❌ 您没有权限使用此命令",
                ephemeral=True
            )
            return

        try:
            user_id = user_id.strip()
            int(user_id)
        except ValueError:
            await interaction.followup.send(
                "❌ 无效的用户ID格式，请输入纯数字ID",
                ephemeral=True
            )
            return

        latest_record = await self.get_last_punishment_for_user(user_id)
        if not latest_record:
            await interaction.followup.send(
                f"❌ 未找到用户 {user_id} 的处罚记录",
                ephemeral=True
            )
            return

        local_record = await self.get_last_revocable_local_record_for_user(user_id)

        if latest_record.get("source_type") == "sync":
            if not local_record:
                await interaction.followup.send(
                    f"在数据库中找不到（{user_id}）的上次处罚移除了什么身份组，可能是由于上次处罚来源于同步，请检查日志频道。",
                    ephemeral=True
                )
                return

            warn_embed = discord.Embed(
                title="⚠️ 二次确认 - 最新记录为同步记录",
                description=(
                    "最新处罚记录来源为 **sync**，该记录通常不包含可恢复身份组。\n"
                    "确认后将自动回溯并撤销最近一条可恢复的 **local** 记录。"
                ),
                color=discord.Color.orange(),
                timestamp=datetime.now()
            )
            warn_embed.add_field(
                name="最新记录（sync）",
                value=f"ID: {latest_record['id']}\n时间: {latest_record['timestamp']}\n原因: {latest_record['reason']}",
                inline=False
            )
            warn_embed.add_field(
                name="将撤销记录（local）",
                value=f"ID: {local_record['id']}\n时间: {local_record['timestamp']}\n原因: {local_record['reason']}",
                inline=False
            )

            view = RevokeConfirmView(
                cog=self,
                target_user_id=user_id,
                latest_record=latest_record,
                revoke_record=local_record
            )
            await interaction.followup.send(embed=warn_embed, view=view, ephemeral=True)
            return

        if latest_record.get("source_type") == "local":
            target_record = latest_record
        else:
            target_record = local_record

        if not target_record:
            await interaction.followup.send(
                f"在数据库中找不到（{user_id}）的上次处罚移除了什么身份组，可能是由于上次处罚来源于同步，请检查日志频道。",
                ephemeral=True
            )
            return

        _, message = await self._execute_revoke_record(
            interaction=interaction,
            user_id=user_id,
            record=target_record
        )
        await interaction.followup.send(message, ephemeral=True)


# 定义上下文菜单命令（必须在类外部）
@app_commands.context_menu(name="愉悦送走")
@app_commands.guild_only()
async def quick_punish_context(interaction: discord.Interaction, message: discord.Message):
    """快速处罚上下文菜单命令"""
    # 获取cog实例
    cog = interaction.client.get_cog('QuickPunishCog')
    if not cog:
        await interaction.response.send_message(
            "❌ 模块未加载",
            ephemeral=True
        )
        return
    
    # 检查功能是否启用
    if not cog.enabled:
        await interaction.response.send_message(
            "❌ 愉悦送走功能未启用，请联系机器人开发者。",
            ephemeral=True
        )
        return
    
    # 检查权限
    if not cog.has_permission(interaction):
        await interaction.response.send_message(
            "❌ 没权。只有管理组和类脑自研答疑AI可以给人愉悦送走。",
            ephemeral=True
        )
        return
    
    # 检查目标是否是机器人
    if message.author.bot:
        await interaction.response.send_message(
            "❌ 不能给Bot愉悦送走。",
            ephemeral=True
        )
        return
    
    # 显示确认表单
    modal = QuickPunishModal(target_message=message, cog=cog)
    await interaction.response.send_modal(modal)


async def setup(bot):
    """设置Cog"""
    # 添加Cog
    await bot.add_cog(QuickPunishCog(bot))
    
    # 添加上下文菜单命令
    bot.tree.add_command(quick_punish_context)