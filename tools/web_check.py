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
os.environ["OVERVIEW_KEY"] = "overview-key-for-tests"

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from aiohttp import web as aioweb  # noqa: E402

from bot.data import targets as targets_data  # noqa: E402
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
#: An administrator whose role is absent from the guild's role cache - the
#: shape that used to refuse a raid lead access to their own raid.
CACHE_MISS = 2003


class FakeUser:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeRole:
    def __init__(self, role_id: int, name: str, administrator: bool = False) -> None:
        self.id = role_id
        self.name = name
        self.permissions = type("P", (), {"administrator": administrator})()

    def is_default(self) -> bool:
        return self.name == "@everyone"


class FakeMember(FakeUser):
    def __init__(
        self,
        allowed: bool,
        name: str = "member",
        member_id: int = 0,
        roles: tuple[int, ...] = (),
    ) -> None:
        super().__init__(name)
        self.allowed = allowed
        self.id = member_id
        # Mirrors discord.Member: raw role ids, resolved through the guild's
        # role cache by the `roles` property. An empty `roles` with a non-empty
        # `_roles` is exactly the cache-miss shape this guards against.
        self._roles = roles
        self.roles: list = []


class FakeGuild:
    def __init__(self) -> None:
        self.id = GUILD
        self.name = "Test Guild <b>"  # angle brackets: must come out escaped
        self.owner_id = 999_000
        self._by_id = {
            ADMIN: FakeMember(True, "raidlead", ADMIN),
            OUTSIDER: FakeMember(False, "randomer", OUTSIDER),
            # Holds Administrator, but only via a role the cache never saw.
            CACHE_MISS: FakeMember(False, "ghostadmin", CACHE_MISS, roles=(77,)),
        }
        # Extra members for the reassign search (people who never signed up).
        self._search_pool = [
            FakeMember(True, "newcomer", 5101),
            FakeMember(True, "newbie", 5102),
            FakeMember(True, "totally_different", 5103),
        ]
        self.fetches = 0
        self.role_fetches = 0
        self.roles = [FakeRole(1, "@everyone"), FakeRole(77, "Overlord", administrator=True)]

    @property
    def members(self):
        # what the members intent gives: the full cached list, name-searchable
        ms = list(self._by_id.values()) + self._search_pool
        for m in ms:
            m.display_name = m.name
        return ms

    def get_member(self, user_id: int):
        return None  # the admin path still fetches; reassign falls back to fetch_user

    async def fetch_roles(self):
        self.role_fetches += 1
        return self.roles

    async def fetch_member(self, user_id: int):
        self.fetches += 1
        member = self._by_id.get(user_id)
        if member is None:
            raise LookupError("no such member")
        return member


class FakeBot:
    def __init__(self, store: Store, guild: FakeGuild) -> None:
        self.store = store
        self._guild = guild
        # Mirrors discord.py: get_user is the local cache and is usually empty
        # for someone the bot has not seen this run, fetch_user hits the API.
        self.users = {
            RAIDER: FakeUser("tankadin"),
            RAIDER + 1: FakeUser("healbot"),
            RAIDER + 2: FakeUser("stabby"),
            5101: FakeUser("newcomer"),
            5102: FakeUser("newbie"),
            5103: FakeUser("totally_different"),
        }
        self.user_fetches = 0
        self._ready = True  # tests flip this to exercise the warm-up window

    def is_ready(self) -> bool:
        return self._ready

    def get_guild(self, guild_id: int):
        return self._guild if guild_id == GUILD else None

    def get_user(self, user_id: int):
        return None

    async def fetch_user(self, user_id: int):
        self.user_fetches += 1
        user = self.users.get(user_id)
        if user is None:
            raise LookupError("no such user")
        return user


refreshes: list[int] = []


