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
    WEB_BASE_URL, WEB_BIND, WEB_ENABLED, WEB_PORT, region_label,
)
from ..data import targets as targets_data
from ..data.buffs import evaluate as evaluate_buffs
from ..data.specs import CLASS_COLORS, CLASS_ICONS, ROLE_ORDER, SPECS, Role, get_spec
from ..store import Raid, RaidState, Status, page_expires_at
from ..ui.common import WCL_RE, is_admin, request_raid_refresh
from . import tokens
from .page import render_page

if TYPE_CHECKING:
    from ..client import RaidClient

log = logging.getLogger(__name__)

#: How long an admin-role lookup is trusted before being re-checked. Short
#: enough that a revoked role stops working promptly, long enough that clicking
#: through a queue doesn't fetch the member on every single click.
ADMIN_CACHE_SECONDS = 60

#: Crude per-token flood guard. Generous - a fast raid leader with the keyboard
#: shortcuts is a legitimate ~2/second.
RATE_LIMIT_REQUESTS = 120
RATE_LIMIT_WINDOW = 60


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

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if not WEB_ENABLED:
            log.info("web manager disabled (WEB_BASE_URL is not set)")
            return
        if self._runner is not None:
            return

        app = web.Application()
        app.add_routes(
            [
                web.get("/healthz", self.handle_health),
                web.get("/r/{token}", self.handle_page),
                web.get("/r/{token}/state", self.handle_state),
                web.post("/r/{token}/status", self.handle_status),
                web.post("/r/{token}/spec", self.handle_spec),
                web.post("/r/{token}/remove", self.handle_remove),
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

    async def _is_admin(self, guild_id: int, user_id: int) -> bool:
        """Does this user still hold an admin role in this guild?

        Re-checked per request rather than trusted from link-issue time, so
        losing the Raid Leader role also loses access to any link already held.
        """
        cached = self._admin_cache.get((guild_id, user_id))
        if cached is not None and time.monotonic() - cached[0] < ADMIN_CACHE_SECONDS:
            return cached[1]

        allowed = False
        guild = self.bot.get_guild(guild_id)
        if guild is not None:
            member = guild.get_member(user_id)
            if member is None:
                # The members intent is privileged and this bot does not use it,
                # so the cache is usually empty and a fetch is the normal path.
                try:
                    member = await guild.fetch_member(user_id)
                except Exception:
                    member = None
            allowed = member is not None and is_admin(member)

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

        if not await self._is_admin(raid.guild_id, claims.user_id):
            raise _Denied(403, "You no longer have permission to manage this raid.")

        return claims, raid

    # -------------------------------------------------------------- handlers

    async def handle_health(self, _request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

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
        return _secure(web.json_response(self._state(raid, claims)))

    async def handle_status(self, request: web.Request) -> web.Response:
        return await self._mutate(request, self._apply_status)

    async def handle_spec(self, request: web.Request) -> web.Response:
        return await self._mutate(request, self._apply_spec)

    async def handle_remove(self, request: web.Request) -> web.Response:
        return await self._mutate(request, self._apply_remove)

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
        return _secure(web.json_response(self._state(raid, claims)))

    # --------------------------------------------------------------- actions

    def _target(self, raid: Raid, body: dict[str, Any]) -> int:
        raw = body.get("user_id")
        # Snowflakes exceed JS's safe integer range, so they cross the wire as
        # strings; a number here would already have been rounded.
        if not isinstance(raw, str) or not raw.isdigit():
            raise _Denied(400, "Missing or malformed user_id.")
        user_id = int(raw)
        if self.bot.store.get_signup(raid.id, user_id) is None:
            raise _Denied(404, "That player is no longer signed up.")
        return user_id

    def _apply_status(self, raid: Raid, claims: tokens.Claims, body: dict[str, Any]) -> None:
        user_id = self._target(raid, body)
        try:
            status = Status(body.get("status"))
        except ValueError:
            raise _Denied(400, "Unknown status.")
        self.bot.store.set_status(raid.id, user_id, status, claims.user_id)
        log.info(
            "raid #%s: web user %s set %s -> %s",
            raid.id, claims.user_id, user_id, status.value,
        )

    def _apply_spec(self, raid: Raid, claims: tokens.Claims, body: dict[str, Any]) -> None:
        user_id = self._target(raid, body)
        spec_key = body.get("spec_key")
        if not isinstance(spec_key, str) or get_spec(spec_key) is None:
            raise _Denied(400, "Unknown spec.")
        self.bot.store.set_spec(raid.id, user_id, spec_key, claims.user_id)

    def _apply_remove(self, raid: Raid, claims: tokens.Claims, body: dict[str, Any]) -> None:
        user_id = self._target(raid, body)
        self.bot.store.remove_signup(raid.id, user_id)
        log.info("raid #%s: web user %s removed %s", raid.id, claims.user_id, user_id)

    # ----------------------------------------------------------------- state

    def _state(self, raid: Raid, claims: tokens.Claims) -> dict[str, Any]:
        signups = self.bot.store.signups(raid.id)

        accepted_specs = [
            spec
            for s in signups
            if s.status is Status.ACCEPTED and (spec := get_spec(s.spec_key))
        ]
        counts = targets_data.role_counts([spec.role for spec in accepted_specs])

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
                for b in evaluate_buffs(accepted_specs)
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
