"""Registration helpers for guild-scoped Discord context menus."""

import os
from collections.abc import Sequence

import discord


def parse_guild_id_csv(raw: str) -> tuple[list[int], list[str]]:
    """Parse a comma-separated guild ID list while preserving order."""
    guild_ids: list[int] = []
    invalid_items: list[str] = []
    seen: set[int] = set()

    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        try:
            guild_id = int(token)
        except ValueError:
            invalid_items.append(token)
            continue
        if guild_id in seen:
            continue
        seen.add(guild_id)
        guild_ids.append(guild_id)

    return guild_ids, invalid_items


def get_bot_should_guild_objects() -> tuple[list[discord.Object], list[str]]:
    """Build Discord guild objects from BOT_SHOULD_IN_GUILD_IDS."""
    guild_ids, invalid_items = parse_guild_id_csv(os.getenv("BOT_SHOULD_IN_GUILD_IDS", ""))
    return [discord.Object(id=guild_id) for guild_id in guild_ids], invalid_items


def register_guild_scoped_context_menus(
    tree,
    commands: Sequence,
    *,
    label: str,
) -> list[int]:
    """Register context menus only for configured guilds."""
    guilds, invalid_items = get_bot_should_guild_objects()
    if invalid_items:
        print(f"[{label}] BOT_SHOULD_IN_GUILD_IDS 中存在无效项: {', '.join(invalid_items[:10])}")
    if not guilds:
        print(f"[{label}] BOT_SHOULD_IN_GUILD_IDS 未配置或解析为空，跳过注册 guild-scoped 右键命令。")
        return []

    for guild in guilds:
        for command in commands:
            tree.add_command(command, guild=guild)

    return [guild.id for guild in guilds]


def remove_guild_scoped_context_menus(tree, commands: Sequence) -> None:
    """Remove context menus from every configured guild."""
    guilds, _ = get_bot_should_guild_objects()
    for guild in guilds:
        for command in commands:
            tree.remove_command(command.name, guild=guild, type=command.type)
