"""Slash commands for creating and inspecting raids."""

from __future__ import annotations

import functools
import logging
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from ..config import DEFAULT_CAPS, PRIMARY_REGIONS
from ..data.specs import ROLE_ORDER, get_spec
from ..emojis import registry
from ..store import Status
from ..ui.admin import open_raid_settings, open_roster_manager, send_manager_link
from ..ui.common import deny, is_admin

if TYPE_CHECKING:
    from ..client import RaidClient

log = logging.getLogger(__name__)
from ..ui.embeds import build_raid_embed
from ..ui.panel import RaidView
from ..ui.schedule import (
    is_known_timezone,
    parse_duration,
    parse_when,
    suggest_duration,
    suggest_timezone,
    suggest_when,
)


def autocomplete_guard(name: str):
    """Time every autocomplete call, and degrade instead of erroring.

    Discord allows a callback three seconds and shows the same "Loading options
    failed" for every way of missing that - an exception, a slow reply, an
    oversized payload - without telling the bot which happened. Worse, a
    callback that never gets invoked looks identical from the user's side.

    So: log the timing of each call, log a traceback if one escapes, and return
    an empty list rather than propagating. An empty dropdown lets the raid lead
    type the value by hand, which every one of these fields already accepts.
    """

    def decorate(fn):
        @functools.wraps(fn)
        async def wrapper(self, interaction: discord.Interaction, current: str):
            started = time.perf_counter()
            try:
                choices = await fn(self, interaction, current)
            except Exception:
                log.exception("autocomplete %s failed on %r", name, current)
                return []
            elapsed = (time.perf_counter() - started) * 1000
            # Anything approaching Discord's 3s budget is the interesting case.
            level = logging.WARNING if elapsed > 1000 else logging.INFO
            log.log(
                level, "autocomplete %s: %r -> %d choices in %.1fms",
                name, current, len(choices), elapsed,
            )
            return choices

        return wrapper

    return decorate


def admin_only():
    async def predicate(interaction: discord.Interaction) -> bool:
        if is_admin(interaction.user):
            return True
        raise app_commands.CheckFailure("admin_only")

    return app_commands.check(predicate)


