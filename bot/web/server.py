"""The raid manager web app.

Runs inside the bot's own event loop and shares its `Store` instance, so a
status changed on the page is visible to Discord immediately and vice versa -
there is no second process, no second database connection, and nothing to keep
in sync.

Auth is a signed link (see tokens.py). Because the token travels in the URL
path, every response sets `Referrer-Policy: no-referrer` and every outbound link
is `rel="noreferrer"`: without that, clicking a Warcraft Logs link would hand
warcraftlogs.com a working admin URL in the Referer header.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, cast

from aiohttp import web

from ..config import (
    ADMIN_ROLE_NAMES, WEB_BASE_URL, WEB_BIND, WEB_ENABLED, WEB_PORT, region_label,
)
from ..data import targets as targets_data
from ..data.buffs import evaluate as evaluate_buffs
from ..data.buffs import recruits as buff_recruits
from ..data.specs import CLASS_COLORS, CLASS_ICONS, ROLE_ORDER, SPECS, Role, get_spec
from ..store import Raid, RaidState, Status, page_expires_at
from ..ui.common import (
    WCL_RE, derive_logs_url, is_admin, refresh_raid_message,
    request_raid_refresh, split_character,
)
from . import overview, tokens
from .page import render_page

if TYPE_CHECKING:
    from ..client import RaidClient

log = logging.getLogger(__name__)

#: How long an admin-role lookup is trusted before being re-checked. Short
#: enough that a revoked role stops working promptly, long enough that clicking
#: through a queue doesn't fetch the member on every single click.
ADMIN_CACHE_SECONDS = 60

#: Ceiling on how many unknown Discord handles one request will fetch from the
#: API. A page opened on a raid whose rows all predate the discord_name column
#: would otherwise fire one call per signup back-to-back; instead it fills a
#: few in per poll and converges within a minute.
HANDLE_FETCH_BUDGET = 10

#: How many audit entries the page is given. The log is trimmed to
#: store.AUDIT_RETAINED on write; this is just what one page renders.
AUDIT_PAGE_SIZE = 120

#: Crude per-token flood guard. Generous - a fast raid leader with the keyboard
#: shortcuts is a legitimate ~2/second.
RATE_LIMIT_REQUESTS = 120
RATE_LIMIT_WINDOW = 60


async def _member_is_admin(guild, member) -> bool:
    """Admin test for a member obtained without an interaction.

    The Discord side of the bot can read `Interaction.permissions`, which Discord
    resolves server-side and ships with the payload. There is no such thing here,
    so this has to work it out - and the obvious way, `member.guild_permissions`,
    is not trustworthy on its own: it recomputes the answer by walking
    `member.roles`, and `member.roles` resolves role ids through the *guild's
    role cache*. This bot runs without the members intent, and a role missing
    from that cache contributes no permission bits, so a genuine administrator
    computes to zero and gets told they have lost access to their own raid.

    So a "no" from the cache is treated as "don't know" and confirmed against
    Discord before anyone is refused. A "yes" is taken at face value: the cache
    cannot invent a role the member does not hold.
    """
    if guild.owner_id == member.id:
        return True
    if is_admin(member):
        return True

    try:
        roles = await guild.fetch_roles()
    except Exception as exc:
        log.warning("could not fetch roles for guild %s: %s", guild.id, exc)
        return False

    held = set(getattr(member, "_roles", ()) or ())
    for role in roles:
        # @everyone applies to everybody and is not in the member's role list.
        if not (role.id in held or role.is_default()):
            continue
        if role.permissions.administrator or role.name.lower() in ADMIN_ROLE_NAMES:
            log.info(
                "guild %s: %s admitted via role %r, which the role cache had missed",
                guild.id, member, role.name,
            )
            return True
    return False


class _Denied(Exception):
    """Raised to abort a request with a specific status and message."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class RaidWebServer:
    def __init__(self, bot: "RaidClient") -> None:
        self.bot = bot
        self._runner: web.AppRunner | None = None
        self._admin_cache: dict[tuple[int, int], tuple[float, bool]] = {}
        self._hits: dict[tuple[int, int], list[float]] = {}
        self._hits_pruned = time.monotonic()
        # user_id -> Discord @handle. "" is a *negative* result: an account that
        # no longer resolves must be remembered as unresolvable, or every
        # five-second poll would retry the same doomed fetch forever.
        self._handles: dict[int, str] = {}
        self._overview_key: str | None = None

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if not WEB_ENABLED:
            log.info("web manager disabled (WEB_BASE_URL is not set)")
            return
        if self._runner is not None:
            return

        self._overview_key = overview.load_key()
        app = web.Application()
        if self._overview_key:
            app.add_routes([web.get("/overview/{key}", self.handle_overview)])
        app.add_routes(
            [
                web.get("/healthz", self.handle_health),
                web.get("/r/{token}", self.handle_page),
                web.get("/r/{token}/state", self.handle_state),
                web.post("/r/{token}/status", self.handle_status),
                web.post("/r/{token}/spec", self.handle_spec),
                web.post("/r/{token}/character", self.handle_character),
                web.post("/r/{token}/note", self.handle_note),
                web.post("/r/{token}/remove", self.handle_remove),
                web.post("/r/{token}/refresh", self.handle_refresh),
            ]
        )
        # access_log=None deliberately: the default logger writes the full path
        # of every request, and these paths contain live credentials.
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, WEB_BIND, WEB_PORT)
        await site.start()
        log.info("web manager listening on %s:%s (public %s)", WEB_BIND, WEB_PORT, WEB_BASE_URL)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # ------------------------------------------------------------------ auth

    def _rate_limit(self, key: tuple[int, int]) -> None:
        """Throttle one link holder. Only ever called with a verified token.

        Keying this on anything an unauthenticated caller controls - the raw
        token, say - would be worse than useless: every random string would open
        its own bucket, so the limiter would never fire for the flood it exists
        to stop, while the bucket dict grew without bound until the process ran
        out of memory. Keyed on the verified (raid, user) pair, the number of
        buckets is bounded by the number of people actually holding links.
        """
        now = time.monotonic()
        if now - self._hits_pruned > RATE_LIMIT_WINDOW:
            # Admins come and go; without this their buckets accumulate for the
            # lifetime of the process.
            self._hits = {
                k: recent
                for k, times in self._hits.items()
                if (recent := [t for t in times if now - t < RATE_LIMIT_WINDOW])
            }
            self._hits_pruned = now

        hits = [t for t in self._hits.get(key, ()) if now - t < RATE_LIMIT_WINDOW]
        if len(hits) >= RATE_LIMIT_REQUESTS:
            self._hits[key] = hits
            raise _Denied(429, "Too many requests — slow down for a moment.")
        hits.append(now)
        self._hits[key] = hits

    async def _is_admin(self, guild, user_id: int) -> bool:
        """Does this user still hold an admin role in this (already-resolved) guild?

        Re-checked per request rather than trusted from link-issue time, so
        losing the Raid Leader role also loses access to any link already held.
        The guild is passed in resolved: the caller has already handled the
        can't-see-the-guild-yet case, so a None here would be a caller bug, not
        a state to cache.
        """
        guild_id = guild.id
        cached = self._admin_cache.get((guild_id, user_id))
        if cached is not None and time.monotonic() - cached[0] < ADMIN_CACHE_SECONDS:
            return cached[1]

        member = guild.get_member(user_id)
        if member is None:
            # The members intent is privileged and this bot does not use it,
            # so the cache is usually empty and a fetch is the normal path.
            try:
                member = await guild.fetch_member(user_id)
            except Exception as exc:
                log.warning(
                    "guild %s: could not fetch member %s: %s", guild_id, user_id, exc
                )
                member = None

        allowed = False
        if member is not None:
            # Free: we already paid for the fetch. This is what lets the audit
            # log name the officer who clicked, without a lookup of its own on
            # every single mutation.
            self._handles[user_id] = getattr(member, "name", "") or ""
            allowed = await _member_is_admin(guild, member)
            if not allowed:
                log.info(
                    "guild %s: %s (%s) refused the manager page - roles=%r, "
                    "accepted role names=%r",
                    guild_id, member, user_id,
                    [r.name for r in getattr(member, "roles", [])],
                    sorted(ADMIN_ROLE_NAMES),
                )

        self._admin_cache[(guild_id, user_id)] = (time.monotonic(), allowed)
        return allowed

    async def _authorise(self, request: web.Request) -> tuple[tokens.Claims, Raid]:
        # Verified before anything else is touched. Signature checking is pure
        # CPU with no allocation that outlives the request, so an unauthenticated
        # caller cannot make this handler accumulate state of any kind.
        claims = tokens.verify(request.match_info["token"])
        if claims is None:
            raise _Denied(401, "This link is invalid or has expired. Ask the bot for a new one.")
        self._rate_limit((claims.raid_id, claims.user_id))

        raid = self.bot.store.get_raid(claims.raid_id)
        if raid is None:
            raise _Denied(404, "That raid no longer exists.")

        if int(time.time()) >= page_expires_at(raid):
            raise _Denied(410, "This raid has ended and its page has been retired.")

        guild = self.bot.get_guild(raid.guild_id)
        if guild is None:
            # A cold cache and a genuine "removed from the server" both surface
            # as get_guild -> None, but they need opposite answers. Right after a
            # restart the gateway has not sent the guild list yet, so a None is
            # meaningless: tell the holder to wait, and - crucially - do not let
            # _is_admin cache a False that would lock a real admin out for the
            # length of the admin cache after the bot is up.
            if not self.bot.is_ready():
                raise _Denied(
                    503, "The bot is just starting up. Refresh in a few seconds."
                )
            raise _Denied(
                403,
                "This raid's server can't be reached — the bot may have been "
                "removed from it. Nothing you can fix from here.",
            )

        if not await self._is_admin(guild, claims.user_id):
            raise _Denied(
                403,
                "You don't have permission to manage this raid. It needs Discord's "
                "Administrator permission in that server, or a role named "
                + " or ".join(sorted(n.title() for n in ADMIN_ROLE_NAMES))
                + ".",
            )

        return claims, raid

    # -------------------------------------------------------------- handlers

    async def handle_health(self, _request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def handle_overview(self, request: web.Request) -> web.Response:
        # A wrong key gets the same answer as a path that does not exist, and
        # allocates nothing - there is no per-key state to grow.
        key = self._overview_key
        if not key or not overview.key_matches(request.match_info["key"], key):
            raise web.HTTPNotFound()
        return _secure(overview.render_overview(self.bot))

    async def handle_page(self, request: web.Request) -> web.Response:
        try:
            _claims, raid = await self._authorise(request)
        except _Denied as denied:
            return _error_page(denied)
        # _secure matters most here: this is the response whose own URL carries
        # the token, and the page links out to warcraftlogs.com.
        return _secure(render_page(raid.title))

    async def handle_state(self, request: web.Request) -> web.Response:
        try:
            claims, raid = await self._authorise(request)
        except _Denied as denied:
            return _json_error(denied)
        return _secure(web.json_response(await self._state(raid, claims)))

    async def handle_status(self, request: web.Request) -> web.Response:
        return await self._mutate(request, self._apply_status)

    async def handle_spec(self, request: web.Request) -> web.Response:
        return await self._mutate(request, self._apply_spec)

    async def handle_character(self, request: web.Request) -> web.Response:
        return await self._mutate(request, self._apply_character)

    async def handle_note(self, request: web.Request) -> web.Response:
        return await self._mutate(request, self._apply_note)

    async def handle_remove(self, request: web.Request) -> web.Response:
        return await self._mutate(request, self._apply_remove)

    async def handle_refresh(self, request: web.Request) -> web.Response:
        """Force the Discord board to redraw now. Not a mutation - it changes
        nothing in the store - it just pushes the current state to Discord and
        reports whether that landed, which is how the admin learns a board sits
        in a channel the bot cannot post to."""
        try:
            _claims, raid = await self._authorise(request)
        except _Denied as denied:
            return _json_error(denied)
        ok = await refresh_raid_message(self.bot, raid.id)
        if not ok:
            raise_denied = _Denied(
                502,
                "Couldn't update the Discord board. The bot may lack access to "
                "that channel, or the board message was deleted (use /raid repost).",
            )
            return _json_error(raise_denied)
        return _secure(web.json_response({"ok": True}))

    async def _mutate(self, request: web.Request, apply) -> web.Response:
        try:
            claims, raid = await self._authorise(request)
            try:
                body = await request.json()
            except Exception:
                raise _Denied(400, "Malformed request body.")
            if not isinstance(body, dict):
                raise _Denied(400, "Malformed request body.")
            apply(raid, claims, body)
        except _Denied as denied:
            return _json_error(denied)

        # Debounced: a leader clicking through a queue would otherwise fire one
        # Discord message edit per click and hit the rate limit.
        request_raid_refresh(self.bot, raid.id)
        return _secure(web.json_response(await self._state(raid, claims)))

    # --------------------------------------------------------------- actions

    def _target(self, raid: Raid, body: dict[str, Any]):
        """The signup this request acts on, as it stands *before* the change.

        Returned whole rather than as a bare id because every caller needs the
        old value to log what it changed from.
        """
        raw = body.get("user_id")
        # Snowflakes exceed JS's safe integer range, so they cross the wire as
        # strings; a number here would already have been rounded.
        if not isinstance(raw, str) or not raw.isdigit():
            raise _Denied(400, "Missing or malformed user_id.")
        signup = self.bot.store.get_signup(raid.id, int(raw))
        if signup is None:
            raise _Denied(404, "That player is no longer signed up.")
        return signup

    def _audit(
        self,
        raid: Raid,
        claims: tokens.Claims,
        action: str,
        signup,
        detail: str | None = None,
    ) -> None:
        """Attribute one page action to the link holder.

        source="web" matters: a status that changed with nobody visibly touching
        the board in Discord is otherwise unexplainable, and the first thing an
        officer asks is whether the bot did it on its own.
        """
        self.bot.store.record_audit(
            raid_id=raid.id,
            action=action,
            source="web",
            actor_id=claims.user_id,
            actor_name=self._handles.get(claims.user_id) or None,
            target_id=signup.user_id,
            target_name=signup.discord_name,
            detail=detail,
        )

    def _apply_status(self, raid: Raid, claims: tokens.Claims, body: dict[str, Any]) -> None:
        signup = self._target(raid, body)
        try:
            status = Status(body.get("status"))
        except ValueError:
            raise _Denied(400, "Unknown status.")
        self.bot.store.set_status(raid.id, signup.user_id, status, claims.user_id)
        self._audit(
            raid, claims, "status", signup,
            f"{signup.character_name} — {signup.status.label} -> {status.label}",
        )
        log.info(
            "raid #%s: web user %s set %s -> %s",
            raid.id, claims.user_id, signup.user_id, status.value,
        )

    def _apply_spec(self, raid: Raid, claims: tokens.Claims, body: dict[str, Any]) -> None:
        signup = self._target(raid, body)
        spec_key = body.get("spec_key")
        if not isinstance(spec_key, str) or (spec := get_spec(spec_key)) is None:
            raise _Denied(400, "Unknown spec.")
        self.bot.store.set_spec(raid.id, signup.user_id, spec_key, claims.user_id)
        was = get_spec(signup.spec_key)
        self._audit(
            raid, claims, "spec", signup,
            f"{signup.character_name} — "
            f"{was.full_name if was else signup.spec_key} -> {spec.full_name}",
        )

    def _apply_character(self, raid: Raid, claims: tokens.Claims, body: dict[str, Any]) -> None:
        signup = self._target(raid, body)
        raw = body.get("character")
        if not isinstance(raw, str):
            raise _Denied(400, "Missing character.")
        name = raw.strip()
        if split_character(name) is None:
            raise _Denied(
                400, "Enter the character as Name-Server, e.g. Mimz-Kazzak."
            )
        # Editing the character updates its Warcraft Logs link to match the
        # corrected realm. Falls back to the existing link only when the new
        # name has no derivable region (e.g. an IANA-zone raid).
        logs = derive_logs_url(name, region_label(raid.timezone)) or signup.logs_url
        self.bot.store.set_character(raid.id, signup.user_id, name, logs, claims.user_id)
        self._audit(
            raid, claims, "character", signup,
            f"{signup.character_name} -> {name}",
        )
        log.info("raid #%s: web user %s renamed %s -> %s",
                 raid.id, claims.user_id, signup.user_id, name)

    def _apply_note(self, raid: Raid, claims: tokens.Claims, body: dict[str, Any]) -> None:
        signup = self._target(raid, body)
        raw = body.get("note")
        if not isinstance(raw, str):
            raise _Denied(400, "Missing note.")
        note = raw.strip()[:200] or None  # same 200-char cap as the apply modal
        self.bot.store.set_note(raid.id, signup.user_id, note, claims.user_id)
        self._audit(
            raid, claims, "note", signup,
            f"{signup.character_name}: {'cleared the note' if note is None else note}",
        )

    def _apply_remove(self, raid: Raid, claims: tokens.Claims, body: dict[str, Any]) -> None:
        signup = self._target(raid, body)
        self.bot.store.remove_signup(raid.id, signup.user_id)
        # Written after the delete, deliberately: the row is gone, and the log
        # entry is now the only record that this person was ever on the raid.
        self._audit(
            raid, claims, "remove", signup,
            f"{signup.character_name} — was {signup.status.label}",
        )
        log.info("raid #%s: web user %s removed %s", raid.id, claims.user_id, signup.user_id)

    # ----------------------------------------------------------------- state

    async def _backfill_handles(self, raid: Raid, signups: list) -> None:
        """Fill in the Discord handle on rows written before it was recorded.

        Self-healing rather than a migration: the names simply are not in the
        database to migrate, they have to come from Discord. Resolved handles
        are written back, so this converges to zero work after the first few
        page loads and a raid lead can still identify a troll years later, once
        the account itself has stopped resolving.
        """
        budget = HANDLE_FETCH_BUDGET
        for signup in signups:
            if signup.discord_name:
                continue
            handle = self._handles.get(signup.user_id)
            if handle is None:
                user = self.bot.get_user(signup.user_id)
                if user is None:
                    if budget <= 0:
                        continue  # the next poll picks up where this left off
                    budget -= 1
                    try:
                        user = await self.bot.fetch_user(signup.user_id)
                    except Exception:
                        user = None
                handle = getattr(user, "name", "") or ""
                self._handles[signup.user_id] = handle
            if not handle:
                continue
            signup.discord_name = handle
            self.bot.store.set_discord_name(raid.id, signup.user_id, handle)

    async def _state(self, raid: Raid, claims: tokens.Claims) -> dict[str, Any]:
        signups = self.bot.store.signups(raid.id)
        await self._backfill_handles(raid, signups)

        accepted_specs = [
            spec
            for s in signups
            if s.status is Status.ACCEPTED and (spec := get_spec(s.spec_key))
        ]
        counts = targets_data.role_counts([spec.role for spec in accepted_specs])
        # Evaluated once: the coverage panel and the "who should we invite"
        # panel are two readings of the same answer, and recomputing it would
        # let them disagree.
        buffs = evaluate_buffs(accepted_specs)

        return {
            "raid": {
                "id": raid.id,
                "title": raid.title,
                "description": raid.description,
                "state": raid.state.value,
                "editable": raid.state is RaidState.OPEN,
                "auto_accept": raid.auto_accept,
                "starts_at": raid.starts_at,
                "duration_minutes": raid.duration_minutes,
                "region": region_label(raid.timezone),
                "expires_at": page_expires_at(raid),
            },
            # Always all four: the roster stays split by role even when the
            # targets don't. `cap` is null for a role with no target of its own,
            # which the page renders as a bare count instead of a progress bar.
            "roles": [
                {
                    "key": role.value,
                    "label": role.label,
                    "cap": targets_data.role_cap(raid.caps, role),
                    "accepted": counts[role],
                }
                for role in ROLE_ORDER
            ],
            "targets": [
                {
                    "key": target.key,
                    "label": target.label,
                    "cap": target.cap,
                    "accepted": target.accepted(counts),
                    "roles": [role.value for role in target.roles],
                }
                for target in targets_data.targets(raid.caps)
            ],
            "combined_dps": targets_data.is_combined(raid.caps),
            "raid_size": targets_data.raid_size(raid.caps),
            "signups": [self._signup_json(s) for s in signups],
            "buffs": [
                {
                    "key": b.definition.key,
                    "label": b.label,
                    "count": b.count,
                    "covered": b.covered,
                    "wow_class": b.definition.wow_class,
                    # The class icon answers "who do we still need?" at a
                    # glance, which is the whole job of the missing list. Where
                    # several classes can cover it there is no such answer, so
                    # fall back to the spell's own icon.
                    "icon": (
                        CLASS_ICONS[b.definition.wow_class]
                        if b.definition.wow_class
                        else b.icon
                    ),
                }
                for b in buffs
            ],
            # The missing list says what is absent; this says who to invite to
            # fix it, which is the question a raid lead actually acts on.
            "recruit": [
                {
                    "wow_class": r.wow_class,
                    "icon": CLASS_ICONS[r.wow_class],
                    "color": f"#{CLASS_COLORS[r.wow_class]:06X}",
                    "count": r.count,
                    "covers": [
                        {
                            "label": gap.buff.label,
                            # Empty unless the buff needs a particular spec, so
                            # the page can stay quiet in the common case and
                            # speak up for Lust-on-a-BM-Hunter.
                            "specs": list(gap.spec_names) if gap.spec_locked else [],
                        }
                        for gap in r.gaps
                    ],
                }
                for r in buff_recruits(buffs)
            ],
            "statuses": [
                {"value": s.value, "label": s.label, "emoji": s.emoji} for s in Status
            ],
            "specs": [
                {
                    "key": s.key,
                    "label": s.full_name,
                    "icon": s.icon,
                    "wow_class": s.wow_class,
                    "role": s.role.value,
                }
                for s in SPECS
            ],
            # Admin-only by construction: this whole app is behind _authorise,
            # and nothing in Discord renders the log. There is no non-admin view
            # of the page that could accidentally inherit it.
            "audit": [
                {
                    "id": entry.id,
                    "at": entry.created_at,
                    "actor": entry.actor_name,
                    "actor_id": str(entry.actor_id) if entry.actor_id else None,
                    "action": entry.action,
                    "target": entry.target_name,
                    "target_id": str(entry.target_id) if entry.target_id else None,
                    "detail": entry.detail,
                    "source": entry.source,
                }
                for entry in self.bot.store.audit_entries(raid.id, AUDIT_PAGE_SIZE)
            ],
            "viewer_id": str(claims.user_id),
            "expires_at": claims.expires_at,
        }

    def _signup_json(self, signup) -> dict[str, Any]:
        spec = get_spec(signup.spec_key)
        # Re-validated on the way out, not trusted from the row: rows written by
        # an older build predate the current URL rules, and this one becomes an
        # href.
        logs = signup.logs_url if signup.logs_url and WCL_RE.match(signup.logs_url) else None
        return {
            "user_id": str(signup.user_id),
            "character": signup.character_name,
            # Who is actually behind the character. Null only for a row whose
            # account no longer resolves, which the page shows as the raw id.
            "discord_name": signup.discord_name,
            "spec_key": signup.spec_key,
            "spec_label": spec.full_name if spec else signup.spec_key,
            "icon": spec.icon if spec else None,
            "wow_class": spec.wow_class if spec else None,
            "color": f"#{CLASS_COLORS[spec.wow_class]:06X}" if spec else "#9aa4b2",
            "role": spec.role.value if spec else None,
            "status": signup.status.value,
            "note": signup.note,
            "logs_url": logs,
            "updated_at": signup.updated_at,
        }


# ------------------------------------------------------------------ responses


SECURITY_HEADERS = {
    # The token is in the path; without this it would leak to every host the
    # page links out to or loads an image from.
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}


def _secure(response: web.Response) -> web.Response:
    response.headers.update(SECURITY_HEADERS)
    return response


def _json_error(denied: _Denied) -> web.Response:
    return _secure(
        web.json_response({"error": denied.message}, status=denied.status)
    )


def _error_page(denied: _Denied) -> web.Response:
    from .page import render_error

    return _secure(render_error(denied.status, denied.message))


def manager_url(raid_id: int, user_id: int) -> str:
    """The signed link for one admin to manage one raid."""
    return f"{WEB_BASE_URL}/r/{tokens.issue(raid_id, user_id)}"
