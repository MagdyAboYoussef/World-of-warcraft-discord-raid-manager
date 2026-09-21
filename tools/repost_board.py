"""Post a fresh board for a raid whose message is gone, and re-anchor it.

    python -m tools.repost_board 12             # post a new board for raid 12
    python -m tools.repost_board 12 --dry-run   # say what it would do
    python -m tools.repost_board 12 --force     # even if the old board survives

The companion to tools.refresh_boards, for the one case that tool cannot fix:
its edit needs a message to edit, so a board whose message was deleted - or was
posted into a server the bot had no access to - stays broken for good. The raid
itself is untouched either way; the roster lives in the database, and only the
message the board is drawn on is being replaced.

Raid ids must be given explicitly. There is no "all" mode on purpose: posting a
new message is not idempotent, and a stray run would litter every channel with
duplicate boards.
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
from bot.store import Store  # noqa: E402
from bot.ui.common import SAFE_MENTIONS  # noqa: E402
from bot.ui.embeds import build_raid_embed  # noqa: E402
from bot.ui.panel import RaidView  # noqa: E402


async def _still_there(client: discord.Client, raid) -> bool:
    """Does this raid's current board message still exist and reachable?"""
    if raid.message_id is None:
        return False
    try:
        channel = client.get_channel(raid.channel_id) or await client.fetch_channel(
            raid.channel_id
        )
        await channel.fetch_message(raid.message_id)
        return True
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return False


async def run(wanted: list[int], dry_run: bool, force: bool) -> int:
    store = Store()
    raids = [store.get_raid(rid) for rid in wanted]
    missing = [rid for rid, raid in zip(wanted, raids) if raid is None]
    if missing:
        raise SystemExit(f"no such raid: {', '.join(str(m) for m in missing)}")

    print(f"{len(raids)} raid(s):")
    for raid in raids:
        print(
            f"  #{raid.id:<3} {raid.title[:36]:<38} "
            f"{len(store.signups(raid.id)):>3} signups  channel {raid.channel_id}"
        )
    if dry_run:
        return 0

    client = discord.Client(intents=discord.Intents.default(), allowed_mentions=SAFE_MENTIONS)
    failures = 0

    @client.event
    async def on_ready() -> None:
        nonlocal failures
        # Without this every custom spec, role and buff emoji resolves to an
        # empty string, so the new board would come out with no icons at all.
        await registry.sync(client)
        print()
        for raid in raids:
            try:
                if not force and await _still_there(client, raid):
                    print(
                        f"  #{raid.id} skipped — its board is still there. "
                        f"Use tools.refresh_boards to update it in place, or "
                        f"--force to post a second one anyway."
                    )
                    continue
                channel = client.get_channel(raid.channel_id) or await client.fetch_channel(
                    raid.channel_id
                )
                message = await channel.send(
                    embed=build_raid_embed(raid, store.signups(raid.id)),
                    view=RaidView(raid),
                )
                # Re-anchored before anything else can touch it: until this
                # lands, the raid still points at a message that is not there.
                store.set_raid_message(raid.id, message.id)
                print(f"  #{raid.id} reposted — {raid.title[:44]}")
                print(f"       {message.jump_url}")
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                failures += 1
                print(f"  #{raid.id} FAILED — {type(exc).__name__}: {exc}")
        await client.close()

    await client.start(require_token())
    store.close()
    return failures


def main() -> None:
    flags = {"--dry-run", "--force"}
    args = [a for a in sys.argv[1:] if a not in flags]
    if not args or any(not a.isdigit() for a in args):
        raise SystemExit(__doc__)
    failures = asyncio.run(
        run([int(a) for a in args], "--dry-run" in sys.argv[1:], "--force" in sys.argv[1:])
    )
    print("\n" + ("done" if not failures else f"{failures} raid(s) failed"))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
