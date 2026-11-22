"""Wovie Auto Filter. Run exactly one process per Telegram bot token."""

import asyncio
import logging
import os
import re
from contextlib import AsyncExitStack
from pathlib import Path
from time import monotonic

from aiohttp import ClientSession, web
from pymongo.errors import PyMongoError
from pyrogram import Client, enums, idle, types
from pyrogram.errors import FloodWait, MessageNotModified, RPCError

from database.ia_filterdb import Store, norm, now
from info import bootstrap
from plugins import commands, index, media, settings
from plugins import pm_filter as search
from Script import HELP, button, head, home, keys

log = logging.getLogger("wovie")
OPERATIONS = {
    "index",
    "sindex",
    "jobs",
    "stats",
    "sources",
    "index_channels",
    "requests",
    "broadcast",
    "grp_broadcast",
    "ban",
    "unban",
    "delete",
    "filter",
    "unfilter",
    "filters",
    "add_premium",
    "remove_premium",
}
PUBLIC = {
    "start": "Open the main menu",
    "search": "Find a movie or series",
    "saved": "Open your watchlist",
    "request": "Request a missing title",
    "top": "Trending searches",
    "myplan": "View your membership",
    "usettings": "Personal search settings",
    "settings": "Group settings",
    "id": "Show Telegram IDs",
    "help": "Search guide",
}


async def welcome(app, m, cfg):
    text, markup = home(cfg, m.from_user, app.owner, app.me.username)
    if cfg["start_pic"]:
        try:
            return await m.reply_photo(
                cfg["start_pic"], caption=text, reply_markup=markup
            )
        except FloodWait:
            raise
        except (ValueError, RPCError):
            log.info("Welcome image unavailable, using text")
    await m.reply_text(
        text,
        reply_markup=markup,
        link_preview_options=types.LinkPreviewOptions(is_disabled=True),
    )


async def banned(app, *ids):
    return bool(
        await app.db.users.find_one(
            {"_id": {"$in": [i for i in ids if i != app.owner]}, "banned": True},
            {"_id": 1},
        )
    )


async def register(app, chat):
    r = await app.db.users.update_one(
        {"_id": chat.id}, {"$min": {"seen": now()}}, upsert=True
    )
    if r.upserted_id is not None or not await app.db.peers.find_one(
        {"_id": chat.id}, {"_id": 1}
    ):
        await index.remember(app, chat)


