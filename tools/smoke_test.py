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

print("\n[3c] who to recruit for the buffs still missing")
_recruits = B.recruits

# A lone protection paladin: almost everything is still missing.
_thin = B.evaluate(specs("pal_prot"))
_thin_by_class = {r.wow_class: r for r in _recruits(_thin)}
check("a class that fixes nothing is not listed", "Paladin" not in _thin_by_class,
      "the paladin already covers both buffs a paladin brings")
_order = _recruits(_thin)
check("ordered by how many gaps one invite closes",
      [r.count for r in _order] == sorted((r.count for r in _order), reverse=True))
_top = [r.wow_class for r in _order if r.count == _order[0].count]
check("ties are alphabetical, so the panel does not reshuffle between polls",
      _top == sorted(_top), str(_top))
check("evoker closes three on its own",
      {g.buff.key for g in _thin_by_class["Evoker"].gaps}
      == {"bronze", "lust", "source_of_magic"})

# Druid ties it, but only because Feral carries Attack Speed Slow - exactly the
# case where "bring a Druid" is not specific enough.
_slow = next(g for g in _thin_by_class["Druid"].gaps if g.buff.key == "as_slow")
check("a druid only covers AS Slow as Feral", _slow.spec_locked
      and _slow.spec_names == ("Feral",), str(_slow.spec_names))

_lust = next(g for g in _thin_by_class["Hunter"].gaps if g.buff.key == "lust")
check("a spec-locked gap is flagged", _lust.spec_locked)
check("and it names the spec that actually brings it",
      _lust.spec_names == ("Beast Mastery",), str(_lust.spec_names))
_mark = next(g for g in _thin_by_class["Hunter"].gaps if g.buff.key == "hunters_mark")
check("a gap any spec of the class covers is not spec-locked", not _mark.spec_locked)

check("an upgradeable buff still recruits on its base class",
      "Death Knight" in _thin_by_class
      and any(g.buff.key == "grip" for g in _thin_by_class["Death Knight"].gaps))

# Every class listed must genuinely provide every gap attributed to it.
_bad = [
    (r.wow_class, g.buff.key)
    for r in _recruits(_thin)
    for g in r.gaps
    if not any(sp.wow_class == r.wow_class and g.buff.provided_by(sp) for sp in g.specs)
]
check("every suggestion is backed by a real provider", not _bad, str(_bad))

# Nothing missing -> nothing to recruit. One of each class, plus the specs the
# spec-locked entries need.
_full = specs(
    "warr_arms", "mage_fire", "priest_holy", "druid_resto", "sham_resto",
    "evoker_pres", "pal_prot", "dh_havoc", "monk_ww", "hunter_bm", "rogue_sub",
    "dk_blood", "lock_affli",
)
_covered = B.evaluate(_full)
check("a complete roster asks for nobody", _recruits(_covered) == [],
      str([r.wow_class for r in _recruits(_covered)]))
check("...and that roster really has no gaps", not B.missing(_covered),
      str([m.definition.key for m in B.missing(_covered)]))

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
    # The 40 tanks overflow one field and flow into invisible continuation
    # blocks. The role reads as one block: the first field carries the header
    # padding, the last carries the trailing gap, and the pieces between are
    # tight (no padding), so there is no gaping hole mid-roster.
    tank_fields = []
    for f in packed_embed.fields:
        if f.name.endswith("Tanks (40/40)"):
            tank_fields = [f]
        elif tank_fields and f.name == BLANK:
            tank_fields.append(f)
        elif tank_fields:
            break
    check("the packed role spans more than one field", len(tank_fields) > 1,
          f"{len(tank_fields)}")
    check("its first block carries the header spacing",
          tank_fields[0].value.startswith(SECTION_HEAD))
    check("its last block carries the trailing spacing",
          tank_fields[-1].value.endswith(SECTION_TAIL))
    check("the blocks between are tight (no padding gap)",
          all(not f.value.startswith(SECTION_HEAD) and not f.value.endswith(SECTION_TAIL)
              for f in tank_fields[1:-1]) if len(tank_fields) > 2 else True)
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

