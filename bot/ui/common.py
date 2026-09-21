"""Shared interaction helpers: permissions, log-URL validation, message refresh."""

from __future__ import annotations

import asyncio
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
    """Discord Administrator, or a member of one of ADMIN_ROLES.

    Prefer `interaction_is_admin` anywhere an Interaction is in hand - see the
    note there about why this one can say no to a real administrator.
    """
    if not isinstance(user, discord.Member):
        return False
    if user.guild_permissions.administrator:
        return True
    return any(r.name.lower() in ADMIN_ROLE_NAMES for r in user.roles)


def interaction_is_admin(interaction: discord.Interaction) -> bool:
    """The admin gate for anything driven by an interaction.

    Discord resolves the invoker's permissions server-side and ships them in the
    interaction payload, and `Interaction.permissions` is that value. It is used
    in preference to Member.guild_permissions, which recomputes the answer by
    walking `member.roles` - and `member.roles` resolves role ids through the
    *guild's role cache*. This bot runs on Intents.default() without the members
    intent, so that cache is populated only by the gateway events it happens to
    receive; a role missing from it contributes no permission bits, and a real
    server administrator computes to zero and gets refused. The payload value
    cannot go stale that way, because it never has to be reconstructed.

    The ADMIN_ROLES fallback still runs on the member, since a role *name* is
    not something Discord resolves for us.
    """
    if interaction.permissions.administrator:
        return True
    return is_admin(interaction.user)


def admin_denial_reason(interaction: discord.Interaction) -> str:
    """Why this user failed the gate, for the log. Never shown to them.

    A refusal that leaves no trace is unsupportable: "it says admin-only and I
    am an admin" is impossible to answer without knowing which of the two tests
    was applied and what it saw.
    """
    user = interaction.user
    if not isinstance(user, discord.Member):
        return "not a guild member (no member data on the interaction)"
    roles = [r.name for r in user.roles]
    return (
        f"interaction.permissions.administrator="
        f"{interaction.permissions.administrator}, "
        f"guild_permissions.administrator={user.guild_permissions.administrator}, "
        f"roles={roles}, accepted role names={sorted(ADMIN_ROLE_NAMES)}"
    )


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


#: WoW gameplay region -> the slug Warcraft Logs uses in a character URL. WCL
#: folds the Americas together, so NA, Oceanic and Brazil all resolve to "us".
_WCL_REGION: dict[str, str] = {
    "eu": "eu", "kr": "kr", "tw": "tw", "cn": "cn",
    "na": "us", "us": "us", "us-central": "us", "us-east": "us", "us-west": "us",
    "oce": "us", "oceanic": "us", "br": "us",
}


def split_character(name: str | None) -> tuple[str, str] | None:
    """('Mimz-Kazzak') -> ('Mimz', 'Kazzak'); a flat name -> None.

    The realm is everything after the first hyphen, so a spaced realm like
    'Tarren Mill' survives intact. Both halves must be non-empty, which is the
    whole point: a link, and a clean roster, need the realm.
    """
    left, sep, right = (name or "").strip().partition("-")
    if not sep or not left.strip() or not right.strip():
        return None
    return left.strip(), right.strip()


def _wcl_slug(text: str) -> str:
    """Slug a name or realm the way a Warcraft Logs URL segment expects it."""
    text = text.strip().lower().replace("'", "")
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"[^a-z0-9-]", "", text)
    return re.sub(r"-{2,}", "-", text).strip("-")


def derive_logs_url(character_name: str, region: str | None) -> str | None:
    """Guess a Warcraft Logs character URL from a 'Name-Realm' character.

    Only fires for the Name-Realm shape a WoW character copy produces, because
    without the realm half there is nothing to point a link at. The realm is
    slugged the way WCL writes it ("Tarren Mill" -> "tarren-mill") and the
    region is the raid's own - a guess, but the right one for a guild that
    raids in a single region, which is all a lone "eu"/"na" tag can describe.
    Returns None rather than a broken link when any piece is missing or the
    region is an IANA zone WCL has no name for.
    """
    parts = split_character(character_name)
    if parts is None:
        return None
    name_slug, realm_slug = _wcl_slug(parts[0]), _wcl_slug(parts[1])
    region_slug = _WCL_REGION.get((region or "").strip().lower())
    if not (name_slug and realm_slug and region_slug):
        return None
    url = f"https://www.warcraftlogs.com/character/{region_slug}/{realm_slug}/{name_slug}"
    # Run it past the same gate a typed link faces, so a derived one can never
    # be laxer than one a person could enter.
    return url if WCL_RE.match(url) else None


