"""Archive authorized Telegram media to an independently mounted POSIX volume."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys

from pymongo import AsyncMongoClient
from telethon import TelegramClient, errors
from telethon.sessions import StringSession

from .config import Settings
from .indexer import resolve_peer

BLOCK = 256 * 1024
CHECKPOINT = 8 * 1024 * 1024


class ArchiveFailure(Exception):
    pass


@contextmanager
def manifest(root: Path, *, create=False):
    if create:
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
    elif not (root / "manifest.sqlite").is_file():
        raise ArchiveFailure("Archive manifest missing.")
    with (root / ".lock").open("ab") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ArchiveFailure("Another process is using this archive.") from None
        db = sqlite3.connect(root / "manifest.sqlite")
        try:
            if create:
                db.executescript("""
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS media (
                  chat INTEGER, msg INTEGER, doc INTEGER, name TEXT, bytes INTEGER,
                  sha256 TEXT, saved_at TEXT, PRIMARY KEY(chat,msg,doc));
                CREATE TABLE IF NOT EXISTS progress (
                  chat INTEGER PRIMARY KEY, msg INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS partial (
                  key TEXT PRIMARY KEY, bytes INTEGER NOT NULL, sha256 TEXT NOT NULL);
                """)
            yield db
        finally:
            db.close()


def hash_file(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256")


def object_path(root, sha):
    if not re.fullmatch(r"[a-f0-9]{64}", sha):
        raise ArchiveFailure("Invalid digest in manifest.")
    return root / "objects" / sha[:2] / sha


def sync_dir(directory):
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def verify(root):
    with manifest(root) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ArchiveFailure("Damaged SQLite manifest.")
        count = 0
        for sha, size in db.execute("SELECT DISTINCT sha256,bytes FROM media"):
            path = object_path(root, sha)
            if (
                not path.is_file()
                or path.stat().st_size != size
                or hash_file(path).hexdigest() != sha
            ):
                raise ArchiveFailure(f"Damaged or missing archive object {sha}")
            count += 1
        return {"verified_objects": count}


async def copy_file(root, db, client, chat, msg, reserve):
    document = msg.document
    expected = int(document.size)
    identity = (chat, msg.id, document.id)
    old = db.execute(
        "SELECT sha256 FROM media WHERE chat=? AND msg=? AND doc=?", identity
    ).fetchone()
    if (
        old
        and object_path(root, old[0]).is_file()
        and object_path(root, old[0]).stat().st_size == expected
    ):
        return False
    key = "-".join(map(str, identity))
    path = root / "parts" / key
    progress = db.execute(
        "SELECT bytes,sha256 FROM partial WHERE key=?", (key,)
    ).fetchone()
    offset = progress[0] if progress and path.is_file() else 0
    path.touch(mode=0o600, exist_ok=True)
    with path.open("r+b") as stream:
        if offset > expected or offset > path.stat().st_size or offset % BLOCK:
            offset = 0
        stream.truncate(offset)
        digest = await asyncio.to_thread(hash_file, path)
        if offset and digest.hexdigest() != progress[1]:
            offset = 0
            stream.truncate(0)
            digest = hashlib.sha256()
        if shutil.disk_usage(root).free < expected - offset + reserve:
            raise ArchiveFailure(
                "Archive volume lacks space for the next file and free-space reserve."
            )
        stream.seek(offset)
        last = offset
        if offset < expected:
            chunks = client.iter_download(msg, offset=offset, request_size=BLOCK)
            try:
                async for chunk in chunks:
                    if stream.tell() + len(chunk) > expected:
                        raise ArchiveFailure(
                            "Telegram returned more bytes than expected."
                        )
                    stream.write(chunk)
                    digest.update(chunk)
                    if (
                        stream.tell() - last >= CHECKPOINT
                        and stream.tell() % BLOCK == 0
                    ):
                        stream.flush()
                        os.fsync(stream.fileno())
                        last = stream.tell()
                        with db:
                            db.execute(
                                "INSERT OR REPLACE INTO partial VALUES(?,?,?)",
                                (key, last, digest.hexdigest()),
                            )
            finally:
                await chunks.close()
        if stream.tell() != expected:
            raise ArchiveFailure("Media transfer ended before the expected length.")
        stream.flush()
        os.fsync(stream.fileno())
    sha = digest.hexdigest()
    final = object_path(root, sha)
    final.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if final.exists():
        if (await asyncio.to_thread(hash_file, final)).hexdigest() != sha:
            raise ArchiveFailure(f"Damaged archive object {sha}")
        path.unlink()
    else:
        os.replace(path, final)
        sync_dir(final.parent)
        sync_dir(final.parent.parent)
    now = datetime.now(UTC).isoformat()
    name = (msg.file.name or f"media-{msg.id}")[:512]
    with db:
        db.execute(
            "INSERT OR REPLACE INTO media VALUES(?,?,?,?,?,?,?)",
            (*identity, name, expected, sha, now),
        )
        db.execute("DELETE FROM partial WHERE key=?", (key,))
    return True


async def sync(args):
    cfg = Settings.from_env(require_bot=False)
    session = os.getenv("ARCHIVE_SESSION_STRING", "").strip()
    if (
        not session
        or session in {cfg.session_string, os.getenv("WEB_SESSION_STRING")}
        or not cfg.api_id
        or not cfg.api_hash
    ):
        raise ArchiveFailure(
            "Set API_ID, API_HASH and an independent ARCHIVE_SESSION_STRING."
        )
    client = TelegramClient(
        StringSession(session),
        cfg.api_id,
        cfg.api_hash,
        receive_updates=False,
        flood_sleep_threshold=0,
    )
    mongo = AsyncMongoClient(
        cfg.mongo_uri, maxPoolSize=2, serverSelectionTimeoutMS=10000
    )
    root = args.root.resolve()
    count = 0
    try:
        await client.connect()
        user = await client.get_me()
        if not user or user.bot:
            raise ArchiveFailure("Archive session must be an authorized user.")
        with manifest(root, create=True) as db:
            (root / "parts").mkdir(mode=0o700, exist_ok=True)
            for chat in dict.fromkeys(args.source):
                sources = mongo[cfg.mongo_db].sources
                allowed = {
                    "_id": chat,
                    "active": True,
                    "protected": {"$ne": True},
                    "has_protected_content": {"$ne": True},
                }
                if chat >= 0 or not await sources.find_one(allowed):
                    raise ArchiveFailure(
                        "Select a registered active, unprotected group or channel."
                    )
                peer = await resolve_peer(client, chat)
                if getattr(await client.get_entity(peer), "noforwards", False):
                    raise ArchiveFailure("This source protects its content.")
                row = db.execute(
                    "SELECT msg FROM progress WHERE chat=?", (chat,)
                ).fetchone()
                cursor = 0 if args.rescan or not row else row[0]
                latest = await client.get_messages(peer, limit=1)
                if not latest or latest[0].id <= cursor:
                    continue
                upper = latest[0].id
                async for msg in client.iter_messages(
                    peer, min_id=cursor, max_id=upper + 1, reverse=True
                ):
                    if msg.document and not msg.noforwards:
                        if not await sources.find_one(allowed) or getattr(
                            await client.get_entity(peer), "noforwards", False
                        ):
                            raise ArchiveFailure(
                                "Source access changed during archival."
                            )
                        count += await copy_file(
                            root, db, client, chat, msg, args.reserve_mib * 1024 * 1024
                        )
                    with db:
                        db.execute(
                            """INSERT INTO progress VALUES(?,?) ON CONFLICT(chat)
                            DO UPDATE SET msg=max(msg,excluded.msg)""",
                            (chat, msg.id),
                        )
        return {"archived_documents": count}
    finally:
        await client.disconnect()
        await mongo.close()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    add = commands.add_parser("sync")
    add.add_argument("root", type=Path)
    add.add_argument("--source", type=int, action="append", required=True)
    add.add_argument(
        "--rescan",
        action="store_true",
        help="revisit old posts and preserve new document versions",
    )
    add.add_argument("--reserve-mib", type=int, default=128)
    check = commands.add_parser("verify")
    check.add_argument("root", type=Path)
    args = parser.parse_args()
    if getattr(args, "reserve_mib", 0) < 0:
        parser.error("--reserve-mib must be nonnegative")
    try:
        result = (
            verify(args.root) if args.action == "verify" else asyncio.run(sync(args))
        )
        print(json.dumps(result))
    except errors.FloodWaitError as exc:
        sys.exit(
            f"Telegram requested a wait of {exc.seconds}s. Resume after this interval."
        )
    except ArchiveFailure as exc:
        sys.exit(str(exc))
    except Exception as exc:
        sys.exit(
            f"Archive operation failed ({type(exc).__name__}). Check permissions and free space before resuming."
        )


if __name__ == "__main__":
    main()
