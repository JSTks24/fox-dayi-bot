"""Discord interaction response helpers."""  # drift:ignore[AVS] reason:Small shared API is intentionally stable and dependency-free.

import discord


async def safe_defer(interaction: discord.Interaction, ephemeral: bool = True) -> None:
    """Defer an interaction only when it has not already been acknowledged."""
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=ephemeral)
