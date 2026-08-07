"""Signed, expiring links to a single raid's manager page.

The token *is* the credential - there is no login - so it is deliberately
narrow: it names one raid, one Discord user, and one expiry, and it is signed
with a server-side secret so none of those three can be edited by the holder.

Binding the user id matters for more than audit. Every mutation re-checks that
that specific user still holds an admin role, so a link keeps working only for
as long as the person it was issued to is still allowed to manage raids.
"""

from __future__ import annotations

import base64
import hmac
import logging
import os
import secrets
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from ..config import DB_PATH, WEB_TOKEN_TTL_MINUTES

log = logging.getLogger(__name__)

SECRET_FILE: Path = DB_PATH.parent / ".web_secret"


def _load_secret() -> bytes:
    """WEB_SECRET if set, else a persisted random one.

    Persisting matters: a secret regenerated on boot would invalidate every
    link the moment the bot restarts, which on a small guild bot is often.
    """
    env = os.getenv("WEB_SECRET", "").strip()
    if env:
        return env.encode()
    try:
        return SECRET_FILE.read_bytes().strip()
    except FileNotFoundError:
        pass

    secret = secrets.token_hex(32).encode()
    SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    # O_EXCL + mode in one call: writing then chmod would leave the secret
    # world-readable for the moment in between.
    try:
        fd = os.open(SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:  # raced with another boot; theirs wins
        return SECRET_FILE.read_bytes().strip()
    with os.fdopen(fd, "wb") as handle:
        handle.write(secret)
    log.info("generated a new web signing secret at %s", SECRET_FILE)
    return secret


_SECRET = _load_secret()


@dataclass(frozen=True, slots=True)
class Claims:
    raid_id: int
    user_id: int
    expires_at: int


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _unb64(raw: str) -> bytes:
    return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def _sign(payload: bytes) -> str:
    return _b64(hmac.new(_SECRET, payload, sha256).digest())


def issue(raid_id: int, user_id: int, ttl_minutes: int | None = None) -> str:
    ttl = WEB_TOKEN_TTL_MINUTES if ttl_minutes is None else ttl_minutes
    expires_at = int(time.time()) + ttl * 60
    payload = f"{raid_id}.{user_id}.{expires_at}".encode()
    return f"{_b64(payload)}.{_sign(payload)}"


def verify(token: str) -> Claims | None:
    """Decode a token, or None if it is malformed, forged, or expired."""
    try:
        encoded, signature = token.split(".", 1)
        payload = _unb64(encoded)
    except (ValueError, TypeError, base64.binascii.Error):
        return None

    # compare_digest, not ==, so a wrong signature can't be recovered a byte at
    # a time from how long the comparison took.
    if not hmac.compare_digest(signature, _sign(payload)):
        return None

    try:
        raid_id, user_id, expires_at = (int(p) for p in payload.decode().split("."))
    except (ValueError, UnicodeDecodeError):
        return None

    if expires_at <= int(time.time()):
        return None
    return Claims(raid_id=raid_id, user_id=user_id, expires_at=expires_at)
