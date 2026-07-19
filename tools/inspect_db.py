"""Dump the live database. Diagnostic helper.

    python -m tools.inspect_db
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.config import DB_PATH  # noqa: E402

# NOT mode=ro. The bot runs the database in WAL mode, and a read-only
# connection cannot attach the -shm wal-index, so it silently reads the last
# checkpointed snapshot and hides every recent commit. That made this tool
# report stale rows and sent an earlier debugging session down the wrong path.
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row

print(f"db: {DB_PATH}\n")

print("=== RAIDS ===")
for row in db.execute(
    "SELECT id,title,state,message_id,starts_at,duration_minutes,caps FROM raids"
):
    d = dict(row)
    flag = "  <-- message_id is NULL, refresh cannot work" if d["message_id"] is None else ""
    print(f"  {d}{flag}")

print("\n=== SIGNUPS ===")
for row in db.execute(
    "SELECT raid_id,user_id,character_name,spec_key,status,updated_by,updated_at FROM signups"
    " ORDER BY raid_id, updated_at"
):
    print(f"  {dict(row)}")

print("\n=== PLAYERS (profile cache) ===")
for row in db.execute("SELECT user_id,character_name,spec_key,logs_url FROM players"):
    print(f"  {dict(row)}")
