"""Offline end-to-end check: data integrity, buff maths, store, embed rendering.

    python -m tools.smoke_test

Exercises everything except the Discord transport, so a broken roster or buff
rule is caught here rather than in the raid channel.
"""

from __future__ import annotations

import sys
import tempfile
from collections import Counter
from pathlib import Path

# The rendered embed is full of emoji; Windows consoles default to cp1252.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.data import buffs as B  # noqa: E402
from bot.data.specs import ROLE_ORDER, SPECS, Role, get_spec  # noqa: E402
from bot.store import RaidState, Status, Store  # noqa: E402
from bot.ui.embeds import build_raid_embed  # noqa: E402
from bot.ui.schedule import parse_when  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        failures.append(label)


def status_for(key: str, statuses: list[B.BuffStatus]) -> B.BuffStatus:
    return next(s for s in statuses if s.definition.key == key)


def specs(*keys: str):
    return [get_spec(k) for k in keys]


print("\n[1] spec data")
by_role = Counter(s.role for s in SPECS)
check("40 specs", len(SPECS) == 40, f"got {len(SPECS)}")
check("unique keys", len({s.key for s in SPECS}) == len(SPECS))
check("6 tanks", by_role[Role.TANK] == 6, f"got {by_role[Role.TANK]}")
check("7 healers", by_role[Role.HEALER] == 7, f"got {by_role[Role.HEALER]}")
check("13 melee", by_role[Role.MELEE] == 13, f"got {by_role[Role.MELEE]}")
check("14 ranged", by_role[Role.RANGED] == 14, f"got {by_role[Role.RANGED]}")
check("every spec has an icon asset entry", all(s.icon for s in SPECS))

icon_dir = Path(__file__).resolve().parents[1] / "assets" / "icons"
if icon_dir.exists():
    for role in ROLE_ORDER:
        n = len(list((icon_dir / role.value).glob("*.png")))
        check(f"{role.value} icons on disk == specs", n == by_role[role], f"{n} files")

print("\n[2] buff coverage")
empty = B.evaluate([])
check("empty roster -> nothing covered", all(not s.covered for s in empty))
check("empty roster -> everything missing", len(B.missing(empty)) == len(B.BUFFS))

one_warrior = B.evaluate(specs("warr_arms"))
check("1 warrior -> Battle Shout (1)", status_for("battle_shout", one_warrior).count == 1)

two_warriors = B.evaluate(specs("warr_arms", "warr_fury"))
check("2 warriors -> Battle Shout (2)", status_for("battle_shout", two_warriors).count == 2)

print("\n[3] hunter's mark + grip counting")
hunters = B.evaluate(specs("hunter_bm", "hunter_mm", "hunter_surv"))
check("3 hunters -> Hunter's Mark (3)", status_for("hunters_mark", hunters).count == 3)

frost_only = B.evaluate(specs("dk_frost", "dk_unholy"))
grip = status_for("grip", frost_only)
check("non-blood DKs -> 'Grip'", grip.label == "Grip" and not grip.upgraded, grip.label)
check("2 DKs -> Grip (2)", grip.count == 2, str(grip.count))

with_blood = B.evaluate(specs("dk_blood", "dk_frost", "dk_unholy"))
mass = status_for("grip", with_blood)
check("blood DK present -> 'Mass Grip'", mass.label == "Mass Grip" and mass.upgraded, mass.label)
check("Mass Grip counts blood DKs only (1)", mass.count == 1, str(mass.count))
check("Mass Grip uses upgrade emoji name", mass.emoji_name == "grip_up", mass.emoji_name)

print("\n[3b] missing buffs render a class icon, not an X")
from bot.data.specs import CLASS_COLORS  # noqa: E402
from bot.emojis import registry  # noqa: E402

for b in B.BUFFS:
    if b.wow_class is not None:
        check(f"{b.key} names a real class", b.wow_class in CLASS_COLORS, b.wow_class)

single_class = [b for b in B.BUFFS if b.wow_class]
check("most buffs map to one class", len(single_class) >= 14, str(len(single_class)))

