import asyncio
import io
import os
import sqlite3
import sys
from collections import deque
from datetime import datetime

import discord
import openai
import psutil
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from cogs.utils import get_bot_should_guild_objects, log_slash_command
from paths import USERS_DB

load_dotenv()

TERMINAL_LOG_BUFFER_MAX = 5000
terminal_log_buffer = deque(maxlen=TERMINAL_LOG_BUFFER_MAX)


class TerminalLogCapture:
    """将终端输出同时写入原始流，并缓存最近的日志行。"""

    def __init__(self, original_stream, buffer: deque):
        self.original_stream = original_stream
        self.buffer = buffer
        self._pending = ""

    def write(self, data):
        if not isinstance(data, str):
            data = str(data)

        written = self.original_stream.write(data)
        self._capture(data)
        return written if written is not None else len(data)

    def flush(self):
        self.original_stream.flush()

    def _capture(self, data: str):
        self._pending += data.replace("\r\n", "\n").replace("\r", "\n")

        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            self.buffer.append(line)

    def get_pending_line(self) -> str:
        return self._pending

    def __getattr__(self, name):
        return getattr(self.original_stream, name)


def setup_terminal_log_capture():
    """捕获 stdout/stderr，便于通过命令导出最近终端日志。"""
    if not isinstance(sys.stdout, TerminalLogCapture):
        sys.stdout = TerminalLogCapture(sys.stdout, terminal_log_buffer)
    if not isinstance(sys.stderr, TerminalLogCapture):
        sys.stderr = TerminalLogCapture(sys.stderr, terminal_log_buffer)


def get_recent_terminal_logs(limit: int = 100) -> list[str]:
    """获取最近的终端日志。"""
    limit = max(1, min(limit, 1000))
    logs = list(terminal_log_buffer)

    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, TerminalLogCapture):
            pending_line = stream.get_pending_line()
            if pending_line:
                logs.append(pending_line)

    return logs[-limit:]


setup_terminal_log_capture()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_API_BASE_URL = os.getenv("OPENAI_API_BASE_URL")
OPENAI_MODEL = os.getenv("OPENAI_MODEL")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="/", intents=intents)
bot.admins = []
bot.trusted_users = []
bot.current_parallel_dayi_tasks = 0


if not all([OPENAI_API_KEY, OPENAI_API_BASE_URL, OPENAI_MODEL]):
    print("[错误] 缺少必要的 OpenAI 环境变量。请检查 .env 文件。")
    bot.openai_client = None
else:
    bot.openai_client = openai.AsyncOpenAI(
        api_key=OPENAI_API_KEY,
        base_url=OPENAI_API_BASE_URL,
    )


class ParallelLimitError(app_commands.AppCommandError):
    """自定义异常，用于表示并发达到上限。"""


bot.parallel_limit_error_cls = ParallelLimitError
NON_EXTENSION_MODULES = {"__init__.py", "utils.py"}
SKIPPED_COG_DIRECTORIES = {"deprecated"}
USERS_DB_PATH = str(USERS_DB)


def parse_owner_ids(raw: str) -> tuple[list[int], list[str]]:
    owner_ids: list[int] = []
    invalid_items: list[str] = []
    seen: set[int] = set()

    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        try:
            owner_id = int(token)
        except ValueError:
            invalid_items.append(token)
            continue
        if owner_id in seen:
            continue
        seen.add(owner_id)
        owner_ids.append(owner_id)

    return owner_ids, invalid_items


OWNER_IDS, INVALID_OWNER_ID_ITEMS = parse_owner_ids(os.getenv("OWNER_IDS", ""))
bot.owner_ids = OWNER_IDS
bot.admins = list(OWNER_IDS)


def merge_owner_admins(admins: list[int]) -> list[int]:
    merged: list[int] = []
    seen: set[int] = set()
    for user_id in [*bot.owner_ids, *admins]:
        if user_id in seen:
            continue
        seen.add(user_id)
        merged.append(user_id)
    return merged