async def handle(app, m):
    if m.outgoing or not m.chat:
        return
    chat = m.chat.id
    cfg = await app.db.config()
    if (m.video or m.document or m.audio or m.animation) and chat in cfg["sources"]:
        await index.live(app, m, cfg)
    if m.chat.type == enums.ChatType.CHANNEL:
        return
    if m.from_user is None or m.from_user.is_bot:
        return
    uid = m.from_user.id
    if await banned(app, uid, chat):
        return
    cfg = await settings.group_config(app, chat, cfg)
    if chat > 0 or m.text or m.new_chat_members:
        await register(app, m.chat)
    if m.new_chat_members and cfg["welcome"]:
        return await m.reply_text(
            head("👋", "Welcome")
            + "\nSend a movie or series title to search the catalog.",
            reply_markup=keys(
                [button("🔎 ꜱᴇᴀʀᴄʜ ɢᴜɪᴅᴇ", url=search.link(app, "help"))]
            ),
        )
    text = str(m.text or "").strip()
    operator = uid == app.owner or uid in cfg["admins"]
    if text.startswith("/"):
        word, _, arg = text.partition(" ")
        name, _, target = word[1:].partition("@")
        cmd = name.lower()
        if target and target.lower() != app.me.username.lower():
            return
        arg = arg.strip()
        if cmd == "cancel":
            await app.db.prompts.delete_one({"_id": uid})
            return await m.reply_text("✓ Input cancelled.")
        if cmd == "start":
            if chat > 0 and arg.startswith("f_"):
                return await search.file_card(app, m, arg[2:], cfg)
            if chat > 0 and (match := re.fullmatch(r"p_([a-f0-9]{12})_(\d{1,2})", arg)):
                return await search.send_page(app, m, match[1], int(match[2]), cfg)
            if arg == "help":
                return await m.reply_text(HELP)
            if chat < 0:
                return await m.reply_text(
                    head("🎬", cfg["bot_title"])
                    + "\nSend a title here or open the bot in private.",
                    reply_markup=keys(
                        [button("✦ ᴏᴘᴇɴ ᴍᴇɴᴜ", url=search.link(app, "home"))]
                    ),
                )
            return await welcome(app, m, cfg)
        if cmd in {"bsetting", "bsettings", "botsettings"}:
            return await settings.open_panel(app, m, "b")
        if cmd in {"usetting", "usettings", "usersettings"}:
            return await settings.open_panel(app, m, "u")
        if cmd == "settings":
            return await (
                settings.group_panel(app, m, m.from_user)
                if chat < 0
                else settings.open_panel(app, m, "u")
            )
        if cmd == "search":
            if not arg:
                return await m.reply_text(
                    "🔎 Use /search followed by a movie or series title."
                )
            return await search.start_search(app, m, arg, cfg)
        if cmd == "saved":
            return await search.show_saved(app, m, uid)
        if cmd == "request":
            return await commands.request_title(app, m, uid, arg, cfg)
        if cmd == "help":
            return await m.reply_text(HELP)
        if cmd == "id":
            return await m.reply_text(
                f"User · <code>{uid}</code>\nChat · <code>{chat}</code>"
            )
        if cmd == "myplan":
            return await commands.plan(app, m, uid)
        if cmd == "top":
            return await commands.trending(app, m, cfg)
        if cmd == "ping":
            start = monotonic()
            msg = await m.reply_text("🏓 …")
            return await msg.edit_text(f"🏓 {1000 * (monotonic() - start):.0f} ms")
        if cmd in OPERATIONS:
            if not operator:
                raise ValueError("Catalog admin access required.")
            if cmd in {"index", "sindex"}:
                return await index.propose(app, m, arg, session=cmd == "sindex")
            if cmd == "jobs":
                return await index.status(app, m)
            if cmd in {"add_premium", "remove_premium"}:
                return await commands.set_plan(app, m, cmd, arg)
            return await commands.admin_command(app, m, cmd, arg, cfg)
        if chat > 0:
            return await m.reply_text("Use /help to see the available commands.")
        return
    if chat > 0:
        pending = await app.db.user_prompt(uid)
        if pending and pending["kind"] == "setting":
            return await settings.input_value(app, m, pending)
        if operator and (m.forward_origin or (pending and pending["kind"] == "index")):
            return await index.propose(
                app,
                m,
                text if not m.forward_origin else "",
                session=bool(pending and pending.get("session")),
            )
    if text and len(text) <= 120 and search.allowed(chat, cfg):
        custom = await app.db.db.eva_filters.find_one({"_id": norm(text)})
        if custom:
            return await m.reply_text(
                custom["text"], parse_mode=enums.ParseMode.DISABLED
            )
        return await search.start_search(app, m, text, cfg)


async def callback(app, q):
    if not q.message or not isinstance(q.data, str):
        return
    uid, chat = q.from_user.id, q.message.chat.id
    if await banned(app, uid, chat):
        raise ValueError("Access is disabled.")
    cfg = await settings.group_config(app, chat, await app.db.config())
    family, *parts = q.data.split(":")
    verb = parts[0] if parts else ""
    if family == "cfg":
        return await settings.callback(app, q, parts)
    if family == "grp":
        return await settings.group_panel(app, q.message, q.from_user, *parts)
    if family == "s":
        return await search.search_action(app, q, parts, cfg)
    if family == "f":
        return await search.file_action(app, q, *parts, cfg)
    if family == "idx":
        return await index.action(app, q, *parts, cfg)
    if family in {"req", "bc"}:
        if uid not in {app.owner, *cfg["admins"]}:
            raise ValueError("Catalog admin access required.")
        return await (
            commands.moderate(app, q, *parts)
            if family == "req"
            else commands.broadcast_action(app, q, *parts)
        )
    if family == "nav":
        if verb == "help":
            return await q.message.reply_text(HELP)
        if verb == "saved":
            return await search.show_saved(
                app, q.message, uid, int(parts[1]) if len(parts) > 1 else 0
            )
        if verb == "close" and chat == uid:
            return await q.message.delete()