# Simulate a synced registry so the icon paths are actually exercised. Both
# kinds must be present: single-class buffs fall back to the class icon,
# multi-class ones (Lust, Combat Res, AS Slow) to their own buff icon.
def _stub(name: str):
    return type("E", (), {"__str__": lambda s, n=name: f"<:{n}:1>"})()


registry._by_name = {
    f"class_{c.lower().replace(' ', '_')}": _stub(f"class_{c.lower().replace(' ', '_')}")
    for c in CLASS_COLORS
}
registry._by_name.update({name: _stub(name) for name, _slug in B.icon_jobs()})
from bot.ui.embeds import _buff_panel  # noqa: E402

missing_text, _ = _buff_panel([])
check("no X in the missing line", "❌" not in missing_text, missing_text[:80])
check("warrior icon shown for Battle Shout", "<:class_warrior:1> Battle Shout" in missing_text)
check("DK icon shown for Grip", "<:class_death_knight:1> Grip" in missing_text)
registry._by_name = {}

print("\n[4] the screenshot case: holy priest fills the priest buff")
before = B.evaluate(specs("warr_prot"))
after = B.evaluate(specs("warr_prot", "priest_holy"))
check("no priest -> Fortitude missing", not status_for("fortitude", before).covered)
check("holy priest -> Fortitude (1)", status_for("fortitude", after).count == 1)
check("no paladin -> Devotion still missing", not status_for("devotion_aura", after).covered)

print("\n[5] lust providers")
check("shaman lusts", status_for("lust", B.evaluate(specs("sham_resto"))).count == 1)
check("BM hunter lusts", status_for("lust", B.evaluate(specs("hunter_bm"))).count == 1)
check("MM hunter does not", status_for("lust", B.evaluate(specs("hunter_mm"))).count == 0)

print("\n[6] time parsing (server time)")
from datetime import datetime, timedelta, timezone  # noqa: E402

from bot.config import RAID_TIMEZONE  # noqa: E402
from bot.ui.schedule import _tz, format_display  # noqa: E402

now = datetime.now(_tz())
print(f"  (RAID_TIMEZONE={RAID_TIMEZONE}, now={now:%a %d %b %H:%M %Z})")

for text in ("in 90m", "2026-07-22 20:30", "20:30", "wed 20:30", "wednesday 20:30",
             "tomorrow 20:30", "today 23:59", "sat 19:00", "in 2h"):
    stamp, err = parse_when(text)
    check(f"parses {text!r:<20} -> {format_display(stamp) if stamp else err}", stamp is not None)

for bad in ("next tuesday-ish", "25:00", "20:75", "banana 20:30", "soon"):
    check(f"rejects {bad!r}", parse_when(bad)[1] is not None)

# Every parsed time must land in the future, or reminders fire instantly.
for text in ("20:30", "wed 20:30", "tomorrow 20:30", "sat 19:00", "in 90m"):
    stamp = parse_when(text)[0]
    check(f"{text!r} is in the future", stamp > now.timestamp(),
          f"{format_display(stamp)}")

# A weekday name must land on that weekday.
for name, index in (("mon", 0), ("wed", 2), ("sat", 5), ("sun", 6)):
    stamp = parse_when(f"{name} 20:30")[0]
    landed = datetime.fromtimestamp(stamp, _tz())
    check(f"{name!r} lands on the right weekday", landed.weekday() == index,
          f"got {landed:%a}")

# A bare clock that already passed today must roll to tomorrow, not the past.
past = (now - timedelta(hours=2)).strftime("%H:%M")
stamp = parse_when(past)[0]
check(f"past clock {past!r} rolls to tomorrow", stamp > now.timestamp(),
      format_display(stamp))

# The modal round-trip: format_local output must re-parse to the same instant.
sample = parse_when("2026-07-22 20:30")[0]
from bot.ui.schedule import format_local  # noqa: E402

check("format_local round-trips", parse_when(format_local(sample))[0] == sample)

