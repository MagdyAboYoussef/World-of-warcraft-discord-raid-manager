"""The persistent raid panel attached to the roster message.

Every button uses a static custom_id and resolves its raid from the message it
is attached to, so the view survives bot restarts without any per-raid state.
"""

from __future__ import annotations

import discord

from ..store import RaidState, Status
from .apply import start_application
from .common import deny, is_admin, refresh_raid_message, store_of


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
    else:
        player = store.get_player(interaction.user.id)
        if player is None:
            # First-timer: collect their details and land them on the status they
            # actually pressed. Refusing them here just meant applying first and
            # then correcting it, which is the round trip this avoids.
            await start_application(interaction, raid.id, status)
            return
        store.upsert_signup(
            raid_id=raid.id,
            user_id=interaction.user.id,
            character_name=player.character_name,
            logs_url=player.logs_url,
            spec_key=player.spec_key,
            status=status,
            updated_by=interaction.user.id,
        )

    await interaction.response.send_message(
        f"{status.emoji} You're marked **{status.label}** for **{raid.title}**.", ephemeral=True
    )
    await refresh_raid_message(interaction.client, raid.id)


class RaidView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

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
        label="Bench me", emoji="🪑", style=discord.ButtonStyle.secondary, custom_id="raid:bench", row=0
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
        if store.remove_signup(raid.id, interaction.user.id):
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
        if not is_admin(interaction.user):
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
        if not is_admin(interaction.user):
            await deny(interaction, "🔒 Only raid admins can change raid settings.")
            return
        await open_raid_settings(interaction, raid.id)

    @discord.ui.button(
        label="Open UI", emoji="🌐", style=discord.ButtonStyle.primary,
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
        if not is_admin(interaction.user):
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
