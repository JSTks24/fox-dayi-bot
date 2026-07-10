"""Persistent logging for slash-command invocations."""  # drift:ignore[AVS] reason:Small shared adapter is intentionally a stable import boundary.

import os
from datetime import datetime

import discord

from paths import COMMAND_LOG_FILE


def log_slash_command(interaction: discord.Interaction, success: bool) -> None:
    """Record a slash-command result in the runtime command log."""
    log_file = os.fspath(COMMAND_LOG_FILE)
    log_dir = os.path.dirname(log_file)

    if not os.path.exists(log_dir):
        try:
            os.makedirs(log_dir)
        except OSError as e:
            print(f"[错误] 创建日志文件夹 {log_dir} 失败: {e}")
            return

    try:
        user_id = interaction.user.id
        user_name = interaction.user.name
        command_name = interaction.command.name if interaction.command else "Unknown"
        status = "成功" if success else "失败"

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_entry = f"[{timestamp}] ({user_id}+{user_name}+/{command_name}+{status})\n"

        with open(log_file, "a", encoding="utf-8") as f:
            f.write(log_entry)
    except Exception as e:
        print(f"[错误] 写入日志文件失败: {e}")
