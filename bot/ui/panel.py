"""The persistent raid panel attached to the roster message.

Every button uses a static custom_id and resolves its raid from the message it
is attached to, so the view survives bot restarts without any per-raid state.
"""

from __future__ import annotations

import discord

from ..store import Raid, RaidState, Status, raid_is_closed
from .apply import start_application
from ..config import region_label
from .common import (
    audit, deny, derive_logs_url, handle_of, interaction_is_admin,
    refresh_raid_message, split_character, store_of,
)


async def _resolve_raid(interaction: discord.Interaction):
    """Find the raid this panel belongs to, or explain why we can't."""
    store = store_of(interaction)
    message = interaction.message
    raid = store.get_raid_by_message(message.id) if message else None
    if raid is None:
        await deny(interaction, "This raid is no longer tracked. Ask an admin to post a new one.")
        return None
    return raid


async def _set_own_status(interaction: discord.Interaction, status: Status) -> None:
    """Bench/Absent self-service. Falls back to the cached profile when the
    player has not applied yet, so opting out never requires applying first."""
    raid = await _resolve_raid(interaction)
    if raid is None:
        return
    if raid.state is not RaidState.OPEN:
        await deny(interaction, f"Raid **{raid.title}** is {raid.state.value} — signups are closed.")
        return

    store = store_of(interaction)
    existing = store.get_signup(raid.id, interaction.user.id)

    if existing is not None:
        store.set_status(raid.id, interaction.user.id, status, interaction.user.id)
        audit(
            interaction, raid.id, "status",
            target_id=interaction.user.id,
            target_name=handle_of(interaction.user),
            detail=f"{existing.character_name} — {existing.status.label} -> {status.label}",
        )
    else:
        player = store.get_player(interaction.user.id)
        if player is None or split_character(player.character_name) is None:
            # First-timer, or a saved flat name: collect details (with the
            # Name-Server rule) and land them on the status they pressed, rather
            # than a round trip through Apply.
            await start_application(interaction, raid.id, status)
            return
        store.upsert_signup(
            raid_id=raid.id,
            user_id=interaction.user.id,
            character_name=player.character_name,
            logs_url=player.logs_url
            or derive_logs_url(player.character_name, region_label(raid.timezone)),
            spec_key=player.spec_key,
            status=status,
            updated_by=interaction.user.id,
            discord_name=handle_of(interaction.user),
        )
        audit(
            interaction, raid.id, "apply",
            target_id=interaction.user.id,
            target_name=handle_of(interaction.user),
            detail=f"{player.character_name} — {status.label}",
        )

    await interaction.response.send_message(
        f"{status.emoji} You're marked **{status.label}** for **{raid.title}**.", ephemeral=True
    )
    await refresh_raid_message(interaction.client, raid.id)


#: The buttons that only make sense while a raid is still taking signups.
PLAYER_BUTTONS = frozenset(
    {"raid:apply", "raid:tentative", "raid:bench", "raid:absent", "raid:withdraw"}
)


