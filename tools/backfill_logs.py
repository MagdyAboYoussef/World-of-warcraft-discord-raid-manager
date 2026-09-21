"""Fill in a Warcraft Logs link for signups that never had one.

    python -m tools.backfill_logs            # apply to every raid
    python -m tools.backfill_logs --dry-run  # list what would change
    python -m tools.backfill_logs 21 22      # just these raid ids

For each signup with no logs link whose character reads "Name-Realm", derives
https://www.warcraftlogs.com/character/<region>/<realm>/<name> from the raid's
own region and stores it. Signups that already have a link, or whose character
name has no realm half, are left untouched. Runs directly against the database
- no gateway connection - so it is safe to run while the bot is up; the boards
pick the links up on their next refresh.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.config import region_label  # noqa: E402
from bot.store import Store  # noqa: E402
from bot.ui.common import derive_logs_url  # noqa: E402


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    dry = "--dry-run" in sys.argv[1:]
    if any(not a.isdigit() for a in args):
        raise SystemExit(__doc__)
    wanted = {int(a) for a in args}

    store = Store()
    raids = [r for r in store.all_raids(limit=100000) if not wanted or r.id in wanted]
    filled = skipped_have = skipped_shape = 0

    for raid in raids:
        region = region_label(raid.timezone)
        for s in store.signups(raid.id):
            if s.logs_url:
                skipped_have += 1
                continue
            url = derive_logs_url(s.character_name, region)
            if url is None:
                skipped_shape += 1
                continue
            print(f"  #{raid.id:<4} {s.character_name:<26} [{region}] -> {url}")
            if not dry:
                store.set_logs_url(raid.id, s.user_id, url)
            filled += 1

    verb = "would fill" if dry else "filled"
    print(
        f"\n{verb} {filled} · already had a link {skipped_have} · "
        f"no realm in the name {skipped_shape}"
    )
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