print("\n[7f] cancelled / finished raids close down")
with tempfile.TemporaryDirectory() as tmp:
    import time as _time

    from bot.store import RaidState, raid_is_closed, raid_is_finished

    store = Store(Path(tmp) / "closed.sqlite3")
    now = int(_time.time())

    def make(title, **kw):
        raid = store.create_raid(guild_id=1, channel_id=1, title=title, description=None,
                                 leader_id=1, **kw)
        store.upsert_signup(raid_id=raid.id, user_id=1, character_name="Blocky",
                            logs_url=None, spec_key="pal_prot", status=Status.ACCEPTED)
        return store.get_raid(raid.id)

    upcoming = make("Upcoming", starts_at=now + 7200, duration_minutes=180)
    over = make("Over", starts_at=now - 86400, duration_minutes=180)
    undated = make("No time set", starts_at=None)
    cancelled = make("Cancelled", starts_at=now + 7200, duration_minutes=180)
    store.set_raid_state(cancelled.id, RaidState.CANCELLED)
    cancelled = store.get_raid(cancelled.id)

    check("upcoming raid is not finished", not raid_is_finished(upcoming))
    check("past raid is finished", raid_is_finished(over))
    check("a raid with no start time is never 'finished'", not raid_is_finished(undated),
          "raid_ends_at falls back to created_at for expiry; that must not close a board")
    check("undated raid is not closed", not raid_is_closed(undated))
    check("cancelled raid is closed", raid_is_closed(cancelled))
    check("finished raid is closed", raid_is_closed(over))

    def field_names(raid):
        return [f.name for f in build_raid_embed(raid, store.signups(raid.id)).fields]

    live_fields = field_names(upcoming)
    check("live raid shows both buff panels",
          any("Missing Raid Buffs" in n for n in live_fields)
          and any("Available Buffs" in n for n in live_fields))
    for label, raid in (("finished", over), ("cancelled", cancelled)):
        names = field_names(raid)
        check(f"{label} raid drops the buff panels",
              not any("Buff" in n for n in names), str(names))
        check(f"{label} raid still lists the roster",
              any("Tanks" in n for n in names), str(names))
    check("undated raid keeps its buff panels",
          any("Buff" in n for n in field_names(undated)))
    store.close()

print("\n[7g] a finished board gets exactly one closing redraw")
with tempfile.TemporaryDirectory() as tmp:
    import time as _t2

    from bot.store import raid_is_closed as _closed

    store = Store(Path(tmp) / "sweep.sqlite3")
    now = int(_t2.time())
    over = store.create_raid(guild_id=1, channel_id=1, title="Over", description=None,
                             leader_id=1, starts_at=now - 86400, duration_minutes=180)
    soon = store.create_raid(guild_id=1, channel_id=1, title="Upcoming", description=None,
                             leader_id=1, starts_at=now + 86400, duration_minutes=180)
    undated = store.create_raid(guild_id=1, channel_id=1, title="Undated", description=None,
                                leader_id=1, starts_at=None)
    for r in (over, soon, undated):
        store.set_raid_message(r.id, 1000 + r.id)

    def due():
        return [r.id for r in store.boards_awaiting_close(1) if _closed(r)]

    check("a finished raid is queued for its closing redraw", due() == [over.id], str(due()))
    check("board_closed_at starts empty", store.get_raid(over.id).board_closed_at is None)

    store.mark_board_closed(over.id)
    check("claimed after the redraw", store.get_raid(over.id).board_closed_at is not None)
    check("never redrawn twice", due() == [], str(due()))
    check("an upcoming raid is left alone", soon.id not in due())
    check("an undated raid is left alone", undated.id not in due())

    # A raid with no message cannot be redrawn, so it must not sit in the queue.
    ghost = store.create_raid(guild_id=1, channel_id=1, title="Never posted", description=None,
                              leader_id=1, starts_at=now - 86400, duration_minutes=180)
    check("a raid that was never posted is excluded",
          ghost.id not in [r.id for r in store.boards_awaiting_close(1)])
    store.close()

