"""Bot-owned Telegram media handles and optional private archive posts."""

from struct import error as StructError

from pyrogram import enums
from pyrogram.errors import (
    ChannelInvalid,
    ChannelPrivate,
    ChannelPublicGroupNa,
    ChatAdminRequired,
    ChatForbidden,
    DocumentInvalid,
    FileIdInvalid,
    FileReferenceEmpty,
    FileReferenceExpired,
    FileReferenceInvalid,
    MediaEmpty,
    MediaInvalid,
    MessageIdInvalid,
    PeerIdInvalid,
    UserNotParticipant,
)
from pyrogram.file_id import FileId, FileType

from database.ia_filterdb import attachment

INVALID_HANDLE = (
    FileReferenceEmpty,
    FileReferenceExpired,
    FileReferenceInvalid,
    FileIdInvalid,
    DocumentInvalid,
    MediaEmpty,
    MediaInvalid,
)


class MissingPost(ValueError):
    pass


def cached_ids(d):
    """Try the saved ID first, then the same bot's document without its expiring reference."""
    saved = d.get("file")
    if not saved:
        return
    try:
        decoded = FileId.decode(saved)
    except (ValueError, StructError, IndexError, TypeError):
        return
    if (
        decoded.file_type
        not in {
            FileType.DOCUMENT,
            FileType.DOCUMENT_AS_FILE,
            FileType.VIDEO,
            FileType.AUDIO,
            FileType.ANIMATION,
        }
        or decoded.media_id is None
        or decoded.access_hash is None
    ):
        return
    yield saved
    if decoded.file_reference:
        decoded.file_reference = b""
        yield decoded.encode()


async def archive_channel(app, target, cfg):
    if target in cfg["sources"] or target == app.log_channel:
        raise ValueError(
            "Choose a dedicated private storage channel outside the sources and activity log."
        )
    chat = await app.get_chat(target)
    if (
        not chat
        or chat.type != enums.ChatType.CHANNEL
        or chat.username
        or any(u.active for u in (getattr(chat, "usernames", None) or []))
        or chat.has_protected_content
    ):
        raise ValueError("Use a private storage channel with content sharing enabled.")
    member = await app.get_chat_member(chat.id, app.me.id)
    if member.status != enums.ChatMemberStatus.OWNER and not (
        member.status == enums.ChatMemberStatus.ADMINISTRATOR
        and member.privileges
        and member.privileges.can_post_messages
    ):
        raise ValueError("Give this bot permission to post in the storage channel.")
    return chat


async def archive(app, docs, cfg, *, verify_existing=False):
    target = cfg.get("archive_chat")
    if not target or not docs:
        return
    async with app.db.archive_lock:
        await archive_channel(app, target, cfg)
        for ident, doc in {
            (d.get("unique") or "post:" + d["_id"]): d for d in docs
        }.items():
            coll = await app.db.collection_for(ident)
            if coll is None:
                continue
            row = await coll.find_one({"_id": ident}, {"archive": 1})
            if row is None:
                continue
            anchor = row.get("archive", {})
            if anchor.get("chat") == target:
                if not verify_existing:
                    continue
                try:
                    await read_post(app, target, anchor["mid"], doc.get("unique"))
                except (MissingPost, MessageIdInvalid):
                    pass
                else:
                    continue
            copy = await app.send_cached_media(
                target, doc["file"], disable_notification=True
            )
            media = attachment(copy)
            if not media or (
                doc.get("unique") and media.file_unique_id != doc["unique"]
            ):
                raise ValueError(
                    "Telegram did not confirm the storage copy. Resume the index to retry."
                )
            await app.db.remember_handle(
                ident,
                media.file_id,
                media.mime_type,
                archive={"chat": target, "mid": copy.id},
            )


async def read_post(app, source, mid, unique):
    chat = await app.get_chat(source)
    if not chat or chat.has_protected_content:
        raise MissingPost("This source protects its content.")
    msg = await app.get_messages(chat.id, mid, replies=0)
    media = attachment(msg)
    if (
        not media
        or msg.empty
        or not msg.chat
        or msg.chat.id != source
        or msg.id != mid
        or msg.has_protected_content
        or (unique and media.file_unique_id != unique)
    ):
        raise MissingPost("This post no longer contains the indexed file.")
    return media


async def handles(app, d, cfg):
    """Refresh on demand after the caller exhausts the stored handles."""
    if d["source"] not in cfg["sources"]:
        raise MissingPost("This file is outside the active catalog.")
    rejected = set()
    for handle in cached_ids(d):
        yield handle
        rejected.add(handle)
    absent = (
        MissingPost,
        MessageIdInvalid,
        ChannelPrivate,
        ChannelInvalid,
        ChannelPublicGroupNa,
        ChatAdminRequired,
        ChatForbidden,
        PeerIdInvalid,
        UserNotParticipant,
    )

    async def locations():
        if anchor := d.get("archive"):
            yield anchor["chat"], anchor["mid"]
        yield d["source"], d["mid"]
        async for row in app.db.alternatives(d, cfg["sources"]):
            yield row["source"], row["mid"]

    async for chat, mid in locations():
        try:
            media = await read_post(app, chat, mid, d.get("unique"))
        except absent:
            continue
        if media.file_id not in rejected:
            await app.db.remember_handle(d["media"], media.file_id, media.mime_type)
            yield media.file_id
            rejected.add(media.file_id)
    raise MissingPost(
        "No usable copy is available. A catalog admin can restore the entry with /repair and a matching file."
    )
