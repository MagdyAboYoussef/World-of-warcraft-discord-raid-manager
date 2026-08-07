"""SQLite persistence: cached player profiles, raids, and signups.

sqlite3 is synchronous, but every operation here is a single indexed
row read/write on a local file - microseconds - so it runs inline on the event
loop rather than dragging in an async driver.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .config import (
    DB_PATH, DEFAULT_CAPS, DEFAULT_RAID_DURATION_MINUTES, WEB_RETENTION_DAYS,
)


class Status(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    BENCH = "bench"
    ABSENT = "absent"

    @property
    def label(self) -> str:
        return {
            Status.PENDING: "Pending",
            Status.ACCEPTED: "Accepted",
            Status.DECLINED: "Declined",
            Status.BENCH: "Benched",
            Status.ABSENT: "Absent",
        }[self]

    @property
    def emoji(self) -> str:
        return {
            Status.PENDING: "🕓",
            Status.ACCEPTED: "✅",
            Status.DECLINED: "❌",
            Status.BENCH: "🪑",
            Status.ABSENT: "🚫",
        }[self]


class RaidState(str, Enum):
    OPEN = "open"
    LOCKED = "locked"
    CANCELLED = "cancelled"


SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    user_id        INTEGER PRIMARY KEY,
    character_name TEXT NOT NULL,
    logs_url       TEXT,
    spec_key       TEXT NOT NULL,
    updated_at     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS raids (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER,
    title       TEXT NOT NULL,
    description TEXT,
    leader_id   INTEGER NOT NULL,
    starts_at   INTEGER,
    duration_minutes INTEGER,
    timezone    TEXT,
    state       TEXT NOT NULL DEFAULT 'open',
    caps        TEXT NOT NULL,
    created_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS signups (
    raid_id        INTEGER NOT NULL REFERENCES raids(id) ON DELETE CASCADE,
    user_id        INTEGER NOT NULL,
    character_name TEXT NOT NULL,
    logs_url       TEXT,
    spec_key       TEXT NOT NULL,
    status         TEXT NOT NULL,
    note           TEXT,
    updated_at     INTEGER NOT NULL,
    updated_by     INTEGER,
    PRIMARY KEY (raid_id, user_id)
);

CREATE TABLE IF NOT EXISTS reminders_sent (
    raid_id        INTEGER NOT NULL REFERENCES raids(id) ON DELETE CASCADE,
    offset_minutes INTEGER NOT NULL,
    sent_at        INTEGER NOT NULL,
    PRIMARY KEY (raid_id, offset_minutes)
);

CREATE INDEX IF NOT EXISTS idx_signups_raid ON signups(raid_id);
CREATE INDEX IF NOT EXISTS idx_raids_message ON raids(message_id);
"""


@dataclass(slots=True)
class Player:
    user_id: int
    character_name: str
    logs_url: str | None
    spec_key: str
    updated_at: int


@dataclass(slots=True)
class Raid:
    id: int
    guild_id: int
    channel_id: int
    message_id: int | None
    title: str
    description: str | None
    leader_id: int
    starts_at: int | None
    duration_minutes: int | None
    #: Region shorthand or IANA name. None means "use the configured default".
    timezone: str | None
    state: RaidState
    caps: dict[str, int]
    created_at: int


@dataclass(slots=True)
class Signup:
    raid_id: int
    user_id: int
    character_name: str
    logs_url: str | None
    spec_key: str
    status: Status
    note: str | None
    updated_at: int
    updated_by: int | None


def raid_ends_at(raid: Raid) -> int:
    """Best estimate of when this raid finished, as a unix timestamp.

    Raids are allowed to carry no start time and no duration, so both fall back
    rather than leaving the raid with no end - a raid that never "ends" would
    also never expire.
    """
    duration = (raid.duration_minutes or DEFAULT_RAID_DURATION_MINUTES) * 60
    return (raid.starts_at or raid.created_at) + duration


def page_expires_at(raid: Raid) -> int:
    """When this raid's manager page stops answering."""
    return raid_ends_at(raid) + WEB_RETENTION_DAYS * 86400