print("\n[6b] duration + autocomplete")
from bot.ui.schedule import (  # noqa: E402
    format_duration, parse_duration, suggest_duration, suggest_when,
)

for text, expected in (
    ("3h", 180), ("2h30m", 150), ("2h30", 150), ("90m", 90), ("45min", 45),
    ("2.5h", 150), ("3", 180), ("  4h  ", 240),
):
    got = parse_duration(text)[0]
    check(f"duration {text!r} -> {expected}m", got == expected, f"got {got}")

for bad in ("banana", "0m", "20h", "-3h", "5m"):
    check(f"duration rejects {bad!r}", parse_duration(bad)[1] is not None)

for minutes, expected in ((180, "3h"), (150, "2h30m"), (45, "45m"), (240, "4h")):
    check(f"format_duration({minutes}) == {expected!r}", format_duration(minutes) == expected)

# The user's own example, comma included.
stamp, err = parse_when("Sat, 19:00")
check("parses 'Sat, 19:00' (comma)", stamp is not None, str(err))
check("comma form matches plain form", stamp == parse_when("sat 19:00")[0])

when_choices = suggest_when("")
check("when autocomplete non-empty", len(when_choices) > 0, str(len(when_choices)))
check("when autocomplete <=25 (Discord cap)", len(when_choices) <= 25, str(len(when_choices)))
check(
    "every when suggestion actually parses",
    all(parse_when(v)[0] is not None for _l, v in when_choices),
    str([v for _l, v in when_choices if parse_when(v)[0] is None]),
)
check("typing 'sat' narrows the list", all(
    "sat" in label.lower() or "sat" in value.lower() for label, value in suggest_when("sat")
))

dur_choices = suggest_duration("")
check("duration autocomplete <=25", 0 < len(dur_choices) <= 25, str(len(dur_choices)))
check(
    "every duration suggestion parses",
    all(parse_duration(v)[0] is not None for _l, v in dur_choices),
)

print("\n[6c] schema migration onto an existing database")
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    legacy = Path(tmp) / "legacy.sqlite3"
    # Build a pre-duration database, exactly like the one already running.
    import sqlite3 as _sq

    old = _sq.connect(legacy)
    old.executescript(
        """CREATE TABLE raids (id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
           channel_id INTEGER NOT NULL, message_id INTEGER, title TEXT NOT NULL,
           description TEXT, leader_id INTEGER NOT NULL, starts_at INTEGER,
           state TEXT NOT NULL DEFAULT 'open', caps TEXT NOT NULL, created_at INTEGER NOT NULL);"""
    )
    old.execute(
        "INSERT INTO raids (guild_id,channel_id,title,leader_id,state,caps,created_at)"
        " VALUES (1,2,'Old Raid',3,'open','{\"tank\":2}',0)"
    )
    old.commit()
    old.close()

    migrated = Store(legacy)
    cols = {r["name"] for r in migrated.db.execute("PRAGMA table_info(raids)")}
    check("migration adds duration_minutes", "duration_minutes" in cols)
    kept = migrated.get_raid(1)
    check("existing raid survives migration", kept is not None and kept.title == "Old Raid")
    check("existing raid has NULL duration", kept.duration_minutes is None)
    Store(legacy)  # second open must be a no-op, not an error
    check("migration is idempotent", True)
    migrated.close()

print("\n[6d] per-raid timezone")
from bot.config import REALM_ZONES  # noqa: E402
from bot.ui.schedule import is_known_timezone, suggest_timezone  # noqa: E402

from bot.config import PRIMARY_REGIONS, REALM_ZONES  # noqa: E402

for region in (*PRIMARY_REGIONS, "eu", "na", "US-East", "Europe/Paris", "America/Chicago"):
    check(f"{region!r} is a known region", is_known_timezone(region))
for bad in ("Mars", "EU-West-3", "GMT+2"):
    check(f"{bad!r} is rejected", not is_known_timezone(bad))

