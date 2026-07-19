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

import discord  # noqa: E402

from bot.cogs.raid import RaidCog  # noqa: E402
from bot.data.specs import CLASSES, specs_for_class  # noqa: E402
from bot.ui.admin import DetailsModal, RaidSettings, RosterManager  # noqa: E402
from bot.ui.apply import ProfileModal, SpecPickerView  # noqa: E402
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

print("\n[5] admin views build")
manager = RosterManager(1)
rows: dict[int, int] = {}
for child in manager.children:
    rows[child.row] = rows.get(child.row, 0) + 1
check("roster manager <=5 rows", max(rows) < 5, f"rows={sorted(rows)}")
check("no row exceeds 5 components", all(n <= 5 for n in rows.values()), str(rows))
check("selects present", any(isinstance(c, discord.ui.Select) for c in manager.children))
settings = RaidSettings(1)
check("settings view builds", len(settings.children) == 5, str(len(settings.children)))
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
