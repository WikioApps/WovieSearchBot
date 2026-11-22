"""Optional community tools with chat-local permissions and durable referral credit."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from html import escape
import logging
import re
import secrets
from urllib.parse import urlsplit

from aiogram import F, Router
from aiogram.enums import ChatMemberStatus, ChatType
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    ChatJoinRequest,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from pymongo.errors import DuplicateKeyError

from .billing import extend_once

log = logging.getLogger(__name__)
MENUS = {
    "en": (
        "🎬 Wovie · Your library",
        "Search titles",
        "Save a title",
        "View favorites",
        "Request a title",
        "Premium",
        "Invite friends",
    ),
    "hi": (
        "🎬 Wovie · आपकी लाइब्रेरी",
        "फ़िल्म खोजें",
        "शीर्षक सहेजें",
        "पसंदीदा देखें",
        "फ़िल्म का अनुरोध",
        "प्रीमियम",
        "दोस्तों को आमंत्रित करें",
    ),
    "ta": (
        "🎬 Wovie · உங்கள் நூலகம்",
        "திரைப்படம் தேட",
        "தலைப்பைச் சேமிக்க",
        "விருப்பங்களைப் பார்க்க",
        "திரைப்படம் கோர",
        "பிரீமியம்",
        "நண்பர்களை அழைக்க",
    ),
}


async def reconcile_referrals(store, limit: int = 20) -> int:
    """Replay pending grants after a crash; extend_once fences duplicate credits."""
    count = 0
    async for claim in store.db.referral_claims.find({"status": "pending"}).limit(
        limit
    ):
        inviter = await store.db.users.find_one({"_id": claim["referrer"]})
        newcomer = await store.db.users.find_one({"_id": claim["_id"]})
        if (
            not inviter
            or not newcomer
            or inviter.get("banned")
            or newcomer.get("banned")
        ):
            await store.db.referral_claims.update_one(
                {"_id": claim["_id"], "status": "pending"}, {"$set": {"status": "void"}}
            )
            continue
        if claim["days"]:
            await extend_once(
                store.db, claim["referrer"], f"referral:{claim['_id']}", claim["days"]
            )
        await store.db.referral_claims.update_one(
            {"_id": claim["_id"]},
            {"$set": {"status": "credited", "credited_at": datetime.now(UTC)}},
        )
        count += 1
    return count


def build_router(settings, store, delivery) -> Router:
    router = Router(name="community")

    async def owner(message: Message) -> bool:
        if (
            not message.from_user
            or message.from_user.id not in settings.admins
            or message.chat.type != ChatType.PRIVATE
        ):
            await message.answer(
                "This operation is available to bot owners in private chat."
            )
            return False
        return True

    async def available(message: Message) -> bool:
        if not message.from_user or message.from_user.is_bot:
            return False
        permitted, reason = await delivery.allowed(message.from_user.id)
        if not permitted:
            await message.answer(escape(reason))
            return False
        return True

    async def render_menu(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
        user = await store.db.users.find_one({"_id": user_id}) or {}
        language = user.get("ui_language", "en")
        labels = MENUS.get(language, MENUS["en"])
        commands = (
            "/search title",
            "/watch title",
            "/favorites",
            "/request title",
            "/premium",
            "/referral",
        )
        text = f"<b>{labels[0]}</b>\n\n" + "\n".join(
            f"{label} · <code>{command}</code>"
            for label, command in zip(labels[1:], commands)
        )
        config = await store.setting(0)
        sponsor = config.get("sponsor")
        now = datetime.now(UTC)
        if (
            sponsor
            and sponsor.get("enabled")
            and (not sponsor.get("starts_at") or sponsor["starts_at"] <= now)
            and (not sponsor.get("ends_at") or sponsor["ends_at"] > now)
        ):
            text += f'\n\n📣 <b>Sponsor:</b> <a href="{escape(sponsor["url"], quote=True)}">{escape(sponsor["label"])}</a>'
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=label, callback_data=f"community:language:{code}"
                    )
                    for code, label in (
                        ("en", "English"),
                        ("hi", "हिन्दी"),
                        ("ta", "தமிழ்"),
                    )
                ]
            ]
        )
        return text, keyboard

    @router.message(Command("menu", "language"))
    async def menu(message: Message):
        if not await available(message):
            return
        await store.touch_user(message.from_user)
        words = (message.text or "").split()
        if words[0].split("@")[0] == "/language" and len(words) > 1:
            if words[1] not in MENUS or len(words) > 2:
                return await message.answer("Usage: /language en | hi | ta")
            await store.db.users.update_one(
                {"_id": message.from_user.id}, {"$set": {"ui_language": words[1]}}
            )
        text, keyboard = await render_menu(message.from_user.id)
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")

    @router.callback_query(F.data.startswith("community:language:"))
    async def language_callback(query: CallbackQuery):
        code = (query.data or "").rsplit(":", 1)[-1]
        allowed, _ = await delivery.allowed(query.from_user.id)
        if code not in MENUS or not allowed:
            return await query.answer(
                "Language selection unavailable.", show_alert=True
            )
        await store.touch_user(query.from_user)
        await store.db.users.update_one(
            {"_id": query.from_user.id}, {"$set": {"ui_language": code}}
        )
        await query.answer("✓")
        text, keyboard = await render_menu(query.from_user.id)
        # A group message is shared; reply privately rather than rewrite another person's menu.
        if query.message and query.message.chat.type == ChatType.PRIVATE:
            try:
                await query.message.edit_text(
                    text, reply_markup=keyboard, parse_mode="HTML"
                )
            except TelegramAPIError:
                pass

    @router.message(Command("sponsor"))
    async def sponsor(message: Message):
        if not await owner(message):
            return
        raw = (message.text or "").partition(" ")[2].strip()
        if raw == "off":
            await store.set_setting(0, "sponsor", {"enabled": False})
            return await message.answer("Sponsor placement disabled.")
        if raw.startswith("schedule "):
            config = await store.setting(0)
            placement = config.get("sponsor") or {}
            if not placement.get("url"):
                return await message.answer(
                    "Set a sponsor placement before scheduling it."
                )
            words = raw.split()
            if words == ["schedule", "clear"]:
                placement.pop("starts_at", None)
                placement.pop("ends_at", None)
            else:
                try:
                    if len(words) != 3:
                        raise ValueError
                    start, end = [
                        datetime.fromisoformat(x.replace("Z", "+00:00"))
                        for x in words[1:]
                    ]
                    if not start.tzinfo or not end.tzinfo or end <= start:
                        raise ValueError
                    placement.update(
                        starts_at=start.astimezone(UTC), ends_at=end.astimezone(UTC)
                    )
                except ValueError:
                    return await message.answer(
                        "Use /sponsor schedule START_ISO END_ISO with timezone offsets, e.g. 2026-12-01T00:00:00Z 2026-12-08T00:00:00Z. Or /sponsor schedule clear."
                    )
            await store.set_setting(0, "sponsor", placement)
            return await message.answer(
                "Sponsor display schedule saved. It controls visibility when /menu is opened."
            )
        if not raw.startswith("set ") or "|" not in raw:
            return await message.answer(
                "Usage: /sponsor set LABEL | HTTPS_URL\n/sponsor schedule START_ISO END_ISO\n/sponsor schedule clear\n/sponsor off\nPlacement appears in /menu."
            )
        label, url = [part.strip() for part in raw[4:].split("|", 1)]
        try:
            parsed = urlsplit(url)
        except ValueError:
            return await message.answer("Use a valid HTTPS URL.")
        if (
            not label
            or len(label) > 80
            or len(url) > 500
            or parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
        ):
            return await message.answer(
                "Use a label up to 80 characters and a valid HTTPS URL without embedded credentials."
            )
        await store.set_setting(
            0, "sponsor", {"enabled": True, "label": label, "url": url}
        )
        await message.answer("Sponsor placement saved. Preview with /menu.")

    @router.message(Command("referralconfig"))
    async def referralconfig(message: Message):
        if not await owner(message):
            return
        raw = (message.text or "").partition(" ")[2].strip()
        if not raw.isdecimal() or not 0 <= int(raw) <= 30:
            return await message.answer(
                "Usage: /referralconfig DAYS (0–30). Zero disables premium rewards. Existing claims retain their original reward."
            )
        await store.set_setting(0, "referral_days", int(raw))
        await message.answer(
            f"Referral reward: {raw} premium days per eligible newcomer, credited to the inviter."
        )

    @router.message(Command("referral"))
    async def referral(message: Message):
        if message.chat.type != ChatType.PRIVATE or not await available(message):
            return
        await store.touch_user(message.from_user)
        uid = message.from_user.id
        await store.db.referral_codes.update_one(
            {"_id": uid},
            {
                "$setOnInsert": {
                    "token": secrets.token_hex(12),
                    "created_at": datetime.now(UTC),
                }
            },
            upsert=True,
        )
        code = await store.db.referral_codes.find_one({"_id": uid})
        count = await store.db.referral_claims.count_documents(
            {"referrer": uid, "status": "credited"}
        )
        me = await message.bot.get_me()
        config = await store.setting(0)
        await message.answer(
            f"<b>🎁 Invite friends</b>\nhttps://t.me/{me.username}?start=ref_{code['token']}\n\n"
            f"Confirmed referrals: {count}\nReward: {int(config.get('referral_days', 0))} premium days. "
            "New users can choose one inviter during their first five minutes. Self-referrals are rejected.",
            parse_mode="HTML",
        )

    @router.message(
        CommandStart(deep_link=True),
        F.text.regexp(r"^/start(?:@\w+)?\s+ref_[a-f0-9]{24}$"),
    )
    async def referral_start(message: Message):
        if message.chat.type != ChatType.PRIVATE or not await available(message):
            return
        uid = message.from_user.id
        token = (message.text or "").split()[-1][4:]
        code = await store.db.referral_codes.find_one({"token": token})
        if not code or code["_id"] == uid:
            return await message.answer(
                "This referral is unavailable. Open /menu to continue."
            )
        await store.touch_user(message.from_user)
        user = await store.db.users.find_one({"_id": uid})
        existing = await store.db.referral_claims.find_one({"_id": uid})
        if existing and existing["referrer"] != code["_id"]:
            return await message.answer(
                "Your inviter is already recorded. Open /menu to continue."
            )
        if not existing:
            first_seen = user.get("created_at")
            if not isinstance(first_seen, datetime) or first_seen < datetime.now(
                UTC
            ) - timedelta(minutes=5):
                return await message.answer(
                    "Referral rewards are for new accounts in this bot. Open /menu to continue."
                )
            config = await store.setting(0)
            try:
                await store.db.referral_claims.insert_one(
                    {
                        "_id": uid,
                        "referrer": code["_id"],
                        "days": min(30, max(0, int(config.get("referral_days", 0)))),
                        "status": "pending",
                        "created_at": datetime.now(UTC),
                    }
                )
            except DuplicateKeyError:
                pass
        await reconcile_referrals(store)
        text, keyboard = await render_menu(uid)
        await message.answer(text, reply_markup=keyboard, parse_mode="HTML")

    async def moderator(message: Message, permission: str) -> bool:
        if (
            not message.from_user
            or message.sender_chat
            or message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP)
        ):
            await message.answer(
                "Use this command from your own administrator account inside the target group."
            )
            return False
        try:
            caller = await message.bot.get_chat_member(
                message.chat.id, message.from_user.id
            )
            bot = await message.bot.get_chat_member(message.chat.id, message.bot.id)
            caller_ok = caller.status == ChatMemberStatus.CREATOR or (
                caller.status == ChatMemberStatus.ADMINISTRATOR
                and getattr(caller, permission, False)
            )
            bot_ok = bot.status == ChatMemberStatus.CREATOR or (
                bot.status == ChatMemberStatus.ADMINISTRATOR
                and getattr(bot, permission, False)
            )
            if caller_ok and bot_ok:
                return True
        except TelegramAPIError:
            pass
        await message.answer(
            "Both you and the bot need the relevant Telegram administrator permission."
        )
        return False

    @router.message(Command("modban", "modunban", "mute", "unmute", "pin", "unpin"))
    async def moderate(message: Message):
        command = (message.text or "").split()[0].split("@")[0][1:]
        if not await moderator(
            message,
            "can_pin_messages"
            if command in ("pin", "unpin")
            else "can_restrict_members",
        ):
            return
        reply = message.reply_to_message
        try:
            if command in ("pin", "unpin"):
                if not reply:
                    return await message.answer(
                        "Reply to the target message with /pin or /unpin."
                    )
                if command == "pin":
                    await message.bot.pin_chat_message(
                        message.chat.id, reply.message_id, disable_notification=True
                    )
                else:
                    await message.bot.unpin_chat_message(
                        message.chat.id, reply.message_id
                    )
            else:
                raw = (message.text or "").partition(" ")[2].strip()
                target = (
                    reply.from_user.id
                    if reply and reply.from_user and not reply.sender_chat
                    else None
                )
                if command == "modunban" and raw.isdecimal():
                    target = int(raw)
                if not target:
                    return await message.answer(
                        "Reply to the user's message. /modunban also accepts USER_ID."
                    )
                member = await message.bot.get_chat_member(message.chat.id, target)
                if target in (
                    message.from_user.id,
                    message.bot.id,
                ) or member.status in (
                    ChatMemberStatus.CREATOR,
                    ChatMemberStatus.ADMINISTRATOR,
                ):
                    return await message.answer(
                        "This command cannot target yourself, the bot or group administrators."
                    )
                if command == "modban":
                    await message.bot.ban_chat_member(
                        message.chat.id, target, revoke_messages=False
                    )
                elif command == "modunban":
                    await message.bot.unban_chat_member(
                        message.chat.id, target, only_if_banned=True
                    )
                elif command == "mute":
                    if not raw.isdecimal() or not 1 <= int(raw) <= 10080:
                        return await message.answer(
                            "Reply with /mute MINUTES (1–10080)."
                        )
                    await message.bot.restrict_chat_member(
                        message.chat.id,
                        target,
                        permissions=ChatPermissions(can_send_messages=False),
                        until_date=datetime.now(UTC) + timedelta(minutes=int(raw)),
                        use_independent_chat_permissions=True,
                    )
                else:
                    chat = await message.bot.get_chat(message.chat.id)
                    if not chat.permissions:
                        return await message.answer(
                            "Telegram did not provide this group's default permissions."
                        )
                    await message.bot.restrict_chat_member(
                        message.chat.id,
                        target,
                        permissions=chat.permissions,
                        use_independent_chat_permissions=True,
                    )
            await store.db.audit.insert_one(
                {
                    "action": command,
                    "chat_id": message.chat.id,
                    "actor_id": message.from_user.id,
                    "target_id": reply.message_id
                    if command in ("pin", "unpin")
                    else target,
                    "created_at": datetime.now(UTC),
                }
            )
            await message.answer("✅ Group action completed.")
        except TelegramAPIError:
            await message.answer(
                "Telegram could not complete that action. Check current permissions and whether the target is still available."
            )

    @router.message(Command("joinmode"))
    async def joinmode(message: Message):
        if not await owner(message):
            return
        parts = (message.text or "").split()
        if (
            len(parts) != 3
            or not re.fullmatch(r"-\d+", parts[1])
            or parts[2] not in ("on", "off")
        ):
            return await message.answer(
                "Usage: /joinmode CHAT_ID on|off. Autoapproval is disabled by default."
            )
        chat_id = int(parts[1])
        try:
            member = await message.bot.get_chat_member(chat_id, message.bot.id)
            if parts[2] == "on" and not (
                member.status == ChatMemberStatus.CREATOR
                or getattr(member, "can_invite_users", False)
            ):
                return await message.answer(
                    "The bot needs invite-user administrator permission in that chat."
                )
        except TelegramAPIError:
            return await message.answer("The bot cannot verify access to that chat.")
        await store.set_setting(chat_id, "autoapprove_join_requests", parts[2] == "on")
        await message.answer(
            "Join-request policy saved. Source membership checks remain active for private media."
        )

    @router.message(F.new_chat_members)
    async def welcome_members(message: Message):
        config = await store.setting(message.chat.id)
        if not config.get("enabled") or not config.get("welcome_text"):
            return
        names = [
            escape(person.first_name[:80])
            for person in (message.new_chat_members or [])[:5]
            if not person.is_bot
        ]
        if names:
            await message.answer(
                "👋 "
                + ", ".join(names)
                + "\n\n"
                + escape(str(config["welcome_text"])[:800]),
                parse_mode="HTML",
            )

    @router.chat_join_request()
    async def join_request(request: ChatJoinRequest):
        config = await store.setting(request.chat.id)
        if not config.get("autoapprove_join_requests"):
            return
        uid = request.from_user.id
        if await store.db.users.find_one({"_id": uid, "banned": True}, {"_id": 1}):
            return
        global_config = await store.setting(0)
        required = list(settings.required_channels) + list(
            global_config.get("required_channels") or []
        )
        for channel in required:
            if str(channel) != str(request.chat.id) and not await delivery._member(
                uid, channel, refresh=True
            ):
                return
        key = f"{request.chat.id}:{uid}:{int(request.date.timestamp())}"
        try:
            await request.bot.approve_chat_join_request(request.chat.id, uid)
            await store.db.membership_cache.delete_one(
                {"_id": f"{request.chat.id}:{uid}"}
            )
            await store.db.join_events.update_one(
                {"_id": key},
                {
                    "$set": {
                        "chat_id": request.chat.id,
                        "user_id": uid,
                        "status": "approved",
                        "expires_at": datetime.now(UTC) + timedelta(days=30),
                    }
                },
                upsert=True,
            )
        except TelegramAPIError as exc:
            log.info("Join approval unavailable (%s)", type(exc).__name__)

    return router
