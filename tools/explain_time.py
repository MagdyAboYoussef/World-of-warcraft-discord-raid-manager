"""Show how one stored timestamp renders for players around the world.

    python -m tools.explain_time [epoch]

The bot stores a single absolute instant. Discord - not the bot - renders it in
each viewer's own timezone. This makes that concrete.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.config import RAID_TIMEZONE, resolve_timezone  # noqa: E402
from bot.ui.schedule import parse_when  # noqa: E402

epoch = int(sys.argv[1]) if len(sys.argv) > 1 else parse_when("sat 19:00")[0]

print(f"RAID_TIMEZONE={RAID_TIMEZONE}  ->  {resolve_timezone()}")
print(f"stored in SQLite as a single integer: {epoch}")
print(f"sent to Discord as the literal text: <t:{epoch}:f>\n")
print("Discord renders that same token differently for each viewer:\n")

viewers = [
    ("Raid lead (EU realm time)", resolve_timezone()),
    ("Player in London", "Europe/London"),
    ("Player in Cairo", "Africa/Cairo"),
    ("Player in New York", "America/New_York"),
    ("Player in Los Angeles", "America/Los_Angeles"),
    ("Player in Sydney", "Australia/Sydney"),
]
width = max(len(name) for name, _ in viewers)
for name, zone in viewers:
    local = datetime.fromtimestamp(epoch, ZoneInfo(zone))
    print(f"  {name:<{width}}  {local:%a %d %b, %H:%M}  ({local:%Z}, UTC{local:%z})")

print("\nSame instant every time — only the wall clock differs.")