class Store:
    def __init__(self, path: Path = DB_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a database was already in use.

        SQLite has no IF NOT EXISTS for ADD COLUMN, so check the table first.
        Without this, an existing raid.sqlite3 breaks the moment a new column
        lands - which is exactly the situation on a bot that is already live.
        """
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(raids)")}
        if "duration_minutes" not in columns:
            self.db.execute("ALTER TABLE raids ADD COLUMN duration_minutes INTEGER")
        if "timezone" not in columns:
            self.db.execute("ALTER TABLE raids ADD COLUMN timezone TEXT")

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------------ players

    def get_player(self, user_id: int) -> Player | None:
        row = self.db.execute("SELECT * FROM players WHERE user_id=?", (user_id,)).fetchone()
        return Player(**row) if row else None

    def save_player(self, user_id: int, character_name: str, logs_url: str | None, spec_key: str) -> None:
        """Upsert the remembered profile so the next raid pre-fills itself."""
        self.db.execute(
            """INSERT INTO players (user_id, character_name, logs_url, spec_key, updated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                   character_name=excluded.character_name,
                   logs_url=excluded.logs_url,
                   spec_key=excluded.spec_key,
                   updated_at=excluded.updated_at""",
            (user_id, character_name, logs_url, spec_key, int(time.time())),
        )

    def delete_player(self, user_id: int) -> bool:
        return self.db.execute("DELETE FROM players WHERE user_id=?", (user_id,)).rowcount > 0

    # -------------------------------------------------------------------- raids

    def create_raid(
        self,
        *,
        guild_id: int,
        channel_id: int,
        title: str,
        description: str | None,
        leader_id: int,
        starts_at: int | None,
        duration_minutes: int | None = None,
        timezone: str | None = None,
        caps: dict[str, int] | None = None,
    ) -> Raid:
        caps = caps or dict(DEFAULT_CAPS)
        cur = self.db.execute(
            """INSERT INTO raids (guild_id, channel_id, title, description, leader_id,
                                  starts_at, duration_minutes, timezone, state, caps,
                                  created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                guild_id, channel_id, title, description, leader_id, starts_at,
                duration_minutes, timezone, RaidState.OPEN.value, json.dumps(caps),
                int(time.time()),
            ),
        )
        raid = self.get_raid(int(cur.lastrowid))
        assert raid is not None
        return raid

    def _raid(self, row: sqlite3.Row) -> Raid:
        data = dict(row)
        data["caps"] = json.loads(data["caps"])
        data["state"] = RaidState(data["state"])
        return Raid(**data)

    def get_raid(self, raid_id: int) -> Raid | None:
        row = self.db.execute("SELECT * FROM raids WHERE id=?", (raid_id,)).fetchone()
        return self._raid(row) if row else None

    def get_raid_by_message(self, message_id: int) -> Raid | None:
        row = self.db.execute("SELECT * FROM raids WHERE message_id=?", (message_id,)).fetchone()
        return self._raid(row) if row else None

    def latest_open_raid(self, guild_id: int) -> Raid | None:
        row = self.db.execute(
            "SELECT * FROM raids WHERE guild_id=? AND state='open' ORDER BY id DESC LIMIT 1",
            (guild_id,),
        ).fetchone()
        return self._raid(row) if row else None

    def set_raid_message(self, raid_id: int, message_id: int) -> None:
        self.db.execute("UPDATE raids SET message_id=? WHERE id=?", (message_id, raid_id))

    def set_raid_state(self, raid_id: int, state: RaidState) -> None:
        self.db.execute("UPDATE raids SET state=? WHERE id=?", (state.value, raid_id))

    def set_caps(self, raid_id: int, caps: dict[str, int]) -> None:
        self.db.execute("UPDATE raids SET caps=? WHERE id=?", (json.dumps(caps), raid_id))

    def set_schedule(
        self, raid_id: int, starts_at: int | None, duration_minutes: int | None = None
    ) -> None:
        self.db.execute(
            "UPDATE raids SET starts_at=?, duration_minutes=? WHERE id=?",
            (starts_at, duration_minutes, raid_id),
        )
        # Moving a raid must re-arm its reminders, or a raid pushed back an hour
        # would never announce again.
        self.db.execute("DELETE FROM reminders_sent WHERE raid_id=?", (raid_id,))

    def set_timezone(self, raid_id: int, timezone: str | None) -> None:
        self.db.execute("UPDATE raids SET timezone=? WHERE id=?", (timezone, raid_id))

    def last_timezone(self, guild_id: int) -> str | None:
        """The timezone this guild's most recent raid used.

        New raids default to it, so a raid lead sets their region once rather
        than remembering it for every single raid.
        """
        row = self.db.execute(
            "SELECT timezone FROM raids WHERE guild_id=? AND timezone IS NOT NULL"
            " ORDER BY id DESC LIMIT 1",
            (guild_id,),
        ).fetchone()
        return row["timezone"] if row else None

    def claim_reminder(self, raid_id: int, offset_minutes: int) -> bool:
        """Atomically claim one reminder. False means it already went out.

        The PRIMARY KEY does the work, so this is safe across restarts and
        cannot double-announce even if two sweeps overlap.
        """
        cur = self.db.execute(
            "INSERT OR IGNORE INTO reminders_sent (raid_id, offset_minutes, sent_at)"
            " VALUES (?,?,?)",
            (raid_id, offset_minutes, int(time.time())),
        )
        return cur.rowcount > 0

    def update_raid_details(self, raid_id: int, title: str, description: str | None) -> None:
        self.db.execute(
            "UPDATE raids SET title=?, description=? WHERE id=?", (title, description, raid_id)
        )

    def open_raids(self, guild_id: int) -> list[Raid]:
        rows = self.db.execute(
            "SELECT * FROM raids WHERE guild_id=? AND state!='cancelled' ORDER BY id DESC LIMIT 25",
            (guild_id,),
        ).fetchall()
        return [self._raid(r) for r in rows]

    # ------------------------------------------------------------------ signups

    def _signup(self, row: sqlite3.Row) -> Signup:
        data = dict(row)
        data["status"] = Status(data["status"])
        return Signup(**data)

    def upsert_signup(
        self,
        *,
        raid_id: int,
        user_id: int,
        character_name: str,
        logs_url: str | None,
        spec_key: str,
        status: Status,
        note: str | None = None,
        updated_by: int | None = None,
    ) -> None:
        self.db.execute(
            """INSERT INTO signups (raid_id, user_id, character_name, logs_url, spec_key,
                                    status, note, updated_at, updated_by)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(raid_id, user_id) DO UPDATE SET
                   character_name=excluded.character_name,
                   logs_url=excluded.logs_url,
                   spec_key=excluded.spec_key,
                   status=excluded.status,
                   note=excluded.note,
                   updated_at=excluded.updated_at,
                   updated_by=excluded.updated_by""",
            (
                raid_id, user_id, character_name, logs_url, spec_key, status.value,
                note, int(time.time()), updated_by,
            ),
        )

    def set_status(self, raid_id: int, user_id: int, status: Status, updated_by: int | None) -> bool:
        cur = self.db.execute(
            "UPDATE signups SET status=?, updated_at=?, updated_by=? WHERE raid_id=? AND user_id=?",
            (status.value, int(time.time()), updated_by, raid_id, user_id),
        )
        return cur.rowcount > 0

    def set_spec(self, raid_id: int, user_id: int, spec_key: str, updated_by: int | None) -> bool:
        cur = self.db.execute(
            "UPDATE signups SET spec_key=?, updated_at=?, updated_by=? WHERE raid_id=? AND user_id=?",
            (spec_key, int(time.time()), updated_by, raid_id, user_id),
        )
        return cur.rowcount > 0

    def get_signup(self, raid_id: int, user_id: int) -> Signup | None:
        row = self.db.execute(
            "SELECT * FROM signups WHERE raid_id=? AND user_id=?", (raid_id, user_id)
        ).fetchone()
        return self._signup(row) if row else None

    def signups(self, raid_id: int, status: Status | None = None) -> list[Signup]:
        if status is None:
            rows = self.db.execute(
                "SELECT * FROM signups WHERE raid_id=? ORDER BY updated_at", (raid_id,)
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT * FROM signups WHERE raid_id=? AND status=? ORDER BY updated_at",
                (raid_id, status.value),
            ).fetchall()
        return [self._signup(r) for r in rows]

    def remove_signup(self, raid_id: int, user_id: int) -> bool:
        cur = self.db.execute(
            "DELETE FROM signups WHERE raid_id=? AND user_id=?", (raid_id, user_id)
        )
        return cur.rowcount > 0