print("\n[7h] audit log + discord identity")
with tempfile.TemporaryDirectory() as tmp:
    import sqlite3 as _sqlite3

    from bot.store import AUDIT_ACTIONS, AUDIT_RETAINED

    path = Path(tmp) / "audited.sqlite3"
    store = Store(path)
    raid = store.create_raid(
        guild_id=1, channel_id=1, title="Audited", description=None,
        leader_id=10, starts_at=None,
    )
    store.upsert_signup(
        raid_id=raid.id, user_id=77, character_name="Trollman", logs_url=None,
        spec_key="mage_fire", status=Status.PENDING, discord_name="trollman",
    )
    check("handle stored with the signup",
          store.get_signup(raid.id, 77).discord_name == "trollman")
    # An admin flipping a status has no reason to know the handle, and must not
    # blank the one the application recorded.
    store.upsert_signup(
        raid_id=raid.id, user_id=77, character_name="Trollman", logs_url=None,
        spec_key="mage_fire", status=Status.ACCEPTED,
    )
    check("a later write without a handle keeps the old one",
          store.get_signup(raid.id, 77).discord_name == "trollman")
    store.set_discord_name(raid.id, 77, "trollman_renamed")
    check("handle can be backfilled",
          store.get_signup(raid.id, 77).discord_name == "trollman_renamed")

    store.record_audit(
        raid_id=raid.id, action="status", source="discord", actor_id=10,
        actor_name="raidlead", target_id=77, target_name="trollman",
        detail="Trollman — Pending -> Accepted",
    )
    entry = store.audit_entries(raid.id)[0]
    check("entry names the actor", entry.actor_id == 10 and entry.actor_name == "raidlead")
    check("entry names the target", entry.target_id == 77)
    check("entry records where it happened", entry.source == "discord")
    check("action is in the known vocabulary", entry.action in AUDIT_ACTIONS)

    other = store.create_raid(
        guild_id=1, channel_id=1, title="Other", description=None,
        leader_id=10, starts_at=None,
    )
    store.record_audit(raid_id=other.id, action="raid", source="discord",
                       actor_id=10, actor_name="raidlead", detail="raid cancelled")
    check("logs do not bleed between raids", len(store.audit_entries(raid.id)) == 1)
    check("the retention trim is per raid, not global",
          len(store.audit_entries(other.id)) == 1)

    for n in range(AUDIT_RETAINED + 5):
        store.record_audit(raid_id=raid.id, action="apply", source="discord",
                           actor_id=77, actor_name="trollman", detail=f"spam {n}")
    kept = store.db.execute(
        "SELECT COUNT(*) c FROM audit_log WHERE raid_id=?", (raid.id,)
    ).fetchone()["c"]
    check("one player spamming cannot grow the log without bound",
          kept == AUDIT_RETAINED, f"{kept} rows")
    check("the other raid's log is untouched by that trim",
          len(store.audit_entries(other.id)) == 1)
    store.close()

    # Both are new since the live database was created, so both have to arrive
    # by migration rather than by CREATE TABLE on a fresh file.
    legacy = Path(tmp) / "pre-audit.sqlite3"
    old = _sqlite3.connect(legacy)
    old.executescript("""
        CREATE TABLE signups (
            raid_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            character_name TEXT NOT NULL, logs_url TEXT, spec_key TEXT NOT NULL,
            status TEXT NOT NULL, note TEXT, updated_at INTEGER NOT NULL,
            updated_by INTEGER, PRIMARY KEY (raid_id, user_id));
        INSERT INTO signups (raid_id, user_id, character_name, spec_key, status, updated_at)
        VALUES (1, 55, 'Older', 'pal_prot', 'accepted', 0);
    """)
    old.commit()
    old.close()

    store = Store(legacy)        # must not raise
    kept_signup = store.get_signup(1, 55)
    check("signups written before the column still load",
          kept_signup is not None and kept_signup.character_name == "Older")
    check("their handle is simply unknown", kept_signup.discord_name is None)
    check("the audit table is created on an existing database",
          store.audit_entries(1) == [])
    Store(legacy)                # second open must be a no-op
    check("both migrations are idempotent", True)
    store.close()

print("\n[7m] roster sorts by class alphabetically, then name")
from bot.ui.embeds import _sorted as _srt
from bot.store import Signup as _Sig
def _sig(char, spec):
    return _Sig(raid_id=1, user_id=hash(char) & 0xffff, character_name=char, logs_url=None,
                spec_key=spec, status=Status.ACCEPTED, note=None, updated_at=0,
                updated_by=None, discord_name=None)
# mix classes/specs; expect grouped by class name alphabetical, then char name
rows = [_sig("Zed", "warr_arms"), _sig("Amy", "druid_balance"),
        _sig("Bob", "druid_resto"), _sig("Cara", "evoker_dev"),
        _sig("Dan", "dk_blood")]
