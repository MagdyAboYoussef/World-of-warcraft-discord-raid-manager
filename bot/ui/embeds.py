"""Roster embed rendering: comp by role, buff coverage panel, footer counts."""

from __future__ import annotations

import logging
import time

import discord

from ..config import region_label
from ..data import buffs as buffs_data
from ..data.specs import ROLE_ORDER, get_spec
from ..emojis import registry
from ..store import Raid, RaidState, Signup, Status
from .schedule import format_clock, format_display, format_duration

log = logging.getLogger(__name__)

# Discord's hard limits. Exceeding any of these makes the API reject the whole
# message, so a big roster would silently stop the board updating entirely.
FIELD_LIMIT = 1024
TITLE_LIMIT = 256
DESCRIPTION_LIMIT = 4096
TOTAL_LIMIT = 6000

# Board colour tells you the raid's phase at a glance. Tunable thresholds:
#   • 20+ accepted            → green  "rostered, good to go"
#   • starts within 3 days    → orange "raid soon, still filling"
#   • starts >3 days out / TBD → grey   "plenty of time"
#   • past its end time        → red    "[COMPLETED]"
READY_ACCEPTED = 20
SOON_DAYS = 3
#: A raid with no explicit duration is treated as this long, only for deciding
#: when it has finished. Three hours is a typical raid night.
ASSUMED_RAID_MINUTES = 180

COLOR_READY = 0x2B9E5F      # green
COLOR_SOON = 0xD9822B       # orange
COLOR_FAR = 0x99AAB5        # grey
COLOR_LOCKED = 0x5865F2     # blurple
COLOR_CANCELLED = 0x992D22  # dark red
COLOR_COMPLETED = 0xA83232  # red — the raid is over


