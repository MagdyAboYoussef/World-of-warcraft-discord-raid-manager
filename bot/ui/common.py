"""Shared interaction helpers: permissions, log-URL validation, message refresh."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, cast

import discord

from ..config import ADMIN_ROLE_NAMES
from ..store import Raid, RaidState, Store

if TYPE_CHECKING:  # a runtime import would be circular: client -> ui -> client
    from ..client import RaidClient

log = logging.getLogger(__name__)

#: The only mention policy this bot ever sends with. Raid titles and
#: descriptions are admin-supplied free text that ends up in message *content*
#: for reminders, where an "@everyone" would otherwise really ping the server.
#: Individual raiders are still mentionable - that is the point of a reminder.
SAFE_MENTIONS = discord.AllowedMentions(everyone=False, roles=False, users=True)


def client_of(interaction: discord.Interaction) -> "RaidClient":
    """Typed accessor for the bot behind an interaction."""
    return cast("RaidClient", interaction.client)


def store_of(interaction: discord.Interaction) -> Store:
    return client_of(interaction).store

#: Accepts character pages and report links on any WCL regional domain.
# Parentheses and angle brackets are excluded deliberately: the URL is
# interpolated into a markdown link `[name](url)`, and a ')' in the URL would
# close that link early and let the remainder render as attacker-chosen text.
WCL_RE = re.compile(
    r"^https?://(?:www\.)?(?:[a-z]{2}\.)?warcraftlogs\.com/"
    r"(?:character|reports|user)/[^\s()<>\[\]]+$",
    re.IGNORECASE,
)


def is_admin(user: discord.abc.User | discord.Member) -> bool:
    """Discord Administrator, or a member of one of ADMIN_ROLES."""
    if not isinstance(user, discord.Member):
        return False
    if user.guild_permissions.administrator:
        return True
    return any(r.name.lower() in ADMIN_ROLE_NAMES for r in user.roles)


def normalise_logs_url(raw: str | None) -> tuple[str | None, str | None]:
    """Return (url, error). Empty input is allowed - logs are optional."""
    if raw is None or not raw.strip():
        return None, None
    url = raw.strip()
    if not url.lower().startswith(("http://", "https://")):
        url = f"https://{url}"
    if not WCL_RE.match(url):
        return None, (
            "That doesn't look like a Warcraft Logs link. Expected something like\n"
            "`https://www.warcraftlogs.com/character/eu/kazzak/yourname`"
        )
    return url, None


def raid_is_editable(raid: Raid) -> bool:
    return raid.state is RaidState.OPEN


async def refresh_raid_message(client: discord.Client, raid_id: int) -> None:
    """Re-render the pinned roster message after any roster mutation.

    Failures are logged rather than raised: a stale embed is far better than an
    interaction that errors out in the user's face after their action succeeded.
    """
    from .panel import RaidView  # imported late to avoid a circular import

    store = cast("RaidClient", client).store
    raid = store.get_raid(raid_id)
    if raid is None:
        log.warning("refresh: raid #%s not found", raid_id)
        return
    if raid.message_id is None:
        log.warning(
            "refresh: raid #%s has no message_id, board cannot update. "
            "Use /raid repost to re-anchor it.", raid_id,
        )
        return

    from .embeds import build_raid_embed

    try:
        channel = client.get_channel(raid.channel_id)
        if channel is None:
            channel = await client.fetch_channel(raid.channel_id)
        message = await channel.fetch_message(raid.message_id)  # type: ignore[union-attr]
        await message.edit(
            embed=build_raid_embed(raid, store.signups(raid_id)),
            view=RaidView(),
        )
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
        log.warning("could not refresh raid #%s message: %s", raid_id, exc)


async def deny(interaction: discord.Interaction, message: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)