# The gameplay regions map to the clocks guilds actually raid by.
for region, expected in (
    ("EU", "Europe/Paris"), ("NA", "America/Chicago"),
    ("KR", "Asia/Seoul"), ("TW", "Asia/Taipei"), ("OCE", "Australia/Sydney"),
):
    check(f"{region} -> {expected}", REALM_ZONES[region.lower()] == expected)

# The whole point: the same text means different instants in different regions.
eu = parse_when("sat 19:00", "EU")[0]
na = parse_when("sat 19:00", "NA")[0]
oce = parse_when("sat 19:00", "OCE")[0]
check("EU and NA differ", eu != na, f"{eu} vs {na}")
check("EU and OCE differ", eu != oce)
check("each renders 19:00 in its own region", all(
    format_display(stamp, tz).endswith("19:00")
    for stamp, tz in ((eu, "EU"), (na, "NA"), (oce, "OCE"))
), f"{format_display(eu,'EU')} / {format_display(na,'NA')} / {format_display(oce,'OCE')}")
check(
    "an NA raid does not read as 19:00 in EU",
    not format_display(na, "EU").endswith("19:00"), format_display(na, "EU"),
)

tz_choices = suggest_timezone("")
check("timezone picker offers the gameplay regions",
      [v for _l, v in tz_choices] == list(PRIMARY_REGIONS), str(tz_choices))
check("every timezone suggestion is valid", all(is_known_timezone(v) for _l, v in tz_choices))
check("typing 'kr' narrows to Korea", [v for _l, v in suggest_timezone("kr")] == ["KR"])
check("a typed IANA name is still offered",
      suggest_timezone("America/Denver")[0][1] == "America/Denver")

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    tzs = Store(Path(tmp) / "tz.sqlite3")
    check("no last timezone on a fresh guild", tzs.last_timezone(1) is None)
    r1 = tzs.create_raid(
        guild_id=1, channel_id=2, title="First", description=None, leader_id=1,
        starts_at=na, timezone="NA", caps={"tank": 2},
    )
    check("timezone persisted", tzs.get_raid(r1.id).timezone == "NA")
    check("last_timezone remembers it", tzs.last_timezone(1) == "NA")
    check("other guilds are unaffected", tzs.last_timezone(999) is None)
    tzs.set_timezone(r1.id, "OCE")
    check("timezone editable after creation", tzs.get_raid(r1.id).timezone == "OCE")
    # A raid with no timezone falls back to the configured default, not a crash.
    r2 = tzs.create_raid(
        guild_id=2, channel_id=2, title="No tz", description=None, leader_id=1,
        starts_at=eu, caps={"tank": 2},
    )
    check("timezone is optional", tzs.get_raid(r2.id).timezone is None)
    check("embed renders without a timezone", bool(build_raid_embed(r2, []).description))
    tzs.close()

