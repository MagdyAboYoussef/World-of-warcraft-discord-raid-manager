"""Environment-backed configuration."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

TOKEN: str = os.getenv("DISCORD_TOKEN", "")
#: Optional; when set, slash commands sync instantly to this guild instead of
#: taking up to an hour to propagate globally. Strongly recommended.
GUILD_ID: int | None = int(g) if (g := os.getenv("GUILD_ID", "").strip()) else None

DB_PATH: Path = Path(os.getenv("DB_PATH", ROOT / "data" / "raid.sqlite3"))
ASSETS: Path = ROOT / "assets" / "icons"

#: Roles (by name, case-insensitive) treated as raid admins in addition to
#: anyone holding Discord's Administrator permission.
ADMIN_ROLE_NAMES: frozenset[str] = frozenset(
    n.strip().lower() for n in os.getenv("ADMIN_ROLES", "Raid Leader,Officer").split(",") if n.strip()
)

DEFAULT_CAPS: dict[str, int] = {"tank": 2, "healer": 4, "melee": 7, "ranged": 7}

#: WoW gameplay regions and the realm clock each one runs on.
#:
#: Using the IANA zone rather than a fixed UTC offset means daylight saving is
#: handled for us: 20:30 server time stays 20:30 across the March/October
#: switches, exactly as it does in game.
#:
#: Blizzard has four gameplay regions - Americas & Oceania, Europe, Korea and
#: Taiwan (plus China, operated separately). These are listed first because they
#: are how guilds actually talk. Oceanic realms sit inside the Americas region
#: but run on Sydney time, which is why OCE is separate here.
PRIMARY_REGIONS: tuple[str, ...] = ("EU", "NA", "KR", "TW", "OCE")

#: Shown next to each region in the picker.
REGION_LABELS: dict[str, str] = {
    "EU": "CET/CEST — Europe",
    "NA": "CST/CDT — Americas",
    "KR": "KST — Korea",
    "TW": "UTC+8 — Taiwan",
    "OCE": "AEST/AEDT — Oceanic realms",
    "CN": "UTC+8 — China",
    "BR": "BRT — Brazilian realms",
    "US-EAST": "EST/EDT",
    "US-WEST": "PST/PDT",
}

REALM_ZONES: dict[str, str] = {
    # The four gameplay regions.
    "eu": "Europe/Paris",
    "na": "America/Chicago",
    "kr": "Asia/Seoul",
    "tw": "Asia/Taipei",
    # Oceanic realms are in the Americas region but keep Sydney time.
    "oce": "Australia/Sydney",
    "oceanic": "Australia/Sydney",
    # Aliases and finer overrides, for guilds whose realm clock differs from the
    # regional default. Not offered in the picker, but accepted if typed.
    "us": "America/Chicago",
    "us-central": "America/Chicago",
    "us-east": "America/New_York",
    "us-west": "America/Los_Angeles",
    "cn": "Asia/Shanghai",
    "br": "America/Sao_Paulo",
}

#: Fallback for any raid that doesn't carry its own. Either a region shorthand
#: from REALM_ZONES ("EU") or any IANA name. Individual raids override this, so
#: one shared bot instance can serve guilds on different regions.
RAID_TIMEZONE: str = os.getenv("RAID_TIMEZONE", "EU").strip()


#: --------------------------------------------------------------- web manager
#:
#: The roster manager page. Off unless WEB_BASE_URL is set, so an existing
#: install keeps behaving exactly as it did until its owner opts in.
WEB_BASE_URL: str = os.getenv("WEB_BASE_URL", "").strip().rstrip("/")
WEB_ENABLED: bool = bool(WEB_BASE_URL)

#: Bind to loopback by default. TLS and the public port belong to a reverse
#: proxy - exposing aiohttp directly would serve the roster over plain HTTP,
#: and these links are bearer credentials.
WEB_BIND: str = os.getenv("WEB_BIND", "127.0.0.1").strip()
WEB_PORT: int = int(os.getenv("WEB_PORT", "8080"))

#: How long an issued link stays usable. Short, because anyone holding the URL
#: holds roster control for that raid.
WEB_TOKEN_TTL_MINUTES: int = int(os.getenv("WEB_TOKEN_TTL_MINUTES", "180"))

#: A raid's page stops answering this many days after the raid ends.
WEB_RETENTION_DAYS: int = int(os.getenv("WEB_RETENTION_DAYS", "30"))

#: Assumed length of a raid that never had a duration set, used only to work
#: out when it "ended" for expiry purposes.
DEFAULT_RAID_DURATION_MINUTES: int = 180


def resolve_timezone(name: str | None = None) -> str:
    """Map a region shorthand to its IANA zone; pass IANA names through.

    `name` is a per-raid override; None falls back to the configured default.
    """
    raw = (name or RAID_TIMEZONE).strip()
    return REALM_ZONES.get(raw.lower(), raw)


def region_label(name: str | None) -> str:
    """How a stored timezone is shown to users, e.g. 'EU' or 'Europe/Paris'."""
    return (name or RAID_TIMEZONE).strip()


def require_token() -> str:
    if not TOKEN:
        raise SystemExit(
            "DISCORD_TOKEN is not set. Copy .env.example to .env and fill it in."
        )
    return TOKEN
