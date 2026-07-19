"""Admin-only roster management and raid settings.

Both panels are ephemeral and re-rendered in place, so an admin can run through
a whole application queue without spamming the channel. Every mutation calls
refresh_raid_message so the public roster stays in sync.
"""

from __future__ import annotations

import logging

import discord

from ..config import region_label
from ..data.specs import ROLE_ORDER, Role, get_spec
from ..emojis import registry
from ..store import RaidState, Signup, Status
from .apply import SpecPickerView
from .common import deny, is_admin, refresh_raid_message, store_of
from .embeds import clamp_title
from .schedule import (
    format_duration, format_local, is_known_timezone, parse_duration, parse_when,
)

log = logging.getLogger(__name__)

SELECT_LIMIT = 25


def _signup_label(signup: Signup) -> str:
    spec = get_spec(signup.spec_key)
    return f"{signup.character_name} — {spec.full_name if spec else signup.spec_key}"


class RosterManager(discord.ui.View):
    def __init__(self, raid_id: int) -> None:
        super().__init__(timeout=600)
        self.raid_id = raid_id
        # Show everyone by default. Filtering to Pending hides anyone who has
        # benched or absented themselves, so an admin looking for them finds an
        # empty dropdown and concludes the bot is broken.
        self.filter: Status | None = None
        # Second, independent axis: role is derived from the signup's spec
        # rather than stored on the row, so it is filtered in Python below.
        self.role_filter: Role | None = None
        self.selected_user_id: int | None = None
        self.rebuild()

    # ------------------------------------------------------------------ state

    def _store(self, interaction: discord.Interaction):
        return store_of(interaction)

    def _visible(self, store) -> list[Signup]:
        rows = store.signups(self.raid_id, self.filter)
        if self.role_filter is not None:
            # Narrow before truncating, otherwise the 25-row cap could discard
            # matches that the admin explicitly filtered for. A spec_key the
            # data no longer knows about has no role, so it can't match.
            rows = [
                s for s in rows
                if (spec := get_spec(s.spec_key)) and spec.role is self.role_filter
            ]
        return rows[:SELECT_LIMIT]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_admin(interaction.user):
            return True
        await deny(interaction, "🔒 Admins only.")
        return False

    # ------------------------------------------------------------- rendering

    def rebuild(self, store=None) -> None:
        self.clear_items()
        self.add_item(FilterSelect(self))
        self.add_item(RoleFilterSelect(self))
        self.add_item(PlayerSelect(self, store))
        # All five status buttons share one row (Discord's per-row cap), which
        # frees the row the role filter now occupies.
        for status, style in (
            (Status.ACCEPTED, discord.ButtonStyle.success),
            (Status.DECLINED, discord.ButtonStyle.danger),
            (Status.BENCH, discord.ButtonStyle.secondary),
            (Status.ABSENT, discord.ButtonStyle.secondary),
            (Status.PENDING, discord.ButtonStyle.secondary),
        ):
            self.add_item(StatusButton(self, status, style, 3))
        self.add_item(ChangeSpecButton(self))
        self.add_item(RemoveButton(self))
        self.add_item(DoneButton())

    def summary(self, store) -> discord.Embed:
        raid = store.get_raid(self.raid_id)
        signups = store.signups(self.raid_id)
        counts = {s: 0 for s in Status}
        for signup in signups:
            counts[signup.status] += 1

        embed = discord.Embed(
            title=clamp_title(f"🛠️ Manage roster — {raid.title}"),
            description=" · ".join(f"{s.emoji} {s.label} **{counts[s]}**" for s in Status),
            color=0x5865F2,
        )

        accepted = [s for s in signups if s.status is Status.ACCEPTED]
        role_counts = dict.fromkeys(ROLE_ORDER, 0)
        for signup in accepted:
            spec = get_spec(signup.spec_key)
            if spec:
                role_counts[spec.role] += 1
        embed.add_field(
            name="Comp vs targets",
            value="\n".join(
                f"{registry.role(r.value)} {r.label}: **{role_counts[r]}**/{raid.caps.get(r.value, 0)}"
                + ("  ⚠️" if role_counts[r] < raid.caps.get(r.value, 0) else "")
                for r in ROLE_ORDER
            ),
            inline=False,
        )

        if self.selected_user_id:
            selected = store.get_signup(self.raid_id, self.selected_user_id)
            if selected:
                spec = get_spec(selected.spec_key)
                # Character names and notes are free text typed by players, so
                # they are escaped before being interpolated into markdown.
                lines = [
                    f"{selected.status.emoji} "
                    f"**{discord.utils.escape_markdown(selected.character_name)}** — "
                    f"{spec.full_name if spec else selected.spec_key}",
                    f"<@{selected.user_id}>",
                ]
                if selected.logs_url:
                    lines.append(f"[Warcraft Logs]({selected.logs_url})")
                if selected.note:
                    lines.append(f"💬 {discord.utils.escape_markdown(selected.note)}")
                embed.add_field(name="Selected", value="\n".join(lines), inline=False)
        else:
            embed.add_field(
                name="Selected", value="*Pick a player from the dropdown.*", inline=False
            )
        return embed

    async def refresh(self, interaction: discord.Interaction) -> None:
        store = self._store(interaction)
        self.rebuild(store)
        await interaction.response.edit_message(embed=self.summary(store), view=self)
        await refresh_raid_message(interaction.client, self.raid_id)


