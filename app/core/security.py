"""Security: join codes, admin token, host detection, fingerprinting,
rate-limited join attempts, and fingerprint block/trusted lists (SQLite).

IPs are never persisted here — they live only on DeviceInfo in RAM.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import sqlite3
import string
import threading
import time
from datetime import datetime, timezone

from fastapi import Request

from app.config import ServerConfig, get_config

logger = logging.getLogger(__name__)

SESSION_COOKIE = "filehub_session"
ADMIN_COOKIE = "filehub_admin"

# Escalation tiers for wrong join codes (§12): 3 wrong -> 30 s cooldown,
# then 5 minutes, then a session block until the server restarts.
_COOLDOWN_TIERS: list[tuple[int, int]] = [(3, 30), (6, 300)]
_BLOCK_AFTER = 9

_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


def generate_join_code(length: int = 6) -> str:
    """Return a numeric join code like ``482913``."""
    return "".join(secrets.choice(string.digits) for _ in range(length))


def generate_admin_token() -> str:
    """Return a 32-character URL-safe admin token."""
    return secrets.token_urlsafe(24)[:32]


def detect_os(user_agent: str) -> str:
    """Map a User-Agent string to a coarse OS label."""
    ua = user_agent.lower()
    if "android" in ua:
        return "Android"
    if "iphone" in ua or "ipad" in ua or "ios" in ua:
        return "iOS"
    if "windows" in ua:
        return "Windows"
    if "mac os" in ua or "macintosh" in ua:
        return "macOS"
    if "linux" in ua or "x11" in ua or "termux" in ua:
        return "Linux"
    return "Unknown"


class _Attempt:
    """In-RAM wrong-attempt tracker for one fingerprint."""

    __slots__ = ("count", "cooldown_until", "blocked")

    def __init__(self) -> None:
        self.count = 0
        self.cooldown_until = 0.0
        self.blocked = False


class SecurityManager:
    """Owns join-code validation, rate limiting and the SQLite lists."""

    def __init__(self, config: ServerConfig | None = None) -> None:
        self.config = config or get_config()
        self._attempts: dict[str, _Attempt] = {}
        self._lock = threading.Lock()
        self._ensure_db()

    # ------------------------------------------------------------------ db --

    def _ensure_db(self) -> None:
        with self._connect() as con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS trusted ("
                "fingerprint TEXT PRIMARY KEY, name TEXT, added_at TIMESTAMP)"
            )
            con.execute(
                "CREATE TABLE IF NOT EXISTS blocked ("
                "fingerprint TEXT PRIMARY KEY, name TEXT, added_at TIMESTAMP)"
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.config.trusted_db_path))

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def is_blocked(self, fingerprint: str) -> bool:
        """True if this fingerprint was blocked by the host."""
        if not fingerprint:
            return False
        with self._connect() as con:
            row = con.execute(
                "SELECT 1 FROM blocked WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
        return row is not None

    def block_device(self, fingerprint: str, name: str = "") -> None:
        """Persist a blocked fingerprint (never an IP)."""
        if not fingerprint:
            return
        with self._connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO blocked (fingerprint, name, added_at) "
                "VALUES (?, ?, ?)",
                (fingerprint, name, self._now()),
            )
        logger.info("Blocked fingerprint for %r", name or fingerprint[:12])

    def is_trusted(self, fingerprint: str) -> bool:
        """True if this fingerprint is on the trusted (auto-approve) list."""
        if not fingerprint:
            return False
        with self._connect() as con:
            row = con.execute(
                "SELECT 1 FROM trusted WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
        return row is not None

    def add_trusted(self, fingerprint: str, name: str = "") -> None:
        """Remember a fingerprint so it is auto-approved next time."""
        if not fingerprint:
            return
        with self._connect() as con:
            con.execute(
                "INSERT OR REPLACE INTO trusted (fingerprint, name, added_at) "
                "VALUES (?, ?, ?)",
                (fingerprint, name, self._now()),
            )

    # ------------------------------------------------------------ identity --

    @staticmethod
    def fingerprint(request: Request, screen: str = "", tz: str = "") -> str:
        """Stable device fingerprint: UA + Accept-Language + screen + tz."""
        ua = request.headers.get("user-agent", "")
        lang = request.headers.get("accept-language", "")
        raw = f"{ua}|{lang}|{screen}|{tz}"
        return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()

    @staticmethod
    def is_host(request: Request, admin_token: str) -> bool:
        """Host = loopback client OR a valid admin cookie."""
        client_host = request.client.host if request.client else ""
        if client_host in _LOCAL_HOSTS:
            return True
        token = request.cookies.get(ADMIN_COOKIE, "")
        return bool(token) and secrets.compare_digest(token, admin_token)

    # --------------------------------------------------------- join codes --

    @staticmethod
    def generate_join_code(length: int = 6) -> str:
        return generate_join_code(length)

    @staticmethod
    def generate_admin_token() -> str:
        return generate_admin_token()

    def check_join_code(
        self, code: str, expected: str, fingerprint: str = ""
    ) -> tuple[bool, str, int]:
        """Validate a join code for a fingerprint.

        Returns ``(ok, reason, cooldown_remaining_seconds)``. When
        ``fingerprint`` is given, an active cooldown or block is enforced
        before the code itself is compared.
        """
        if fingerprint:
            attempt = self._attempts.get(fingerprint)
            if attempt is not None:
                if attempt.blocked:
                    return False, "blocked", -1
                now = time.monotonic()
                if attempt.cooldown_until > now:
                    remaining = int(attempt.cooldown_until - now) + 1
                    return False, "cooldown", remaining
        if not secrets.compare_digest(str(code or ""), expected):
            return False, "invalid_code", 0
        return True, "ok", 0

    def track_wrong_attempt(self, fingerprint: str) -> tuple[int, int]:
        """Record a wrong code; returns (attempts, cooldown_seconds)."""
        if not fingerprint:
            return 0, 0
        with self._lock:
            attempt = self._attempts.setdefault(fingerprint, _Attempt())
            attempt.count += 1
            cooldown = 0
            if attempt.count >= _BLOCK_AFTER:
                attempt.blocked = True
            else:
                for threshold, seconds in reversed(_COOLDOWN_TIERS):
                    if attempt.count >= threshold:
                        cooldown = seconds
                        attempt.cooldown_until = time.monotonic() + seconds
                        break
            return attempt.count, cooldown

    def clear_attempts(self, fingerprint: str) -> None:
        """Forget wrong attempts after a successful join."""
        with self._lock:
            self._attempts.pop(fingerprint, None)

    def attempts_left(self, fingerprint: str) -> int:
        attempt = self._attempts.get(fingerprint)
        if attempt is None:
            return self.config.wrong_code_attempts_before_cooldown
        return max(0, _BLOCK_AFTER - attempt.count)


# Process-wide singleton.
security = SecurityManager()