print("\n[7] store + embed")
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    store = Store(Path(tmp) / "t.sqlite3")
    raid = store.create_raid(
        guild_id=1, channel_id=2, title="Manaforge Omega — Mythic",
        description="Invites 20:15. Full consumes.", leader_id=99,
        starts_at=parse_when("sat 19:00")[0],
        duration_minutes=parse_duration("3h")[0],
        caps={"tank": 2, "healer": 4, "melee": 7, "ranged": 7},
    )
    check("raid created open", raid.state is RaidState.OPEN)
    check("duration persisted", raid.duration_minutes == 180, str(raid.duration_minutes))

    roster = [
        (101, "Mimz", "pal_prot", Status.ACCEPTED),
        (102, "Tankalot", "dk_blood", Status.ACCEPTED),
        (103, "Healbot", "priest_holy", Status.ACCEPTED),
        (104, "Treehugger", "druid_resto", Status.ACCEPTED),
        (105, "Stabby", "rogue_assa", Status.ACCEPTED),
        (106, "Boomy", "druid_balance", Status.ACCEPTED),
        (107, "Newguy", "mage_fire", Status.PENDING),
        (108, "Benchwarmer", "warr_fury", Status.BENCH),
        (109, "Awayman", "sham_ele", Status.ABSENT),
    ]
    for uid, name, spec_key, status in roster:
        store.upsert_signup(
            raid_id=raid.id, user_id=uid, character_name=name,
            logs_url=f"https://www.warcraftlogs.com/character/eu/kazzak/{name.lower()}",
            spec_key=spec_key, status=status, updated_by=99,
        )
        store.save_player(uid, name, None, spec_key)

    check("9 signups stored", len(store.signups(raid.id)) == 9)
    check("6 accepted", len(store.signups(raid.id, Status.ACCEPTED)) == 6)
    check("cache round-trips", store.get_player(101).character_name == "Mimz")

    store.set_status(raid.id, 107, Status.ACCEPTED, 99)
    check("accept moves to accepted", len(store.signups(raid.id, Status.ACCEPTED)) == 7)
    store.set_spec(raid.id, 107, "mage_frost", 99)
    check("spec reassign persists", store.get_signup(raid.id, 107).spec_key == "mage_frost")

    embed = build_raid_embed(raid, store.signups(raid.id))
    names = [f.name for f in embed.fields]
    check("embed has all four role fields", sum("Tanks" in n or "Healers" in n or "Melee" in n or "Ranged" in n for n in names) == 4)
    check("embed has missing-buff field", any("Missing Raid Buffs" in n for n in names))
    check("embed has available-buff field", any("Available Buffs" in n for n in names))
    check("no field exceeds 1024 chars", all(len(f.value) <= 1024 for f in embed.fields))
    check("title preserved", "Manaforge Omega" in embed.title)

    print("\n--- rendered embed ---")
    print(f"{embed.title}\n{embed.description}\n")
    for field in embed.fields:
        print(f"{field.name}\n{field.value}\n")
    print(f"footer: {embed.footer.text}")

    store.close()

print("\n[7b] combined DPS target")
with tempfile.TemporaryDirectory() as tmp:
    from bot.data import targets as T  # noqa: E402

    store = Store(Path(tmp) / "combined.sqlite3")
    raid = store.create_raid(
        guild_id=1, channel_id=1, title="Combined night", description=None,
        leader_id=1, starts_at=None, caps={"tank": 2, "healer": 4, "dps": 14},
    )
    for index, (name, spec_key) in enumerate(
        [("Blocky", "pal_prot"), ("Mender", "priest_holy"),
         ("Chop", "warr_arms"), ("Bolt", "mage_fire"), ("Stab", "rogue_sub")]
    ):
        store.upsert_signup(
            raid_id=raid.id, user_id=100 + index, character_name=name, logs_url=None,
            spec_key=spec_key, status=Status.ACCEPTED,
        )
    raid = store.get_raid(raid.id)

    check("detected as combined", T.is_combined(raid.caps))
    check("three targets", len(T.targets(raid.caps)) == 3)
    check("summary reads 2 / 4 / 14", T.summary(raid.caps) == "2 / 4 / 14", T.summary(raid.caps))
    check("raid size is 20", T.raid_size(raid.caps) == 20)
    check("melee has no cap of its own", T.role_cap(raid.caps, Role.MELEE) is None)
    check("tank keeps its cap", T.role_cap(raid.caps, Role.TANK) == 2)

    embed = build_raid_embed(raid, store.signups(raid.id))
    names = [f.name for f in embed.fields]
    check("roster still splits melee from ranged",
          any("Melee" in n for n in names) and any("Ranged" in n for n in names))
    check("melee header carries a bare count, not x/0",
          any(n.endswith("(2)") for n in names), str(names[:4]))
    check("tank header still shows its target", any(n.endswith("(1/2)") for n in names))
    check("combined target is stated", "DPS **3/14**" in embed.description, embed.description)
    check("footer counts against the combined size", "5/20 accepted" in embed.footer.text)

    # A four-target raid must render exactly as it always did.
    plain = store.create_raid(
        guild_id=1, channel_id=1, title="Plain", description=None, leader_id=1,
        starts_at=None, caps={"tank": 2, "healer": 4, "melee": 7, "ranged": 7},
    )
    plain_embed = build_raid_embed(store.get_raid(plain.id), [])
    check("four-target board unchanged", "🎯 Targets" not in (plain_embed.description or ""))
    check("four-target headers keep x/y",
          all("/" in f.name for f in plain_embed.fields[:4]))
    store.close()