class FilterSelect(discord.ui.Select):
    def __init__(self, manager: RosterManager) -> None:
        self.manager = manager
        options = [
            discord.SelectOption(
                label="All signups", value="all", default=manager.filter is None
            )
        ] + [
            discord.SelectOption(
                label=s.label, value=s.value, emoji=s.emoji, default=manager.filter is s
            )
            for s in Status
        ]
        super().__init__(placeholder="Filter by status…", options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        self.manager.filter = None if value == "all" else Status(value)
        self.manager.selected_user_id = None
        await self.manager.refresh(interaction)


class RoleFilterSelect(discord.ui.Select):
    """Narrows the player list by role, on top of whatever status is selected.

    Combines with FilterSelect rather than replacing it, so "pending healers"
    is reachable without hunting through every pending applicant.
    """

    def __init__(self, manager: RosterManager) -> None:
        self.manager = manager
        options = [
            discord.SelectOption(
                label="All roles", value="all", default=manager.role_filter is None
            )
        ] + [
            discord.SelectOption(
                label=r.label,
                value=r.value,
                emoji=registry.role(r.value) or None,
                default=manager.role_filter is r,
            )
            for r in ROLE_ORDER
        ]
        super().__init__(placeholder="Filter by role…", options=options, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        self.manager.role_filter = None if value == "all" else Role(value)
        self.manager.selected_user_id = None
        await self.manager.refresh(interaction)


class PlayerSelect(discord.ui.Select):
    def __init__(self, manager: RosterManager, store=None) -> None:
        self.manager = manager
        rows = manager._visible(store) if store is not None else []
        options = [
            discord.SelectOption(
                label=_signup_label(s)[:100],
                value=str(s.user_id),
                emoji=registry.spec(s.spec_key) or s.status.emoji,
                description=f"{s.status.label}" + (f" · {s.note[:60]}" if s.note else ""),
                default=s.user_id == manager.selected_user_id,
            )
            for s in rows
        ] or [discord.SelectOption(label="no signups match these filters", value="_none")]
        super().__init__(
            placeholder="Choose a player…", options=options, disabled=not rows, row=2
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.manager.selected_user_id = int(self.values[0])
        await self.manager.refresh(interaction)


class _NeedsSelection(discord.ui.Button):
    """Action button that operates on the currently selected player.

    Deliberately never rendered disabled: a disabled button gives the admin no
    feedback whatsoever, so a mis-click looks identical to a broken bot. It
    stays clickable and explains what's missing instead.
    """

    def __init__(self, manager: RosterManager, **kwargs) -> None:
        self.manager = manager
        super().__init__(**kwargs)

    async def resolve(self, interaction: discord.Interaction) -> Signup | None:
        if self.manager.selected_user_id is None:
            await deny(
                interaction,
                "Pick someone from the **Choose a player…** dropdown first, "
                "then press this again.",
            )
            return None
        store = self.manager._store(interaction)
        signup = store.get_signup(self.manager.raid_id, self.manager.selected_user_id)
        if signup is None:
            self.manager.selected_user_id = None
            await deny(interaction, "That player is no longer signed up for this raid.")
            return None
        return signup


class StatusButton(_NeedsSelection):
    def __init__(
        self, manager: RosterManager, status: Status, style: discord.ButtonStyle, row: int
    ) -> None:
        self.status = status
        super().__init__(manager, label=status.label, emoji=status.emoji, style=style, row=row)

    async def callback(self, interaction: discord.Interaction) -> None:
        signup = await self.resolve(interaction)
        if signup is None:
            return
        store = self.manager._store(interaction)
        if not store.set_status(
            self.manager.raid_id, signup.user_id, self.status, interaction.user.id
        ):
            log.error(
                "raid #%s: set_status matched no rows for user %s",
                self.manager.raid_id, signup.user_id,
            )
            await deny(interaction, "Couldn't update that player — nothing was changed.")
            return
        log.info(
            "raid #%s: %s set %s (%s) -> %s",
            self.manager.raid_id, interaction.user, signup.character_name,
            signup.user_id, self.status.value,
        )
        await self.manager.refresh(interaction)


class ChangeSpecButton(_NeedsSelection):
    def __init__(self, manager: RosterManager) -> None:
        super().__init__(
            manager, label="Change spec", emoji="🔁", style=discord.ButtonStyle.primary, row=4
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        signup = await self.resolve(interaction)
        if signup is None:
            return
        store = self.manager._store(interaction)

        async def apply_spec(spec_interaction: discord.Interaction, spec_key: str) -> None:
            store.set_spec(
                self.manager.raid_id, signup.user_id, spec_key, spec_interaction.user.id
            )
            spec = get_spec(spec_key)
            await spec_interaction.response.edit_message(
                content=f"✅ **{signup.character_name}** is now "
                f"{registry.spec(spec_key)} {spec.full_name if spec else spec_key}.",
                view=None,
            )
            await refresh_raid_message(spec_interaction.client, self.manager.raid_id)

        await interaction.response.send_message(
            content=f"Reassign **{signup.character_name}**:",
            view=SpecPickerView(apply_spec, initial_spec_key=signup.spec_key),
            ephemeral=True,
        )


class RemoveButton(_NeedsSelection):
    def __init__(self, manager: RosterManager) -> None:
        super().__init__(
            manager, label="Remove", emoji="🗑️", style=discord.ButtonStyle.danger, row=4
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        signup = await self.resolve(interaction)
        if signup is None:
            return
        store = self.manager._store(interaction)
        store.remove_signup(self.manager.raid_id, signup.user_id)
        log.info(
            "raid #%s: %s removed %s", self.manager.raid_id, interaction.user, signup.character_name
        )
        self.manager.selected_user_id = None
        await self.manager.refresh(interaction)


class DoneButton(discord.ui.Button):
    """Closes the ephemeral panel, so it doesn't linger once you're finished."""

    def __init__(self, row: int = 4) -> None:
        super().__init__(label="Done", emoji="✔️", style=discord.ButtonStyle.secondary, row=row)

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            content="✔️ Closed — the raid board above is up to date.",
            embed=None,
            view=None,
        )


async def open_roster_manager(interaction: discord.Interaction, raid_id: int) -> None:
    manager = RosterManager(raid_id)
    store = store_of(interaction)
    manager.rebuild(store)
    await interaction.response.send_message(
        embed=manager.summary(store), view=manager, ephemeral=True
    )


# --------------------------------------------------------------------- settings


class CapsModal(discord.ui.Modal, title="Role targets"):
    def __init__(self, raid_id: int, caps: dict[str, int]) -> None:
        super().__init__()
        self.raid_id = raid_id
        self.inputs: dict[str, discord.ui.TextInput] = {}
        for role in ROLE_ORDER:
            field = discord.ui.TextInput(
                label=role.label,
                default=str(caps.get(role.value, 0)),
                max_length=2,
                required=True,
            )
            self.inputs[role.value] = field
            self.add_item(field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        caps: dict[str, int] = {}
        for key, field in self.inputs.items():
            raw = str(field).strip()
            if not raw.isdigit():
                await interaction.response.send_message(
                    f"`{raw}` isn't a whole number — targets must be digits.", ephemeral=True
                )
                return
            caps[key] = int(raw)
        store = store_of(interaction)
        store.set_caps(self.raid_id, caps)
        await interaction.response.send_message(
            "✅ Role targets updated: "
            + ", ".join(f"{k} {v}" for k, v in caps.items())
            + f" (raid size {sum(caps.values())}).",
            ephemeral=True,
        )
        await refresh_raid_message(interaction.client, self.raid_id)


class DetailsModal(discord.ui.Modal, title="Raid details"):
    def __init__(
        self,
        raid_id: int,
        title_value: str,
        description: str | None,
        when: str,
        duration: str = "",
        timezone: str = "",
    ) -> None:
        super().__init__()
        self.raid_id = raid_id
        self.title_input = discord.ui.TextInput(
            label="Title", default=title_value, max_length=100, required=True
        )
        self.description_input = discord.ui.TextInput(
            label="Description (optional)",
            style=discord.TextStyle.paragraph,
            default=description or None,
            max_length=1000,
            required=False,
        )
        self.when_input = discord.ui.TextInput(
            label="Start time (server time, optional)",
            placeholder="20:30  ·  wed 20:30  ·  tomorrow 20:30  ·  blank to clear",
            default=when or None,
            max_length=40,
            required=False,
        )
        self.duration_input = discord.ui.TextInput(
            label="Duration (optional)",
            placeholder="3h  ·  2h30m  ·  90m  ·  blank to clear",
            default=duration or None,
            max_length=12,
            required=False,
        )
        self.timezone_input = discord.ui.TextInput(
            label="Realm region (optional)",
            placeholder="EU  ·  NA  ·  KR  ·  TW  ·  OCE  ·  or an IANA name",
            default=timezone or None,
            max_length=40,
            required=False,
        )
        for item in (
            self.title_input, self.description_input, self.when_input,
            self.duration_input, self.timezone_input,
        ):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        tz_name = str(self.timezone_input).strip() or None
        if tz_name and not is_known_timezone(tz_name):
            await interaction.response.send_message(
                f"`{tz_name}` isn't a region I recognise. Try `EU`, `NA`, `KR`, "
                "`TW`, `OCE`, or an IANA name like `Europe/Paris`.",
                ephemeral=True,
            )
            return

        raw_when = str(self.when_input).strip()
        starts_at: int | None = None
        if raw_when:
            starts_at, error = parse_when(raw_when, tz_name)
            if error:
                await interaction.response.send_message(error, ephemeral=True)
                return

        raw_duration = str(self.duration_input).strip()
        duration_minutes: int | None = None
        if raw_duration:
            duration_minutes, error = parse_duration(raw_duration)
            if error:
                await interaction.response.send_message(error, ephemeral=True)
                return
            if starts_at is None:
                await interaction.response.send_message(
                    "A duration needs a start time to run from.", ephemeral=True
                )
                return

        store = store_of(interaction)
        store.update_raid_details(
            self.raid_id, str(self.title_input).strip(), str(self.description_input).strip() or None
        )
        store.set_timezone(self.raid_id, tz_name)
        store.set_schedule(self.raid_id, starts_at, duration_minutes)

        if starts_at and duration_minutes:
            detail = (
                f" Starts <t:{starts_at}:R>, running {format_duration(duration_minutes)}."
            )
        elif starts_at:
            detail = f" Starts <t:{starts_at}:R>."
        else:
            detail = " Timer cleared."
        await interaction.response.send_message(f"✅ Raid details updated.{detail}", ephemeral=True)
        await refresh_raid_message(interaction.client, self.raid_id)


class RaidSettings(discord.ui.View):
    def __init__(self, raid_id: int) -> None:
        super().__init__(timeout=600)
        self.raid_id = raid_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if is_admin(interaction.user):
            return True
        await deny(interaction, "🔒 Admins only.")
        return False

    def _raid(self, interaction: discord.Interaction):
        return store_of(interaction).get_raid(self.raid_id)

    @discord.ui.button(label="Edit title / time", emoji="✏️", style=discord.ButtonStyle.primary)
    async def edit_details(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        raid = self._raid(interaction)
        when = format_local(raid.starts_at, raid.timezone) if raid.starts_at else ""
        duration = format_duration(raid.duration_minutes) if raid.duration_minutes else ""
        await interaction.response.send_modal(
            DetailsModal(
                self.raid_id, raid.title, raid.description, when, duration,
                region_label(raid.timezone),
            )
        )

    @discord.ui.button(label="Role targets", emoji="🎯", style=discord.ButtonStyle.primary)
    async def edit_caps(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        raid = self._raid(interaction)
        await interaction.response.send_modal(CapsModal(self.raid_id, raid.caps))

    @discord.ui.button(label="Lock / Unlock", emoji="🔒", style=discord.ButtonStyle.secondary)
    async def toggle_lock(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        raid = self._raid(interaction)
        new_state = RaidState.OPEN if raid.state is RaidState.LOCKED else RaidState.LOCKED
        store_of(interaction).set_raid_state(self.raid_id, new_state)
        await interaction.response.send_message(
            f"Raid is now **{new_state.value}**.", ephemeral=True
        )
        await refresh_raid_message(interaction.client, self.raid_id)

    @discord.ui.button(label="Cancel raid", emoji="🛑", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        store_of(interaction).set_raid_state(self.raid_id, RaidState.CANCELLED)
        await interaction.response.send_message("🛑 Raid cancelled.", ephemeral=True)
        await refresh_raid_message(interaction.client, self.raid_id)

    @discord.ui.button(label="Done", emoji="✔️", style=discord.ButtonStyle.secondary, row=1)
    async def done(self, interaction: discord.Interaction, _b: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="✔️ Closed.", embed=None, view=None)


async def open_raid_settings(interaction: discord.Interaction, raid_id: int) -> None:
    store = store_of(interaction)
    raid = store.get_raid(raid_id)
    embed = discord.Embed(
        title=f"⚙️ Settings — {raid.title}",
        description=(
            f"State: **{raid.state.value}**\n"
            f"Targets: "
            + ", ".join(f"{r.label} {raid.caps.get(r.value, 0)}" for r in ROLE_ORDER)
            + f"\nSize: **{sum(raid.caps.values())}**\n"
            + (
                f"Starts: <t:{raid.starts_at}:F> (<t:{raid.starts_at}:R>)"
                if raid.starts_at
                else "No start time set"
            )
            + (
                f"\nDuration: **{format_duration(raid.duration_minutes)}**"
                if raid.duration_minutes
                else ""
            )
            + f"\nRealm region: **{region_label(raid.timezone)}**"
        ),
        color=0x5865F2,
    )
    await interaction.response.send_message(
        embed=embed, view=RaidSettings(raid_id), ephemeral=True
    )
