"""Owner and personal inline settings. Values live in the primary database."""

from html import escape

from info import CHOICES, DEFAULTS, DESCRIPTIONS, PERSONAL, SECTIONS, parse_setting
from Script import button, caps, head, keys


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
        return button(caps(k.replace("_", " ")), prefix + "key:" + k)

    if view == "home" and not personal:
        return title + "\n\nChoose a section.", keys(
            *[
                [button(caps(name), prefix + f"section:{i}")]
                for i, name in enumerate(SECTIONS)
            ],
            [button("✕ ᴄʟᴏꜱᴇ", "nav:close")],
        )
    if view in {"home", "section"}:
        fields = list(defaults) if personal else list(SECTIONS.values())[int(arg)]
        rows = [list(map(item, fields[n : n + 2])) for n in range(0, len(fields), 2)]
        return title + "\n\nChoose a setting.", keys(
            *rows,
            [
                button("✕ ᴄʟᴏꜱᴇ", "nav:close")
                if personal
                else button("‹ ʙᴀᴄᴋ", prefix + "home")
            ],
        )
    if arg not in defaults:
        raise ValueError("Unknown setting.")
    label = arg.replace("_", " ")
    lines = [
        title,
        "",
        f"<b>{escape(caps(label))}</b>",
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
            )
            for v in choices
        ]
        rows = [options[n : n + 3] for n in range(0, len(options), 3)]
    else:
        rows = [[button("✎ ᴇᴅɪᴛ", prefix + "edit:" + arg, style="primary")]]
    return "\n".join(lines), keys(
        *rows, [button("↺ ʀᴇꜱᴇᴛ", prefix + "reset:" + arg), button("‹ ʙᴀᴄᴋ", back)]
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
            await app.db.prompt(uid, "setting", scope=scope, key=key)
            return await q.message.reply_text(
                head("✎", "Edit " + key.replace("_", " "))
                + "\n\n"
                + escape(DESCRIPTIONS.get(key, "Send the new value."))
                + "\nSend /cancel to leave."
            )
        if verb == "reset":
            await app.db.setting(key, None, target, reset=True)
        else:
            await app.db.setting(key, parse_setting(key, args[1], personal), target)
        verb = "key"
    text, markup = await panel(app, uid, scope, verb, key)
    if q.message.photo:
        return await q.message.reply_text(text, reply_markup=markup)
    return await q.message.edit_text(text, reply_markup=markup)


async def input_value(app, m, prompt):
    uid = m.from_user.id
    scope, key = prompt["scope"], prompt["key"]
    access(app, uid, m.chat.id, scope)
    raw = m.photo.file_id if key == "start_pic" and m.photo else m.text
    if raw is None:
        raise ValueError("Send a text value or /cancel.")
    value = parse_setting(key, raw, scope == "u")
    if key == "tmdb_key":
        from pyrogram.errors import RPCError

        try:
            await m.delete()
        except RPCError:
            pass
    await app.db.setting(key, value, uid if scope == "u" else None)
    await app.db.prompts.delete_one({"_id": uid, "kind": "setting", "key": key})
    text, markup = await panel(app, uid, scope, "key", key)
    await m.reply_text("✓ Saved\n\n" + text, reply_markup=markup)


GROUP_FIELDS = ("group_search", "spell_check", "welcome", "layout")


async def group_panel(app, m, user, verb=None, key=None):
    from pyrogram import enums

    if m.chat.id >= 0:
        raise ValueError("Use /settings inside the group you manage.")
    if user.id != app.owner:
        member = await app.get_chat_member(m.chat.id, user.id)
        if member.status not in {
            enums.ChatMemberStatus.OWNER,
            enums.ChatMemberStatus.ADMINISTRATOR,
        }:
            raise ValueError("Group administrator access required.")
    coll = app.db.db.eva_groups
    global_cfg = await app.db.config()
    cfg = await group_config(app, m.chat.id, global_cfg)
    if key in GROUP_FIELDS:
        v = (
            ("links" if cfg[key] == "buttons" else "buttons")
            if key == "layout"
            else not cfg[key]
        )
        await coll.update_one(
            {"_id": m.chat.id}, {"$set": {f"values.{key}": v}}, upsert=True
        )
    elif verb == "reset":
        await coll.delete_one({"_id": m.chat.id})
    cfg = await group_config(app, m.chat.id, global_cfg)
    markup = keys(
        *[
            [
                button(
                    caps(k.replace("_", " ")) + " · " + display(cfg[k]),
                    f"grp:toggle:{k}",
                )
            ]
            for k in GROUP_FIELDS
        ],
        [button("↺ ᴜꜱᴇ ʙᴏᴛ ᴅᴇꜰᴀᴜʟᴛꜱ", "grp:reset")],
    )
    text = head("⚙", "Group settings") + "\n\nTap a row to change it for this group."
    if verb:
        return await m.edit_text(text, reply_markup=markup)
    await m.reply_text(text, reply_markup=markup)


async def group_config(app, chat, cfg):
    if chat >= 0:
        return cfg
    row = await app.db.db.eva_groups.find_one({"_id": chat})
    overrides = (row or {}).get("values", {}).copy()
    # The owner can turn off all group searching, including customized groups.
    if not cfg["group_search"]:
        overrides.pop("group_search", None)
    return cfg | overrides
