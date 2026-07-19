"""The bot class.

Kept out of __main__.py so that UI modules can import `RaidClient` for typing
without pulling in the process entry point.
"""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from .config import GUILD_ID
from .emojis import registry
from .store import Store
from .ui.common import SAFE_MENTIONS
from .ui.panel import RaidView
from .ui.schedule import ReminderTask

log = logging.getLogger(__name__)


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
        self._emojis_synced = False

    async def setup_hook(self) -> None:
        await self.load_extension("bot.cogs.raid")
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
        # on_ready fires again after every reconnect, so guard the one-time work.
        if not self._emojis_synced:
            await registry.sync(self)
            self._emojis_synced = True
        if not self.reminders._loop.is_running():
            self.reminders.start()
        log.info("logged in as %s (%s guilds)", self.user, len(self.guilds))

    async def close(self) -> None:
        self.reminders.stop()
        self.store.close()
        await super().close()