def is_admin(interaction: discord.Interaction) -> bool:
    """检查用户是否为管理员。"""
    return interaction.user.id in getattr(bot, "admins", [])


def _load_database_sync() -> tuple[list[int], list[int]]:
    with sqlite3.connect(USERS_DB_PATH) as conn:
        cursor = conn.cursor()

        cursor.execute("SELECT id FROM admins")
        admins = [int(row[0]) for row in cursor.fetchall()]

        cursor.execute("SELECT id FROM trusted_users")
        trusted_users = [int(row[0]) for row in cursor.fetchall()]

    return admins, trusted_users


async def load_database(*, raise_on_error: bool = False) -> None:
    """从用户权限数据库加载管理员与受信任用户。"""
    try:
        admins, trusted_users = await asyncio.to_thread(_load_database_sync)
    except sqlite3.Error as exc:
        if raise_on_error:
            raise RuntimeError(f"SQLite 数据库错误: {exc}") from exc

        print(f"[错误] SQLite 数据库错误: {exc}。将使用空数据库。")
        bot.admins = list(bot.owner_ids)
        bot.trusted_users = []
        return
    except Exception as exc:
        if raise_on_error:
            raise RuntimeError(f"加载数据库时发生未知错误: {exc}") from exc

        print(f"[错误] 加载数据库时发生未知错误: {exc}。将使用空数据库。")
        bot.admins = list(bot.owner_ids)
        bot.trusted_users = []
        return

    bot.admins = merge_owner_admins(admins)
    bot.trusted_users = trusted_users


bot.load_database = load_database


@bot.event
async def setup_hook():
    """机器人启动时的设置钩子。"""
    await bot.load_database()
    await load_cogs()
    print("✅ 所有扩展已加载")


@bot.event
async def on_ready():
    """机器人启动时触发。"""
    print(f"✅ 机器人已登录: {bot.user}")
    print(f"📊 连接到 {len(bot.guilds)} 个服务器")
    if INVALID_OWNER_ID_ITEMS:
        print(f"⚠️ OWNER_IDS 中存在无效项: {', '.join(INVALID_OWNER_ID_ITEMS[:10])}")
    print(f"🔐 最高权限用户ID: {bot.owner_ids}")
    print(f"👑 管理员ID: {bot.admins}")
    print(f"🤝 受信任用户ID: {bot.trusted_users}")

    command_scope_guilds, invalid_scope_items = get_bot_should_guild_objects()
    if invalid_scope_items:
        print(f"⚠️ BOT_SHOULD_IN_GUILD_IDS 中存在无效项: {', '.join(invalid_scope_items[:10])}")

    try:
        synced = await bot.tree.sync()
        print(f"✅ 已同步 {len(synced)} 个全局应用命令")
    except Exception as exc:
        print(f"❌ 同步全局应用命令失败: {exc}")

    for guild in command_scope_guilds:
        try:
            synced = await bot.tree.sync(guild=guild)
            print(f"✅ 已向服务器 {guild.id} 同步 {len(synced)} 个 guild-scoped 应用命令")
        except Exception as exc:
            print(f"❌ 向服务器 {guild.id} 同步 guild-scoped 应用命令失败: {exc}")


@bot.tree.command(name="ping", description="显示机器人延迟和系统信息")
async def ping(interaction: discord.Interaction):
    """显示延迟、内存使用率、CPU 使用率等系统信息。"""
    if not is_admin(interaction):
        await interaction.response.send_message("❌ 此命令仅限管理员使用。", ephemeral=True)
        log_slash_command(interaction, False)
        return

    latency = round(bot.latency * 1000, 2)
    memory = psutil.virtual_memory()
    cpu_percent = psutil.cpu_percent(interval=1)

    embed = discord.Embed(title="Pong!", color=discord.Color.green())
    embed.add_field(name="延迟", value=f"{latency} ms", inline=True)
    embed.add_field(name="内存使用率", value=f"{memory.percent}%", inline=True)
    embed.add_field(name="CPU使用率", value=f"{cpu_percent}%", inline=True)
    embed.add_field(
        name="内存详情",
        value=f"已用: {memory.used / (1024**3):.2f} GB / 总计: {memory.total / (1024**3):.2f} GB",
        inline=False,
    )

    await interaction.response.send_message(embed=embed)
    log_slash_command(interaction, True)


