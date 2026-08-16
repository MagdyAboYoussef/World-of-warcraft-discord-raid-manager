"""Import every module and assert the Discord component tree is well-formed.

    python -m tools.import_check

Catches circular imports, bad custom_ids, and Discord's structural limits
(25 options per select, 5 components per row, 5 rows per view) without needing
a token or a live gateway connection.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import time as _t  # noqa: E402

import discord  # noqa: E402

from bot.cogs.raid import RaidCog  # noqa: E402
from bot.data.specs import CLASSES, specs_for_class  # noqa: E402
from bot.ui.admin import DetailsModal, RaidSettings, RosterManager  # noqa: E402
from bot.ui.apply import (  # noqa: E402
    CachedProfileView, ProfileModal, SpecPickerView,
)
from bot.ui.panel import RaidView  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {label} {detail}".rstrip())
    if not condition:
        failures.append(label)


print(f"discord.py {discord.__version__}")

print("\n[1] application emoji API available")
for attr in ("fetch_application_emojis", "create_application_emoji"):
    check(f"Client.{attr}", hasattr(discord.Client, attr))

print("\n[2] persistent raid panel")
view = RaidView()
check("timeout is None (persistent)", view.timeout is None)
check("view.is_persistent()", view.is_persistent())
ids = [c.custom_id for c in view.children]
check("all buttons have custom_ids", all(ids), str(ids))
check("custom_ids unique", len(set(ids)) == len(ids))
check("<=25 components", len(view.children) <= 25, str(len(view.children)))

print("\n[3] spec picker respects the 25-option select cap")
check("13 classes fits one select", len(CLASSES) <= 25, str(len(CLASSES)))
worst = max(len(specs_for_class(c)) for c in CLASSES)
check("largest class fits one select", worst <= 25, f"{worst} specs")
picker = SpecPickerView(lambda i, s: None)
for child in picker.children:
    if isinstance(child, discord.ui.Select):
        check(f"select '{child.placeholder}' <=25 options", len(child.options) <= 25)

print("\n[4] modals")
modal = ProfileModal(1)
check("apply modal has <=5 inputs", len(modal.children) <= 5, str(len(modal.children)))
check("apply modal title set", bool(modal.title))

# Discord allows at most 5 components in a modal. DetailsModal is at the cap,
# so a sixth field would fail at runtime rather than here.
details = DetailsModal(1, "t", None, "", "", "EU")
check("details modal has <=5 inputs", len(details.children) <= 5, str(len(details.children)))
check(
    "details modal covers title/desc/when/duration/timezone",
    len(details.children) == 5, str(len(details.children)),
)

print("\n[4b] cached-profile view (apply / tentative / bench / absent)")


class _Player:
    character_name = "Mimz"
    logs_url = None
    spec_key = "pal_holy"


_cached = CachedProfileView(1, _Player())
_rows: dict[int, int] = {}
for _item in _cached.children:
    _rows[_item.row] = _rows.get(_item.row, 0) + 1
check("<=5 rows", len(_rows) <= 5, str(sorted(_rows)))
check("no row exceeds 5 components", all(n <= 5 for n in _rows.values()), str(_rows))
_labels = {getattr(i, "label", None) for i in _cached.children}
check("offers a direct tentative sign-up", "Tentative / late" in _labels, str(_labels))
check("offers a direct bench sign-up", "Bench me" in _labels)
check("offers a direct absent sign-up", "Absent" in _labels)

print("\n[5] admin views build")
manager = RosterManager(1)
rows: dict[int, int] = {}
for child in manager.children:
    rows[child.row] = rows.get(child.row, 0) + 1
check("roster manager <=5 rows", max(rows) < 5, f"rows={sorted(rows)}")
check("no row exceeds 5 components", all(n <= 5 for n in rows.values()), str(rows))
check("selects present", any(isinstance(c, discord.ui.Select) for c in manager.children))
settings = RaidSettings(1)
settings_rows: dict[int, int] = {}
for item in settings.children:
    settings_rows[item.row or 0] = settings_rows.get(item.row or 0, 0) + 1
check("settings view builds", bool(settings.children), str(len(settings.children)))
check("settings uses <=5 rows", len(settings_rows) <= 5, str(sorted(settings_rows)))
check("no settings row exceeds 5 components",
      all(n <= 5 for n in settings_rows.values()), str(settings_rows))
check("settings has an auto-accept toggle",
      any(getattr(c, "label", None) == "Auto-accept" for c in settings.children))
check(
    "settings has a Done button",
    any(getattr(c, "label", None) == "Done" for c in settings.children),
)
check(
    "roster manager has a Done button",
    any(getattr(c, "label", None) == "Done" for c in manager.children),
)
# A disabled action button is a silent no-op for the admin - the exact bug that
# made an Accept click do nothing. They must stay clickable and explain instead.
action_labels = {"Accepted", "Declined", "Benched", "Absent", "Pending", "Change spec", "Remove"}
stuck = [
    c.label for c in manager.children
    if getattr(c, "label", None) in action_labels and getattr(c, "disabled", False)
]
check("no action button ships disabled", not stuck, str(stuck))
# Defaulting to Pending hides players who benched/absented themselves, which
# reads as "the bot ignored my accept".
check("roster manager defaults to showing all signups", manager.filter is None, str(manager.filter))

# A read-only SQLite connection cannot attach the WAL index, so it reports a
# stale snapshot and hides recent commits. That is a debugging trap.
inspect_code = [
    line
    for line in (Path(__file__).parent / "inspect_db.py").read_text(encoding="utf-8").splitlines()
    if not line.lstrip().startswith("#")
]
check(
    "inspect_db does not open the WAL database read-only",
    not any("mode=ro" in line for line in inspect_code),
)

print("\n[4c] player buttons disappear once a raid is closed")


class _Raid:
    """Minimal stand-in: RaidView only asks whether the raid is closed."""
    def __init__(self, state, starts_at, duration_minutes):
        self.state, self.starts_at = state, starts_at
        self.duration_minutes, self.created_at = duration_minutes, 0


from bot.store import RaidState as _RS  # noqa: E402
from bot.ui.panel import PLAYER_BUTTONS  # noqa: E402

_ids = lambda v: {c.custom_id for c in v.children}
_open = _Raid(_RS.OPEN, int(_t.time()) + 7200, 180)
_over = _Raid(_RS.OPEN, int(_t.time()) - 86400, 180)
_cancelled = _Raid(_RS.CANCELLED, int(_t.time()) + 7200, 180)

check("no-arg view keeps every button (needed for persistence)",
      PLAYER_BUTTONS <= _ids(RaidView()), str(_ids(RaidView())))
check("upcoming raid keeps the player buttons", PLAYER_BUTTONS <= _ids(RaidView(_open)))
for _label, _r in (("finished", _over), ("cancelled", _cancelled)):
    _left = _ids(RaidView(_r))
    check(f"{_label} raid drops every player button",
          not (PLAYER_BUTTONS & _left), str(_left))
    check(f"{_label} raid keeps the admin buttons",
          {"raid:manage", "raid:settings"} <= _left, str(_left))

print("\n[5b] /raid create option descriptions")
_create = next(c for c in RaidCog.raid.commands if c.name == "create")
for _p in _create.parameters:
    check(f"{_p.name}: description within Discord's 100 chars",
          len(_p.description) <= 100, str(len(_p.description)))
_optional = [p for p in _create.parameters if not p.required]
check("only `title` is required",
      [p.name for p in _create.parameters if p.required] == ["title"])
check("every optional option is marked (optional)",
      all(p.description.startswith("(optional)") for p in _optional),
      str([p.name for p in _optional if not p.description.startswith("(optional)")]))
check("auto_accept exists and defaults to false",
      any(p.name == "auto_accept" and p.default is False for p in _create.parameters))
check("melee/ranged say they are unused with dps",
      all("dps" in p.description for p in _optional if p.name in ("melee", "ranged")))

print("\n[6] command tree")
group = RaidCog.raid
names = {c.name for c in group.commands}
check("raid group commands", {"create", "list", "manage", "settings", "repost"} <= names, str(names))

top_level = {c.name for c in RaidCog.__cog_app_commands__}
check("top-level commands include help + profile", {"help", "profile"} <= top_level, str(top_level))
check("help is not admin-gated", not RaidCog.help_command.checks)

print()
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    raise SystemExit(1)
print("all checks passed")