class RaidCog(commands.Cog):
    raid = app_commands.Group(
        name="raid", description="Create and manage raid signups", guild_only=True
    )

    def __init__(self, bot: "RaidClient") -> None:
        self.bot = bot

    # ------------------------------------------------------------------ create

    @raid.command(name="create", description="Post a new raid signup board (admin only)")
    @app_commands.describe(
        title="Raid title, e.g. 'Manaforge Omega — Mythic'",
        description="(optional) Blurb: loot rules, invite time, requirements…",
        when="(optional) Server time: '20:30', 'wed 20:30', 'tomorrow 20:30', 'in 90m'",
        duration="(optional) How long the raid runs: '3h', '2h30m', '90m'",
        timezone="(optional) EU, NA, KR, TW, OCE. Defaults to your last raid's region",
        tanks="(optional) Target tank count. Default 2",
        healers="(optional) Target healer count. Default 4",
        melee="(optional) Target melee count. Default 7, and unused if you set dps",
        ranged="(optional) Target ranged count. Default 7, and unused if you set dps",
        dps="(optional) One combined DPS target replacing melee and ranged, e.g. 14",
        auto_accept="(optional) Accept every application immediately. Default false",
    )
    @admin_only()
    async def create(
        self,
        interaction: discord.Interaction,
        # Length-bounded at the Discord level so the embed can never exceed the
        # API's title/description limits and silently fail to render.
        title: app_commands.Range[str, 1, 100],
        description: app_commands.Range[str, 1, 1000] | None = None,
        when: str | None = None,
        duration: str | None = None,
        timezone: str | None = None,
        tanks: app_commands.Range[int, 0, 40] = DEFAULT_CAPS["tank"],
        healers: app_commands.Range[int, 0, 40] = DEFAULT_CAPS["healer"],
        # These default to None rather than DEFAULT_CAPS so that "left blank"
        # can be told apart from "deliberately set", which is what makes the
        # clash with `dps` detectable instead of silently ignored.
        melee: app_commands.Range[int, 0, 40] | None = None,
        ranged: app_commands.Range[int, 0, 40] | None = None,
        dps: app_commands.Range[int, 0, 80] | None = None,
        auto_accept: bool = False,
    ) -> None:
        if dps is not None and (melee is not None or ranged is not None):
            await interaction.response.send_message(
                "Set **either** `dps` **or** `melee`/`ranged`, not both — `dps` is the "
                "combined target that replaces the two.",
                ephemeral=True,
            )
            return

        if dps is not None:
            caps = {"tank": tanks, "healer": healers, "dps": dps}
        else:
            caps = {
                "tank": tanks,
                "healer": healers,
                "melee": DEFAULT_CAPS["melee"] if melee is None else melee,
                "ranged": DEFAULT_CAPS["ranged"] if ranged is None else ranged,
            }

        store = self.bot.store
        # Default to whatever this guild's last raid used, so a raid lead sets
        # their region once rather than on every raid.
        tz_name = timezone or store.last_timezone(interaction.guild_id)
        if tz_name and not is_known_timezone(tz_name):
            await interaction.response.send_message(
                f"`{tz_name}` isn't a region I recognise. Use one of "
                f"{', '.join(PRIMARY_REGIONS)}, or any IANA name like `Europe/Paris`.",
                ephemeral=True,
            )
            return

        starts_at: int | None = None
        if when:
            starts_at, error = parse_when(when, tz_name)
            if error:
                await interaction.response.send_message(error, ephemeral=True)
                return

        duration_minutes: int | None = None
        if duration:
            duration_minutes, error = parse_duration(duration)
            if error:
                await interaction.response.send_message(error, ephemeral=True)
                return
            if starts_at is None:
                await interaction.response.send_message(
                    "A `duration` needs a `when` to run from — set a start time too.",
                    ephemeral=True,
                )
                return

        raid = store.create_raid(
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            title=title,
            description=description,
            leader_id=interaction.user.id,
            starts_at=starts_at,
            duration_minutes=duration_minutes,
            timezone=tz_name,
            caps=caps,
            auto_accept=auto_accept,
        )

        await interaction.response.send_message(
            embed=build_raid_embed(raid, []), view=RaidView()
        )
        message = await interaction.original_response()
        store.set_raid_message(raid.id, message.id)

    # Discord can't pre-fill a slash option, but autocomplete fires as soon as
    # the field is focused - so an admin sees ready-made choices and can either
    # pick one or keep typing their own.
    @create.autocomplete("when")
    @autocomplete_guard("when")
    async def when_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        # Suggest in the region this guild actually raids in, so "Today, 20:00"
        # means 20:00 on their realm rather than the bot host's.
        tz_name = self.bot.store.last_timezone(interaction.guild_id)
        return [
            app_commands.Choice(name=label, value=value)
            for label, value in suggest_when(current, tz_name=tz_name)
        ]

    @create.autocomplete("timezone")
    @autocomplete_guard("timezone")
    async def timezone_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return [
            app_commands.Choice(name=label, value=value)
            for label, value in suggest_timezone(current)
        ]

    @create.autocomplete("duration")
    @autocomplete_guard("duration")
    async def duration_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        return [
            app_commands.Choice(name=label, value=value)
            for label, value in suggest_duration(current)
        ]

    # -------------------------------------------------------------------- info

    @raid.command(name="list", description="List recent raids in this server")
    async def list_raids(self, interaction: discord.Interaction) -> None:
        store = self.bot.store
        raids = store.open_raids(interaction.guild_id)
        if not raids:
            await interaction.response.send_message("No raids yet.", ephemeral=True)
            return

        lines = []
        for raid in raids:
            accepted = len(store.signups(raid.id, Status.ACCEPTED))
            when = f" · <t:{raid.starts_at}:R>" if raid.starts_at else ""
            link = (
                f"https://discord.com/channels/{raid.guild_id}/{raid.channel_id}/{raid.message_id}"
                if raid.message_id
                else ""
            )
            title = f"[{raid.title}]({link})" if link else raid.title
            lines.append(
                f"`#{raid.id}` **{title}** — {accepted}/{sum(raid.caps.values())} "
                f"· {raid.state.value}{when}"
            )
        await interaction.response.send_message(
            embed=discord.Embed(title="Raids", description="\n".join(lines), color=0x5865F2),
            ephemeral=True,
        )

    @raid.command(name="manage", description="Open the roster manager (admin only)")
    @app_commands.describe(raid_id="Defaults to the most recent open raid")
    @admin_only()
    async def manage(self, interaction: discord.Interaction, raid_id: int | None = None) -> None:
        raid = await self._resolve(interaction, raid_id)
        if raid:
            await open_roster_manager(interaction, raid.id)

    @raid.command(name="page", description="Get a private link to the web roster manager (admin only)")
    @app_commands.describe(raid_id="Defaults to the most recent open raid")
    @admin_only()
    async def page(self, interaction: discord.Interaction, raid_id: int | None = None) -> None:
        raid = await self._resolve(interaction, raid_id)
        if raid:
            await send_manager_link(interaction, raid.id)

    @raid.command(name="settings", description="Edit title, time, targets, lock (admin only)")
    @app_commands.describe(raid_id="Defaults to the most recent open raid")
    @admin_only()
    async def settings(self, interaction: discord.Interaction, raid_id: int | None = None) -> None:
        raid = await self._resolve(interaction, raid_id)
        if raid:
            await open_raid_settings(interaction, raid.id)

    @raid.command(name="repost", description="Re-post the signup board (admin only)")
    @admin_only()
    async def repost(self, interaction: discord.Interaction, raid_id: int | None = None) -> None:
        raid = await self._resolve(interaction, raid_id)
        if raid is None:
            return
        store = self.bot.store
        await interaction.response.send_message(
            embed=build_raid_embed(raid, store.signups(raid.id)), view=RaidView()
        )
        message = await interaction.original_response()
        store.set_raid_message(raid.id, message.id)

    # ----------------------------------------------------------------- profile

    @app_commands.command(name="profile", description="Show or clear your remembered character")
    @app_commands.guild_only()
    async def profile(self, interaction: discord.Interaction, clear: bool = False) -> None:
        store = self.bot.store
        if clear:
            store.delete_player(interaction.user.id)
            await interaction.response.send_message(
                "🗑️ Cleared. Your next application will ask for details again.", ephemeral=True
            )
            return

        player = store.get_player(interaction.user.id)
        if player is None:
            await interaction.response.send_message(
                "No saved character yet — apply to a raid once and I'll remember it.",
                ephemeral=True,
            )
            return

        spec = get_spec(player.spec_key)
        embed = discord.Embed(
            title="Your saved character",
            description=(
                f"{registry.spec(player.spec_key)} **{player.character_name}** — "
                f"{spec.full_name if spec else player.spec_key}\n"
                + (f"[Warcraft Logs]({player.logs_url})" if player.logs_url else "*no logs saved*")
            ),
            color=spec.color if spec else 0x5865F2,
        )
        embed.set_footer(text="Used to pre-fill every future application.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # -------------------------------------------------------------------- help

    @app_commands.command(name="help", description="How to use the raid bot")
    async def help_command(self, interaction: discord.Interaction) -> None:
        admin = is_admin(interaction.user)
        embed = discord.Embed(
            title="📖 Raid Bot — Help",
            description=(
                "Sign up for raids with your character, spec and Warcraft Logs link. "
                "The board tracks your comp and shows which raid buffs you're still missing."
            ),
            color=0x5865F2,
        )

        embed.add_field(
            name="🧑 For players — use the buttons on the raid board",
            value=(
                "**📝 Apply** — first time asks for your character name, logs link "
                "(optional) and a note, then class → spec. After that it's remembered, "
                "so re-applying next raid is one click.\n"
                "**🪑 Bench me** — put yourself on the bench.\n"
                "**🚫 Absent** — mark yourself out for this raid.\n"
                "**🗑️ Withdraw** — remove yourself entirely.\n\n"
                "Your application sits as **🕓 Pending** until a raid lead accepts it."
            ),
            inline=False,
        )

        embed.add_field(
            name="🧑 For players — commands",
            value=(
                "`/profile` — show the character I've remembered for you\n"
                "`/profile clear:True` — forget it and start fresh\n"
                "`/help` — this message"
            ),
            inline=False,
        )

        if admin:
            embed.add_field(
                name="👑 For raid leads — commands",
                value=(
                    "`/raid create` — post a new signup board.\n"
                    "• `title` required, `description` optional\n"
                    "• `when` optional — `20:30`, `sat 19:00`, `tomorrow 20:30`, "
                    "`2026-07-22 20:30`, or `in 90m`\n"
                    "• `duration` optional — `3h`, `2h30m`, `90m`\n"
                    "• both fields suggest options as soon as you click them\n"
                    "• `tanks` `healers` `melee` `ranged` set your role targets\n\n"
                    "`/raid manage` — accept / decline / bench / absent, change someone's "
                    "spec, or remove them\n"
                    "`/raid settings` — edit title, description, start time, role targets; "
                    "lock or cancel the raid\n"
                    "`/raid list` — recent raids with jump links\n"
                    "`/raid repost` — post the board again if it's buried"
                ),
                inline=False,
            )
            embed.add_field(
                name="👑 Buttons on the board",
                value=(
                    "**🛠️ Manage roster** and **⚙️ Raid settings** do the same as the "
                    "commands above, without typing. Both are admin-only and everything "
                    "you see there is private to you."
                ),
                inline=False,
            )

        embed.add_field(
            name="⏰ Raid timer",
            value=(
                "Times are entered and shown in **server time**. The board also shows a "
                "live countdown and each player's own local time, so nobody has to do "
                "timezone maths.\n"
                "The accepted roster gets pinged **10 minutes** before start."
            ),
            inline=False,
        )

        embed.add_field(
            name="✨ Buff tracking",
            value=(
                "The board shows what your accepted roster covers, with a count of how "
                "many people bring each one:\n"
                "`Mark of the Wild 2` · `Hunter's Mark 1` · `Lust 2`\n"
                "Anything at zero shows under **⚠️ Missing Raid Buffs**. Accept a Holy "
                "Priest and *Fortitude* stops being missing.\n"
                "Some entries upgrade — any Death Knight brings **Grip**, but a *Blood* "
                "DK brings **Mass Grip**."
            ),
            inline=False,
        )

        embed.set_footer(
            text="Admin-only actions are hidden from non-admins."
            if not admin
            else "You have admin access to this bot."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------ shared

    async def _resolve(self, interaction: discord.Interaction, raid_id: int | None):
        store = self.bot.store
        raid = store.get_raid(raid_id) if raid_id else store.latest_open_raid(interaction.guild_id)
        if raid is None:
            await interaction.response.send_message(
                "No open raid found. Create one with `/raid create`.", ephemeral=True
            )
            return None
        if raid.guild_id != interaction.guild_id:
            await interaction.response.send_message("That raid isn't in this server.", ephemeral=True)
            return None
        return raid

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CheckFailure):
            await deny(interaction, "🔒 That command is admin-only.")
            return
        # Never leave the user staring at "the application did not respond",
        # and never leak an internal traceback into the channel.
        log.exception("command %s failed", getattr(interaction.command, "name", "?"), exc_info=error)
        await deny(
            interaction, "Something went wrong running that command. It has been logged."
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RaidCog(bot))  # type: ignore[arg-type]
