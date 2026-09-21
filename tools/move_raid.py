"""Move a raid's signup board to another channel, keeping every signup.

    python -m tools.move_raid RAID_ID CHANNEL_ID              # move one raid
    python -m tools.move_raid 20 123456789012345678 --dry-run
    python -m tools.move_raid 20 123456789012345678 --delete-old

Signups belong to the raid, not to the message, so nobody has to sign up again:
the board is posted afresh in the target channel and the raid is re-anchored to
it. The old board is edited into a one-line pointer at the new location rather
than deleted, so anyone still looking at the old channel finds their way -
pass --delete-old to remove it outright instead.

One raid per run, deliberately. Posting is not idempotent, and a typo in a bulk
form would scatter boards across the wrong channels with no undo.
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


async def run(raid_id: int, channel_id: int, dry_run: bool, delete_old: bool) -> int:
    store = Store()
    raid = store.get_raid(raid_id)
    if raid is None:
        raise SystemExit(f"no such raid: {raid_id}")
    if raid.channel_id == channel_id:
        raise SystemExit(f"raid #{raid_id} is already in channel {channel_id}")

    signups = store.signups(raid.id)
    print(f"raid #{raid.id}  {raid.title}")
    print(f"  {len(signups)} signups (all kept)")
    print(f"  from channel {raid.channel_id}")
    print(f"  to   channel {channel_id}")
    if dry_run:
        return 0

    client = discord.Client(intents=discord.Intents.default(), allowed_mentions=SAFE_MENTIONS)
    failed = 0

    @client.event
    async def on_ready() -> None:
        nonlocal failed
        # Without this every spec, role and buff emoji renders as an empty
        # string and the moved board arrives with no icons.
        await registry.sync(client)
        try:
            target = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
            if getattr(target, "guild", None) is None or target.guild.id != raid.guild_id:
                raise SystemExit(
                    f"channel {channel_id} is not in raid #{raid.id}'s server - refusing"
                )

            # New board first. If this fails nothing has changed yet.
            message = await target.send(
                embed=build_raid_embed(raid, signups), view=RaidView(raid)
            )
            old_channel_id, old_message_id = raid.channel_id, raid.message_id
            store.move_raid_board(raid.id, channel_id, message.id)
            store.record_audit(
                raid_id=raid.id, action="raid", source="system",
                detail=f"board moved from channel {old_channel_id} to {channel_id}",
            )
            print(f"\n  posted  {message.jump_url}")
            print(f"  re-anchored raid #{raid.id} -> channel {channel_id}, message {message.id}")

            # Old board last. It is now just a stale rendering; the raid no
            # longer points at it, so its buttons would fail anyway.
            if old_message_id is None:
                print("  no old board to tidy up")
                return
            try:
                source = client.get_channel(old_channel_id) or await client.fetch_channel(
                    old_channel_id
                )
                old = await source.fetch_message(old_message_id)
                if delete_old:
                    await old.delete()
                    print("  deleted the old board")
                else:
                    await old.edit(
                        content=f"📦 **{raid.title}** has moved to <#{channel_id}> — "
                        f"sign up there. Your existing signup is already carried over.",
                        embed=None, view=None,
                    )
                    print("  old board replaced with a pointer to the new channel")
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                # Not a failure of the move - the raid is already re-anchored.
                print(f"  ! could not tidy the old board ({type(exc).__name__}: {exc});"
                      f" delete it by hand")
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            failed = 1
            print(f"\n  FAILED - {type(exc).__name__}: {exc}")
        finally:
            await client.close()

    await client.start(require_token())
    store.close()
    return failed


def main() -> None:
    flags = {"--dry-run", "--delete-old"}
    args = [a for a in sys.argv[1:] if a not in flags]
    if len(args) != 2 or not all(a.isdigit() for a in args):
        raise SystemExit(__doc__)
    failed = asyncio.run(
        run(int(args[0]), int(args[1]), "--dry-run" in sys.argv, "--delete-old" in sys.argv)
    )
    print("\n" + ("done" if not failed else "failed"))
    sys.exit(failed)


if __name__ == "__main__":
    main()
