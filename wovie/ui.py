"""Small, original Telegram layouts; all variable content is HTML escaped."""

from html import escape
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def safe(value: object) -> str:
    return escape(str(value), quote=False)


def size_label(size: int | None) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return ""


def keyboard(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def button(
    text: str, data: str | None = None, url: str | None = None
) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text[:64], callback_data=data, url=url)


def home(name: str, first_name: str) -> str:
    return (
        f"🎬 <b>{safe(name)}</b>\n\n"
        f"Hello, {safe(first_name)}! Send a movie or series title to search your available files.\n\n"
        "<b>Search your way</b>\n"
        "<code>Dune year:2024 quality:1080p</code>\n"
        "<code>Dark season:2 episode:3</code>\n\n"
        "🔎 Search · 📁 Save favorites · 🔔 Follow releases\n"
        "Use /help for commands and filters."
    )


def help_text() -> str:
    return (
        "🎬 <b>Your movie desk</b>\n\n"
        "/search title — Search accessible files\n"
        "/recent — Latest accessible files\n"
        "/browse language:english — Distinct catalog titles\n"
        "/popular — Opted-in public title rankings\n"
        "/suggest title — Close catalog titles\n"
        "/series title — Seasons and episodes\n"
        "/variants title — Available releases\n"
        "/season title season:1 — Confirm a season batch\n"
        "/trending · /pick · /watched — Discover and track\n"
        "/favorites — Your saved files\n"
        "/watch title — Follow matching new releases\n"
        "/watchlist — View subscriptions\n"
        "/unwatch ID — Remove a subscription\n"
        "/request title — Request a missing title\n"
        "/myrequests — Your request status\n"
        "/movie title — Movie and TV information\n"
        "/history — Recent searches\n"
        "/clearhistory — Delete your search history\n"
        "/premium — Plans and account status\n\n"
        "<b>Search filters</b>\n"
        "<code>year:2024 language:english quality:1080p</code>\n"
        "<code>season:1 episode:2</code>\n\n"
        "/menu — Interactive menu\n"
        "/admin — Owner command guide\n\n"
        "Private sources require membership. Groups must be enabled by an administrator. "
        "File buttons open a private delivery link."
    )


def caption(record: dict, template: str | None = None) -> str:
    fields = {
        "filename": record.get("filename", "Media"),
        "file_name": record.get("filename", "Media"),
        "title": record.get("title", "Media"),
        "size": size_label(record.get("file_size")),
        "year": record.get("year") or "",
        "quality": record.get("quality") or "",
        "language": ", ".join(record.get("languages") or []),
    }
    if template:
        text = template
        for key, value in fields.items():
            text = text.replace("{" + key + "}", str(value))
        return safe(text[:900])
    return (
        f"🎬 <b>{safe(str(fields['file_name'])[:700])}</b>\n📦 {safe(fields['size'])}"
    )
