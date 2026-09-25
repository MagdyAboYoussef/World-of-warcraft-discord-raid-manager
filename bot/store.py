"""SQLite persistence: cached player profiles, raids, and signups.

sqlite3 is synchronous, but every operation here is a single indexed
row read/write on a local file - microseconds - so it runs inline on the event
loop rather than dragging in an async driver.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .config import (
    DB_PATH, DEFAULT_CAPS, DEFAULT_RAID_DURATION_MINUTES, WEB_RETENTION_DAYS,
)

log = logging.getLogger(__name__)


class Status(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    BENCH = "bench"
    ABSENT = "absent"
    #: "I intend to come but can't promise, or I'll be late." Appended rather
    #: than slotted next to PENDING so that nothing depending on the existing
    #: member order shifts underneath it.
    TENTATIVE = "tentative"

    @property
    def label(self) -> str:
        return {
            Status.PENDING: "Pending",
            Status.ACCEPTED: "Accepted",
            Status.DECLINED: "Out",
            Status.BENCH: "Backup",
            Status.ABSENT: "Absent",
            Status.TENTATIVE: "Tentative",
        }[self]

    @property
    def emoji(self) -> str:
        return {
            Status.PENDING: "🕓",
            Status.ACCEPTED: "✅",
            Status.DECLINED: "❌",
            Status.BENCH: "⭐",
            Status.ABSENT: "🚫",
            Status.TENTATIVE: "❔",
        }[self]

    @property
    def self_service(self) -> bool:
        """Can a player put themselves in this state without an admin?"""
        return self in (Status.BENCH, Status.ABSENT, Status.TENTATIVE)


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
    auto_accept INTEGER NOT NULL DEFAULT 0,
    board_closed_at INTEGER,
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
    discord_name   TEXT,
    PRIMARY KEY (raid_id, user_id)
);

CREATE TABLE IF NOT EXISTS reminders_sent (
    raid_id        INTEGER NOT NULL REFERENCES raids(id) ON DELETE CASCADE,
    offset_minutes INTEGER NOT NULL,
    sent_at        INTEGER NOT NULL,
    PRIMARY KEY (raid_id, offset_minutes)
);

-- Who did what to whom, per raid. Admin-only: nothing in Discord reads this,
-- it is surfaced solely on the manager page, which is already admin-gated.
-- Rows are immutable - the only writes are the INSERT and the retention trim.
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    raid_id     INTEGER NOT NULL REFERENCES raids(id) ON DELETE CASCADE,
    created_at  INTEGER NOT NULL,
    -- Who performed the action. NULL is the bot acting on its own.
    actor_id    INTEGER,
    actor_name  TEXT,
    -- 'discord' | 'web' | 'system'. A raid lead can act from either place, and
    -- "it changed and nobody in Discord touched it" is a question worth
    -- being able to answer.
    source      TEXT NOT NULL,
    action      TEXT NOT NULL,
    -- The signup that was acted on. Equal to the actor for self-service.
    target_id   INTEGER,
    target_name TEXT,
    detail      TEXT
);

CREATE INDEX IF NOT EXISTS idx_signups_raid ON signups(raid_id);
CREATE INDEX IF NOT EXISTS idx_audit_raid ON audit_log(raid_id, id DESC);
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
    #: Accept applications the moment they arrive, instead of queueing them as
    #: Pending for a raid lead to work through.
    auto_accept: bool
    caps: dict[str, int]
    created_at: int
    #: When the board was last re-rendered *because* the raid closed. NULL means
    #: that final render still owes to happen - see ReminderTask.
    board_closed_at: int | None = None


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
    #: The Discord handle behind this character, captured when the row was
    #: written. Stored rather than resolved on demand so a raid lead can still
    #: tell who someone was after they have left the server and the API stops
    #: resolving their id. NULL on rows written before this was recorded.
    discord_name: str | None = None


@dataclass(slots=True)
class AuditEntry:
    id: int
    raid_id: int
    created_at: int
    actor_id: int | None
    actor_name: str | None
    source: str
    action: str
    target_id: int | None
    target_name: str | None
    detail: str | None


#: Kept per raid. Long enough to cover an argument about who benched whom,
#: short enough that someone spamming Apply cannot grow the table without
#: bound. Older entries are dropped as new ones arrive.
AUDIT_RETAINED = 500

#: The closed vocabulary for audit_log.action. The manager page maps each to a
#: verb; anything outside this set renders as the bare action name.
AUDIT_ACTIONS: tuple[str, ...] = (
    "apply",     # signed up, or re-submitted their application
    "status",    # accepted / declined / benched / ...
    "spec",      # reassigned to another spec
    "character", # an admin corrected the character name / realm
    "reassign",  # an admin moved a slot to a different Discord account
    "note",      # an admin edited a signup's note
    "remove",    # an admin took the signup off the raid
    "withdraw",  # the player took themselves off it
    "raid",      # a raid-level setting changed (lock, cancel, targets, ...)
)


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


def raid_is_finished(raid: Raid) -> bool:
    """Has this raid's scheduled window already passed?

    Requires a real start time. raid_ends_at falls back to created_at so that
    every raid expires eventually, but treating "no time set" as finished three
    hours after creation would close a board nobody had scheduled yet.
    """
    return raid.starts_at is not None and int(time.time()) >= raid_ends_at(raid)


def raid_is_closed(raid: Raid) -> bool:
    """Cancelled, or over. Either way there is nothing left to sign up for."""
    return raid.state is RaidState.CANCELLED or raid_is_finished(raid)


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
        if "auto_accept" not in columns:
            self.db.execute(
                "ALTER TABLE raids ADD COLUMN auto_accept INTEGER NOT NULL DEFAULT 0"
            )
        if "board_closed_at" not in columns:
            self.db.execute("ALTER TABLE raids ADD COLUMN board_closed_at INTEGER")

        signup_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(signups)")}
        if "discord_name" not in signup_columns:
            self.db.execute("ALTER TABLE signups ADD COLUMN discord_name TEXT")

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
        auto_accept: bool = False,
    ) -> Raid:
        caps = caps or dict(DEFAULT_CAPS)
        cur = self.db.execute(
            """INSERT INTO raids (guild_id, channel_id, title, description, leader_id,
                                  starts_at, duration_minutes, timezone, state,
                                  auto_accept, caps, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                guild_id, channel_id, title, description, leader_id, starts_at,
                duration_minutes, timezone, RaidState.OPEN.value, int(auto_accept),
                json.dumps(caps), int(time.time()),
            ),
        )
        raid = self.get_raid(int(cur.lastrowid))
        assert raid is not None
        return raid

    def _raid(self, row: sqlite3.Row) -> Raid:
        data = dict(row)
        data["caps"] = json.loads(data["caps"])
        data["state"] = RaidState(data["state"])
        # SQLite has no boolean type; the column round-trips as 0/1.
        data["auto_accept"] = bool(data["auto_accept"])
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

    def move_raid_board(self, raid_id: int, channel_id: int, message_id: int) -> None:
        """Re-anchor a raid to a board posted in another channel.

        Both columns in one statement: the panel's buttons find their raid by
        message id, and reminders find their channel by channel id, so a raid
        left with one updated and not the other is half-broken in a way that is
        painful to notice.
        """
        self.db.execute(
            "UPDATE raids SET channel_id=?, message_id=? WHERE id=?",
            (channel_id, message_id, raid_id),
        )

    def set_raid_state(self, raid_id: int, state: RaidState) -> None:
        self.db.execute("UPDATE raids SET state=? WHERE id=?", (state.value, raid_id))

    def boards_awaiting_close(self, guild_id: int) -> list[Raid]:
        """Raids whose board has not yet been redrawn in its closed form.

        A board is only redrawn when something happens to it, and a raid ending
        is the one state change that nothing triggers: the clock passes its end
        time and no signup, button press or command follows. Without a sweep,
        a finished raid keeps offering Apply until someone happens to touch it.
        """
        rows = self.db.execute(
            "SELECT * FROM raids WHERE guild_id=? AND message_id IS NOT NULL"
            " AND board_closed_at IS NULL",
            (guild_id,),
        ).fetchall()
        return [self._raid(row) for row in rows]

    def mark_board_closed(self, raid_id: int) -> None:
        self.db.execute(
            "UPDATE raids SET board_closed_at=? WHERE id=?", (int(time.time()), raid_id)
        )

    def set_auto_accept(self, raid_id: int, enabled: bool) -> None:
        self.db.execute(
            "UPDATE raids SET auto_accept=? WHERE id=?", (int(enabled), raid_id)
        )

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

    def all_raids(self, limit: int = 500) -> list[Raid]:
        """Every raid across every guild, newest first."""
        rows = self.db.execute(
            "SELECT * FROM raids ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self._raid(r) for r in rows]

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
        discord_name: str | None = None,
    ) -> None:
        self.db.execute(
            """INSERT INTO signups (raid_id, user_id, character_name, logs_url, spec_key,
                                    status, note, updated_at, updated_by, discord_name)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(raid_id, user_id) DO UPDATE SET
                   character_name=excluded.character_name,
                   logs_url=excluded.logs_url,
                   spec_key=excluded.spec_key,
                   status=excluded.status,
                   note=excluded.note,
                   updated_at=excluded.updated_at,
                   updated_by=excluded.updated_by,
                   -- COALESCE, not excluded: a caller that doesn't happen to
                   -- know the handle must not erase one we already recorded.
                   discord_name=COALESCE(excluded.discord_name, signups.discord_name)""",
            (
                raid_id, user_id, character_name, logs_url, spec_key, status.value,
                note, int(time.time()), updated_by, discord_name,
            ),
        )

    def set_logs_url(self, raid_id: int, user_id: int, logs_url: str | None) -> None:
        """Backfill a logs link without touching updated_at - this is bookkeeping,
        not a roster change, and bumping the timestamp would reorder the board."""
        self.db.execute(
            "UPDATE signups SET logs_url=? WHERE raid_id=? AND user_id=?",
            (logs_url, raid_id, user_id),
        )

    def set_discord_name(self, raid_id: int, user_id: int, discord_name: str) -> None:
        """Backfill the handle on a row written before it was captured.

        Deliberately does not touch updated_at: this is bookkeeping, not a
        roster change, and bumping the timestamp would reorder the board.
        """
        self.db.execute(
            "UPDATE signups SET discord_name=? WHERE raid_id=? AND user_id=?",
            (discord_name, raid_id, user_id),
        )

    def set_status(self, raid_id: int, user_id: int, status: Status, updated_by: int | None) -> bool:
        cur = self.db.execute(
            "UPDATE signups SET status=?, updated_at=?, updated_by=? WHERE raid_id=? AND user_id=?",
            (status.value, int(time.time()), updated_by, raid_id, user_id),
        )
        return cur.rowcount > 0

    def reassign_signup(
        self, raid_id: int, old_user_id: int, new_user_id: int,
        new_discord_name: str | None, updated_by: int | None,
    ) -> str:
        """Move a roster slot to a different Discord account.

        Returns "conflict" if the target already has a signup here (the PK is
        (raid_id, user_id), and two rows for one person make no sense),
        "missing" if the original slot is gone, else "ok". The character/spec/
        status ride along unchanged - only who holds the slot changes.
        """
        if self.get_signup(raid_id, new_user_id) is not None:
            return "conflict"
        cur = self.db.execute(
            "UPDATE signups SET user_id=?, discord_name=?, updated_at=?, updated_by=?"
            " WHERE raid_id=? AND user_id=?",
            (new_user_id, new_discord_name, int(time.time()), updated_by, raid_id, old_user_id),
        )
        return "ok" if cur.rowcount else "missing"

    def set_character(
        self, raid_id: int, user_id: int, character_name: str,
        logs_url: str | None, updated_by: int | None,
    ) -> bool:
        """Correct a signup's character (and its derived logs link). An admin
        fixing a typo'd Name-Server, so updated_at is bumped like any edit."""
        cur = self.db.execute(
            "UPDATE signups SET character_name=?, logs_url=?, updated_at=?, updated_by=?"
            " WHERE raid_id=? AND user_id=?",
            (character_name, logs_url, int(time.time()), updated_by, raid_id, user_id),
        )
        return cur.rowcount > 0

    def set_note(self, raid_id: int, user_id: int, note: str | None, updated_by: int | None) -> bool:
        """Set or clear a signup's note. Bumps updated_at/by like any edit; the
        roster is ordered by class now, not time, so this never reshuffles it."""
        cur = self.db.execute(
            "UPDATE signups SET note=?, updated_at=?, updated_by=? WHERE raid_id=? AND user_id=?",
            (note, int(time.time()), updated_by, raid_id, user_id),
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

    # ---------------------------------------------------------------- audit log

    def record_audit(
        self,
        *,
        raid_id: int,
        action: str,
        source: str,
        actor_id: int | None = None,
        actor_name: str | None = None,
        target_id: int | None = None,
        target_name: str | None = None,
        detail: str | None = None,
    ) -> None:
        """Append one entry. Never raises - a lost log line must not lose a roster change.

        Every caller here is on the success path of a mutation that has already
        been committed. If writing the audit row somehow failed and that
        propagated, an admin would see their accept error out *after* it had
        taken effect, which is a worse outcome than an incomplete log.
        """
        try:
            self.db.execute(
                """INSERT INTO audit_log (raid_id, created_at, actor_id, actor_name,
                                          source, action, target_id, target_name, detail)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    raid_id, int(time.time()), actor_id, actor_name, source, action,
                    target_id, target_name, detail,
                ),
            )
            self.db.execute(
                """DELETE FROM audit_log WHERE raid_id=? AND id NOT IN (
                       SELECT id FROM audit_log WHERE raid_id=? ORDER BY id DESC LIMIT ?)""",
                (raid_id, raid_id, AUDIT_RETAINED),
            )
        except sqlite3.Error:
            log.exception("raid #%s: could not record audit entry (%s)", raid_id, action)

    def audit_entries(self, raid_id: int, limit: int = 100) -> list[AuditEntry]:
        """Newest first."""
        rows = self.db.execute(
            "SELECT * FROM audit_log WHERE raid_id=? ORDER BY id DESC LIMIT ?",
            (raid_id, limit),
        ).fetchall()
        return [AuditEntry(**row) for row in rows]
