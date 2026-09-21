"""Player-facing application flow.

Apply -> (cached profile? confirm : modal) -> class select -> spec select -> pending.

Discord caps a select menu at 25 options and there are 40 specs, so the spec
picker is two-step: pick class (13), then pick spec (3-4).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import discord

from ..data.specs import CLASSES, SPECS_BY_KEY, get_spec, specs_for_class
from ..emojis import registry
from ..store import Status
from ..config import region_label
from .common import (
    audit, derive_logs_url, handle_of, normalise_logs_url, refresh_raid_message,
    split_character, store_of,
)
from .embeds import build_profile_embed

OnPick = Callable[[discord.Interaction, str], Awaitable[None]]


class ClassSelect(discord.ui.Select):
    def __init__(self, picker: "SpecPickerView") -> None:
        self.picker = picker
        options = [
            discord.SelectOption(
                label=wow_class,
                value=wow_class,
                default=wow_class == picker.wow_class,
            )
            for wow_class in CLASSES
        ]
        super().__init__(placeholder="1️⃣ Choose your class…", options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        self.picker.wow_class = self.values[0]
        self.picker.spec_key = None
        self.picker.rebuild()
        await interaction.response.edit_message(view=self.picker)


class SpecSelect(discord.ui.Select):
    def __init__(self, picker: "SpecPickerView") -> None:
        self.picker = picker
        specs = specs_for_class(picker.wow_class) if picker.wow_class else []
        options = [
            discord.SelectOption(
                label=spec.name,
                value=spec.key,
                description=spec.role.label.rstrip("s"),
                emoji=registry.spec(spec.key) or None,
                default=spec.key == picker.spec_key,
            )
            for spec in specs
        ] or [discord.SelectOption(label="pick a class first", value="_none")]
        super().__init__(
            placeholder="2️⃣ Choose your spec…",
            options=options,
            disabled=not specs,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.picker.spec_key = self.values[0]
        self.picker.rebuild()
        await interaction.response.edit_message(view=self.picker)


class ConfirmSpecButton(discord.ui.Button):
    def __init__(self, picker: "SpecPickerView") -> None:
        self.picker = picker
        super().__init__(
            label="Confirm",
            style=discord.ButtonStyle.success,
            disabled=picker.spec_key is None,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.picker.spec_key is not None
        await self.picker.on_pick(interaction, self.picker.spec_key)
        self.picker.stop()


class SpecPickerView(discord.ui.View):
    """Ephemeral, per-user; never persisted, so a memory-held callback is fine."""

    def __init__(self, on_pick: OnPick, *, initial_spec_key: str | None = None) -> None:
        super().__init__(timeout=300)
        self.on_pick = on_pick
        self.spec_key = initial_spec_key
        spec = get_spec(initial_spec_key) if initial_spec_key else None
        self.wow_class = spec.wow_class if spec else None
        self.rebuild()

    def rebuild(self) -> None:
        self.clear_items()
        self.add_item(ClassSelect(self))
        self.add_item(SpecSelect(self))
        self.add_item(ConfirmSpecButton(self))


class ProfileModal(discord.ui.Modal):
    """Character name + optional Warcraft Logs link + optional note."""

    def __init__(
        self,
        raid_id: int,
        *,
        character_name: str = "",
        logs_url: str = "",
        spec_key: str | None = None,
        status: Status = Status.PENDING,
        note: str = "",
    ) -> None:
        super().__init__(title="Raid Application")
        self.raid_id = raid_id
        self.spec_key = spec_key
        # Carried through the whole flow so that "Backup" from a player the
        # bot has never seen still lands them on the bench, rather than
        # applying them and making them change it straight afterwards.
        self.status = status

        self.character = discord.ui.TextInput(
            label="Character name",
            placeholder="Mimz-Kazzak",
            default=character_name or None,
            max_length=32,
            required=True,
        )
        self.logs = discord.ui.TextInput(
            label="Warcraft Logs link (optional)",
            placeholder="https://www.warcraftlogs.com/character/eu/kazzak/mimz",
            default=logs_url or None,
            max_length=300,
            required=False,
        )
        self.note = discord.ui.TextInput(
            label="Note for the raid lead (optional)",
            style=discord.TextStyle.paragraph,
            placeholder="Can only make it after 20:30",
            default=note or None,
            max_length=200,
            required=False,
        )
        for item in (self.character, self.logs, self.note):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name = str(self.character).strip()
        logs_raw = str(self.logs)
        note = str(self.note).strip() or None

        logs_url, logs_error = normalise_logs_url(logs_raw)
        problems: list[str] = []
        if split_character(name) is None:
            problems.append(
                "• Enter your character as **Name-Server** — e.g. `Mimz-Kazzak`. "
                "The server is what links your Warcraft Logs."
            )
        if logs_error:
            problems.append("• " + logs_error)
        if problems:
            # Discord closes a modal on submit and cannot open another from a
            # modal, so we hand back a button that reopens this one pre-filled
            # with everything they typed - they fix the one field, not start over.
            await interaction.response.send_message(
                "That didn't go through:\n\n" + "\n".join(problems),
                view=_FixApplicationView(
                    self.raid_id, name, logs_raw, str(self.note), self.spec_key, self.status
                ),
                ephemeral=True,
            )
            return

        async def finish(spec_interaction: discord.Interaction, spec_key: str) -> None:
            await submit_application(
                spec_interaction,
                raid_id=self.raid_id,
                character_name=name,
                logs_url=logs_url,
                spec_key=spec_key,
                note=note,
                status=self.status,
            )

        await interaction.response.send_message(
            content=f"**{discord.utils.escape_markdown(name)}** — now pick your spec:",
            view=SpecPickerView(finish, initial_spec_key=self.spec_key),
            ephemeral=True,
        )


class _FixApplicationView(discord.ui.View):
    """Shown when an application fails validation: one button to reopen the
    modal pre-filled, so the player edits the bad field instead of retyping."""

    def __init__(self, raid_id, character, logs, note, spec_key, status) -> None:
        super().__init__(timeout=300)
        self.raid_id = raid_id
        self.character = character
        self.logs = logs
        self.note = note
        self.spec_key = spec_key
        self.status = status

    @discord.ui.button(label="Fix and resubmit", emoji="✏️", style=discord.ButtonStyle.primary)
    async def fix(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            ProfileModal(
                self.raid_id,
                character_name=self.character,
                logs_url=self.logs,
                spec_key=self.spec_key,
                status=self.status,
                note=self.note,
            )
        )
        self.stop()


async def submit_application(
    interaction: discord.Interaction,
    *,
    raid_id: int,
    character_name: str,
    logs_url: str | None,
    spec_key: str,
    note: str | None,
    status: Status = Status.PENDING,
) -> None:
    """Persist the signup, cache the profile, refresh the roster."""
    store = store_of(interaction)
    if spec_key not in SPECS_BY_KEY:
        await interaction.response.send_message("Unknown spec, please retry.", ephemeral=True)
        return

    raid = store.get_raid(raid_id)
    # Read before the upsert overwrites it, so the log can say whether this was
    # a first signup or someone changing their mind.
    existing = store.get_signup(raid_id, interaction.user.id)
    # Auto-accept only ever promotes Pending. Someone who deliberately signed up
    # as Bench, Absent or Tentative has said something specific about their
    # availability, and accepting them over that would be wrong.
    if raid is not None and raid.auto_accept and status is Status.PENDING:
        status = Status.ACCEPTED

    # No link given, but a "Name-Realm" character and a known raid region are
    # enough to point one at Warcraft Logs. The stored profile keeps whatever
    # the player actually typed (usually nothing), so the next raid re-derives
    # against *its* region rather than baking this raid's in.
    provided = logs_url
    if not provided and raid is not None:
        logs_url = derive_logs_url(character_name, region_label(raid.timezone))

    store.upsert_signup(
        raid_id=raid_id,
        user_id=interaction.user.id,
        character_name=character_name,
        logs_url=logs_url,
        spec_key=spec_key,
        status=status,
        note=note,
        updated_by=interaction.user.id,
        discord_name=handle_of(interaction.user),
    )
    # Remembered for next time - this is what makes re-applying one click.
    store.save_player(interaction.user.id, character_name, provided, spec_key)

    spec = get_spec(spec_key)
    audit(
        interaction,
        raid_id,
        "apply",
        target_id=interaction.user.id,
        target_name=handle_of(interaction.user),
        detail=(
            f"{character_name} — {spec.full_name if spec else spec_key} — {status.label}"
            + ("" if existing is None else f" (was {existing.status.label})")
        ),
    )

    await interaction.response.edit_message(
        content=f"{status.emoji} You're **{status.label}** for this raid.",
        embed=build_profile_embed(character_name, spec_key, logs_url),
        view=None,
    )
    await refresh_raid_message(interaction.client, raid_id)


#: Offered alongside Apply wherever someone signs themselves up.
SELF_SERVICE: tuple[tuple[Status, str, str], ...] = (
    (Status.TENTATIVE, "Tentative / late", "❔"),
    (Status.BENCH, "Backup", "⭐"),
    (Status.ABSENT, "Absent", "🚫"),
)


class _SignUpAs(discord.ui.Button):
    """Sign up with the cached character, but not as Pending.

    Saves the round trip of applying and then immediately correcting yourself,
    which is what everyone was doing when they already knew they'd be late.
    """

    def __init__(self, status: Status, label: str, emoji: str) -> None:
        self.status = status
        super().__init__(label=label, emoji=emoji, style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        view: "CachedProfileView" = self.view  # type: ignore[assignment]
        await submit_application(
            interaction,
            raid_id=view.raid_id,
            character_name=view.player.character_name,
            logs_url=view.player.logs_url,
            spec_key=view.player.spec_key,
            note=None,
            status=self.status,
        )
        view.stop()


class CachedProfileView(discord.ui.View):
    """Offered when we already know the player: one click to re-apply."""

    def __init__(self, raid_id: int, player) -> None:
        super().__init__(timeout=300)
        self.raid_id = raid_id
        self.player = player
        for status, label, emoji in SELF_SERVICE:
            self.add_item(_SignUpAs(status, label, emoji))

    @discord.ui.button(
        label="Apply with these details", emoji="📝", style=discord.ButtonStyle.success, row=0
    )
    async def use_cached(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await submit_application(
            interaction,
            raid_id=self.raid_id,
            character_name=self.player.character_name,
            logs_url=self.player.logs_url,
            spec_key=self.player.spec_key,
            note=None,
        )
        self.stop()

    @discord.ui.button(label="Change spec only", style=discord.ButtonStyle.primary, row=2)
    async def change_spec(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        async def finish(spec_interaction: discord.Interaction, spec_key: str) -> None:
            await submit_application(
                spec_interaction,
                raid_id=self.raid_id,
                character_name=self.player.character_name,
                logs_url=self.player.logs_url,
                spec_key=spec_key,
                note=None,
            )

        await interaction.response.edit_message(
            content="Pick your spec for this raid:",
            embed=None,
            view=SpecPickerView(finish, initial_spec_key=self.player.spec_key),
        )
        self.stop()

    @discord.ui.button(label="Edit everything", style=discord.ButtonStyle.secondary, row=2)
    async def edit_all(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            ProfileModal(
                self.raid_id,
                character_name=self.player.character_name,
                logs_url=self.player.logs_url or "",
                spec_key=self.player.spec_key,
            )
        )
        self.stop()


async def start_application(
    interaction: discord.Interaction, raid_id: int, status: Status = Status.PENDING
) -> None:
    """Entry point from Apply, and from Bench/Absent/Tentative for new players.

    `status` is where an unknown player lands once they've given their details -
    pressing "Backup" as a first-timer should set them backup, not apply them.
    """
    store = store_of(interaction)
    player = store.get_player(interaction.user.id)
    # A brand-new player, or one whose saved character predates the Name-Server
    # rule, gets the modal (pre-filled) so they supply the realm rather than
    # quietly re-applying with a flat name.
    if player is None or split_character(player.character_name) is None:
        await interaction.response.send_modal(ProfileModal(
            raid_id, status=status,
            character_name=player.character_name if player else "",
            logs_url=(player.logs_url or "") if player else "",
            spec_key=player.spec_key if player else None,
        ))
        return

    spec = get_spec(player.spec_key)
    existing = store.get_signup(raid_id, interaction.user.id)

    lines = [
        f"{registry.spec(player.spec_key)} "
        f"**{discord.utils.escape_markdown(player.character_name)}** — "
        f"{spec.full_name if spec else player.spec_key}",
        (f"[Warcraft Logs]({player.logs_url})" if player.logs_url else "*no logs saved*"),
    ]
    if existing is not None:
        # Without this, someone who benched or absented themselves sees a plain
        # "apply again?" card, assumes the click alone re-applied them, and
        # never presses confirm - so their status silently never changes.
        lines.append(
            f"\n{existing.status.emoji} You are currently **{existing.status.label}** "
            f"for this raid. Confirming below puts you back to 🕓 Pending."
        )

    embed = discord.Embed(
        title="Apply again with your saved character?",
        description="\n".join(lines),
        color=spec.color if spec else 0x5865F2,
    )
    embed.set_footer(
        text="Nothing changes until you press a button below. "
        "Not sure you'll make it? Sign up as Tentative, Backup or Absent instead."
    )
    await interaction.response.send_message(
        embed=embed, view=CachedProfileView(raid_id, player), ephemeral=True
    )