print("\n[7c] tentative status + section spacing")
with tempfile.TemporaryDirectory() as tmp:
    from bot.ui.embeds import BLANK, CONTENT_LIMIT, FIELD_LIMIT, SECTION_HEAD, SECTION_TAIL

    check("tentative is self-service", Status.TENTATIVE.self_service)
    check("accepted is not self-service", not Status.ACCEPTED.self_service)
    check("tentative has its own emoji",
          len({s.emoji for s in Status}) == len(list(Status)), "emoji must stay unique")
    check("tentative has its own label",
          len({s.label for s in Status}) == len(list(Status)))

    store = Store(Path(tmp) / "tent.sqlite3")
    raid = store.create_raid(
        guild_id=1, channel_id=1, title="Spacing", description=None, leader_id=1,
        starts_at=None, caps={"tank": 2, "healer": 4, "melee": 7, "ranged": 7},
    )
    for index, (name, spec_key, status) in enumerate([
        ("Blocky", "pal_prot", Status.ACCEPTED),
        ("Mender", "priest_holy", Status.ACCEPTED),
        ("Maybe", "warr_arms", Status.TENTATIVE),
        ("Perhaps", "mage_fire", Status.TENTATIVE),
        ("Sitting", "rogue_sub", Status.BENCH),
    ]):
        store.upsert_signup(
            raid_id=raid.id, user_id=200 + index, character_name=name, logs_url=None,
            spec_key=spec_key, status=status,
        )
    embed = build_raid_embed(store.get_raid(raid.id), store.signups(raid.id))

    check("tentative gets its own section",
          any(f.name and f.name.startswith("❔ Tentative") for f in embed.fields),
          str([f.name for f in embed.fields]))
    check("tentative section counts its members",
          any(f.name == "❔ Tentative (2)" for f in embed.fields))
    check("tentative is not counted as accepted", "2/20 accepted" in embed.footer.text,
          embed.footer.text)

    for field in embed.fields:
        check(f"blank line under header: {field.name[:22]}", field.value.startswith(SECTION_HEAD))
        check(f"two blank lines under body: {field.name[:22]}", field.value.endswith(SECTION_TAIL))
    check("spacing uses a zero-width space, which Discord keeps", BLANK == "​")
    check("no field exceeds the limit with spacing added",
          all(len(f.value) <= FIELD_LIMIT for f in embed.fields),
          str(max(len(f.value) for f in embed.fields)))
    check("content budget leaves room for the padding",
          CONTENT_LIMIT == FIELD_LIMIT - len(SECTION_HEAD) - len(SECTION_TAIL))

    # A full field must still fit once the padding is wrapped around it.
    packed = Store(Path(tmp) / "packed.sqlite3")
    big = packed.create_raid(
        guild_id=1, channel_id=1, title="Packed", description=None, leader_id=1,
        starts_at=None, caps={"tank": 40, "healer": 4, "melee": 7, "ranged": 7},
    )
    for index in range(40):
        packed.upsert_signup(
            raid_id=big.id, user_id=900 + index, character_name=f"Tankilicious{index:02d}",
            logs_url="https://www.warcraftlogs.com/character/eu/kazzak/someoneverylong",
            spec_key="pal_prot", status=Status.ACCEPTED,
        )
    packed_embed = build_raid_embed(packed.get_raid(big.id), packed.signups(big.id))
    check("overflowing field still within the limit",
          all(len(f.value) <= FIELD_LIMIT for f in packed_embed.fields),
          str(max(len(f.value) for f in packed_embed.fields)))
    check("overflowing field keeps its spacing",
          all(f.value.startswith(SECTION_HEAD) and f.value.endswith(SECTION_TAIL)
              for f in packed_embed.fields))
    packed.close()
    store.close()