order = [(_srt(rows)[i].character_name) for i in range(len(rows))]
# classes: Death Knight(Dan), Druid(Amy,Bob), Evoker(Cara), Warrior(Zed)
check("class-alphabetical, name within class", order == ["Dan", "Amy", "Bob", "Cara", "Zed"],
      str(order))
check("two specs of one class stay adjacent",
      abs(order.index("Amy") - order.index("Bob")) == 1)

print("\n[7l] @mention on the accepted roster (label, never a ping)")
from bot.ui.embeds import _roster_line as _rl
from bot.store import Signup as _Signup
_mk = lambda uid, st=Status.ACCEPTED: _Signup(
    raid_id=1, user_id=uid, character_name="Miimzz-kazzak", logs_url=None,
    spec_key="druid_resto", status=st, note=None, updated_at=0, updated_by=None,
    discord_name="mimz")
check("a line without mention has no <@ tag", "<@" not in _rl(_mk(1), mention=False))
check("mention appends <@user_id> to the line", "<@42>" in _rl(_mk(42), mention=True))
check("the mention is a bare user tag Discord renders as @handle in an embed",
      _rl(_mk(42), mention=True).rstrip().endswith("<@42>"))
# The config gate: only listed ids (or "all") are mentioned.
import os as _os, importlib as _il, bot.config as _cfg
_prev = _os.environ.get("MENTION_ON_ACCEPT")
try:
    _os.environ["MENTION_ON_ACCEPT"] = "42, 99"
    _il.reload(_cfg)
    check("a listed id is mentioned", _cfg.mention_on_accept(42) and _cfg.mention_on_accept(99))
    check("an unlisted id is not", not _cfg.mention_on_accept(7))
    _os.environ["MENTION_ON_ACCEPT"] = "all"
    _il.reload(_cfg)
    check("'all' mentions everyone", _cfg.mention_on_accept(123456))
    _os.environ["MENTION_ON_ACCEPT"] = ""
    _il.reload(_cfg)
    check("blank turns it off", not _cfg.mention_on_accept(42))
finally:
    if _prev is None: _os.environ.pop("MENTION_ON_ACCEPT", None)
    else: _os.environ["MENTION_ON_ACCEPT"] = _prev
    _il.reload(_cfg)

print("\n[7k] accepted comp keeps full format, overflowing into invisible blocks")
import bot.emojis as _emojis
from bot.ui.embeds import BLANK as _BLANK
_orig_spec = _emojis.registry.spec
_emojis.registry.spec = lambda key: "<:spec_placeholder:1528176451957030954>"  # ~40ch
try:
    with tempfile.TemporaryDirectory() as _tmp:
        _st = Store(Path(_tmp) / "comp.sqlite3")
        _raid = _st.create_raid(guild_id=1, channel_id=1, title="Big", description=None,
                                leader_id=1, starts_at=None, timezone="EU",
                                caps={"tank": 2, "healer": 2, "dps": 23})
        def _add(spec, n, start):
            for _i in range(n):
                u = start + _i; nm = f"Char{u}"
                _st.upsert_signup(raid_id=_raid.id, user_id=u, character_name=f"{nm}-Kazzak",
                    logs_url=None, spec_key=spec, status=Status.ACCEPTED, discord_name=f"u{u}")
                _st.set_logs_url(_raid.id, u,
                    f"https://www.warcraftlogs.com/character/eu/kazzak/{nm.lower()}")
        _add("pal_prot", 2, 1); _add("priest_holy", 2, 50); _add("mage_frost", 23, 100)
        _e = build_raid_embed(_st.get_raid(_raid.id), _st.signups(_raid.id))

        # collect the ranged header field and its following invisible blocks
        _blocks, _seen_header = [], False
        for f in _e.fields:
            if "Ranged" in f.name:
                _blocks = [f]; _seen_header = True
            elif _seen_header and f.name == _BLANK:
                _blocks.append(f)
            elif _seen_header:
                break
        _shown = sum(b.value.count("Char") for b in _blocks)
        check("all 23 ranged shown across the blocks", _shown == 23, f"{_shown}/23")
        check("more than one block was needed", len(_blocks) > 1, f"{len(_blocks)} blocks")
        check("only the first block carries the role heading",
              "Ranged" in _blocks[0].name and all(b.name == _BLANK for b in _blocks[1:]))
        check("every block keeps the spec emoji + link + full class",
              all("spec_placeholder" in b.value and "warcraftlogs" in b.value
                  and "—" in b.value for b in _blocks))
        check("no '(cont.)' label, no '+N more'",
              not any("(cont.)" in f.name for f in _e.fields)
              and not any("more" in b.value for b in _blocks))
        check("every field within Discord's 1024", all(len(f.value) <= 1024 for f in _e.fields))
        check("whole embed within 6000", len(_e) <= 6000, str(len(_e)))
        check("field count within Discord's 25", len(_e.fields) <= 25, str(len(_e.fields)))
        _st.close()
