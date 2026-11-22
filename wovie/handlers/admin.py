"""Owner operations and narrowly scoped Telegram group administration."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from html import escape

from aiogram import Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import Message

from wovie.buttons import MAX_BUTTONS, validate_button


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def arguments(message: Message) -> str:
    return (message.text or message.caption or "").partition(" ")[2].strip()


def integer(
    value: str, label: str, minimum: int | None = None, maximum: int | None = None
) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer.") from exc
    if minimum is not None and number < minimum:
        raise ValueError(f"{label} must be at least {minimum}.")
    if maximum is not None and number > maximum:
        raise ValueError(f"{label} must be at most {maximum}.")
    return number


def build_router(settings, store, indexer) -> Router:
    router = Router(name="administration")

    async def respond(message: Message, text: str) -> None:
        await message.answer(text, parse_mode="HTML")

    async def authorize(message: Message, group: bool = False) -> bool:
        user = message.from_user
        if not user or message.sender_chat:
            await respond(message, "Use your own Telegram account for administration.")
            return False
        if user.id in settings.admins:
            return True
        if group and message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            try:
                member = await message.bot.get_chat_member(message.chat.id, user.id)
                if member.status in (
                    ChatMemberStatus.ADMINISTRATOR,
                    ChatMemberStatus.CREATOR,
                ):
                    return True
            except TelegramAPIError:
                pass
        await respond(message, "⛔ This action requires an authorized administrator.")
        return False

    async def private_owner(message: Message) -> bool:
        if not await authorize(message):
            return False
        if message.chat.type != ChatType.PRIVATE:
            await respond(message, "Open a private chat with me for this operation.")
            return False
        return True

    async def private_operator(message: Message) -> bool:
        if message.chat.type != ChatType.PRIVATE or not message.from_user:
            await respond(message, "Open a private chat for operational diagnostics.")
            return False
        if message.from_user.id in settings.admins:
            return True
        record = await store.db.users.find_one(
            {"_id": message.from_user.id, "role": "operator", "banned": {"$ne": True}}
        )
        if record:
            return True
        await respond(
            message, "This diagnostic command requires an owner or assigned operator."
        )
        return False

    async def error(message: Message, exc: ValueError) -> None:
        await respond(message, "⚠️ " + escape(str(exc)))

    @router.message(Command("admin"))
    async def admin_help(message: Message):
        if not await authorize(message, group=True):
            return
        await respond(
            message,
            "<b>⚙️ Wovie · Administration</b>\n\n"
            "<b>Sources:</b> /source · /index · /jobs · /pause · /resume · /cancel\n"
            "<b>Groups:</b> /group · /settings · /set · /buttons · /filter · /filterbutton · /filters · /delfilter\n"
            "<b>Catalog:</b> /batch · /delete · /restore · /purge\n"
            "<b>People:</b> /ban · /unban · /grant · /revoke · /redeemcode · /operator\n"
            "<b>Operations:</b> /stats · /broadcast · /requests · /requestdone · /backup · /export\n\n"
            "Send a command without arguments for its usage. Group administrators can edit "
            "their own group's display settings and filters only.",
        )

    @router.message(Command("source"))
    async def source(message: Message):
        if not await private_owner(message):
            return
        parts = arguments(message).split()
        if not parts or parts[0] == "list":
            try:
                page = integer(parts[1], "Page", 1, 100000) if len(parts) > 1 else 1
            except ValueError as exc:
                return await error(message, exc)
            rows = (
                await store.db.sources.find({})
                .sort("_id", 1)
                .skip((page - 1) * 15)
                .limit(16)
                .to_list(16)
            )
            text = [f"<b>📚 Sources · page {page}</b>"]
            for row in rows[:15]:
                text.append(
                    f"<code>{row['_id']}</code> · {'active' if row.get('active') else 'disabled'} · "
                    f"{'public' if row.get('public') else 'members only'} · {escape(str(row.get('title', ''))[:70])}"
                )
            if len(rows) > 15:
                text.append(f"Next: /source list {page + 1}")
            text.append(
                "\n/source add CHAT_ID [public]\n/source remove CHAT_ID\n"
                "/source publish CHAT_ID\n/source unpublish CHAT_ID"
            )
            return await respond(message, "\n".join(text))
        try:
            if len(parts) < 2 or parts[0] not in {
                "add",
                "remove",
                "publish",
                "unpublish",
            }:
                raise ValueError(
                    "Use /source add CHAT_ID [public], remove, publish, unpublish, or list."
                )
            chat_id = integer(parts[1], "Chat ID")
            if chat_id >= 0:
                raise ValueError(
                    "Use a negative Telegram group/channel ID, usually -100…"
                )
            now = utcnow()
            if parts[0] == "add":
                if len(parts) > 3 or (len(parts) == 3 and parts[2] != "public"):
                    raise ValueError(
                        "Use /source add CHAT_ID [public]. Default: members only."
                    )
                title = str(chat_id)
                try:
                    chat = await message.bot.get_chat(chat_id)
                    if chat.has_protected_content:
                        raise ValueError(
                            "This source protects its content; delivery/copying cannot be enabled."
                        )
                    title = chat.title or str(chat_id)
                except TelegramAPIError:
                    if not settings.session_string:
                        raise ValueError(
                            "The bot cannot access this chat. Add it to the source or configure an authorized session."
                        )
                    entity = await indexer.resolve_source(chat_id)
                    if getattr(entity, "noforwards", False):
                        raise ValueError(
                            "This source protects its content; delivery/copying cannot be enabled."
                        )
                    title = getattr(entity, "title", None) or str(chat_id)
                await store.db.sources.update_one(
                    {"_id": chat_id},
                    {
                        "$set": {
                            "active": True,
                            "public": len(parts) == 3,
                            "title": title,
                            "updated_at": now,
                        },
                        "$setOnInsert": {
                            "created_at": now,
                            "created_by": message.from_user.id,
                        },
                    },
                    upsert=True,
                )
                await respond(
                    message,
                    f"✅ Source <code>{chat_id}</code> enabled "
                    f"({'public catalog' if len(parts) == 3 else 'members only'}).\n"
                    "New posts can be indexed. Use /index for accessible history.",
                )
            else:
                changes = {"updated_at": now}
                changes["active" if parts[0] == "remove" else "public"] = (
                    parts[0] == "publish"
                )
                result = await store.db.sources.update_one(
                    {"_id": chat_id}, {"$set": changes}
                )
                if not result.matched_count:
                    raise ValueError("Source not found. Add it first.")
                await respond(
                    message,
                    "✅ Source updated. Existing catalog records are preserved.",
                )
        except ValueError as exc:
            await error(message, exc)

    @router.message(Command("index"))
    async def index(message: Message):
        if not await private_owner(message):
            return
        try:
            parts = arguments(message).split()
            if len(parts) not in (1, 2, 3):
                raise ValueError(
                    "Use /index CHAT_ID [START_MESSAGE_ID [END_MESSAGE_ID]]."
                )
            chat_id = integer(parts[0], "Chat ID")
            start = integer(parts[1], "Start ID", 0) if len(parts) > 1 else 0
            end = integer(parts[2], "End ID", 0) if len(parts) > 2 else 0
            if end and end < start:
                raise ValueError("End ID must be at least the start ID.")
            source_row = await store.db.sources.find_one(
                {"_id": chat_id, "active": True}
            )
            if not source_row:
                raise ValueError("Enable this source with /source add first.")
            job_id = await indexer.queue(chat_id, message.from_user.id, start, end)
            await respond(
                message,
                f"📥 Index queued: <code>{escape(str(job_id))}</code>\nUse /jobs for progress.",
            )
        except (ValueError, RuntimeError) as exc:
            await error(message, ValueError(str(exc)))

    @router.message(Command("jobs"))
    async def jobs(message: Message):
        if not await private_operator(message):
            return
        try:
            page = integer(arguments(message) or "1", "Page", 1, 100000)
        except ValueError as exc:
            return await error(message, exc)
        rows = (
            await store.db.jobs.find({})
            .sort("created_at", -1)
            .skip((page - 1) * 12)
            .limit(13)
            .to_list(13)
        )
        lines = [f"<b>⏳ Jobs · page {page}</b>"]
        for row in rows[:12]:
            cursor = row.get("payload", {}).get("cursor", 0)
            lines.append(
                f"<code>{escape(str(row['_id']))}</code>\n"
                f"{escape(row.get('kind', '?'))} · {escape(row.get('status', '?'))} · cursor {escape(str(cursor))}"
            )
        if len(rows) > 12:
            lines.append(f"Next: /jobs {page + 1}")
        await respond(message, "\n".join(lines))

    @router.message(Command("pause", "resume", "cancel"))
    async def job_control(message: Message):
        if not await private_owner(message):
            return
        action = (message.text or "").split()[0].split("@")[0][1:]
        job_id = arguments(message)
        if not job_id or len(job_id) > 128:
            return await respond(message, f"Use /{action} JOB_ID.")
        row = await store.db.jobs.find_one({"_id": job_id})
        if not row:
            return await respond(message, "Job not found.")
        permitted = {
            "pause": {"queued", "running"},
            "resume": {"paused", "failed"},
            "cancel": {"queued", "running", "paused", "failed"},
        }
        if row.get("status") not in permitted[action]:
            return await respond(
                message, "This job cannot make that transition. Check /jobs."
            )
        changes = {
            "status": {"pause": "paused", "resume": "queued", "cancel": "cancelled"}[
                action
            ],
            "updated_at": utcnow(),
        }
        if action == "resume":
            changes.update(run_at=utcnow(), attempts=0)
        result = await store.db.jobs.update_one(
            {"_id": job_id, "status": row["status"]},
            {"$set": changes, "$unset": {"worker_id": "", "lease_until": ""}},
        )
        await respond(
            message,
            "✅ Job state updated. An already accepted Telegram send may still finish."
            if result.modified_count
            else "Job changed while processing; refresh /jobs.",
        )

    @router.message(Command("stats"))
    async def stats(message: Message):
        if not await private_operator(message):
            return
        values = await store.stats()
        lines = ["<b>📊 Wovie · Status</b>"]
        for key, value in list(values.items())[:30]:
            lines.append(
                f"<b>{escape(str(key).replace('_', ' '))}:</b> {escape(str(value))[:160]}"
            )
        await respond(message, "\n".join(lines))

    @router.message(Command("operator"))
    async def operator(message: Message):
        if not await private_owner(message):
            return
        try:
            parts = arguments(message).split()
            if len(parts) != 2 or parts[0] not in {"add", "remove"}:
                raise ValueError(
                    "Use /operator add USER_ID or /operator remove USER_ID. Operators can only read /jobs, /stats and /health in private chat."
                )
            user_id = integer(parts[1], "User ID", 1)
            if user_id in settings.admins:
                raise ValueError(
                    "Configured owners retain their role through ADMINS; operator assignment does not change it."
                )
            await store.db.users.update_one(
                {"_id": user_id},
                {
                    "$set": {
                        "role": "operator" if parts[0] == "add" else "user",
                        "updated_at": utcnow(),
                    }
                },
                upsert=True,
            )
            await respond(
                message,
                "✅ Operator role updated. Source permissions, billing, data mutations and inbox replay remain owner-only.",
            )
        except ValueError as exc:
            await error(message, exc)

    @router.message(Command("group"))
    async def group(message: Message):
        if not await private_owner(message):
            return
        try:
            parts = arguments(message).split()
            if len(parts) != 2 or parts[0] not in ("add", "remove"):
                raise ValueError(
                    "Use /group add GROUP_ID or /group remove GROUP_ID. Then /set GROUP_ID source_ids ID,ID."
                )
            chat_id = integer(parts[1], "Group ID", maximum=-1)
            await store.set_setting(chat_id, "enabled", parts[0] == "add")
            await respond(message, "✅ Group activation saved.")
        except ValueError as exc:
            await error(message, exc)

    async def setting_target(
        message: Message, parts: list[str]
    ) -> tuple[int, list[str]]:
        target = message.chat.id
        if parts and re.fullmatch(r"-?\d+", parts[0]):
            target = integer(parts[0], "Chat ID")
            parts = parts[1:]
        if target != message.chat.id and message.from_user.id not in settings.admins:
            raise ValueError(
                "Group administrators can change only their current group."
            )
        return target, parts

    @router.message(Command("settings"))
    async def show_settings(message: Message):
        if not await authorize(message, group=True):
            return
        try:
            target, extra = await setting_target(message, arguments(message).split())
            if extra:
                raise ValueError("Use /settings [CHAT_ID].")
            values = await store.setting(target)
            visible = {
                key: value
                for key, value in values.items()
                if key
                in {
                    "enabled",
                    "source_ids",
                    "caption_template",
                    "delete_after",
                    "protect_content",
                    "premium_only",
                    "required_channels",
                    "daily_limit",
                    "welcome_text",
                    "delivery_buttons",
                    "broadcast_opt_in",
                    "autoapprove_join_requests",
                    "batch_admission_enabled",
                    "popularity_enabled",
                }
            }
            rendered = json.dumps(visible, ensure_ascii=False, indent=2, default=str)
            await respond(
                message,
                f"<b>⚙️ Settings · {target}</b>\n<pre>{escape(rendered[:3000])}</pre>\n"
                "Use /set [CHAT_ID] KEY VALUE. Available keys: enabled, source_ids, caption_template, "
                "delete_after, protect_content, premium_only, required_channels, daily_limit, welcome_text.",
            )
        except ValueError as exc:
            await error(message, exc)

    @router.message(Command("set"))
    async def set_setting(message: Message):
        if not await authorize(message, group=True):
            return
        try:
            target, parts = await setting_target(message, arguments(message).split())
            if len(parts) < 2:
                raise ValueError("Use /set [CHAT_ID] KEY VALUE. See /settings.")
            key, raw = parts[0], " ".join(parts[1:])
            owner = message.from_user.id in settings.admins
            group_keys = {
                "caption_template",
                "delete_after",
                "protect_content",
                "welcome_text",
            }
            if not owner and key not in group_keys:
                raise ValueError(
                    "Only bot owners can change activation, access rules or source scopes."
                )
            if key in {"batch_admission_enabled", "popularity_enabled"} and target != 0:
                raise ValueError(
                    "This feature is global. Use /set 0 " + key + " true or false."
                )
            if key in {
                "enabled",
                "protect_content",
                "premium_only",
                "batch_admission_enabled",
                "popularity_enabled",
            }:
                if raw.lower() not in {"true", "false", "on", "off"}:
                    raise ValueError("Use true/false or on/off.")
                value = raw.lower() in {"true", "on"}
            elif key == "delete_after":
                value = integer(raw, "Deletion delay (seconds)", 0, 172700)
            elif key == "daily_limit":
                value = integer(raw, "Daily limit", 0, 100000)
            elif key in {"source_ids", "required_channels"}:
                value = (
                    []
                    if raw.lower() in {"none", "clear"}
                    else [
                        integer(x.strip(), "Chat ID", maximum=-1)
                        for x in raw.split(",")
                    ]
                )
                if len(value) > 50:
                    raise ValueError("At most 50 chat IDs are supported per setting.")
                if key == "source_ids":
                    for source_id in value:
                        if not await store.db.sources.find_one(
                            {"_id": source_id, "active": True}
                        ):
                            raise ValueError(f"Source {source_id} is not active.")
            elif key in {"caption_template", "welcome_text"}:
                if len(raw) > 800:
                    raise ValueError("Template must be at most 800 characters.")
                if key == "caption_template":
                    import string

                    for _, name, spec, conversion in string.Formatter().parse(raw):
                        if name is not None and (
                            name
                            not in {
                                "filename",
                                "file_name",
                                "title",
                                "size",
                                "year",
                                "quality",
                                "language",
                            }
                            or spec
                            or conversion
                        ):
                            raise ValueError(
                                "Allowed placeholders: {filename}, {file_name}, {title}, {size}, {year}, {quality}, {language}; no format modifiers."
                            )
                value = raw
            else:
                raise ValueError("Unknown setting. See /settings.")
            await store.set_setting(target, key, value)
            await respond(
                message,
                f"✅ <code>{escape(key)}</code> saved for <code>{target}</code>.",
            )
        except ValueError as exc:
            await error(message, exc)

    def filter_scope(message: Message, raw: str) -> tuple[int, str]:
        if raw.startswith("global "):
            if message.from_user.id not in settings.admins:
                raise ValueError("Global filters are owner-only.")
            return 0, raw[7:].strip()
        return message.chat.id, raw

    @router.message(Command("filter"))
    async def save_filter(message: Message):
        if not await authorize(message, group=True):
            return
        try:
            scope, raw = filter_scope(message, arguments(message))
            keyword, separator, text = raw.partition("|")
            keyword = " ".join(keyword.casefold().split())
            if not keyword or len(keyword) > 80:
                raise ValueError(
                    "Use /filter [global] KEYWORD | RESPONSE, or reply to a message with /filter KEYWORD. Keyword: 1–80 characters."
                )
            if len(text) > 3000:
                raise ValueError("Filter response must be at most 3000 characters.")
            reply = message.reply_to_message
            if not text.strip() and not reply:
                raise ValueError(
                    "Add a response after |, or reply to the message to save."
                )
            if reply and reply.has_protected_content:
                raise ValueError("Protected messages cannot be copied into filters.")
            row = {
                "chat_id": scope,
                "keyword": keyword,
                "text": text.strip(),
                "created_by": message.from_user.id,
                "updated_at": utcnow(),
            }
            if reply and not separator:
                row.update(source_chat=reply.chat.id, message_id=reply.message_id)
            await store.db.filters.replace_one(
                {"_id": f"{scope}:{keyword}"}, row, upsert=True
            )
            await respond(message, f"✅ Filter <b>{escape(keyword)}</b> saved.")
        except ValueError as exc:
            await error(message, exc)

    @router.message(Command("filters"))
    async def list_filters(message: Message):
        if not await authorize(message, group=True):
            return
        try:
            raw = arguments(message)
            if raw == "global":
                raw += " 1"
            scope, raw = filter_scope(message, raw)
            page = integer(raw or "1", "Page", 1, 100000)
            rows = (
                await store.db.filters.find({"chat_id": scope})
                .sort("keyword", 1)
                .skip((page - 1) * 25)
                .limit(26)
                .to_list(26)
            )
            lines = [f"<b>📝 Filters · page {page}</b>"] + [
                escape(x["keyword"]) for x in rows[:25]
            ]
            if len(rows) > 25:
                lines.append(
                    f"Next: /filters {'global ' if scope == 0 else ''}{page + 1}"
                )
            await respond(message, "\n".join(lines))
        except ValueError as exc:
            await error(message, exc)

    @router.message(Command("delfilter"))
    async def delete_filter(message: Message):
        if not await authorize(message, group=True):
            return
        try:
            scope, keyword = filter_scope(message, arguments(message))
            keyword = " ".join(keyword.casefold().split())
            if not keyword:
                raise ValueError("Use /delfilter [global] KEYWORD.")
            row = await store.db.filters.find_one({"_id": f"{scope}:{keyword}"})
            if not row:
                raise ValueError("Filter not found.")
            await store.db.filter_trash.replace_one(
                {"_id": row["_id"]}, {**row, "deleted_at": utcnow()}, upsert=True
            )
            await store.db.filters.delete_one(
                {"_id": row["_id"], "updated_at": row.get("updated_at")}
            )
            await respond(
                message,
                "✅ Filter removed. Its last saved copy is retained in filter_trash and backups.",
            )
        except ValueError as exc:
            await error(message, exc)

    @router.message(Command("filterbutton"))
    async def filter_button(message: Message):
        if not await authorize(message, group=True):
            return
        try:
            scope, raw = filter_scope(message, arguments(message))
            parts = [part.strip() for part in raw.split("|", 2)]
            if len(parts) not in (2, 3):
                raise ValueError(
                    "Use /filterbutton [global] KEYWORD | LABEL | HTTPS_URL, or KEYWORD | clear."
                )
            keyword = " ".join(parts[0].casefold().split())
            record = await store.db.filters.find_one({"_id": f"{scope}:{keyword}"})
            if not record:
                raise ValueError("Save this filter with /filter first.")
            if len(parts) == 2 and parts[1].lower() == "clear":
                buttons = []
            elif len(parts) == 3:
                button = validate_button(parts[1], parts[2])
                buttons = [
                    item
                    for item in record.get("buttons", [])
                    if isinstance(item, dict) and item.get("text") != button["text"]
                ]
                if len(buttons) >= MAX_BUTTONS:
                    raise ValueError(
                        f"At most {MAX_BUTTONS} buttons are allowed. Reuse a label to replace it, or clear first."
                    )
                buttons.append(button)
            else:
                raise ValueError("Use LABEL | HTTPS_URL, or clear.")
            await store.db.filters.update_one(
                {"_id": record["_id"]},
                {"$set": {"buttons": buttons, "updated_at": utcnow()}},
            )
            await respond(
                message,
                f"✅ Filter buttons saved ({len(buttons)}). Trigger the keyword to preview.",
            )
        except ValueError as exc:
            await error(message, exc)

    @router.message(Command("buttons"))
    async def delivery_buttons(message: Message):
        if not await authorize(message, group=True):
            return
        try:
            raw = arguments(message)
            first, separator, remainder = raw.partition(" ")
            target = message.chat.id
            if re.fullmatch(r"-?\d+", first):
                target = integer(first, "Chat ID")
                raw = remainder.strip()
            if (
                message.from_user.id not in settings.admins
                and target != message.chat.id
            ):
                raise ValueError(
                    "Group administrators can configure only their current group."
                )
            config = await store.setting(target)
            if raw.lower() == "clear":
                buttons = []
            else:
                parts = [part.strip() for part in raw.split("|", 1)]
                if len(parts) != 2:
                    raise ValueError(
                        "Use /buttons [CHAT_ID] LABEL | HTTPS_URL, or /buttons [CHAT_ID] clear. Owners use CHAT_ID 0 for the global default."
                    )
                button = validate_button(parts[0], parts[1], template=True)
                buttons = [
                    item
                    for item in config.get("delivery_buttons", [])
                    if isinstance(item, dict) and item.get("text") != button["text"]
                ]
                if len(buttons) >= MAX_BUTTONS:
                    raise ValueError(
                        f"At most {MAX_BUTTONS} buttons are allowed. Reuse a label to replace it, or clear first."
                    )
                buttons.append(button)
            await store.set_setting(target, "delivery_buttons", buttons)
            await respond(
                message,
                f"✅ Delivery buttons saved for <code>{target}</code> ({len(buttons)}). Group lists replace the global list for deliveries from that group's search links.",
            )
        except ValueError as exc:
            await error(message, exc)

    @router.message(Command("broadcast"))
    async def broadcast(message: Message):
        if not await private_owner(message):
            return
        reply = message.reply_to_message
        if not reply:
            return await respond(
                message,
                "Reply to a message with /broadcast. Add confirm to queue delivery to all active bot users.",
            )
        if reply.has_protected_content:
            return await respond(
                message, "Protected messages cannot be broadcast by copying."
            )
        args = arguments(message).split()
        target = "groups" if args and args[0] == "groups" else "users"
        if args and args[0] in {"groups", "users"}:
            args = args[1:]
        if args not in ([], ["confirm"]):
            return await respond(
                message,
                "Reply with /broadcast [users|groups] [confirm]. Groups must explicitly opt in with /groupbroadcast on.",
            )
        count = await (
            store.db.settings.count_documents(
                {"_id": {"$lt": 0}, "enabled": True, "broadcast_opt_in": True}
            )
            if target == "groups"
            else store.db.users.count_documents(
                {"banned": {"$ne": True}, "blocked": {"$ne": True}}
            )
        )
        if args != ["confirm"]:
            return await respond(
                message,
                f"Recipients: <b>{count} {target}</b>. Reply to the same message with /broadcast {target} confirm.",
            )
        job_id = await store.enqueue(
            "broadcast",
            {
                "actor_id": message.from_user.id,
                "source_chat": reply.chat.id,
                "message_id": reply.message_id,
                "target": target,
                "cursor": -9223372036854775808 if target == "groups" else 0,
            },
            key=f"broadcast:{reply.chat.id}:{reply.message_id}:{target}",
        )
        await respond(
            message, f"📣 Broadcast queued: <code>{escape(str(job_id))}</code>."
        )

    @router.message(Command("groupbroadcast"))
    async def groupbroadcast(message: Message):
        if (
            message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP)
            or not message.from_user
            or message.sender_chat
        ):
            return await respond(
                message,
                "A group administrator must use /groupbroadcast on|off inside that group.",
            )
        raw = arguments(message)
        if raw not in {"on", "off"}:
            return await respond(
                message,
                "Use /groupbroadcast on or /groupbroadcast off. This controls owner announcements to this group.",
            )
        try:
            member = await message.bot.get_chat_member(
                message.chat.id, message.from_user.id
            )
            if member.status not in (
                ChatMemberStatus.CREATOR,
                ChatMemberStatus.ADMINISTRATOR,
            ):
                return await respond(
                    message,
                    "Only a current administrator of this group can change announcement consent.",
                )
        except TelegramAPIError:
            return await respond(
                message, "Could not verify group-administrator permissions."
            )
        await store.set_setting(message.chat.id, "broadcast_opt_in", raw == "on")
        await respond(
            message,
            "✅ Group announcements "
            + (
                "enabled. The group must also be activated by a bot owner."
                if raw == "on"
                else "disabled."
            ),
        )

    @router.message(Command("batch"))
    async def batch(message: Message):
        if not await private_owner(message):
            return
        try:
            ids = list(dict.fromkeys(re.split(r"[\s,]+", arguments(message))))
            if (
                not ids[0]
                or len(ids) > 100
                or any(not re.fullmatch(r"[a-f0-9]{32}", x) for x in ids)
            ):
                raise ValueError(
                    "Use /batch FILE_ID FILE_ID … (up to 100 catalog IDs)."
                )
            for file_id in ids:
                record = await store.get_file(file_id)
                if not record:
                    raise ValueError(f"Catalog file {file_id} is missing or deleted.")
            token = await store.create_link(ids)
            bot = await message.bot.get_me()
            await respond(
                message,
                f"<b>📦 Bundle · {len(ids)} files</b>\n"
                f"https://t.me/{bot.username}?start=link_{token}\nMembership and access rules apply at delivery.",
            )
        except ValueError as exc:
            await error(message, exc)

    async def catalog_record(file_id: str):
        for catalog in store.catalogs:
            row = await catalog.files.find_one({"_id": file_id})
            if row:
                return catalog, row
        return None, None

    @router.message(Command("delete", "restore", "purge"))
    async def catalog_control(message: Message):
        if not await private_owner(message):
            return
        action = (message.text or "").split()[0].split("@")[0][1:]
        parts = arguments(message).split()
        if not parts or not re.fullmatch(r"[a-f0-9]{32}", parts[0]):
            return await respond(
                message,
                f"Use /{action} FILE_ID{' confirm' if action == 'purge' else ''}. Each operation affects one catalog record.",
            )
        database, row = await catalog_record(parts[0])
        if row is None:
            return await respond(message, "Catalog record not found.")
        if action == "purge":
            if not row.get("deleted_at"):
                return await respond(
                    message, "Soft-delete this record with /delete first."
                )
            if len(parts) != 2 or parts[1] != "confirm":
                return await respond(
                    message,
                    f"Permanent purge affects <b>1 record</b>: {escape(str(row.get('filename', ''))[:160])}.\n"
                    f"Use /purge {parts[0]} confirm. A verified backup is needed to recover it.",
                )
            await database.files.delete_one(
                {"_id": parts[0], "deleted_at": row["deleted_at"]}
            )
        elif action == "delete":
            await database.files.update_one(
                {"_id": parts[0]},
                {"$set": {"deleted_at": utcnow(), "deleted_by": message.from_user.id}},
            )
        else:
            await database.files.update_one(
                {"_id": parts[0]}, {"$unset": {"deleted_at": "", "deleted_by": ""}}
            )
        await store.db.audit.insert_one(
            {
                "action": action,
                "file_id": parts[0],
                "actor_id": message.from_user.id,
                "created_at": utcnow(),
            }
        )
        await respond(
            message,
            f"✅ Catalog action <b>{action}</b> completed. Telegram source media was not changed.",
        )

    @router.message(Command("ban", "unban", "grant", "revoke"))
    async def user_control(message: Message):
        if not await private_owner(message):
            return
        action = (message.text or "").split()[0].split("@")[0][1:]
        try:
            parts = arguments(message).split()
            if len(parts) != (2 if action == "grant" else 1):
                raise ValueError(
                    f"Use /{action} USER_ID{' DAYS' if action == 'grant' else ''}."
                )
            user_id = integer(parts[0], "User ID", 1)
            if action == "ban" and user_id in settings.admins:
                raise ValueError("Configured bot owners cannot be banned.")
            update = {"updated_at": utcnow()}
            if action in {"ban", "unban"}:
                update["banned"] = action == "ban"
            elif action == "grant":
                days = integer(parts[1], "Days", 1, 3650)
                from wovie.billing import extend_once

                await extend_once(
                    store.db,
                    user_id,
                    f"grant:{message.bot.id}:{message.chat.id}:{message.message_id}",
                    days,
                )
                return await respond(
                    message, f"✅ Added {days} premium days for <code>{user_id}</code>."
                )
            else:
                update["premium_until"] = utcnow()
            await store.db.users.update_one(
                {"_id": user_id}, {"$set": update}, upsert=True
            )
            await respond(
                message, f"✅ {action.capitalize()} saved for <code>{user_id}</code>."
            )
        except ValueError as exc:
            await error(message, exc)

    @router.message(Command("redeemcode"))
    async def redeemcode(message: Message):
        if not await private_owner(message):
            return
        try:
            parts = arguments(message).split()
            if len(parts) not in (2, 3, 4):
                raise ValueError(
                    "Use /redeemcode CODE DAYS [USES] [EXPIRES_DAYS]. Use auto for a random code."
                )
            code = (
                secrets.token_hex(8).upper()
                if parts[0].lower() == "auto"
                else parts[0].upper()
            )
            if not re.fullmatch(r"[A-Z0-9_-]{6,64}", code):
                raise ValueError(
                    "Code must have 6–64 letters, digits, underscores or hyphens."
                )
            days = integer(parts[1], "Premium days", 1, 3650)
            uses = integer(parts[2], "Uses", 1, 10000) if len(parts) > 2 else 1
            expiry = integer(parts[3], "Expiry days", 1, 3650) if len(parts) > 3 else 30
            digest = hashlib.sha256(code.encode()).hexdigest()
            result = await store.db.redeem_codes.update_one(
                {"_id": digest},
                {
                    "$setOnInsert": {
                        "days": days,
                        "max_uses": uses,
                        "uses": 0,
                        "expires_at": utcnow() + timedelta(days=expiry),
                        "created_at": utcnow(),
                        "created_by": message.from_user.id,
                    }
                },
                upsert=True,
            )
            if result.upserted_id is None:
                raise ValueError("That code already exists; create a different code.")
            await respond(
                message,
                f"🎟 <code>{escape(code)}</code>\n{days} premium days · {uses} claims · expires in {expiry} days.",
            )
        except ValueError as exc:
            await error(message, exc)

    @router.message(Command("requests"))
    async def requests(message: Message):
        if not await private_owner(message):
            return
        try:
            page = integer(arguments(message) or "1", "Page", 1, 100000)
        except ValueError as exc:
            return await error(message, exc)
        rows = (
            await store.db.requests.find({"status": "pending"})
            .sort("created_at", 1)
            .skip((page - 1) * 10)
            .limit(11)
            .to_list(11)
        )
        lines = [f"<b>🎬 Pending requests · page {page}</b>"]
        for row in rows[:10]:
            lines.append(
                f"<code>{escape(str(row['_id']))}</code> · {escape(str(row.get('query', ''))[:160])}"
            )
        if len(rows) > 10:
            lines.append(f"Next: /requests {page + 1}")
        await respond(message, "\n".join(lines))

    @router.message(Command("requestdone"))
    async def requestdone(message: Message):
        if not await private_owner(message):
            return
        request_id = arguments(message)
        row = await store.db.requests.find_one({"_id": request_id})
        if not row:
            return await respond(message, "Use /requestdone REQUEST_ID from /requests.")
        await store.enqueue(
            "notify",
            {
                "chat_id": row["user_id"],
                "text": f"🎬 Your request is available: {str(row.get('query', ''))[:200]}. Search the bot to find it.",
            },
            key=f"requestdone:{request_id}",
        )
        await store.db.requests.update_one(
            {"_id": request_id},
            {"$set": {"status": "fulfilled", "updated_at": utcnow()}},
        )
        await respond(message, "✅ Request fulfilled; notification queued.")

    @router.message(Command("backup", "export"))
    async def export_data(message: Message):
        if not await private_owner(message):
            return
        action = (message.text or "").split()[0].split("@")[0][1:]
        if action == "backup" and not settings.backup_key:
            return await respond(
                message,
                "Configure BACKUP_KEY before making an encrypted backup. See the recovery guide.",
            )
        job_id = await store.enqueue(
            action,
            {"actor_id": message.from_user.id, "chat_id": message.chat.id},
            key=f"{action}:{message.bot.id}:{message.chat.id}:{message.message_id}",
        )
        await respond(
            message,
            f"📤 {action.capitalize()} queued: <code>{escape(str(job_id))}</code>. Delivery is private.",
        )

    return router