print("\n[7d] auto-accept")
with tempfile.TemporaryDirectory() as tmp:
    store = Store(Path(tmp) / "auto.sqlite3")

    off = store.create_raid(
        guild_id=1, channel_id=1, title="Manual", description=None, leader_id=1,
        starts_at=None,
    )
    check("defaults to off", off.auto_accept is False, repr(off.auto_accept))
    check("it is a real bool, not sqlite's 0/1", isinstance(off.auto_accept, bool))

    on = store.create_raid(
        guild_id=1, channel_id=1, title="Auto", description=None, leader_id=1,
        starts_at=None, auto_accept=True,
    )
    check("can be set at creation", store.get_raid(on.id).auto_accept is True)

    store.set_auto_accept(off.id, True)
    check("can be toggled on", store.get_raid(off.id).auto_accept is True)
    store.set_auto_accept(off.id, False)
    check("can be toggled off", store.get_raid(off.id).auto_accept is False)

    # The promotion rule itself, as submit_application applies it.
    def resolved(raid_auto: bool, applied_as: Status) -> Status:
        return (
            Status.ACCEPTED
            if raid_auto and applied_as is Status.PENDING
            else applied_as
        )

    check("auto-accept promotes a pending application",
          resolved(True, Status.PENDING) is Status.ACCEPTED)
    check("without it, applications stay pending",
          resolved(False, Status.PENDING) is Status.PENDING)
    for deliberate in (Status.BENCH, Status.ABSENT, Status.TENTATIVE):
        check(f"never overrides a deliberate {deliberate.label}",
              resolved(True, deliberate) is deliberate)

    embed = build_raid_embed(store.get_raid(on.id), [])
    check("board announces auto-accept", "Auto-accept is on" in embed.description)
    check("board stays quiet when it is off",
          "Auto-accept" not in build_raid_embed(store.get_raid(off.id), []).description)
    store.close()