def handle_of(user: discord.abc.User | discord.Member) -> str:
    """The Discord @handle, in preference to a nickname.

    A nickname is per-guild and can be changed by the person wearing it, which
    makes it the wrong thing to write into a permanent log: the whole point of
    recording who did something is that it still identifies them afterwards.
    """
    return getattr(user, "name", None) or str(user)


def audit(
    interaction: discord.Interaction,
    raid_id: int,
    action: str,
    *,
    target_id: int | None = None,
    target_name: str | None = None,
    detail: str | None = None,
) -> None:
    """Record one roster change made from Discord.

    The actor is always `interaction.user` - never the raid leader, never the
    bot. "Someone accepted them, but which of the four officers?" is precisely
    the question this log exists to answer, so attributing an action to anyone
    but the person who clicked would defeat it.
    """
    store_of(interaction).record_audit(
        raid_id=raid_id,
        action=action,
        source="discord",
        actor_id=interaction.user.id,
        actor_name=handle_of(interaction.user),
        target_id=target_id,
        target_name=target_name,
        detail=detail,
    )


def raid_is_editable(raid: Raid) -> bool:
    return raid.state is RaidState.OPEN


async def refresh_raid_message(client: discord.Client, raid_id: int) -> bool:
    """Re-render the pinned roster message after any roster mutation.

    Failures are logged rather than raised: a stale embed is far better than an
    interaction that errors out in the user's face after their action succeeded.
    Returns True if the board message was edited, False if it could not be
    (missing message, or no channel access) - the manual refresh button reports
    that to the admin.
    """
    from .panel import RaidView  # imported late to avoid a circular import

    store = cast("RaidClient", client).store
    raid = store.get_raid(raid_id)
    if raid is None:
        log.warning("refresh: raid #%s not found", raid_id)
        return False
    if raid.message_id is None:
        log.warning(
            "refresh: raid #%s has no message_id, board cannot update. "
            "Use /raid repost to re-anchor it.", raid_id,
        )
        return False

    from .embeds import build_raid_embed

    try:
        channel = client.get_channel(raid.channel_id)
        if channel is None:
            channel = await client.fetch_channel(raid.channel_id)
        message = await channel.fetch_message(raid.message_id)  # type: ignore[union-attr]
        await message.edit(
            embed=build_raid_embed(raid, store.signups(raid_id)),
            view=RaidView(raid),
        )
        return True
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
        log.warning("could not refresh raid #%s message: %s", raid_id, exc)
        return False


#: How long to wait for more changes before re-rendering the board.
REFRESH_DEBOUNCE_SECONDS = 1.5

_refresh_tasks: dict[int, asyncio.Task] = {}
_refresh_dirty: set[int] = set()


async def _debounced_refresh(client: discord.Client, raid_id: int) -> None:
    try:
        # Re-checking `dirty` after each pass means changes that land *during* a
        # refresh still get a follow-up render, so the board never settles on a
        # stale state just because an edit arrived at an awkward moment.
        while raid_id in _refresh_dirty:
            _refresh_dirty.discard(raid_id)
            await asyncio.sleep(REFRESH_DEBOUNCE_SECONDS)
            await refresh_raid_message(client, raid_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("debounced refresh for raid #%s failed", raid_id)
    finally:
        _refresh_tasks.pop(raid_id, None)


def request_raid_refresh(client: discord.Client, raid_id: int) -> None:
    """Ask for a board re-render soon, collapsing bursts into one edit.

    An admin clicking through twenty applicants would otherwise fire twenty
    message edits in a few seconds, which Discord rate-limits hard - the last
    few would be delayed or dropped and the board would look stuck. Callers that
    genuinely need the edit to have landed should await refresh_raid_message.
    """
    _refresh_dirty.add(raid_id)
    task = _refresh_tasks.get(raid_id)
    if task is not None and not task.done():
        return
    # Held in the dict for the task's lifetime, which also keeps it from being
    # garbage collected mid-flight.
    _refresh_tasks[raid_id] = asyncio.create_task(_debounced_refresh(client, raid_id))


async def deny(interaction: discord.Interaction, message: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)