def clamp(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def clamp_title(text: str) -> str:
    return clamp(text, TITLE_LIMIT)


def _spec_of(signup: Signup):
    return get_spec(signup.spec_key)


def _sorted(signups: list[Signup]) -> list[Signup]:
    """Stable, alphabetical order.

    The store returns rows by updated_at, which would make the roster visibly
    reshuffle every time anyone's status changed.
    """
    return sorted(signups, key=lambda s: s.character_name.casefold())


def _roster_line(signup: Signup, *, show_status: bool = False) -> str:
    spec = _spec_of(signup)
    icon = registry.spec(signup.spec_key) or "•"
    name = discord.utils.escape_markdown(signup.character_name)
    name = f"[**{name}**]({signup.logs_url})" if signup.logs_url else f"**{name}**"
    spec_text = spec.full_name if spec else signup.spec_key
    prefix = f"{signup.status.emoji} " if show_status else ""
    return f"{prefix}{icon} {name} — {spec_text}"


def _fit(lines: list[str], empty: str = "*—*") -> str:
    """Join lines, trimming to Discord's field limit with an overflow marker."""
    if not lines:
        return empty
    out: list[str] = []
    used = 0
    for i, line in enumerate(lines):
        remaining = len(lines) - i
        marker = f"\n*…+{remaining} more*"
        if used + len(line) + 1 + len(marker) > FIELD_LIMIT:
            out.append(marker.strip())
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


def _buff_panel(accepted: list[Signup]) -> tuple[str, str]:
    """Return (missing_text, covered_text) for the buff coverage fields."""
    specs = [s for s in (_spec_of(a) for a in accepted) if s is not None]
    statuses = buffs_data.evaluate(specs)

    missing = buffs_data.missing(statuses)
    covered = buffs_data.covered(statuses)

    def missing_icon(status: buffs_data.BuffStatus) -> str:
        """Class icon where one class owns the buff, else the buff's own icon —
        the panel is really answering 'which class do we still need?'."""
        if status.definition.wow_class:
            icon = registry.wow_class(status.definition.wow_class)
            if icon:
                return icon
        return registry.buff(status.emoji_name) or "❌"

    def missing_items() -> list[str]:
        """One entry per *class* we still need, not per buff.

        A Druid brings both Mark of the Wild and Innervate, so listing them
        separately repeats the druid icon and reads like a duplicate. Buffs
        several classes can cover (Lust, Combat Res, AS Slow) have no owning
        class, so they keep their own icon and never merge.
        """
        order: list[str] = []
        icons: dict[str, str] = {}
        labels: dict[str, list[str]] = {}
        for s in missing:
            key = s.definition.wow_class or f"_{s.definition.key}"
            if key not in labels:
                order.append(key)
                labels[key] = []
                icons[key] = missing_icon(s)
            labels[key].append(s.definition.missing_label)
        return [f"{icons[k]} {' / '.join(labels[k])}" for k in order]

    missing_text = (
        _fit_inline(missing_items())
        if missing
        else "✨ **All raid buffs covered**"
    )
    covered_text = _fit_inline(
        [f"{registry.buff(s.emoji_name) or '•'} {s.label} `{s.count}`" for s in covered]
    )
    return missing_text, covered_text


def _fit_inline(items: list[str], sep: str = "  ", empty: str = "*—*") -> str:
    """Join items on one line within the field limit, dropping whole items.

    Slicing the joined string instead would cut through a `<:name:id>` emoji
    token and leave raw text in the embed.
    """
    if not items:
        return empty
    out: list[str] = []
    used = 0
    for item in items:
        if used + len(item) + len(sep) > FIELD_LIMIT:
            break
        out.append(item)
        used += len(item) + len(sep)
    return sep.join(out) if out else empty


def _raid_appearance(raid: Raid, accepted_count: int) -> tuple[int, str]:
    """Return (colour, title prefix) for the raid's current phase.

    Cancelled and completed are terminal and win over everything; a locked raid
    keeps its own colour; otherwise the applying-phase colour is driven by how
    full the roster is and how soon the raid starts.
    """
    now = int(time.time())
    if raid.state is RaidState.CANCELLED:
        return COLOR_CANCELLED, "[CANCELLED] "
    if raid.starts_at is not None:
        ends_at = raid.starts_at + (raid.duration_minutes or ASSUMED_RAID_MINUTES) * 60
        if now >= ends_at:
            return COLOR_COMPLETED, "[COMPLETED] "
    if raid.state is RaidState.LOCKED:
        return COLOR_LOCKED, "[LOCKED] "
    if accepted_count >= READY_ACCEPTED:
        return COLOR_READY, ""
    if raid.starts_at is not None and raid.starts_at - now <= SOON_DAYS * 86400:
        return COLOR_SOON, ""
    return COLOR_FAR, ""


def build_raid_embed(raid: Raid, signups: list[Signup]) -> discord.Embed:
    by_status: dict[Status, list[Signup]] = {s: [] for s in Status}
    for signup in signups:
        by_status[signup.status].append(signup)

    accepted = by_status[Status.ACCEPTED]
    total_cap = sum(raid.caps.values())

    color, title_prefix = _raid_appearance(raid, len(accepted))
    title = f"{title_prefix}{raid.title}"

    description_parts: list[str] = []
    if raid.description:
        # Leave room for the schedule and raid-lead lines appended below.
        description_parts.append(clamp(raid.description, DESCRIPTION_LIMIT - 500))
    if raid.starts_at:
        # Server time is stated explicitly because Discord's <t:> markup renders
        # in each viewer's own timezone - great for players abroad, but the raid
        # lead still needs one canonical time everyone can agree on.
        window = format_display(raid.starts_at, raid.timezone)
        runtime = ""
        if raid.duration_minutes:
            ends_at = raid.starts_at + raid.duration_minutes * 60
            window += f" – {format_clock(ends_at, raid.timezone)}"
            runtime = f" · runs {format_duration(raid.duration_minutes)}"
        description_parts.append(
            f"🕒 **{window}** {region_label(raid.timezone)} server time{runtime}\n"
            f"⏳ Starts <t:{raid.starts_at}:R> · 🌍 your time <t:{raid.starts_at}:f>"
        )
    description_parts.append(f"👑 Raid Lead: <@{raid.leader_id}>")

    embed = discord.Embed(
        title=clamp_title(title),
        description=clamp("\n\n".join(description_parts), DESCRIPTION_LIMIT),
        color=color,
    )

    # --- comp, one field per role ---
    for role in ROLE_ORDER:
        members = _sorted([s for s in accepted if (sp := _spec_of(s)) and sp.role is role])
        cap = raid.caps.get(role.value, 0)
        icon = registry.role(role.value)
        embed.add_field(
            name=f"{icon} {role.label} ({len(members)}/{cap})",
            value=_fit([_roster_line(m) for m in members]),
            inline=False,
        )

    # --- buff coverage ---
    missing_text, covered_text = _buff_panel(accepted)
    embed.add_field(name="⚠️ Missing Raid Buffs", value=missing_text, inline=False)
    embed.add_field(name="✅ Available Buffs", value=covered_text, inline=False)

    # --- queues ---
    pending = by_status[Status.PENDING]
    if pending:
        embed.add_field(
            name=f"🕓 Pending ({len(pending)})",
            value=_fit([_roster_line(p) for p in _sorted(pending)]),
            inline=False,
        )

    side: list[tuple[str, list[Signup]]] = [
        ("🪑 Bench", by_status[Status.BENCH]),
        ("🚫 Absent", by_status[Status.ABSENT]),
        ("❌ Declined", by_status[Status.DECLINED]),
    ]
    for label, members in side:
        if members:
            embed.add_field(
                name=f"{label} ({len(members)})",
                value=_fit([_roster_line(m) for m in _sorted(members)]),
                inline=True,
            )

    embed.set_footer(
        text=f"Raid #{raid.id} · {len(accepted)}/{total_cap} accepted · "
        f"{len(pending)} pending · {len(signups)} signed up"
    )
    return _within_total_limit(embed)


def _within_total_limit(embed: discord.Embed) -> discord.Embed:
    """Drop optional trailing fields until the embed fits Discord's 6000 total.

    A large guild can otherwise build a roster that the API refuses outright,
    which would freeze the board rather than merely truncate it. The comp and
    buff panel are the point of the board, so the side queues go first.
    """
    droppable = ("❌ Declined", "🚫 Absent", "🪑 Bench", "🕓 Pending")
    for name_prefix in droppable:
        if len(embed) <= TOTAL_LIMIT:
            break
        for index, field in enumerate(embed.fields):
            if field.name and field.name.startswith(name_prefix):
                embed.remove_field(index)
                break
    if len(embed) > TOTAL_LIMIT:
        log.warning("raid embed still %d chars after trimming optional fields", len(embed))
    return embed


def build_profile_embed(character_name: str, spec_key: str, logs_url: str | None) -> discord.Embed:
    """Small confirmation card shown to a player after they apply."""
    spec = get_spec(spec_key)
    embed = discord.Embed(
        title="Application submitted",
        description=(
            f"{registry.spec(spec_key)} **{discord.utils.escape_markdown(character_name)}** — "
            f"{spec.full_name if spec else spec_key}"
        ),
        color=spec.color if spec else 0x5865F2,
    )
    embed.add_field(name="Status", value="🕓 Pending raid lead review", inline=True)
    embed.add_field(
        name="Warcraft Logs",
        value=f"[View profile]({logs_url})" if logs_url else "*not provided*",
        inline=True,
    )
    embed.set_footer(text="Saved — your next application will pre-fill with these details.")
    return embed
