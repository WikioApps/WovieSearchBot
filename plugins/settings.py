"""Owner and personal inline settings. Values live in the primary database."""

from html import escape

from info import CHOICES, DEFAULTS, DESCRIPTIONS, PERSONAL, SECTIONS, parse_setting
from plugins.file_store import archive_channel
from Script import button, caps, edit_panel, head, keys

LABELS = {
    "force_sub": "Required channels",
    "pm_search": "Private search",
    "layout": "Result layout",
    "requests_chat": "Request inbox",
    "archive_chat": "Private storage channel",
    "updates_chat": "Announcement channel",
    "sources": "Media sources",
    "search_groups": "Allowed groups",
    "start_pic": "Welcome image",
    "start_text": "Welcome text",
    "batch_size": "Index batch size",
    "protect_files": "Protect deliveries",
    "tmdb_key": "TMDB key",
}


def label(key):
    return LABELS.get(key, key.replace("_", " "))


def display(v):
    if type(v) is bool:
        return "On" if v else "Off"
    if isinstance(v, list):
        return ", ".join(map(str, v)) or "Empty"
    return str(v) if v != "" else "Empty"


def access(app, uid, chat, scope):
    if chat != uid:
        raise ValueError("Open settings in a private chat with the bot.")
    if scope not in {"b", "u"} or (scope == "b" and uid != app.owner):
        raise ValueError("Only the owner can edit bot settings.")


async def panel(app, uid, scope, view="home", arg=None):
    personal = scope == "u"
    defaults = PERSONAL if personal else DEFAULTS
    cfg = await app.db.config(uid if personal else None)
    prefix = f"cfg:{scope}:"
    title = head(
        "⚙" if personal else "🛠", "My settings" if personal else "Bot settings"
    )

    def item(k):
        return button(caps(label(k)), prefix + "key:" + k)

    if view == "home" and not personal:
        return title + "\n\nChoose a section.", keys(
            *[
                [button(caps(name), prefix + f"section:{i}")]
                for i, name in enumerate(SECTIONS)
            ],
            [button("‹ ʜᴏᴍᴇ", "nav:home")],
        )
    if view in {"home", "section"}:
        fields = list(defaults) if personal else list(SECTIONS.values())[int(arg)]
        rows = [list(map(item, fields[n : n + 2])) for n in range(0, len(fields), 2)]
        return title + "\n\nChoose a setting.", keys(
            *rows,
            [
                button("‹ ʜᴏᴍᴇ", "nav:home")
                if personal
                else button("‹ ʙᴀᴄᴋ", prefix + "home")
            ],
        )
    if arg not in defaults:
        raise ValueError("Unknown setting.")
    name = label(arg)
    lines = [
        title,
        "",
        f"<b>{escape(caps(name))}</b>",
        escape(DESCRIPTIONS.get(arg, "Choose the value that this bot should use.")),
        f"Current · <code>{escape('••••••' if arg == 'tmdb_key' and cfg[arg] else display(cfg[arg]))}</code>",
        f"Default · <code>{escape(display(defaults[arg]))}</code>",
    ]
    section = next((i for i, ks in enumerate(SECTIONS.values()) if arg in ks), 0)
    back = prefix + ("home" if personal else f"section:{section}")
    choices = CHOICES.get(arg)
    if personal and arg == "layout":
        choices = ("default", *choices)
    if type(defaults[arg]) is bool:
        choices = ("on", "off")
    if choices:
        options = [
            button(
                ("✓ " if str(v).lower() == display(cfg[arg]).lower() else "") + caps(v),
                prefix + f"set:{arg}:{v}",
                style="danger"
                if v == "off"
                else "success"
                if v == "on" or str(v).lower() == display(cfg[arg]).lower()
                else "primary",
            )
            for v in choices
        ]
        rows = [options[n : n + 3] for n in range(0, len(options), 3)]
    else:
        rows = [[button("✎ ᴇᴅɪᴛ", prefix + "edit:" + arg, style="primary")]]
    return "\n".join(lines), keys(
        *rows,
        [
            button("↺ ʀᴇꜱᴇᴛ", prefix + "reset:" + arg, style="danger"),
            button("‹ ʙᴀᴄᴋ", back),
        ],
    )


async def open_panel(app, m, scope):
    access(app, m.from_user.id, m.chat.id, scope)
    await app.db.prompts.delete_one({"_id": m.from_user.id})
    text, markup = await panel(app, m.from_user.id, scope)
    await m.reply_text(text, reply_markup=markup)


async def callback(app, q, parts):
    scope, verb, *args = parts
    uid = q.from_user.id
    access(app, uid, q.message.chat.id, scope)
    personal = scope == "u"
    target = uid if personal else None
    defaults = PERSONAL if personal else DEFAULTS
    key = next(iter(args), None)
    await app.db.prompts.delete_one({"_id": uid})
    if verb in {"reset", "set", "edit"}:
        if key not in defaults:
            raise ValueError("Unknown setting.")
        if verb == "edit":
            await app.db.prompt(
                uid, "setting", scope=scope, key=key, panel=q.message.id
            )
            return await edit_panel(
                q.message,
                head("✎", "Edit " + key.replace("_", " "))
                + "\n\n"
                + escape(DESCRIPTIONS.get(key, "Send the new value."))
                + "\nSend the new value in this chat.",
                keys([button("‹ ʙᴀᴄᴋ", f"cfg:{scope}:key:{key}")]),
            )
        if verb == "reset":
            await app.db.setting(key, None, target, reset=True)
        else:
            if key not in CHOICES and type(defaults[key]) is not bool:
                raise ValueError("Use Edit to enter this setting.")
            await app.db.setting(key, parse_setting(key, args[1], personal), target)
        verb = "key"
    text, markup = await panel(app, uid, scope, verb, key)
    return await edit_panel(q.message, text, markup)


async def input_value(app, m, prompt):
    uid = m.from_user.id
    scope, key = prompt["scope"], prompt["key"]
    access(app, uid, m.chat.id, scope)
    raw = m.photo.file_id if key == "start_pic" and m.photo else m.text
    if raw is None:
        raise ValueError("Send a text value or /cancel.")
    value = parse_setting(key, raw, scope == "u")
    if key == "archive_chat" and value:
        from plugins.index import remember

        chat = await archive_channel(app, value, await app.db.config())
        await remember(app, chat)
    if key == "sources" and (await app.db.config()).get("archive_chat") in value:
        raise ValueError(
            "Keep the private storage channel outside the searchable sources."
        )
    if key == "tmdb_key":
        from pyrogram.errors import RPCError

        try:
            await m.delete()
        except RPCError:
            pass
    await app.db.setting(key, value, uid if scope == "u" else None)
    await app.db.prompts.delete_one({"_id": uid, "kind": "setting", "key": key})
    text, markup = await panel(app, uid, scope, "key", key)
    if prompt.get("panel"):
        from pyrogram.errors import MessageIdInvalid

        try:
            old = await app.get_messages(uid, prompt["panel"], replies=0)
            if old and not old.empty:
                return await edit_panel(old, "✓ Saved\n\n" + text, markup)
        except MessageIdInvalid:
            pass
    await m.reply_text("✓ Saved\n\n" + text, reply_markup=markup)
