"""Re-render existing raid boards in place, without changing any roster data.

    python -m tools.refresh_boards            # every live raid
    python -m tools.refresh_boards 3 6        # just these raid ids
    python -m tools.refresh_boards --dry-run  # list what would be touched

A board is only redrawn when something about it changes, so a raid posted
before a layout or button change keeps the old rendering indefinitely. This
edits the existing message, which means the raid keeps its place in the channel
and its replies - unlike `/raid repost`, which abandons the old message and
posts a new one.

Runs as its own short-lived connection rather than inside the bot. It is
deliberately a bare Client and not RaidClient: the latter starts the reminder
loop, and a second process running that alongside the live bot could ping the
roster twice.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import discord  # noqa: E402

from bot.config import require_token  # noqa: E402
from bot.emojis import registry  # noqa: E402
from bot.store import RaidState, Store  # noqa: E402
from bot.ui.common import SAFE_MENTIONS  # noqa: E402
from bot.ui.embeds import build_raid_embed  # noqa: E402
from bot.ui.panel import RaidView  # noqa: E402


def targets(store: Store, wanted: list[int]) -> list:
    if wanted:
        raids = [store.get_raid(rid) for rid in wanted]
        missing = [rid for rid, raid in zip(wanted, raids) if raid is None]
        if missing:
            raise SystemExit(f"no such raid: {', '.join(str(m) for m in missing)}")
        return list(raids)

    rows = store.db.execute(
        "SELECT id FROM raids WHERE state != ? AND message_id IS NOT NULL ORDER BY id",
        (RaidState.CANCELLED.value,),
    ).fetchall()
    return [store.get_raid(row["id"]) for row in rows]


async def run(wanted: list[int], dry_run: bool) -> int:
    store = Store()
    raids = targets(store, wanted)
    if not raids:
        print("nothing to refresh")
        return 0

    print(f"{len(raids)} board(s) to refresh:")
    for raid in raids:
        signups = len(store.signups(raid.id))
        anchor = raid.message_id or "— never posted, use /raid repost"
        print(f"  #{raid.id:<3} {raid.title[:40]:<42} {signups:>3} signups  msg {anchor}")
    if dry_run:
        return 0

    client = discord.Client(intents=discord.Intents.default(), allowed_mentions=SAFE_MENTIONS)
    failures = 0

    @client.event
    async def on_ready() -> None:
        nonlocal failures
        # Without this every custom spec, role and buff emoji resolves to an
        # empty string, so a "refresh" would quietly strip the board's icons.
        await registry.sync(client)
        print()
        for raid in raids:
            if raid.message_id is None:
                print(f"  #{raid.id} skipped — no message to edit (use /raid repost)")
                continue
            try:
                channel = client.get_channel(raid.channel_id) or await client.fetch_channel(
                    raid.channel_id
                )
                message = await channel.fetch_message(raid.message_id)
                await message.edit(
                    embed=build_raid_embed(raid, store.signups(raid.id)), view=RaidView(raid)
                )
                print(f"  #{raid.id} refreshed — {raid.title[:50]}")
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                failures += 1
                print(f"  #{raid.id} FAILED — {type(exc).__name__}: {exc}")
        await client.close()

    await client.start(require_token())
    store.close()
    return failures


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    dry_run = "--dry-run" in sys.argv[1:]
    if any(not a.isdigit() for a in args):
        raise SystemExit(__doc__)
    failures = asyncio.run(run([int(a) for a in args], dry_run))
    print("\n" + ("done" if not failures else f"{failures} board(s) failed"))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
