"""Telegram panels use native HTML entities and semantic Telegram button colours."""

import re
import unicodedata
from html import escape

from pyrogram import types
from pyrogram.enums import ButtonStyle
from pyrogram.errors import MessageNotModified
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

_SMALL = str.maketrans("abcdefghijklmnopqrstuvwxyz", "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ")


def caps(text):
    return text.lower().translate(_SMALL)


def head(icon, title):
    return f"{icon} <b>{escape(caps(title))}</b>"


def button(label, data=None, *, url=None, style="primary"):
    return InlineKeyboardButton(
        label, callback_data=data, url=url, style=ButtonStyle(style)
    )


def keys(*rows, close=True):
    keyboard = [list(row) for row in rows if row]
    if close:
        cross = button("✕ ᴄʟᴏꜱᴇ", "nav:close", style="danger")
        if (
            keyboard
            and len(keyboard[-1]) == 1
            and (
                keyboard[-1][0].text.startswith("‹")
                or keyboard[-1][0].text.startswith("↻")
            )
        ):
            keyboard[-1].append(cross)
        else:
            keyboard.append([cross])
    return InlineKeyboardMarkup(keyboard)


def size(n):
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB", "EB"):
        if float(f"{value:.4g}") < 1000 or unit == "EB":
            return f"{value:.4g} {unit}"
        value /= 1000


def duration(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m" if h else f"{m}m {s}s" if m else f"{s}s"


def release(name):
    """Display-only cleanup. Index keys and Telegram's original filename stay intact."""
    text = unicodedata.normalize("NFKC", str(name))
    text = re.sub(r"</?(?:b|i|u|s|code|strong|em)>", " ", text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?i)\.(mkv|mp4|webm|avi|mov|m4v|ts|mp3|aac|flac|m4a|zip|pdf)$",
        "",
        text.strip(),
    )
    text = re.sub(r"(?:https?://|(?:www\.)?t\.me/)\S+|(?<!\w)@[\w]{3,}", " ", text)
    text = re.sub(r"(?<!\d)([257])\.([01])(?!\d)", r"\1·\2", text)
    text = re.sub(r"[._*~`|#\[\]{}]+", " ", text).replace("·", ".")
    text = "".join(
        " " if unicodedata.category(c) in {"So", "Cf", "Cc"} or c == "\ufe0f" else c
        for c in text
    )
    return " ".join(text.split()).strip(" -–•") or "Media file"


async def edit_panel(message, text, markup):
    try:
        if message.photo:
            if len(text) > 1000:
                new = await message.reply_text(text, reply_markup=markup)
                await message.delete()
                return new
            return await message.edit_caption(caption=text, reply_markup=markup)
        return await message.edit_text(
            text,
            reply_markup=markup,
            link_preview_options=types.LinkPreviewOptions(is_disabled=True),
        )
    except MessageNotModified:
        return message


def home(cfg, user, owner, username):
    greeting = cfg["start_text"].format(
        name=user.first_name or "friend", bot=cfg["bot_title"]
    )
    text = head("🎬", cfg["bot_title"]) + "\n\n" + escape(caps(greeting[:820]))
    return text, keys(
        [button("🔎 ꜱᴇᴀʀᴄʜ ɢᴜɪᴅᴇ", "nav:help"), button("★ ꜱᴀᴠᴇᴅ", "nav:saved")],
        [button("⚙ ᴍʏ ꜱᴇᴛᴛɪɴɢꜱ", "cfg:u:home")]
        + ([button("🛠 ʙᴏᴛ ꜱᴇᴛᴛɪɴɢꜱ", "cfg:b:home")] if user.id == owner else []),
        [button("▤ ᴅᴀꜱʜʙᴏᴀʀᴅ", "stats:refresh")]
        if user.id in {owner, *cfg["admins"]}
        else [],
        [button("📢 ᴄʜᴀɴɴᴇʟꜱ", "nav:links")] if any(cfg[k] for k in LINKS) else [],
        [
            button(
                "＋ ᴀᴅᴅ ᴛᴏ ɢʀᴏᴜᴘ",
                url=f"https://t.me/{username}?startgroup=true",
                style="success",
            )
        ],
    )


LINKS = {
    "updates_link": "📢 ᴜᴘᴅᴀᴛᴇꜱ",
    "support_link": "💬 ꜱᴜᴘᴘᴏʀᴛ",
    "films_link": "🎬 ꜰɪʟᴍꜱ",
    "tutorial_link": "📖 ɢᴜɪᴅᴇ",
}


def channels(cfg):
    links = [button(label, url=cfg[k]) for k, label in LINKS.items() if cfg[k]]
    return head("📢", "Channels & links"), keys(
        links[:2], links[2:], [button("‹ ʜᴏᴍᴇ", "nav:home")]
    )


HELP = (
    head("🔎", "Movie search")
    + "\n\nSend a title like <b>Dune 2021</b> or <b>Dark S02E04</b>. Choose an edition and tap Get file.\n\nDelivered copies expire in five minutes. Forward a shareable copy to Saved Messages to keep it.\n\n/request · Request a missing title\n/usettings · Choose language and quality"
)
