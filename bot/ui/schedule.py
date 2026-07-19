"""Raid start-time parsing and the live countdown.

The "timer" is deliberately not a bot-side ticking clock. The raid's start is
stored as a unix epoch and rendered with Discord's own `<t:...:R>` markup, which
counts down live in every viewer's local timezone with zero edits from us. A
single background task exists only to fire the one-off reminders.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord.ext import tasks

from ..config import PRIMARY_REGIONS, RAID_TIMEZONE, REGION_LABELS, resolve_timezone
from ..store import Raid, RaidState, Status
from .common import SAFE_MENTIONS

if TYPE_CHECKING:
    from ..client import RaidClient

log = logging.getLogger(__name__)

RELATIVE_RE = re.compile(r"^in\s+(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?$", re.IGNORECASE)
ABSOLUTE_FORMATS = ("%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M")
CLOCK_RE = re.compile(r"^(?P<hour>\d{1,2}):(?P<minute>\d{2})$")
#: "wed 20:30", "wednesday 20:30", "today 20:30", "tomorrow 20:30"
DAY_CLOCK_RE = re.compile(
    r"^(?P<word>[a-z]+)\s+(?P<hour>\d{1,2}):(?P<minute>\d{2})$", re.IGNORECASE
)

WEEKDAYS = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}

#: Minutes before start at which to ping the accepted roster.
REMINDER_OFFSETS = (60, 10)


def _tz(name: str | None = None) -> ZoneInfo | timezone:
    """Resolve a per-raid region override, falling back to the configured default."""
    resolved = resolve_timezone(name)
    try:
        return ZoneInfo(resolved)
    except (ZoneInfoNotFoundError, ValueError):
        log.warning(
            "unknown timezone %r (resolved to %r), falling back to UTC. "
            "Use a region shorthand (EU, US-East, US-West, OCE) or an IANA name.",
            name or RAID_TIMEZONE, resolved,
        )
        return timezone.utc


def is_known_timezone(name: str) -> bool:
    try:
        ZoneInfo(resolve_timezone(name))
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def parse_when(raw: str, tz_name: str | None = None) -> tuple[int | None, str | None]:
    """Parse an admin-typed start time into (epoch_seconds, error)."""
    # Commas and doubled spaces are how people naturally write these
    # ("Sat, 19:00"), so normalise them away rather than rejecting.
    text = " ".join(raw.replace(",", " ").split())
    if not text:
        return None, None

    if text.isdigit() and len(text) >= 9:  # already an epoch
        return int(text), None

    if (match := RELATIVE_RE.match(text)) and any(match.groups()):
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2) or 0)
        return int((datetime.now(timezone.utc) + timedelta(hours=hours, minutes=minutes)).timestamp()), None

    tz = _tz(tz_name)
    for fmt in ABSOLUTE_FORMATS:
        try:
            naive = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return int(naive.replace(tzinfo=tz).timestamp()), None

    now = datetime.now(tz)

    # Bare "20:30" - the next occurrence of that clock time.
    if match := CLOCK_RE.match(text):
        stamp = _at_clock(now, match, days_ahead=0, roll_forward=True)
        if stamp is not None:
            return stamp, None
        return None, _bad_clock(text)

    # "wed 20:30", "tomorrow 20:30" - a weekday or today/tomorrow plus a time.
    if match := DAY_CLOCK_RE.match(text):
        word = match.group(1).lower()
        if word in ("today", "tonight"):
            days = 0
        elif word == "tomorrow":
            days = 1
        elif (target := WEEKDAYS.get(word[:3])) is not None:
            days = (target - now.weekday()) % 7
        else:
            return None, _unparsed(raw)
        stamp = _at_clock(
            now, match, days_ahead=days, roll_forward=days == 0 and word not in ("today", "tonight"),
            weekly=word[:3] in WEEKDAYS,
        )
        if stamp is not None:
            return stamp, None
        return None, _bad_clock(text)

    return None, _unparsed(raw)


def _at_clock(
    now: datetime,
    match: re.Match[str],
    *,
    days_ahead: int,
    roll_forward: bool,
    weekly: bool = False,
) -> int | None:
    """Build a timestamp from a matched HH:MM, or None if the clock is invalid.

    Both callers' patterns expose named `hour`/`minute` groups, so this does not
    depend on their group *positions*.
    """
    hour, minute = int(match.group("hour")), int(match.group("minute"))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    candidate = now.replace(
        hour=hour, minute=minute, second=0, microsecond=0
    ) + timedelta(days=days_ahead)
    if candidate <= now and roll_forward:
        # "20:30" after 20:30 means tomorrow; "wed 20:30" on a Wednesday
        # evening means next Wednesday.
        candidate += timedelta(days=7 if weekly else 1)
    return int(candidate.timestamp())


def _bad_clock(text: str) -> str:
    return f"`{text}` has an impossible clock time — hours are 00-23, minutes 00-59."


def _unparsed(raw: str) -> str:
    return (
        f"Couldn't read `{raw}` as a time. All times are **server time**. "
        f"Accepted formats:\n"
        "• `20:30` — next time it's 20:30\n"
        "• `wed 20:30` — next Wednesday\n"
        "• `tomorrow 20:30` / `today 20:30`\n"
        "• `2026-07-22 20:30` — exact date\n"
        "• `in 90m` / `in 2h`"
    )


MIN_DURATION, MAX_DURATION = 15, 12 * 60


def parse_duration(raw: str) -> tuple[int | None, str | None]:
    """Parse '3h', '2h30m', '90m', '2.5h' or a bare '3' into (minutes, error)."""
    text = raw.strip().lower().replace(" ", "")
    if not text:
        return None, None

    total: int | None = None
    if match := re.fullmatch(r"(\d+(?:\.\d+)?)h(?:(\d+)m?)?", text):
        total = int(float(match.group(1)) * 60) + int(match.group(2) or 0)
    elif match := re.fullmatch(r"(\d+)m(?:in(?:s)?)?", text):
        total = int(match.group(1))
    elif match := re.fullmatch(r"(\d+(?:\.\d+)?)", text):
        total = int(float(match.group(1)) * 60)  # bare number means hours

    if total is None:
        return None, (
            f"Couldn't read `{raw}` as a duration. Try `3h`, `2h30m`, `90m`, or `2.5h`."
        )
    if not (MIN_DURATION <= total <= MAX_DURATION):
        return None, (
            f"`{raw}` works out to {format_duration(total)} — durations must be between "
            f"15 minutes and 12 hours."
        )
    return total, None


def format_duration(minutes: int) -> str:
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours}h{mins:02d}m"
    return f"{hours}h" if hours else f"{mins}m"


def format_local(epoch: int, tz_name: str | None = None) -> str:
    """Round-trips back into the edit modal, so it must stay parseable."""
    return datetime.fromtimestamp(epoch, _tz(tz_name)).strftime("%Y-%m-%d %H:%M")


def format_display(epoch: int, tz_name: str | None = None) -> str:
    """Human-readable server time, e.g. 'Wed 22 Jul, 20:30'."""
    return datetime.fromtimestamp(epoch, _tz(tz_name)).strftime("%a %d %b, %H:%M")


def format_clock(epoch: int, tz_name: str | None = None) -> str:
    """Just the clock, for the end of a time range, e.g. '23:30'."""
    return datetime.fromtimestamp(epoch, _tz(tz_name)).strftime("%H:%M")


#: Times guilds actually raid at, used to seed the `when` suggestions.
COMMON_START_TIMES = ("19:00", "19:30", "20:00", "20:30", "21:00")
COMMON_DURATIONS = ("1h30m", "2h", "2h30m", "3h", "3h30m", "4h")


def suggest_when(
    current: str, limit: int = 25, tz_name: str | None = None
) -> list[tuple[str, str]]:
    """(label, value) start-time suggestions, filtered by what's been typed.

    Discord shows these the moment the field is focused, which is as close to a
    pre-filled default as a slash command can get.
    """
    now = datetime.now(_tz(tz_name))
    options: list[tuple[str, str]] = []

    for clock in COMMON_START_TIMES:
        hour, minute = (int(part) for part in clock.split(":"))
        today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if today > now:
            options.append((f"Today, {clock}  ·  {today:%a %d %b}", f"today {clock}"))

    for days in range(1, 8):
        day = now + timedelta(days=days)
        label_day = "Tomorrow" if days == 1 else f"{day:%A}"
        for clock in ("19:00", "20:00", "20:30"):
            options.append((f"{label_day}, {clock}  ·  {day:%a %d %b}", f"{day:%a} {clock}"))

    options += [("In 1 hour", "in 1h"), ("In 90 minutes", "in 90m"), ("In 2 hours", "in 2h")]
    return _filter_choices(
        options, current, limit, lambda text: parse_when(text, tz_name)
    )


def suggest_timezone(current: str, limit: int = 25) -> list[tuple[str, str]]:
    """Realm-region choices for the `timezone` option.

    Offers the gameplay regions a guild would recognise. Finer overrides
    (US-East, BR, an IANA name) still validate if typed, they are just not
    cluttering the list.
    """
    options = [
        (f"{region}  ·  {REGION_LABELS.get(region, '')}".rstrip(" ·"), region)
        for region in PRIMARY_REGIONS
    ]
    typed = current.strip().lower()
    if typed:
        options = [o for o in options if typed in o[0].lower() or typed in o[1].lower()]
        if not options and is_known_timezone(current):
            options = [(f"Use “{current.strip()}”", current.strip())]
    return options[:limit]


def suggest_duration(current: str, limit: int = 25) -> list[tuple[str, str]]:
    options = [
        (f"{format_duration(m)}  ·  {_pretty_duration(m)}", text)
        for text in COMMON_DURATIONS
        if (m := parse_duration(text)[0]) is not None
    ]
    return _filter_choices(options, current, limit, parse_duration)


def _pretty_duration(minutes: int) -> str:
    hours, mins = divmod(minutes, 60)
    parts = [f"{hours} hour{'s' if hours != 1 else ''}"] if hours else []
    if mins:
        parts.append(f"{mins} min")
    return " ".join(parts)


Parser = Callable[[str], tuple[int | None, str | None]]


def _filter_choices(
    options: list[tuple[str, str]], current: str, limit: int, parser: Parser
) -> list[tuple[str, str]]:
    """Substring match on either half, so typing '20:' or 'sat' both narrow.

    `parser` must be the one belonging to *this* field. Checking both parsers
    would offer "Use 20:30" in the duration box, where it is not a valid value.
    """
    typed = current.strip().lower()
    if typed:
        options = [o for o in options if typed in o[0].lower() or typed in o[1].lower()]
        # Let a valid free-typed value through as the first choice, so the
        # suggestions never box an admin out of a value they can legitimately use.
        if parser(current)[0] is not None:
            options.insert(0, (f"Use “{current.strip()}”", current.strip()))
    return options[:limit]


#: Only fire a reminder if we are within this many seconds past its due time.
#: Wider than the 60s tick so a slow tick cannot skip one, narrow enough that a
#: bot restarted hours later does not ping about a raid that already happened.
REMINDER_WINDOW = 90


class ReminderTask:
    """Fires 60-minute, 10-minute and at-start pings for scheduled raids.

    Which reminders have already gone out is recorded in the database rather
    than in memory: a restart mid-evening would otherwise re-ping everybody for
    every raid still inside its window.
    """

    def __init__(self, client: "RaidClient") -> None:
        self.client = client

    def start(self) -> None:
        self._loop.start()

    def stop(self) -> None:
        self._loop.cancel()

    @tasks.loop(minutes=1)
    async def _loop(self) -> None:
        now = int(datetime.now(timezone.utc).timestamp())
        for guild in self.client.guilds:
            # One guild raising must not kill the loop for every other guild.
            try:
                await self._check_guild(guild.id, now)
            except Exception:  # noqa: BLE001 - deliberately broad, loop must survive
                log.exception("reminder sweep failed for guild %s", guild.id)

    async def _check_guild(self, guild_id: int, now: int) -> None:
        store = self.client.store
        for raid in store.open_raids(guild_id):
            if raid.starts_at is None or raid.state is not RaidState.OPEN:
                continue
            for offset in (*REMINDER_OFFSETS, 0):
                due = raid.starts_at - offset * 60
                if not (due <= now < due + REMINDER_WINDOW):
                    continue
                # claim_reminder is an atomic INSERT: it returns False if this
                # reminder has already been sent, including by a previous run.
                if store.claim_reminder(raid.id, offset):
                    await self._announce(raid, offset)

    async def _announce(self, raid: Raid, offset: int) -> None:
        accepted = self.client.store.signups(raid.id, Status.ACCEPTED)
        if not accepted:
            return
        mentions = " ".join(f"<@{s.user_id}>" for s in accepted)
        title = discord.utils.escape_markdown(raid.title)
        headline = (
            f"🔔 **{title}** starts <t:{raid.starts_at}:R>"
            if offset
            else f"🚀 **{title}** is starting now!"
        )
        try:
            channel = self.client.get_channel(raid.channel_id) or await self.client.fetch_channel(
                raid.channel_id
            )
            await channel.send(  # type: ignore[union-attr]
                f"{headline}\n{mentions}",
                # Explicit as well as the client-wide default, because this is
                # the one place a raid title reaches message content.
                allowed_mentions=SAFE_MENTIONS,
            )
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            log.warning("reminder for raid #%s failed: %s", raid.id, exc)

    @_loop.before_loop
    async def _before(self) -> None:
        await self.client.wait_until_ready()
