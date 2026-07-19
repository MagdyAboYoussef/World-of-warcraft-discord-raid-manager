"""Application-emoji registry.

Application emojis belong to the bot rather than to a guild: they do not consume
the server's 50-emoji budget, they work in every guild the bot is in, and the
app may own up to 2000 of them. We need ~60 (40 specs + 4 roles + ~19 buffs).

`sync()` is idempotent - it uploads only what is missing, so it is safe to run
on every startup.
"""

from __future__ import annotations

import logging
from pathlib import Path

import discord

from .config import ASSETS
from .data.buffs import icon_jobs
from .data.specs import CLASS_ICONS, ROLE_ORDER, SPECS

log = logging.getLogger(__name__)

#: Plain-text stand-ins used when an emoji has not been uploaded yet, so the
#: roster still renders sensibly on a fresh install.
ROLE_FALLBACK = {"tank": "🛡️", "healer": "💚", "melee": "⚔️", "ranged": "🏹"}


class EmojiRegistry:
    def __init__(self) -> None:
        self._by_name: dict[str, discord.Emoji] = {}

    # ------------------------------------------------------------------ lookup

    def get(self, name: str) -> str:
        emoji = self._by_name.get(name)
        return str(emoji) if emoji else ""

    def spec(self, spec_key: str) -> str:
        return self.get(spec_key)

    def role(self, role_value: str) -> str:
        return self.get(f"role_{role_value}") or ROLE_FALLBACK.get(role_value, "")

    def buff(self, emoji_name: str) -> str:
        return self.get(emoji_name)

    def wow_class(self, class_name: str) -> str:
        return self.get(f"class_{class_name.lower().replace(' ', '_')}")

    # -------------------------------------------------------------------- sync

    def _expected(self) -> dict[str, Path]:
        """emoji name -> source PNG."""
        want: dict[str, Path] = {}
        for spec in SPECS:
            want[spec.emoji_name] = ASSETS / spec.role.value / f"{spec.key}.png"
        for role in ROLE_ORDER:
            want[f"role_{role.value}"] = ASSETS / "role" / f"{role.value}.png"
        for wow_class in CLASS_ICONS:
            key = wow_class.lower().replace(" ", "_")
            want[f"class_{key}"] = ASSETS / "class" / f"{key}.png"
        for name, _slug in icon_jobs():
            want[name] = ASSETS / "buff" / f"{name}.png"
        return want

    async def sync(self, client: discord.Client) -> None:
        """Load existing application emojis and upload any that are missing."""
        try:
            existing = await client.fetch_application_emojis()
        except discord.HTTPException as exc:
            log.warning("could not fetch application emojis: %s", exc)
            return
        self._by_name = {e.name: e for e in existing}

        created = skipped = 0
        for name, path in self._expected().items():
            if name in self._by_name:
                continue
            if not path.exists():
                skipped += 1
                continue
            try:
                emoji = await client.create_application_emoji(name=name, image=path.read_bytes())
            except discord.HTTPException as exc:
                log.warning("failed to upload emoji %s: %s", name, exc)
                continue
            self._by_name[name] = emoji
            created += 1

        log.info(
            "emoji sync: %d present, %d uploaded, %d missing assets",
            len(self._by_name), created, skipped,
        )
        if skipped:
            log.warning("run `python -m tools.fetch_icons` to download missing icons")


registry = EmojiRegistry()
