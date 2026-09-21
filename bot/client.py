"""The bot class.

Kept out of __main__.py so that UI modules can import `RaidClient` for typing
without pulling in the process entry point.
"""

from __future__ import annotations

import logging
import os
import threading
import time

import discord
from discord.ext import commands

from .config import GATEWAY_WATCHDOG_SECONDS, GUILD_ID
from .emojis import registry
from .store import Store
from .ui.common import SAFE_MENTIONS
from .ui.panel import RaidView
from .ui.schedule import ReminderTask
from .web.server import RaidWebServer

log = logging.getLogger(__name__)


def gateway_is_stale(
    connected: bool, down_since: float | None, now: float, threshold: float
) -> bool:
    """Has the gateway been down long enough that a fresh restart beats waiting?

    Pure so the decision is testable; the side effect (exit the process) lives
    in the watchdog thread. A threshold of 0, a live connection, or a bot that
    has simply never been up yet all read as "not stale".
    """
    if threshold <= 0 or connected or down_since is None:
        return False
    return (now - down_since) >= threshold


class RaidClient(commands.Bot):
    def __init__(self) -> None:
        super().__init__(
            command_prefix=commands.when_mentioned,
            # Signups are entirely slash-command and component driven, so no
            # privileged intents are needed.
            intents=discord.Intents.default(),
            help_command=None,
            # Defence in depth: no message this bot ever sends can ping @everyone
            # or a role, whatever ends up in a raid title or description.
            allowed_mentions=SAFE_MENTIONS,
        )
        self.store = Store()
        self.reminders = ReminderTask(self)
        self.web = RaidWebServer(self)
        self._emojis_synced = False
        # Gateway health for the watchdog. Start "down" as of construction, so a
        # process that never manages to connect at all is also caught and
        # recycled rather than hanging forever on the initial backoff.
        self._gw_connected = False
        self._gw_down_since: float | None = time.monotonic()
        self._watchdog_started = False

    def _mark_gateway_alive(self) -> None:
        self._gw_connected = True
        self._gw_down_since = None

    def _mark_gateway_down(self) -> None:
        # Only the first drop of a continuous outage sets the clock, so the
        # watchdog measures how long we have been *continuously* disconnected
        # rather than resetting on each retry's on_disconnect.
        if self._gw_connected or self._gw_down_since is None:
            self._gw_down_since = time.monotonic()
        self._gw_connected = False

    async def on_connect(self) -> None:
        self._mark_gateway_alive()

    async def on_resumed(self) -> None:
        self._mark_gateway_alive()

    async def on_disconnect(self) -> None:
        self._mark_gateway_down()

    def _start_watchdog(self) -> None:
        """A daemon thread that force-restarts a bot stuck offline.

        A thread, not an asyncio task, deliberately: it must still fire if the
        event loop itself wedges, not only when the gateway drops under a
        healthy loop. It reads two plain attributes the loop writes; in CPython
        those reads need no lock.
        """
        if self._watchdog_started or GATEWAY_WATCHDOG_SECONDS <= 0:
            return
        self._watchdog_started = True
        threshold = GATEWAY_WATCHDOG_SECONDS
        # Check a few times per window, and at least every 15s, so a stale
        # gateway is caught promptly without a busy loop.
        interval = max(5.0, min(15.0, threshold / 4))

        def run() -> None:
            while True:
                time.sleep(interval)
                if gateway_is_stale(
                    self._gw_connected, self._gw_down_since, time.monotonic(), threshold
                ):
                    down_for = time.monotonic() - (self._gw_down_since or 0.0)
                    log.critical(
                        "gateway down for %.0fs (>= %ss); exiting so systemd "
                        "restarts a fresh session", down_for, threshold,
                    )
                    logging.shutdown()
                    os._exit(1)

        threading.Thread(target=run, name="gateway-watchdog", daemon=True).start()
        log.info("gateway watchdog armed (%ss)", threshold)

    async def setup_hook(self) -> None:
        self._start_watchdog()
        await self.load_extension("bot.cogs.raid")
        # Started here rather than in on_ready so it comes up once, not again
        # after every reconnect.
        await self.web.start()
        # Re-register the persistent panel so buttons on old messages keep working.
        self.add_view(RaidView())

        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("commands synced to guild %s", GUILD_ID)
        else:
            await self.tree.sync()
            log.info("commands synced globally (may take up to an hour to appear)")

    async def on_ready(self) -> None:
        self._mark_gateway_alive()
        # on_ready fires again after every reconnect, so guard the one-time work.
        if not self._emojis_synced:
            await registry.sync(self)
            self._emojis_synced = True
        if not self.reminders._loop.is_running():
            self.reminders.start()
        log.info("logged in as %s (%s guilds)", self.user, len(self.guilds))

    async def close(self) -> None:
        self.reminders.stop()
        await self.web.stop()
        self.store.close()
        await super().close()


