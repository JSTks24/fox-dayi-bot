"""Bot-level permission checks shared by commands and context menus."""  # drift:ignore[AVS] reason:Small shared policy API is intentionally stable.

import discord


def check_admin(interaction: discord.Interaction) -> bool:
    """Return whether the interaction user is a bot administrator."""
    owner_ids = getattr(interaction.client, "owner_ids", [])
    if interaction.user.id in owner_ids:
        return True

    admins = getattr(interaction.client, "admins", [])
    return interaction.user.id in admins


def check_admin_or_trusted(interaction: discord.Interaction) -> bool:
    """Return whether the interaction user is an admin or trusted user."""
    if check_admin(interaction):
        return True

    trusted_users = getattr(interaction.client, "trusted_users", [])
    return interaction.user.id in trusted_users


def get_user_tier(client: object, user_id: int) -> str:
    """Return the user's bot-level tier based on in-memory permission lists."""
    owner_ids = getattr(client, "owner_ids", [])
    if user_id in owner_ids:
        return "admin"

    admins = getattr(client, "admins", [])
    if user_id in admins:
        return "admin"

    trusted_users = getattr(client, "trusted_users", [])
    if user_id in trusted_users:
        return "trusted"

    return "other"