finally:
    _emojis.registry.spec = _orig_spec

print("\n[7j] derived Warcraft Logs links")
from bot.ui.common import WCL_RE, derive_logs_url  # noqa: E402

check("the documented case: mimz-kazzak + EU",
      derive_logs_url("mimz-kazzak", "EU")
      == "https://www.warcraftlogs.com/character/eu/kazzak/mimz")
check("a realm with a space is slugged",
      derive_logs_url("Acidtab-Tarren Mill", "EU")
      == "https://www.warcraftlogs.com/character/eu/tarren-mill/acidtab")
check("NA folds to the WCL 'us' region",
      derive_logs_url("Fabregaas-Turalyon", "NA")
      == "https://www.warcraftlogs.com/character/us/turalyon/fabregaas")
check("Oceanic also folds to us", derive_logs_url("x-Frostmourne", "OCE").split("/")[4] == "us")
check("no realm half -> no guess", derive_logs_url("Mimz", "EU") is None)
check("an IANA zone WCL cannot name -> no guess",
      derive_logs_url("mimz-kazzak", "Europe/Paris") is None)
check("empty / missing inputs -> None",
      derive_logs_url("", "EU") is None and derive_logs_url("a-b", None) is None)
check("every derived link passes the same gate a typed one faces",
      all(WCL_RE.match(derive_logs_url(c, "EU")) for c in
          ("mimz-kazzak", "Acidtab-Tarren Mill", "Ka'el-Argent Dawn")))
check("an accented name keeps its accent (matsú, not mats)",
      derive_logs_url("matsú-Draenor", "EU")
      == "https://www.warcraftlogs.com/character/eu/draenor/matsú")
check("a diaeresis survives (Kaïasana, not Kaasana)",
      derive_logs_url("Kaïasana-Ysondre", "EU")
      == "https://www.warcraftlogs.com/character/eu/ysondre/kaïasana")
check("a spaced realm with an accented name still slugs both",
      derive_logs_url("Ocbslìm-Tarren Mill", "EU")
      == "https://www.warcraftlogs.com/character/eu/tarren-mill/ocbslìm")
check("only the first hyphen splits name from realm",
      derive_logs_url("Name-Two-Word", "EU")
      == "https://www.warcraftlogs.com/character/eu/two-word/name")

print("\n[7i] gateway watchdog decision")
from bot.client import gateway_is_stale  # noqa: E402

# Live connection: never stale, no matter how long ago it last dropped.
check("a connected gateway is never stale",
      not gateway_is_stale(True, None, 10_000.0, 120))
check("connected wins even with a stale down_since",
      not gateway_is_stale(True, 0.0, 10_000.0, 120))
# Disconnected: stale only once the threshold is crossed.
check("just-dropped is not yet stale",
      not gateway_is_stale(False, 1_000.0, 1_030.0, 120))
check("crossing the threshold reads as stale",
      gateway_is_stale(False, 1_000.0, 1_120.0, 120))
check("well past the threshold is stale",
      gateway_is_stale(False, 1_000.0, 1_500.0, 120))
# A bot that has never connected has down_since set at construction, so a boot
# that never reaches the gateway is recycled too.
check("a never-connected boot goes stale after the window",
      gateway_is_stale(False, 0.0, 121.0, 120))
# The escape hatch and the pre-connect state must never trip it.
check("threshold 0 disables the watchdog",
      not gateway_is_stale(False, 0.0, 10_000.0, 0))
check("no recorded downtime is never stale",
      not gateway_is_stale(False, None, 10_000.0, 120))

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
