"""Telegram copy and keyboard helpers shared by the plugins."""

from html import escape

from pyrogram.enums import ButtonStyle
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

_SMALL = str.maketrans("abcdefghijklmnopqrstuvwxyz", "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ")


def caps(text):
    return text.lower().translate(_SMALL)


def head(icon, title):
    return f"{icon} <b>{escape(caps(title))}</b>"


def button(label, data=None, *, url=None, style=None):
    return InlineKeyboardButton(
        label, callback_data=data, url=url, style=ButtonStyle(style) if style else None
    )


def keys(*rows):
    return InlineKeyboardMarkup([list(row) for row in rows if row])


def size(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def home(cfg, user, owner, username):
    text = (
        head("✦", cfg["bot_title"])
        + "\n\n"
        + escape(
            cfg["start_text"].format(
                name=user.first_name or "there", bot=cfg["bot_title"]
            )[:850]
        )
    )
    links = [
        button(label, url=cfg[key])
        for key, label in (
            ("updates_link", "📢 ᴜᴘᴅᴀᴛᴇꜱ"),
            ("support_link", "💬 ꜱᴜᴘᴘᴏʀᴛ"),
            ("films_link", "🎞 ꜰɪʟᴍꜱ"),
            ("tutorial_link", "📖 ɢᴜɪᴅᴇ"),
        )
        if cfg[key]
    ]
    return text, keys(
        [
            button("🔎 ʜᴏᴡ ᴛᴏ ꜱᴇᴀʀᴄʜ", "nav:help", style="primary"),
            button("⭐ ꜱᴀᴠᴇᴅ", "nav:saved"),
        ],
        [button("⚙ ᴍʏ ꜱᴇᴛᴛɪɴɢꜱ", "cfg:u:home")]
        + ([button("🛠 ʙᴏᴛ ꜱᴇᴛᴛɪɴɢꜱ", "cfg:b:home")] if user.id == owner else []),
        links[:2],
        links[2:],
        [button("➕ ᴀᴅᴅ ᴛᴏ ɢʀᴏᴜᴘ", url=f"https://t.me/{username}?startgroup=true")],
    )


HELP = """🔎 <b>ꜱᴇᴀʀᴄʜ ɢᴜɪᴅᴇ</b>

Send a title, or use <code>/search Dune 2021</code>. Narrow a release with <code>1080p</code>, <code>Hindi</code>, <code>S02E04</code> or the filter buttons. Tap an edition to open its file card in private.

⭐ /saved opens your bookmarked files.
📮 /request TITLE sends a missing title to the moderators.
⚙ /usettings remembers your language, quality, layout and file-size display.
🛠 Owners can use /bsettings, forward a source post to index, and use /jobs to see progress."""
