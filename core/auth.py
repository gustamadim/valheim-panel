"""Authentication: password hashing, sessions, CSRF.

Sessions are in-memory (a panel restart logging everyone out is correct for a
service that can stop a game server). Users and audit live in SQLite so they
survive rebuilds.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import time
from dataclasses import dataclass

PBKDF2_ROUNDS = 260_000
SESSION_COOKIE = "vp_session"
MIN_PASSWORD_LEN = 10

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,32}$")


def hash_password(password: str, *, rounds: int = PBKDF2_ROUNDS) -> str:
    """Return `pbkdf2_sha256$<rounds>$<salt_b64>$<hash_b64>`."""
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, rounds)
    return "pbkdf2_sha256${}${}${}".format(
        rounds,
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, rounds_s, salt_b64, hash_b64 = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(rounds_s))
        return hmac.compare_digest(expected, actual)
    except (ValueError, TypeError):
        return False


def validate_username(name: str) -> str | None:
    """Return an error message, or None when the name is acceptable."""
    if not USERNAME_RE.match(name or ""):
        return "username must be 3-32 chars: letters, digits, dot, dash, underscore"
    return None


def validate_password(pw: str) -> str | None:
    if not pw or len(pw) < MIN_PASSWORD_LEN:
        return f"password must be at least {MIN_PASSWORD_LEN} characters"
    return None


@dataclass
class Session:
    sid: str
    user: str
    role: str
    expires_at: float
    csrf: str
    user_id: int | None = None


class SessionStore:
    def __init__(self, secret: str, ttl_s: int = 12 * 3600) -> None:
        self._secret = secret.encode()
        self._ttl = ttl_s
        self._sessions: dict[str, Session] = {}
        self._failures: dict[str, list[float]] = {}

    # ---- rate limiting ---------------------------------------------------
    def note_failure(self, ip: str) -> None:
        now = time.time()
        bucket = [t for t in self._failures.get(ip, []) if now - t < 300]
        bucket.append(now)
        self._failures[ip] = bucket

    def is_rate_limited(self, ip: str, limit: int = 5, window: float = 300.0) -> bool:
        now = time.time()
        bucket = [t for t in self._failures.get(ip, []) if now - t < window]
        self._failures[ip] = bucket
        return len(bucket) >= limit

    def clear_failures(self, ip: str) -> None:
        self._failures.pop(ip, None)

    # ---- sessions --------------------------------------------------------
    def create(self, user: str, role: str, user_id: int | None = None) -> Session:
        sid = secrets.token_urlsafe(32)
        session = Session(
            sid=sid,
            user=user,
            role=role,
            expires_at=time.time() + self._ttl,
            csrf=secrets.token_urlsafe(24),
            user_id=user_id,
        )
        self._sessions[sid] = session
        return session

    def get(self, sid: str | None) -> Session | None:
        if not sid:
            return None
        session = self._sessions.get(sid)
        if session is None:
            return None
        if session.expires_at < time.time():
            self._sessions.pop(sid, None)
            return None
        return session

    def destroy(self, sid: str | None) -> None:
        if sid:
            self._sessions.pop(sid, None)

    def destroy_user(self, username: str) -> int:
        """Invalidate every session for a user (role change, disable, delete)."""
        sids = [s for s, v in self._sessions.items() if v.user == username]
        for sid in sids:
            self._sessions.pop(sid, None)
        return len(sids)

    def refresh_role(self, username: str, role: str) -> int:
        """Apply a role change to live sessions without logging the user out."""
        n = 0
        for session in self._sessions.values():
            if session.user == username:
                session.role = role
                n += 1
        return n

    def sweep(self) -> None:
        now = time.time()
        for sid in [s for s, v in self._sessions.items() if v.expires_at < now]:
            self._sessions.pop(sid, None)


def csrf_ok(session: Session | None, presented: str | None) -> bool:
    if session is None or not presented:
        return False
    return hmac.compare_digest(session.csrf, presented)


def new_secret() -> str:
    return secrets.token_urlsafe(48)
