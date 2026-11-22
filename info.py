"""Bootstrap secrets and editable factory defaults. Mongo overrides win until reset."""

import json
import os
import re
from string import Formatter
from urllib.parse import urlsplit

from dotenv import load_dotenv

load_dotenv()
# Change these defaults for a fork, or edit the same values live in /bsettings.
DEFAULTS = {
    "bot_title": "Wovie Auto Filter",
    "stream_url": "",
    "premium_stream": False,
    "tmdb_key": "",
    "start_text": "Hey {name}! Send me a movie or series title. I’ll find the available editions.",
    "start_pic": "",
    "updates_link": "",
    "support_link": "",
    "films_link": "",
    "tutorial_link": "",
    "caption": "🎞 {filename}\n📦 {size}  •  {quality}  •  {language}",
    "sources": [],
    "search_groups": [],
    "admins": [],
    "force_sub": [],
    "requests_chat": 0,
    "updates_chat": 0,
    "log_chat": 0,
    "pm_search": True,
    "group_search": True,
    "spell_check": True,
    "protect_files": False,
    "auto_delete": 0,
    "batch_size": 50,
    "page_size": 8,
    "layout": "buttons",
    "welcome": False,
}
PERSONAL = {
    "language": "any",
    "quality": "any",
    "layout": "default",
    "sort": "newest",
    "show_size": True,
}
SECTIONS = {
    "Brand & links": (
        "bot_title",
        "start_text",
        "start_pic",
        "updates_link",
        "support_link",
        "films_link",
        "tutorial_link",
        "caption",
        "stream_url",
        "tmdb_key",
        "premium_stream",
    ),
    "Chats & access": (
        "sources",
        "search_groups",
        "admins",
        "force_sub",
        "requests_chat",
        "updates_chat",
        "log_chat",
    ),
    "Search & files": (
        "pm_search",
        "group_search",
        "spell_check",
        "protect_files",
        "auto_delete",
        "batch_size",
        "page_size",
        "layout",
        "welcome",
    ),
}
LANGUAGES = (
    "english",
    "hindi",
    "tamil",
    "telugu",
    "malayalam",
    "kannada",
    "bengali",
    "marathi",
    "punjabi",
    "korean",
    "japanese",
    "multi",
)
QUALITIES = ("2160p", "1080p", "720p", "480p", "360p", "4k")
CHOICES = {
    "language": ("any", *LANGUAGES),
    "quality": ("any", *QUALITIES),
    "layout": ("buttons", "links"),
    "sort": ("newest", "oldest"),
}
DESCRIPTIONS = {
    "premium_stream": "Limit browser streaming and downloads to accounts with an active premium membership.",
    "stream_url": "Public HTTPS address of this bot for expiring stream and download links. Reset to disable. Protected deliveries always stay in Telegram.",
    "tmdb_key": "Optional TMDB v3 API key for search posters and movie details. Stored in your private Mongo database.",
    "sources": "Chat IDs containing media to publish in your searchable catalog. Index confirmation also registers a source.",
    "search_groups": "Group IDs allowed to search. Empty allows any group where group search is on.",
    "admins": "Trusted user IDs allowed to index sources, broadcast, manage requests and operate the catalog. Only the owner can edit bot settings.",
    "force_sub": "Required channel IDs. Make the bot an administrator there. The Updates button can hold an invite link.",
    "start_pic": "Send a Telegram photo, its file ID or a public HTTPS image URL. Reset for a text welcome.",
    "start_text": "Welcome text. Fields: {name}, {bot}.",
    "caption": "File caption. Fields: {filename}, {size}, {title}, {quality}, {language}.",
    "auto_delete": "Seconds before deleting delivered messages. 0 keeps them.",
    "batch_size": "Message IDs per indexing request, 1–100.",
    "page_size": "File rows per page, 3–10.",
    "updates_chat": "Destination for announcements of new live media. 0 turns this off.",
    "requests_chat": "Destination for title requests. 0 sends them to the owner.",
    "log_chat": "Destination for index summaries. 0 turns this off.",
}


