"""Offline check of the web roster manager: tokens, auth, routes, mutations.

    python -m tools.web_check

Runs the real aiohttp app against a temporary database and a stand-in bot, so
the whole request path is exercised without a token or a live gateway.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Must be set before bot.config is imported: it reads all of these at import
# time, and tokens.py derives its signing secret from them.
_TMP = Path(tempfile.mkdtemp(prefix="raidweb-"))
os.environ["DB_PATH"] = str(_TMP / "raid.sqlite3")
os.environ["WEB_BASE_URL"] = "https://wow-raid-manager.magdy.org"
os.environ["WEB_SECRET"] = "signing-secret-for-tests"
os.environ["WEB_RETENTION_DAYS"] = "30"

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from aiohttp import web as aioweb  # noqa: E402

from bot.store import Status, Store, page_expires_at  # noqa: E402
from bot.web import server as websrv, tokens  # noqa: E402

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if condition else 'FAIL'} {label} {detail}".rstrip())
    if not condition:
        failures.append(label)


GUILD = 1000
ADMIN = 2001
RAIDER = 3001
OUTSIDER = 2002


class FakeMember:
    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed


class FakeGuild:
    def __init__(self) -> None:
        self.members = {ADMIN: FakeMember(True), OUTSIDER: FakeMember(False)}
        self.fetches = 0

    def get_member(self, user_id: int):
        return None  # mirrors reality: this bot runs without the members intent

    async def fetch_member(self, user_id: int):
        self.fetches += 1
        member = self.members.get(user_id)
        if member is None:
            raise LookupError("no such member")
        return member


class FakeBot:
    def __init__(self, store: Store, guild: FakeGuild) -> None:
        self.store = store
        self._guild = guild

    def get_guild(self, guild_id: int):
        return self._guild if guild_id == GUILD else None


refreshes: list[int] = []


async def main() -> None:
    # is_admin() insists on a real discord.Member; the fake stands in for the
    # role lookup so the surrounding cache/fetch path is what gets tested.
    websrv.is_admin = lambda member: member.allowed
    websrv.request_raid_refresh = lambda _bot, raid_id: refreshes.append(raid_id)

    store = Store()
    guild = FakeGuild()
    bot = FakeBot(store, guild)

    now = int(time.time())
    raid = store.create_raid(
        guild_id=GUILD, channel_id=42, title="Manaforge Omega — Mythic",
        description=None, leader_id=ADMIN, starts_at=now + 3600,
        duration_minutes=180, timezone="EU",
        caps={"tank": 2, "healer": 4, "melee": 7, "ranged": 7},
    )
    for user_id, name, spec in (
        (RAIDER, "Tankadin", "pal_prot"),
        (RAIDER + 1, "Healbot", "priest_holy"),
        (RAIDER + 2, "Stabby", "rogue_sub"),
    ):
        store.upsert_signup(
            raid_id=raid.id, user_id=user_id, character_name=name, logs_url=None,
            spec_key=spec, status=Status.PENDING,
        )

    print("\n[1] tokens")
    good = tokens.issue(raid.id, ADMIN)
    claims = tokens.verify(good)
    check("round-trips", claims is not None and claims.raid_id == raid.id
          and claims.user_id == ADMIN)
    check("rejects a tampered payload", tokens.verify("x" + good) is None)
    body, sig = good.split(".", 1)
    check("rejects a swapped signature", tokens.verify(f"{body}.{sig[::-1]}") is None)
    check("rejects garbage", tokens.verify("not-a-token") is None)
    check("rejects an expired link", tokens.verify(tokens.issue(raid.id, ADMIN, -1)) is None)

    srv = websrv.RaidWebServer(bot)
    app = aioweb.Application()
    app.add_routes([
        aioweb.get("/r/{token}", srv.handle_page),
        aioweb.get("/r/{token}/state", srv.handle_state),
        aioweb.post("/r/{token}/status", srv.handle_status),
        aioweb.post("/r/{token}/spec", srv.handle_spec),
        aioweb.post("/r/{token}/remove", srv.handle_remove),
    ])
    client = TestClient(TestServer(app))
    await client.start_server()

    print("\n[2] page + headers")
    res = await client.get(f"/r/{good}")
    html = await res.text()
    check("serves the page", res.status == 200)
    check("no-referrer set", res.headers.get("Referrer-Policy") == "no-referrer")
    check("CSP present with a nonce", "nonce-" in res.headers.get("Content-Security-Policy", ""))
    check("denies framing", res.headers.get("X-Frame-Options") == "DENY")
    check("not cached", res.headers.get("Cache-Control") == "no-store")
    check("title rendered", "Manaforge Omega" in html)

    res = await client.get("/r/rubbish")
    check("bad token gets an error page", res.status == 401)

    print("\n[3] authorisation")
    res = await client.get(f"/r/{tokens.issue(raid.id, OUTSIDER)}/state")
    check("non-admin refused", res.status == 403)
    res = await client.get(f"/r/{tokens.issue(raid.id, 999999)}/state")
    check("unknown member refused", res.status == 403)
    res = await client.get(f"/r/{tokens.issue(4242, ADMIN)}/state")
    check("unknown raid refused", res.status == 404)

    before = guild.fetches
    for _ in range(3):
        await client.get(f"/r/{good}/state")
    check("admin lookups are cached", guild.fetches == before, f"{guild.fetches - before} fetches")

    print("\n[4] state payload")
    state = await (await client.get(f"/r/{good}/state")).json()
    check("ids are strings", all(isinstance(s["user_id"], str) for s in state["signups"]),
          "snowflakes lose precision as JS numbers")
    check("all signups present", len(state["signups"]) == 3)
    check("four role columns", [r["key"] for r in state["roles"]]
          == ["tank", "healer", "melee", "ranged"])
    check("nothing accepted yet", all(r["accepted"] == 0 for r in state["roles"]))
    check("no buffs covered yet", all(not b["covered"] for b in state["buffs"]))
    check("every buff carries an icon", all(b["icon"] for b in state["buffs"]))
    check("single-class buffs use the class icon",
          next(b for b in state["buffs"] if b["key"] == "devotion_aura")["icon"]
          == "classicon_paladin")
    check("shared buffs fall back to the spell icon",
          next(b for b in state["buffs"] if b["key"] == "lust")["icon"]
          == "spell_nature_bloodlust")
    check("every signup carries a spec icon", all(s["icon"] for s in state["signups"]))
    check("expiry is 30 days past raid end",
          abs(state["raid"]["expires_at"] - (now + 3600 + 180 * 60 + 30 * 86400)) <= 1)

    print("\n[5] mutations")
    res = await client.post(f"/r/{good}/status",
                            json={"user_id": str(RAIDER), "status": "accepted"})
    state = await res.json()
    check("accept succeeds", res.status == 200)
    check("store updated",
          store.get_signup(raid.id, RAIDER).status is Status.ACCEPTED)
    check("attributed to the link holder",
          store.get_signup(raid.id, RAIDER).updated_by == ADMIN)
    check("tank column counts it",
          next(r for r in state["roles"] if r["key"] == "tank")["accepted"] == 1)
    check("devotion aura now covered",
          next(b for b in state["buffs"] if b["key"] == "devotion_aura")["covered"])
    check("board refresh requested", refreshes == [raid.id])

    res = await client.post(f"/r/{good}/spec",
                            json={"user_id": str(RAIDER), "spec_key": "pal_holy"})
    check("spec reassign succeeds", res.status == 200)
    check("spec persisted", store.get_signup(raid.id, RAIDER).spec_key == "pal_holy")

    res = await client.post(f"/r/{good}/remove", json={"user_id": str(RAIDER + 2)})
    check("remove succeeds", res.status == 200)
    check("signup gone", store.get_signup(raid.id, RAIDER + 2) is None)

    print("\n[6] bad input")
    for label, payload, expect in (
        ("unknown status", {"user_id": str(RAIDER), "status": "vibing"}, 400),
        ("missing user_id", {"status": "accepted"}, 400),
        ("numeric user_id", {"user_id": RAIDER, "status": "accepted"}, 400),
        ("unknown player", {"user_id": "77777", "status": "accepted"}, 404),
        ("unknown spec", {"user_id": str(RAIDER), "spec_key": "gnome_dancer"}, 400),
    ):
        route = "spec" if "spec_key" in payload else "status"
        res = await client.post(f"/r/{good}/{route}", json=payload)
        check(f"{label} -> {expect}", res.status == expect, f"got {res.status}")

    res = await client.post(f"/r/{good}/status", data="not json")
    check("non-JSON body -> 400", res.status == 400, f"got {res.status}")

    print("\n[7] retirement")
    old = store.create_raid(
        guild_id=GUILD, channel_id=42, title="Last tier", description=None,
        leader_id=ADMIN, starts_at=now - 40 * 86400, duration_minutes=180, timezone="EU",
    )
    check("expiry is in the past", page_expires_at(old) < now)
    res = await client.get(f"/r/{tokens.issue(old.id, ADMIN)}/state")
    check("retired raid -> 410", res.status == 410, f"got {res.status}")
    check("roster survives retirement", store.get_raid(old.id) is not None,
          "expiry gates access, it does not delete")

    await client.close()
    store.close()


if __name__ == "__main__":
    asyncio.run(main())
    print("\n" + ("all checks passed" if not failures else f"{len(failures)} FAILED: {failures}"))
    sys.exit(1 if failures else 0)