async def main() -> None:
    # is_admin() insists on a real discord.Member; the fake stands in for the
    # role lookup so the surrounding cache/fetch path is what gets tested.
    websrv.is_admin = lambda member: member.allowed
    websrv.request_raid_refresh = lambda _bot, raid_id: refreshes.append(raid_id)
    refresh_ok = {"value": True}
    async def _fake_refresh(_bot, raid_id):
        refreshes.append(raid_id)
        return refresh_ok["value"]
    websrv.refresh_raid_message = _fake_refresh

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
        aioweb.post("/r/{token}/character", srv.handle_character),
        aioweb.post("/r/{token}/note", srv.handle_note),
        aioweb.get("/r/{token}/members", srv.handle_members),
        aioweb.post("/r/{token}/reassign", srv.handle_reassign),
        aioweb.post("/r/{token}/remove", srv.handle_remove),
        aioweb.post("/r/{token}/refresh", srv.handle_refresh),
        aioweb.get("/overview/{key}", srv.handle_overview),
        aioweb.get("/g/{token}", srv.handle_guild_index),
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
    check("activity log panel served", 'id="log"' in html)

    # The preview and the live page share one body template precisely because
    # they used to drift; this catches the other half of that bug - a script
    # that reaches for an element the markup never grew.
    import re as _re

    from bot.web.page import BODY, SCRIPT

    wanted = set(_re.findall(r"\$\('#([a-z0-9-]+)'\)", SCRIPT))
    present = set(_re.findall(r'id="([a-z0-9-]+)"', BODY))
    check("every element the client script writes to exists in the page",
          wanted <= present, f"missing {sorted(wanted - present)}")

    # SCRIPT is a plain (non-raw) Python string, so a "\n" written in page.py
    # becomes a real newline in the JavaScript. Inside a JS string literal that
    # is a syntax error, the whole script fails to parse, and the page renders
    # as an empty shell with every panel blank - while every other check here
    # still passes, because the server side is perfectly healthy.
    def unterminated(line: str) -> str | None:
        quote, i = None, 0
        while i < len(line):
            c = line[i]
            if quote:
                if c == "\\":
                    i += 2
                    continue
                if c == quote:
                    quote = None
            elif c in "'\"":
                quote = c
            elif c == "/" and line[i + 1:i + 2] == "/":
                break
            i += 1
        return quote

    dangling = [n for n, line in enumerate(SCRIPT.splitlines(), 1) if unterminated(line)]
    check("no JS string literal runs past its line", not dangling,
          f"lines {dangling} — an unescaped newline breaks the entire script")

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

    print("\n[3a] startup warm-up window")
    # A raid whose guild the bot cannot see yet. Before READY this is a cold
    # cache, not a real absence, so it must read as "starting up", not "no
    # access" - and it must NOT poison the admin cache with a False that would
    # linger after the gateway connects.
    orphan = store.create_raid(
        guild_id=GUILD + 777, channel_id=9, title="Elsewhere", description=None,
        leader_id=ADMIN, starts_at=now + 3600,
    )
    otok = tokens.issue(orphan.id, ADMIN)
    bot._ready = False
    res = await client.get(f"/r/{otok}/state")
    check("cold cache during startup -> 503, not 403", res.status == 503, f"got {res.status}")
    check("nothing cached from the warm-up miss",
          (orphan.guild_id, ADMIN) not in srv._admin_cache)
    bot._ready = True
    res = await client.get(f"/r/{otok}/state")
    check("once ready, an unreachable guild -> 403", res.status == 403, f"got {res.status}")

    # The bug this whole section exists for: a warm-up 503 must not lock a real
    # admin out of their OWN reachable raid afterwards.
    bot._ready = False
    await client.get(f"/r/{good}/state")  # arrives mid-startup
    bot._ready = True
    res = await client.get(f"/r/{good}/state")
    check("a real admin is not locked out after a startup blip",
          res.status == 200, f"got {res.status}")

    # The regression: an administrator whose role is missing from the guild's
    # role cache. member.guild_permissions computes zero from an empty role
    # list, and the page used to tell them they had lost access to their raid.
    ghost = tokens.issue(raid.id, CACHE_MISS)
    res = await client.get(f"/r/{ghost}/state")
    check("an admin missed by the role cache is admitted", res.status == 200,
          f"got {res.status} — this is the 'You no longer have permission' bug")
    check("it took a REST role fetch to establish that", guild.role_fetches >= 1)

    srv._admin_cache.clear()
    before = guild.role_fetches
    res = await client.get(f"/r/{tokens.issue(raid.id, OUTSIDER)}/state")
    check("a genuine non-admin is still refused", res.status == 403, f"got {res.status}")
    check("and the REST fallback was consulted before refusing them",
          guild.role_fetches == before + 1)

    print("\n[4] state payload")
    state = await (await client.get(f"/r/{good}/state")).json()
    check("ids are strings", all(isinstance(s["user_id"], str) for s in state["signups"]),
          "snowflakes lose precision as JS numbers")
    check("all signups present", len(state["signups"]) == 3)
    check("four role columns", [r["key"] for r in state["roles"]]
          == ["tank", "healer", "ranged", "melee"])
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
    check("auto_accept exposed to the page", state["raid"]["auto_accept"] is False)
    check("expiry is 30 days past raid end",
          abs(state["raid"]["expires_at"] - (now + 3600 + 180 * 60 + 30 * 86400)) <= 1)

    print("\n[4b] who to recruit")
    rec = {r["wow_class"]: r for r in state["recruit"]}
    gaps = {b["label"] for b in state["buffs"] if not b["covered"]}
    check("recruit list reaches the page", bool(rec))
    check("every suggestion carries a class icon and colour",
          all(r["icon"] and r["color"].startswith("#") for r in rec.values()))
    check("nothing is suggested for a buff that is already covered",
          all(c["label"] in gaps for r in rec.values() for c in r["covers"]),
          "a covered buff must never appear as a reason to invite anyone")
    check("count matches the listed fixes",
          all(r["count"] == len(r["covers"]) for r in rec.values()))
    check("best-first", [r["count"] for r in state["recruit"]]
          == sorted((r["count"] for r in state["recruit"]), reverse=True))
    check("a spec-locked gap names its specs",
          rec["Hunter"]["covers"] and any(
              c["specs"] == ["Beast Mastery"] for c in rec["Hunter"]["covers"]),
          str(rec.get("Hunter")))
    check("an unrestricted gap sends no spec list",
          any(c["specs"] == [] for c in rec["Hunter"]["covers"]))

    # Accepting a mage must retire Intellect from both panels at once - they are
    # two readings of one evaluation, and a disagreement between them would be
    # worse than either being wrong alone.
    store.upsert_signup(
        raid_id=raid.id, user_id=6001, character_name="Bolty", logs_url=None,
        spec_key="mage_fire", status=Status.ACCEPTED,
    )
    after = await (await client.get(f"/r/{good}/state")).json()
    still = {r["wow_class"]: r for r in after["recruit"]}
    check("accepting a mage covers Arcane Intellect",
          next(b for b in after["buffs"] if b["key"] == "arcane_intellect")["covered"])
    check("...and nobody is recruited for it any more",
          all(c["label"] != "Arcane Intellect"
              for r in still.values() for c in r["covers"]))
    check("...and Mage drops off the list entirely once it fixes nothing",
          "Mage" not in still, str(list(still)))
    store.remove_signup(raid.id, 6001)

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

    # Measured as a delta: an earlier step in this run reassigned RAIDER to a
    # healer spec, so the healer column is legitimately non-zero already.
    healers = lambda st: next(r for r in st["roles"] if r["key"] == "healer")["accepted"]
    before_tentative = healers(await (await client.get(f"/r/{good}/state")).json())
    res = await client.post(f"/r/{good}/status",
                            json={"user_id": str(RAIDER + 1), "status": "tentative"})
    state = await res.json()
    check("tentative accepted as a status", res.status == 200)
    check("tentative persisted",
          store.get_signup(raid.id, RAIDER + 1).status is Status.TENTATIVE)
    check("tentative does not join the accepted roster",
          healers(state) == before_tentative, f"{before_tentative} -> {healers(state)}")
    check("tentative offered to the page",
          any(s["value"] == "tentative" for s in state["statuses"]))

    res = await client.post(f"/r/{good}/remove", json={"user_id": str(RAIDER + 2)})
    check("remove succeeds", res.status == 200)
    check("signup gone", store.get_signup(raid.id, RAIDER + 2) is None)

    print("\n[5b] discord identity")
    state = await (await client.get(f"/r/{good}/state")).json()
    by_id = {s["user_id"]: s for s in state["signups"]}
    check("handle resolved onto the card",
          by_id[str(RAIDER)]["discord_name"] == "tankadin")
    check("handle written back to the row",
          store.get_signup(raid.id, RAIDER).discord_name == "tankadin")
    before = bot.user_fetches
    await client.get(f"/r/{good}/state")
    check("a resolved handle is never fetched twice",
          bot.user_fetches == before, f"{bot.user_fetches - before} extra fetches")

    # A signup whose account has been deleted: the id resolves to nothing, and
    # the page has to keep working rather than retrying it on every poll.
    store.upsert_signup(
        raid_id=raid.id, user_id=8888, character_name="Ghost", logs_url=None,
        spec_key="mage_fire", status=Status.PENDING,
    )
    state = await (await client.get(f"/r/{good}/state")).json()
    ghost = next(s for s in state["signups"] if s["user_id"] == "8888")
    check("unresolvable account still renders", ghost["discord_name"] is None)
    before = bot.user_fetches
    for _ in range(3):
        await client.get(f"/r/{good}/state")
    check("an unresolvable account is not re-fetched on every poll",
          bot.user_fetches == before, f"{bot.user_fetches - before} fetches")
    store.remove_signup(raid.id, 8888)

    print("\n[5c] audit log")
    entries = store.audit_entries(raid.id)
    check("mutations were recorded", len(entries) >= 4, f"{len(entries)} entries")
    check("every entry names an actor", all(e.actor_id == ADMIN for e in entries))
    check("the actor is named, not just numbered",
          all(e.actor_name == "raidlead" for e in entries),
          str({e.actor_name for e in entries}))
    check("web actions are tagged as such", all(e.source == "web" for e in entries))

    accept = next(e for e in reversed(entries) if e.action == "status")
    check("status change records what it changed from and to",
          accept.detail == "Tankadin — Pending -> Accepted", repr(accept.detail))
    check("status change names its target", accept.target_id == RAIDER)
    check("spec change recorded", any(e.action == "spec" for e in entries))
    removal = next(e for e in entries if e.action == "remove")
    check("removal outlives the row it deleted",
          store.get_signup(raid.id, RAIDER + 2) is None and removal.target_id == RAIDER + 2)
    check("removal remembers who it was",
          removal.target_name == "stabby", repr(removal.target_name))

    state = await (await client.get(f"/r/{good}/state")).json()
    check("log reaches the page", len(state["audit"]) >= 4)
    check("newest first", state["audit"][0]["id"] > state["audit"][-1]["id"])
    check("page ids are strings",
          all(isinstance(e["actor_id"], str) for e in state["audit"]))

    # The trim is what stops one persistent troll growing the table forever.
    from bot.store import AUDIT_RETAINED
    for n in range(AUDIT_RETAINED + 20):
        store.record_audit(raid_id=raid.id, action="status", source="web",
                           actor_id=ADMIN, actor_name="raidlead", detail=f"noise {n}")
    kept = store.db.execute(
        "SELECT COUNT(*) c FROM audit_log WHERE raid_id=?", (raid.id,)
    ).fetchone()["c"]
    check("log is trimmed to its retention limit", kept == AUDIT_RETAINED, f"{kept} rows")
    check("the trim keeps the newest",
          store.audit_entries(raid.id, 1)[0].detail == f"noise {AUDIT_RETAINED + 19}")

    print("\n[5d] admin edits a character name / server")
    res = await client.post(f"/r/{good}/character",
                            json={"user_id": str(RAIDER), "character": "Renamed-Kazzak"})
    check("rename succeeds", res.status == 200, f"got {res.status}")
    _sg = store.get_signup(raid.id, RAIDER)
    check("character updated", _sg.character_name == "Renamed-Kazzak")
    check("logs link re-derived from the new Name-Server",
          _sg.logs_url == "https://www.warcraftlogs.com/character/eu/kazzak/renamed",
          str(_sg.logs_url))
    check("attributed to the link holder", _sg.updated_by == ADMIN)
    res = await client.post(f"/r/{good}/character",
                            json={"user_id": str(RAIDER), "character": "FlatName"})
    check("a flat name is rejected", res.status == 400, f"got {res.status}")
    check("the rename is in the audit log",
          any(e.action == "character" for e in store.audit_entries(raid.id)))
    # Restore the original character so later sections (the overview page) see
    # the name they expect — this section is meant to be self-contained.
    store.set_character(raid.id, RAIDER, "Tankadin", None, ADMIN)

    print("\n[5d2] admin edits a note")
    res = await client.post(f"/r/{good}/note",
                            json={"user_id": str(RAIDER), "note": "  bring flasks  "})
    check("note set (and trimmed)", res.status == 200
          and store.get_signup(raid.id, RAIDER).note == "bring flasks")
    check("note edit is attributed", store.get_signup(raid.id, RAIDER).updated_by == ADMIN)
    res = await client.post(f"/r/{good}/note", json={"user_id": str(RAIDER), "note": ""})
    check("empty note clears it", res.status == 200
          and store.get_signup(raid.id, RAIDER).note is None)
    check("a note edit is logged",
          any(e.action == "note" for e in store.audit_entries(raid.id)))
    res = await client.post(f"/r/{good}/note", json={"user_id": str(RAIDER)})
    check("missing note field -> 400", res.status == 400, f"got {res.status}")

    print("\n[5d3] member search + reassign a slot")
    res = await client.get(f"/r/{good}/members?q=new")
    found = (await res.json())["members"]
    check("member search returns matches", res.status == 200 and len(found) >= 2, str(found))
    check("search is a substring match", any(m["name"] == "newcomer" for m in found))
    mid = (await (await client.get(f"/r/{good}/members?q=omer")).json())["members"]
    check("a mid-word substring also matches (substring search)",
          any(m["name"] == "newcomer" for m in mid), str(mid))
    res_at = await client.get(f"/r/{good}/members?q=@new")
    check("a leading @ is ignored in the query",
          {m["id"] for m in (await res_at.json())["members"]}
          == {m["id"] for m in found})
    res = await client.get(f"/r/{good}/members?q=x")
    check("too-short query returns nothing", (await res.json())["members"] == [])

    before = store.get_signup(raid.id, RAIDER)
    res = await client.post(f"/r/{good}/reassign",
                            json={"user_id": str(RAIDER), "new_user_id": "5101"})
    check("reassign succeeds", res.status == 200, f"got {res.status}")
    check("old holder no longer on the raid", store.get_signup(raid.id, RAIDER) is None)
    moved = store.get_signup(raid.id, 5101)
    check("new holder keeps the same character/spec",
          moved is not None and moved.character_name == before.character_name
          and moved.spec_key == before.spec_key)
    check("new holder handle recorded", moved.discord_name == "newcomer")
    check("reassign attributed + logged",
          moved.updated_by == ADMIN
          and any(e.action == "reassign" for e in store.audit_entries(raid.id)))
    res = await client.post(f"/r/{good}/reassign",
                            json={"user_id": "5101", "new_user_id": str(RAIDER + 1)})
    check("reassign onto an existing signup refused", res.status == 409, f"got {res.status}")
    store.reassign_signup(raid.id, 5101, RAIDER, "tankadin", ADMIN)
    store.set_character(raid.id, RAIDER, "Tankadin", None, ADMIN)

    print("\n[5e] manual board refresh")
    refresh_ok["value"] = True
    res = await client.post(f"/r/{good}/refresh", json={})
    check("refresh returns ok when the board updates", res.status == 200, f"got {res.status}")
    check("it actually asked for a board redraw", refreshes and refreshes[-1] == raid.id)
    refresh_ok["value"] = False
    res = await client.post(f"/r/{good}/refresh", json={})
    body = await res.json()
    check("refresh reports failure the admin can act on", res.status == 502, f"got {res.status}")
    check("with a message about channel access / repost",
          "channel" in body.get("error", "").lower() or "repost" in body.get("error", "").lower())
    refresh_ok["value"] = True

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

    print("\n[6b] an unauthenticated caller cannot make the server accumulate state")
    srv._hits.clear()
    for n in range(60):
        res = await client.get(f"/r/forged-token-{n}/state")
        assert res.status == 401, res.status
    check("garbage tokens allocate no rate-limit buckets", len(srv._hits) == 0,
          f"{len(srv._hits)} buckets — unbounded growth is a memory-exhaustion DoS")
    check("garbage tokens allocate no admin-cache entries",
          all(k[1] != 0 for k in srv._admin_cache), str(srv._admin_cache.keys()))

    before = len(srv._hits)
    await client.get(f"/r/{good}/state")
    check("a verified token gets exactly one bucket", len(srv._hits) == before + 1)
    check("bucket is keyed on the verified pair, not the token",
          (raid.id, ADMIN) in srv._hits, str(list(srv._hits)))
    for _ in range(5):
        await client.get(f"/r/{good}/state")
    check("re-using a link reuses its bucket", len(srv._hits) == before + 1)

    print("\n[7] combined DPS target")
    combined = store.create_raid(
        guild_id=GUILD, channel_id=42, title="Combined", description=None,
        leader_id=ADMIN, starts_at=now + 3600, duration_minutes=180, timezone="EU",
        caps={"tank": 2, "healer": 4, "dps": 14},
    )
    for user_id, name, spec in (
        (5001, "Chopper", "warr_arms"),      # melee
        (5002, "Bolt", "mage_fire"),         # ranged
        (5003, "Blocky", "pal_prot"),        # tank
    ):
        store.upsert_signup(
            raid_id=combined.id, user_id=user_id, character_name=name, logs_url=None,
            spec_key=spec, status=Status.ACCEPTED,
        )
    ctoken = tokens.issue(combined.id, ADMIN)
    cstate = await (await client.get(f"/r/{ctoken}/state")).json()

    roles = {r["key"]: r for r in cstate["roles"]}
    check("roster still splits into four roles", len(cstate["roles"]) == 4)
    check("melee has no cap of its own", roles["melee"]["cap"] is None)
    check("ranged has no cap of its own", roles["ranged"]["cap"] is None)
    check("tank keeps its own cap", roles["tank"]["cap"] == 2)
    check("melee still counted separately", roles["melee"]["accepted"] == 1)
    check("ranged still counted separately", roles["ranged"]["accepted"] == 1)

    tg = {t["key"]: t for t in cstate["targets"]}
    check("three targets, not four", len(cstate["targets"]) == 3)
    check("dps target is the combined cap", tg["dps"]["cap"] == 14)
    check("dps target sums melee and ranged", tg["dps"]["accepted"] == 2)
    check("dps target names both roles", sorted(tg["dps"]["roles"]) == ["melee", "ranged"])
    check("flagged as combined", cstate["combined_dps"] is True)
    check("raid size adds up", cstate["raid_size"] == 20)
    check("summary reads 2 / 4 / 14", targets_data.summary(combined.caps) == "2 / 4 / 14")

    plain = await (await client.get(f"/r/{good}/state")).json()
    check("four-target raids unaffected", plain["combined_dps"] is False
          and len(plain["targets"]) == 4
          and all(r["cap"] is not None for r in plain["roles"]))
    check("four-target size still right", plain["raid_size"] == 20)

    print("\n[7b] overview page")
    from bot.web import overview as ov

    srv._overview_key = ov.load_key()
    check("key loaded from the environment", srv._overview_key == "overview-key-for-tests")

    res = await client.get("/overview/wrong-key")
    check("wrong key -> 404, indistinguishable from no route", res.status == 404)
    res = await client.get("/overview/")
    check("empty key -> 404", res.status == 404)

    res = await client.get(f"/overview/{srv._overview_key}")
    page = await res.text()
    check("right key -> 200", res.status == 200)
    check("no-referrer on the overview too",
          res.headers.get("Referrer-Policy") == "no-referrer")
    check("CSP present", "nonce-" in res.headers.get("Content-Security-Policy", ""))
    check("every raid listed", all(f"#{r.id} " in page for r in store.all_raids()))
    check("signups listed with their handle", "Tankadin" in page and "@tankadin" in page)
    check("guild name is escaped", "Test Guild &lt;b&gt;" in page and "<b>" not in page.split("<h2>")[1][:60])
    check("both servers' raids appear under a header per server",
          page.count('class="guild"') >= 1)

    saved = srv._overview_key
    srv._overview_key = None
    res = await client.get(f"/overview/{saved}")
    check("route is inert without a key", res.status == 404)
    srv._overview_key = saved

    print("\n[7c] per-server admin index")
    gtok = tokens.issue_guild(GUILD, ADMIN)
    res = await client.get(f"/g/{gtok}")
    page = await res.text()
    check("admin gets the server index", res.status == 200, f"got {res.status}")
    check("no-referrer on the index (token in path)",
          res.headers.get("Referrer-Policy") == "no-referrer")
    check("index lists this server's raids", '"id": ' + str(raid.id) in page or f"#{raid.id}" in page)
    check("index links open per-raid manager pages", "/r/" in page)
    # admin-only: a non-admin's guild token is refused
    res = await client.get(f"/g/{tokens.issue_guild(GUILD, OUTSIDER)}")
    check("a non-admin is refused the index", res.status == 403, f"got {res.status}")
    # a raid token must not work as a guild token and vice versa
    res = await client.get(f"/g/{good}")
    check("a raid token is rejected on the guild route", res.status == 401, f"got {res.status}")
    check("guild verify rejects a raid token", tokens.verify_guild(good) is None)
    check("raid verify rejects a guild token", tokens.verify(gtok) is None)

    print("\n[8] retirement")
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