print("\n[7e] auto_accept migrates onto a database that predates it")
with tempfile.TemporaryDirectory() as tmp:
    import sqlite3 as _sqlite3

    path = Path(tmp) / "old.sqlite3"
    # A raids table exactly as it looked before the column existed.
    old = _sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE raids (
            id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL, message_id INTEGER, title TEXT NOT NULL,
            description TEXT, leader_id INTEGER NOT NULL, starts_at INTEGER,
            duration_minutes INTEGER, timezone TEXT,
            state TEXT NOT NULL DEFAULT 'open', caps TEXT NOT NULL,
            created_at INTEGER NOT NULL);
        INSERT INTO raids (guild_id, channel_id, title, leader_id, state, caps, created_at)
        VALUES (1, 1, 'Pre-existing raid', 1, 'open', '{"tank": 2}', 0);
    """)
    old.commit()
    old.close()

    store = Store(path)          # must not raise
    raid = store.get_raid(1)
    check("existing raid still loads", raid is not None and raid.title == "Pre-existing raid")
    check("back-filled as off", raid.auto_accept is False)
    check("new raids still work after migrating",
          store.create_raid(guild_id=1, channel_id=1, title="After", description=None,
                            leader_id=1, starts_at=None, auto_accept=True).auto_accept is True)
    store.close()

print("\n[8] hardening: hostile input and Discord's hard limits")
from bot.ui.common import SAFE_MENTIONS, normalise_logs_url  # noqa: E402
from bot.ui.embeds import (  # noqa: E402
    DESCRIPTION_LIMIT, FIELD_LIMIT, TITLE_LIMIT, TOTAL_LIMIT, _fit_inline, _sorted, clamp,
)

# --- mention injection ---
check("bot never mentions @everyone", SAFE_MENTIONS.everyone is False)
check("bot never mentions roles", SAFE_MENTIONS.roles is False)
check("bot may still mention raiders", SAFE_MENTIONS.users is True)

# --- markdown / link injection via the logs URL ---
for hostile in (
    "https://www.warcraftlogs.com/character/eu/x/y)[click](http://evil.example)",
    "https://www.warcraftlogs.com/character/eu/x/y<script>",
    "javascript:alert(1)",
    "https://evil.example/character/eu/x/y",
    "https://warcraftlogs.com.evil.example/character/a/b",
):
    url, err = normalise_logs_url(hostile)
    check(f"rejects hostile logs url {hostile[:44]!r}", url is None and err is not None)

check(
    "accepts a normal logs url",
    normalise_logs_url("https://www.warcraftlogs.com/character/eu/kazzak/miimzz")[0] is not None,
)
check("bare domain gets https:// added", normalise_logs_url(
    "www.warcraftlogs.com/character/eu/kazzak/miimzz")[0].startswith("https://"))

# --- embed limits ---
check("clamp shortens and marks", clamp("x" * 500, 100) == "x" * 99 + "…")
check("clamp leaves short text alone", clamp("short", 100) == "short")

emoji_items = [f"<:class_warrior:1234567890123456789> Battle Shout {i}" for i in range(200)]
fitted = _fit_inline(emoji_items)
check("inline fit stays within field limit", len(fitted) <= FIELD_LIMIT, str(len(fitted)))
check("inline fit never cuts an emoji token", fitted.count("<:") == fitted.count(">"))

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
    big = Store(Path(tmp) / "big.sqlite3")
    huge = big.create_raid(
        guild_id=1, channel_id=2,
        title="T" * 300,                  # over Discord's 256 title limit
        description="D" * 5000,           # over the 4096 description limit
        leader_id=1, starts_at=parse_when("sat 19:00")[0], duration_minutes=180,
        caps={"tank": 2, "healer": 4, "melee": 7, "ranged": 7},
    )
    # A full mythic roster of long names in every status.
    for i in range(40):
        big.upsert_signup(
            raid_id=huge.id, user_id=1000 + i,
            character_name=f"Verylongcharactername{i:02d}",
            logs_url="https://www.warcraftlogs.com/character/eu/kazzak/someone",
            spec_key=SPECS[i % len(SPECS)].key,
            status=list(Status)[i % len(Status)],
            note="a note " * 20,
        )
    big_embed = build_raid_embed(huge, big.signups(huge.id))
    check("title clamped", len(big_embed.title) <= TITLE_LIMIT, str(len(big_embed.title)))
    check(
        "description clamped",
        len(big_embed.description) <= DESCRIPTION_LIMIT, str(len(big_embed.description)),
    )
    check(
        "no field over the limit",
        all(len(f.value) <= FIELD_LIMIT for f in big_embed.fields),
        str(max(len(f.value) for f in big_embed.fields)),
    )
    check("whole embed under total limit", len(big_embed) <= TOTAL_LIMIT, str(len(big_embed)))
    check("comp fields survive trimming", sum(
        1 for f in big_embed.fields if any(r.label in (f.name or "") for r in ROLE_ORDER)
    ) == 4)

    # --- roster ordering is stable, not by last-touched ---
    for uid in (1039, 1001, 1020):
        big.set_status(huge.id, uid, Status.ACCEPTED, 1)
    accepted_names = [s.character_name for s in _sorted(big.signups(huge.id, Status.ACCEPTED))]
    check("roster sorted alphabetically", accepted_names == sorted(accepted_names, key=str.casefold))

    # --- reminders fire once, and re-arm when the raid moves ---
    check("first claim wins", big.claim_reminder(huge.id, 60) is True)
    check("second claim is refused", big.claim_reminder(huge.id, 60) is False)
    check("a different offset is independent", big.claim_reminder(huge.id, 10) is True)
    big.set_schedule(huge.id, parse_when("sun 20:00")[0], 180)
    check("rescheduling re-arms reminders", big.claim_reminder(huge.id, 60) is True)
    big.close()

# --- autocomplete must validate against its own field ---
check(
    "duration box does not offer a clock time",
    not any(v == "20:30" for _l, v in suggest_duration("20:30")),
)
check(
    "when box does offer a typed clock time",
    any(v == "20:30" for _l, v in suggest_when("20:30")),
)

print()
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    raise SystemExit(1)
print("all checks passed")
