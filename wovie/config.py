"""Runtime configuration. Secret values never appear in configuration errors."""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from dotenv import load_dotenv


def values(key: str) -> tuple[str, ...]:
    raw = os.getenv(key, "").strip()
    if not raw:
        return ()
    if raw.startswith("["):
        try:
            data = json.loads(raw)
        except ValueError:
            raise ValueError(f"{key} must contain a JSON array") from None
    else:
        data = raw.split(",")
    if not isinstance(data, list) or not all(isinstance(x, (str, int)) for x in data):
        raise ValueError(f"Invalid {key} list")
    return tuple(str(x).strip() for x in data if str(x).strip())


def number(key: str, default: int, low: int, high: int) -> int:
    try:
        n = int(os.getenv(key) or default)
        if low <= n <= high:
            return n
    except ValueError:
        pass
    raise ValueError(f"{key} must be an integer from {low} to {high}")


def flag(key: str, default: bool) -> bool:
    raw = os.getenv(key, str(default)).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{key} must be true or false")


@dataclass(frozen=True, repr=False)
class Settings:
    bot_token: str
    admins: frozenset[int]
    mongo_uri: str
    mongo_db: str
    catalog_uris: tuple[str, ...]
    api_id: int | None
    api_hash: str | None
    session_string: str | None
    required_channels: tuple[int | str, ...]
    page_size: int
    delete_after: int
    protect_content: bool
    daily_limit: int
    rate_limit: int
    tmdb_api_key: str | None
    backup_key: str | None
    backup_chat: int | None
    premium_stars: int
    premium_days: int
    support_url: str | None
    bot_name: str
    log_level: str
    poll_timeout: int
    job_interval: float
    max_file_size: int
    public_search: bool
    shortener_api_url: str | None
    shortener_api_key: str | None
    verification_required: bool
    verification_ttl: int
    verification_min_seconds: int
    catalog_max_mib: int

    @classmethod
    def from_env(cls, *, require_bot: bool = True) -> Settings:
        load_dotenv(override=False)
        opt = lambda k: os.getenv(k, "").strip() or None
        try:
            admins = frozenset(map(int, values("ADMINS")))
            channels = tuple(
                int(x) if re.fullmatch(r"-?\d+", x) else x
                for x in values("REQUIRED_CHANNELS")
            )
            backup_chat = int(os.environ["BACKUP_CHAT"]) if opt("BACKUP_CHAT") else None
            interval = float(opt("JOB_INTERVAL") or 1)
        except ValueError:
            raise ValueError("Invalid ADMINS, BACKUP_CHAT or JOB_INTERVAL") from None
        obj = cls(
            opt("BOT_TOKEN") or "",
            admins,
            opt("MONGO_URI") or "",
            opt("MONGO_DB") or "wovie",
            values("CATALOG_URIS"),
            number("API_ID", 0, 0, 2**31 - 1) or None,
            opt("API_HASH"),
            opt("SESSION_STRING"),
            channels,
            number("PAGE_SIZE", 8, 1, 20),
            number("DELETE_AFTER", 0, 0, 172799),
            flag("PROTECT_CONTENT", False),
            number("DAILY_LIMIT", 0, 0, 10**7),
            number("RATE_LIMIT", 12, 1, 6000),
            opt("TMDB_API_KEY"),
            opt("BACKUP_KEY"),
            backup_chat,
            number("PREMIUM_STARS", 0, 0, 10000),
            number("PREMIUM_DAYS", 30, 1, 3650),
            opt("SUPPORT_URL"),
            opt("BOT_NAME") or "Wovie",
            (opt("LOG_LEVEL") or "INFO").upper(),
            number("POLL_TIMEOUT", 30, 2, 55),
            interval,
            number("MAX_FILE_SIZE", 0, 0, 2**63 - 1),
            flag("PUBLIC_SEARCH", True),
            opt("SHORTENER_API_URL"),
            opt("SHORTENER_API_KEY"),
            flag("VERIFICATION_REQUIRED", False),
            number("VERIFICATION_TTL", 43200, 300, 604800),
            number("VERIFICATION_MIN_SECONDS", 10, 0, 300),
            number("CATALOG_MAX_MIB", 0, 0, 2**30),
        )
        obj.validate(require_bot=require_bot)
        return obj

    def validate(self, *, require_bot: bool = True) -> None:
        if require_bot:
            if not re.fullmatch(r"\d+:[\w-]{20,}", self.bot_token):
                raise ValueError("Set BOT_TOKEN to the token from BotFather")
            if not self.admins or min(self.admins) <= 0:
                raise ValueError("Set ADMINS to at least one positive Telegram user ID")
        if not self.mongo_uri or not all(
            u.startswith(("mongodb://", "mongodb+srv://"))
            for u in (self.mongo_uri, *self.catalog_uris)
        ):
            raise ValueError("MONGO_URI is required and all MongoDB URIs must be valid")
        if not re.fullmatch(r"[\w-]{1,63}", self.mongo_db) or self.mongo_db in {
            "admin",
            "local",
            "config",
        }:
            raise ValueError("Use a dedicated MONGO_DB database name")
        if self.session_string and not (
            self.api_id and re.fullmatch(r"[a-fA-F0-9]{32}", self.api_hash or "")
        ):
            raise ValueError("SESSION_STRING needs API_ID and API_HASH")
        if self.support_url and (
            urlsplit(self.support_url).scheme != "https"
            or not urlsplit(self.support_url).hostname
        ):
            raise ValueError("SUPPORT_URL must be an HTTPS URL")
        if self.premium_stars and not self.support_url:
            raise ValueError("Paid plans need SUPPORT_URL for payment support")
        if not 0.1 <= self.job_interval <= 60:
            raise ValueError("JOB_INTERVAL must be between 0.1 and 60 seconds")
        if self.log_level not in {"INFO", "WARNING", "ERROR", "DEBUG"}:
            raise ValueError("Invalid LOG_LEVEL")
        if any(
            not isinstance(x, int) and not re.fullmatch(r"@[A-Za-z0-9_]{5,32}", x)
            for x in self.required_channels
        ):
            raise ValueError(
                "REQUIRED_CHANNELS accepts numeric chat IDs or @channel usernames"
            )
        if self.verification_required and not (
            self.shortener_api_url and self.shortener_api_key
        ):
            raise ValueError(
                "VERIFICATION_REQUIRED needs SHORTENER_API_URL and SHORTENER_API_KEY"
            )
        if self.shortener_api_url:
            u = urlsplit(self.shortener_api_url)
            if u.scheme != "https" or not u.hostname or u.username or u.password:
                raise ValueError(
                    "SHORTENER_API_URL must be an HTTPS endpoint without embedded credentials"
                )
        if self.backup_key:
            try:
                if (
                    len(
                        base64.b64decode(
                            self.backup_key + "=" * (-len(self.backup_key) % 4),
                            altchars=b"-_",
                            validate=True,
                        )
                    )
                    != 32
                ):
                    raise ValueError
            except ValueError:
                raise ValueError(
                    "BACKUP_KEY must encode exactly 32 random bytes in URL-safe base64"
                ) from None