class RaidView(discord.ui.View):
    """The panel under a raid board.

    Pass the raid to drop the player buttons once it is cancelled or over —
    there is nothing to sign up for, and leaving five dead buttons that only
    answer "signups are closed" is just noise on a finished raid. The admin row
    stays, so a lead can still open the roster afterwards.

    `RaidView()` with no raid keeps every button, which is the form registered
    at startup for persistence: the custom_ids have to stay known to the client
    for interactions on older messages to route at all.
    """

    def __init__(self, raid: Raid | None = None) -> None:
        super().__init__(timeout=None)
        if raid is not None and raid_is_closed(raid):
            for item in list(self.children):
                if getattr(item, "custom_id", None) in PLAYER_BUTTONS:
                    self.remove_item(item)

    # ------------------------------------------------------------ player row

    @discord.ui.button(
        label="Apply", emoji="📝", style=discord.ButtonStyle.success, custom_id="raid:apply", row=0
    )
    async def apply(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        raid = await _resolve_raid(interaction)
        if raid is None:
            return
        if raid.state is not RaidState.OPEN:
            await deny(interaction, f"Raid **{raid.title}** is {raid.state.value} — signups are closed.")
            return
        await start_application(interaction, raid.id)

    @discord.ui.button(
        label="Tentative", emoji="❔", style=discord.ButtonStyle.secondary,
        custom_id="raid:tentative", row=0,
    )
    async def tentative(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await _set_own_status(interaction, Status.TENTATIVE)

    @discord.ui.button(
        label="Backup", emoji="⭐", style=discord.ButtonStyle.secondary, custom_id="raid:bench", row=0
    )
    async def bench(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await _set_own_status(interaction, Status.BENCH)

    @discord.ui.button(
        label="Absent", emoji="🚫", style=discord.ButtonStyle.secondary, custom_id="raid:absent", row=0
    )
    async def absent(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await _set_own_status(interaction, Status.ABSENT)

    @discord.ui.button(
        label="Withdraw", emoji="🗑️", style=discord.ButtonStyle.danger, custom_id="raid:withdraw", row=0
    )
    async def withdraw(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        raid = await _resolve_raid(interaction)
        if raid is None:
            return
        store = store_of(interaction)
        existing = store.get_signup(raid.id, interaction.user.id)
        if store.remove_signup(raid.id, interaction.user.id):
            audit(
                interaction, raid.id, "withdraw",
                target_id=interaction.user.id,
                target_name=handle_of(interaction.user),
                detail=existing.character_name if existing else None,
            )
            await interaction.response.send_message(
                f"Removed you from **{raid.title}**.", ephemeral=True
            )
            await refresh_raid_message(interaction.client, raid.id)
        else:
            await deny(interaction, "You aren't signed up for this raid.")

    # ------------------------------------------------------------- admin row

    @discord.ui.button(
        label="Manage roster", emoji="🛠️", style=discord.ButtonStyle.primary,
        custom_id="raid:manage", row=1,
    )
    async def manage(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        from .admin import open_roster_manager

        raid = await _resolve_raid(interaction)
        if raid is None:
            return
        if not interaction_is_admin(interaction):
            await deny(interaction, "🔒 Only raid admins can manage the roster.")
            return
        await open_roster_manager(interaction, raid.id)

    @discord.ui.button(
        label="Raid settings", emoji="⚙️", style=discord.ButtonStyle.secondary,
        custom_id="raid:settings", row=1,
    )
    async def settings(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        from .admin import open_raid_settings

        raid = await _resolve_raid(interaction)
        if raid is None:
            return
        if not interaction_is_admin(interaction):
            await deny(interaction, "🔒 Only raid admins can change raid settings.")
            return
        await open_raid_settings(interaction, raid.id)

    @discord.ui.button(
        label="Admin UI", emoji="🌐", style=discord.ButtonStyle.primary,
        custom_id="raid:web", row=1,
    )
    async def web_manager(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        """Signed link to the web roster manager.

        Discord has no way to show a component to some viewers and not others -
        anything on a public message is on it for everyone - so the gate is on
        the response instead. Both branches are ephemeral, so a raider pressing
        this sees only an explanation, and never anyone else's link.
        """
        from .admin import send_manager_link

        raid = await _resolve_raid(interaction)
        if raid is None:
            return
        if not interaction_is_admin(interaction):
            await deny(
                interaction,
                "🔒 **The roster manager is for raid leads.**\n"
                "It's where applications get accepted, so it's limited to admins "
                "and anyone with a raid-lead role.\n\n"
                "To sign up, use **📝 Apply** on this board. If you *are* a raid "
                "lead and this still says no, ask an admin to check your roles — "
                "leads can also open it with `/raid page`.",
            )
            return
        await send_manager_link(interaction, raid.id)
