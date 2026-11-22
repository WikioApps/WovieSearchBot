"""Validated HTTPS-only buttons with URL-encoded metadata placeholders."""

from __future__ import annotations

import string
from urllib.parse import quote, urlsplit

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

FIELDS = frozenset(
    {"filename", "file_name", "title", "year", "quality", "language", "size"}
)
MAX_BUTTONS = 4


def validate_button(text: str, url: str, template: bool = False) -> dict[str, str]:
    label = " ".join(text.split())
    url = url.strip()
    if not label or len(label) > 40 or any(ord(c) < 32 for c in label):
        raise ValueError("Button label must contain 1–40 printable characters.")
    if not url or len(url) > 1024 or any(ord(c) < 33 for c in url):
        raise ValueError(
            "Button URL must contain 1–1024 characters without spaces or control characters."
        )
    try:
        parsed = urlsplit(url)
        port = parsed.port
        parts = tuple(string.Formatter().parse(url))
    except ValueError:
        raise ValueError("Invalid HTTPS URL or placeholder syntax.") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("Buttons require an HTTPS URL without embedded credentials.")
    if "{" in parsed.netloc or "}" in parsed.netloc:
        raise ValueError(
            "The HTTPS hostname must be fixed; placeholders are allowed only after it."
        )
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Invalid HTTPS port.")
    for _, field, spec, conversion in parts:
        if field is not None and (
            not template or field not in FIELDS or spec or conversion
        ):
            if not template:
                raise ValueError(
                    "Manual-filter button URLs must be static; placeholders belong to delivery buttons."
                )
            raise ValueError(
                "Allowed URL fields: filename, file_name, title, year, quality, language, size. Format modifiers are unsupported."
            )
    return {"text": label, "url": url}


def _values(record: dict) -> dict[str, str]:
    name = str(record.get("filename") or "Media")
    amount = float(record.get("file_size") or 0)
    unit = "B"
    for candidate in ("B", "KB", "MB", "GB", "TB"):
        unit = candidate
        if amount < 1024 or candidate == "TB":
            break
        amount /= 1024
    values = {
        "filename": name,
        "file_name": name,
        "title": str(record.get("title") or "Media"),
        "year": str(record.get("year") or ""),
        "quality": str(record.get("quality") or ""),
        "language": ", ".join(str(x) for x in (record.get("languages") or [])),
        "size": f"{int(amount)} B" if unit == "B" else f"{amount:.1f} {unit}",
    }
    return {key: quote(value, safe="") for key, value in values.items()}


def render_buttons(
    buttons: list[dict] | None, record: dict | None = None
) -> InlineKeyboardMarkup | None:
    """Validate again on read; a malformed optional button never prevents delivery."""
    if not isinstance(buttons, list):
        return None
    rendered = []
    for item in buttons[:MAX_BUTTONS]:
        try:
            if not isinstance(item, dict):
                continue
            button = validate_button(
                str(item.get("text", "")),
                str(item.get("url", "")),
                template=record is not None,
            )
            url = (
                button["url"].format_map(_values(record))
                if record is not None
                else button["url"]
            )
            if len(url) > 2048:
                continue
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                continue
            rendered.append(InlineKeyboardButton(text=button["text"], url=url))
        except (TypeError, ValueError, KeyError, OverflowError):
            continue
    if not rendered:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[rendered[i : i + 2] for i in range(0, len(rendered), 2)]
    )