def bootstrap():
    e = {
        k: os.getenv(k, "").strip()
        for k in ("API_ID", "API_HASH", "BOT_TOKEN", "OWNER_ID", "DATABASE_URI")
    }
    missing = [k for k, v in e.items() if not v]
    if missing:
        raise ValueError("Missing " + ", ".join(missing))
    if (
        not e["OWNER_ID"].isdigit()
        or not e["API_ID"].isdigit()
        or min(int(e["OWNER_ID"]), int(e["API_ID"])) < 1
    ):
        raise ValueError("Invalid API_ID or OWNER_ID")
    if not re.fullmatch(r"[0-9a-fA-F]{32}", e["API_HASH"]) or not re.fullmatch(
        r"\d+:[\w-]{20,}", e["BOT_TOKEN"]
    ):
        raise ValueError("Invalid API_HASH or BOT_TOKEN")
    uris = (
        json.loads(e["DATABASE_URI"])
        if e["DATABASE_URI"].startswith("[")
        else [e["DATABASE_URI"]]
    )
    if (
        not isinstance(uris, list)
        or not 1 <= len(uris) <= 8
        or not all(
            isinstance(u, str) and u.startswith(("mongodb://", "mongodb+srv://"))
            for u in uris
        )
    ):
        raise ValueError("DATABASE_URI needs one Mongo URI or a JSON list")
    return int(e["API_ID"]), e["API_HASH"], e["BOT_TOKEN"], int(e["OWNER_ID"]), uris


def parse_setting(key, value, personal=False):
    defaults = PERSONAL if personal else DEFAULTS
    base = defaults[key]
    value = value.strip()
    if type(base) is bool:
        if value.lower() not in {"on", "off"}:
            raise ValueError("Choose On or Off.")
        return value.lower() == "on"
    if isinstance(base, list):
        try:
            nums = (
                list(dict.fromkeys(map(int, re.split(r"[\s,]+", value))))
                if value
                else []
            )
        except ValueError:
            raise ValueError("Send numeric IDs separated by spaces.") from None
        if len(nums) > 100 or any(
            not 0 < n < 2**53 if key == "admins" else not -(2**53) < n < 0 for n in nums
        ):
            raise ValueError(
                "Use positive user IDs or negative chat IDs (maximum 100)."
            )
        return nums
    if type(base) is int:
        low, high = {
            "page_size": (3, 10),
            "batch_size": (1, 100),
            "auto_delete": (0, 172000),
        }.get(key, (-(2**53), 0))
        try:
            n = int(value)
        except ValueError:
            raise ValueError("Enter a whole number.") from None
        if not low <= n <= high:
            raise ValueError(f"Choose a number from {low} to {high}.")
        return n
    if key in CHOICES:
        allowed = (
            (*CHOICES[key], "default") if personal and key == "layout" else CHOICES[key]
        )
        if value.lower() not in allowed:
            raise ValueError("Choose one of the displayed options.")
        return value.lower()
    if len(value) > (64 if key == "bot_title" else 780):
        raise ValueError("Text is too long.")
    if key == "tmdb_key" and value and not re.fullmatch(r"[a-fA-F0-9]{32}", value):
        raise ValueError("Enter a TMDB v3 API key.")
    if (key.endswith("_link") or key == "stream_url") and value:
        u = urlsplit(value)
        if (
            u.scheme != "https"
            or not u.hostname
            or u.username
            or u.password
            or any(c.isspace() for c in value)
        ):
            raise ValueError("Send an HTTPS link.")
    if (
        key == "stream_url"
        and value
        and (u.query or u.fragment or u.path not in {"", "/"})
    ):
        raise ValueError("Enter the public HTTPS origin without a path.")
    if key in {"start_text", "caption"}:
        allowed = (
            {"name", "bot"}
            if key == "start_text"
            else {"filename", "size", "title", "quality", "language"}
        )
        for _, f, fmt, conv in Formatter().parse(value):
            if f is not None and (f not in allowed or fmt or conv):
                raise ValueError("Unsupported template field.")
    if key in {"bot_title", "start_text", "caption"} and not value:
        raise ValueError("This value cannot be empty.")
    return value