def handlers(app):
    @app.on_message()
    async def on_message(client, m):
        try:
            await client.ready.wait()
            await handle(client, m)
        except FloodWait as e:
            await m.reply_text(
                f"Telegram requested a pause. Try again in {e.value} seconds."
            )
        except ValueError as e:
            await m.reply_text(str(e), parse_mode=enums.ParseMode.DISABLED)
        except (RPCError, PyMongoError):
            log.exception("Message could not be completed")
            if m.chat and m.chat.type != enums.ChatType.CHANNEL:
                await m.reply_text(
                    "This action is temporarily unavailable. Check the bot’s chat permissions or try again shortly."
                )

    @app.on_edited_message()
    async def edited(client, m):
        await client.ready.wait()
        if m.chat and m.chat.id < 0:
            await index.live(client, m, await client.db.config(), edited=True)

    @app.on_deleted_messages()
    async def deleted(client, messages):
        await client.ready.wait()
        await index.remove_posts(client, messages, await client.db.config())

    @app.on_callback_query()
    async def pressed(client, q):
        # Acknowledge straight away, including when Telegram needs time to fetch media.
        try:
            await q.answer()
            await client.ready.wait()
            await callback(client, q)
        except MessageNotModified:
            pass
        except FloodWait as e:
            await q.message.reply_text(f"Try again in {e.value} seconds.")
        except (ValueError, KeyError, IndexError) as e:
            await q.message.reply_text(str(e), parse_mode=enums.ParseMode.DISABLED)
        except (RPCError, PyMongoError):
            log.exception("Button action failed")
            if q.message:
                await q.message.reply_text(
                    "The action could not finish. Reopen the menu or try again shortly."
                )

    @app.on_inline_query()
    async def inline(client, q):
        await client.ready.wait()
        if await banned(client, q.from_user.id):
            return await q.answer([], cache_time=0, is_personal=True)
        await search.inline(client, q, await client.db.config())


async def run():
    api_id, api_hash, token, owner, uris = bootstrap()
    port = int(os.getenv("PORT", "8080"))
    app = Client(
        "wovie",
        api_id=api_id,
        api_hash=api_hash,
        bot_token=token,
        in_memory=True,
        workers=6,
        parse_mode=enums.ParseMode.HTML,
        sleep_threshold=8,
        max_concurrent_transmissions=4,
    )
    app.db = Store(uris)
    app.owner = owner
    app.reader = None
    app.stream_slots = asyncio.Semaphore(3)
    app.ready = asyncio.Event()
    handlers(app)
    async with AsyncExitStack() as stack:
        stack.push_async_callback(app.db.close)
        await app.db.open()
        # Peer hashes belong to the Telegram identity that observed them.
        bot_id = int(token.partition(":")[0])
        ident = await app.db.db.eva_meta.find_one_and_update(
            {"_id": "telegram"},
            {"$setOnInsert": {"id": bot_id}},
            upsert=True,
            return_document=True,
        )
        if ident["id"] != bot_id:
            raise ValueError(
                "Use the original bot token or choose a separate Mongo database for this bot."
            )
        app.http = await stack.enter_async_context(ClientSession())
        secret = Path("index.session-string")
        if secret.is_file():
            reader = Client(
                "reader",
                api_id=api_id,
                api_hash=api_hash,
                session_string=secret.read_text().strip(),
                in_memory=True,
                no_updates=True,
            )
            app.reader = await stack.enter_async_context(reader)
            if reader.me.is_bot:
                raise ValueError(
                    "The optional indexing session must be a user session."
                )
        await stack.enter_async_context(app)
        peers = app.db.peers.find().batch_size(500)
        while batch := await peers.to_list(500):
            await app.storage.update_peers(
                [(p["_id"], p["hash"], p["type"], None) for p in batch]
            )
        app.ready.set()
        await app.set_bot_commands([types.BotCommand(k, v) for k, v in PUBLIC.items()])
        if port:
            webapp = web.Application()

            async def health(req):
                try:
                    await app.db.db.command("ping")
                except PyMongoError:
                    return web.json_response(
                        {"status": "database unavailable"}, status=503
                    )
                return web.json_response(
                    {"status": "ready"} if app.is_connected else {"status": "offline"},
                    status=200 if app.is_connected else 503,
                )

            async def stream(req):
                return await media.serve(app, req)

            webapp.router.add_get("/", health)
            webapp.router.add_get("/health", health)
            webapp.router.add_get("/media/{token}", stream)
            runner = web.AppRunner(webapp, access_log=None)
            await runner.setup()
            stack.push_async_callback(runner.cleanup)
            await web.TCPSite(runner, "0.0.0.0", port).start()
        tasks = [
            asyncio.create_task(index.worker(app)),
            asyncio.create_task(commands.housekeeping(app)),
        ]
        try:
            log.info("Ready as @%s", app.me.username)
            await idle()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    logging.getLogger("pyrogram").setLevel(logging.WARNING)
    try:
        asyncio.run(run())
    except (ValueError, PyMongoError) as exc:
        raise SystemExit(f"Startup failed: {exc}") from None