@bot.tree.command(name="看看日志", description="导出最近打印到终端的日志")
@app_commands.describe(条数="要导出的最近日志条数，默认100，最大1000")
async def view_logs(
    interaction: discord.Interaction,
    条数: app_commands.Range[int, 1, 1000] = 100,
):
    """将最近的终端日志导出为 txt 附件，并以私密消息发送。"""
    if not is_admin(interaction):
        await interaction.response.send_message("❌ 此命令仅限管理员使用。", ephemeral=True)
        log_slash_command(interaction, False)
        return

    recent_logs = get_recent_terminal_logs(条数)
    if not recent_logs:
        await interaction.response.send_message("⚠️ 当前还没有可导出的终端日志。", ephemeral=True)
        log_slash_command(interaction, True)
        return

    export_time = datetime.now()
    file_name = f"terminal_logs_{export_time.strftime('%Y%m%d_%H%M%S')}.txt"
    file_content = (
        f"导出时间: {export_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"请求用户: {interaction.user} ({interaction.user.id})\n"
        f"日志条数: {len(recent_logs)}\n"
        + "=" * 60
        + "\n"
        + "\n".join(recent_logs)
        + "\n"
    )

    log_file = discord.File(io.BytesIO(file_content.encode("utf-8")), filename=file_name)
    await interaction.response.send_message(
        content=f"📄 已为你准备最近 {len(recent_logs)} 条终端日志，见附件下载。",
        file=log_file,
        ephemeral=True,
    )
    log_slash_command(interaction, True)


@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
):
    """处理应用命令错误。"""
    log_slash_command(interaction, False)

    if interaction.response.is_done():
        print(f"未处理的斜杠命令错误: {error}")
        return

    if isinstance(error, ParallelLimitError):
        await interaction.response.send_message(f"❌ {error}", ephemeral=True)
    elif isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("❌ 你没有权限使用此命令。", ephemeral=True)
    else:
        print(f"未处理的斜杠命令错误: {error}")
        await interaction.response.send_message("❌ 执行命令时发生未知错误。", ephemeral=True)


@bot.event
async def on_command_error(ctx, error):
    """处理前缀命令错误。"""
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ 你没有权限使用此命令")
        return
    print(f"错误: {error}")


async def load_cogs():
    """加载 cogs 文件夹下的所有扩展。"""
    cogs_dir = "cogs"
    if not os.path.exists(cogs_dir):
        print(f"[警告] 未找到 '{cogs_dir}' 文件夹，跳过加载 cogs。")
        return

    extension_names: list[str] = []
    for entry in sorted(os.listdir(cogs_dir)):
        entry_path = os.path.join(cogs_dir, entry)
        if entry.endswith(".py") and entry not in NON_EXTENSION_MODULES:
            extension_names.append(f"{cogs_dir}.{entry[:-3]}")
            continue
        if entry.startswith("__") or entry in SKIPPED_COG_DIRECTORIES or not os.path.isdir(entry_path):
            continue
        if os.path.exists(os.path.join(entry_path, "__init__.py")):
            extension_names.append(f"{cogs_dir}.{entry}")

    for extension_name in extension_names:
        try:
            await bot.load_extension(extension_name)
            print(f"✅ 已成功加载 cog: {extension_name}")
        except Exception as exc:
            print(f"❌ 加载 cog {extension_name} 时发生错误: {exc}")


async def main():
    """机器人启动主函数。"""
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        print("❌ 错误: 未设置 DISCORD_BOT_TOKEN 环境变量。")
        print("请在 .env 文件中或系统环境中设置 DISCORD_BOT_TOKEN。")
        return

    async with bot:
        print("🚀 正在启动机器人...")
        await bot.start(token)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🤖 机器人被手动关闭。")
