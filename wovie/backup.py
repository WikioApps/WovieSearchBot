"""Authenticated, streaming BSON archives for dedicated Wovie MongoDB databases."""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
from collections.abc import Iterator, Sequence
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import struct
import sys
import tempfile
from typing import BinaryIO

from bson import BSON
from bson.codec_options import CodecOptions, DatetimeConversion
from bson.raw_bson import RawBSONDocument
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from dotenv import load_dotenv
from pymongo import AsyncMongoClient
from pymongo.errors import PyMongoError

from .config import values

MAGIC = b"WOVIE-BSON\x01"
LOCK = "_wovie_restore_lock"
DRILL_MARKER = "_wovie_drill_owner"
MAX_FRAME = 16 * 1024 * 1024 + 17
BATCH_BYTES = 2 * 1024 * 1024
NAMESPACE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,119}\Z")
CODEC = CodecOptions(
    tz_aware=True, datetime_conversion=DatetimeConversion.DATETIME_AUTO
)
RAW_CODEC = CODEC.with_options(document_class=RawBSONDocument)


class ArchiveError(ValueError):
    """An error safe to display to the operator."""


def _cipher(key: str, salt: bytes) -> AESGCM:
    try:
        secret = base64.b64decode(
            key + "=" * (-len(key) % 4), altchars=b"-_", validate=True
        )
    except (ValueError, binascii.Error) as exc:
        raise ArchiveError("Invalid BACKUP_KEY encoding.") from exc
    if len(secret) != 32:
        raise ArchiveError("BACKUP_KEY must encode 32 random bytes.")
    derived = hmac.digest(secret, MAGIC + salt, hashlib.sha256)
    return AESGCM(derived)


def _namespace(name: str) -> None:
    if (
        not isinstance(name, str)
        or not NAMESPACE.fullmatch(name)
        or name in {LOCK, DRILL_MARKER}
    ):
        raise ArchiveError("Unexpected collection name. Use dedicated Wovie databases.")


async def export_archive(
    store, path: Path, key: str, *, max_bytes: int | None = None
) -> dict:
    """Export to a new file. Stop writers first for a consistent multi-database copy."""
    return await _export(store.catalogs, Path(path), key, max_bytes)


async def _export(dbs: Sequence, path: Path, key: str, max_bytes: int | None) -> dict:
    if path.exists() or path.is_symlink():
        raise ArchiveError("Export destination exists. Choose a new archive path.")
    salt = os.urandom(32)
    cipher = _cipher(key, salt)
    header = MAGIC + salt
    identities = []
    for db in dbs:
        identity = await db["_wovie_meta"].find_one({"_id": "catalog_identity"})
        if not identity or not isinstance(identity.get("uuid"), str):
            raise ArchiveError("A configured source lacks its Wovie catalog identity.")
        identities.append(identity["uuid"])
    registry = await dbs[0]["_wovie_meta"].find_one({"_id": "catalog_registry"})
    schema = await dbs[0]["_wovie_meta"].find_one({"_id": "schema"})
    if (
        len(set(identities)) != len(dbs)
        or not registry
        or set(registry["catalog_ids"]) != set(identities)
        or not schema
        or schema.get("version") != 1
    ):
        raise ArchiveError(
            "Incomplete catalog topology or incompatible application schema."
        )
    counts = {}
    documents = 0
    frame_no = 0
    temporary = tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=".wovie-", delete=False
    )
    try:
        with temporary as stream:
            stream.write(header)

            def write(kind: bytes, data: bytes) -> None:
                nonlocal frame_no
                nonce = frame_no.to_bytes(12, "big")
                encrypted = cipher.encrypt(nonce, kind + data, header)
                if len(encrypted) > MAX_FRAME:
                    raise ArchiveError("Record exceeds MongoDB's document size limit.")
                if (
                    max_bytes is not None
                    and stream.tell() + 4 + len(encrypted) > max_bytes
                ):
                    raise ArchiveError(
                        "Archive exceeds the configured transfer size limit. Use offline CLI export."
                    )
                stream.write(struct.pack(">I", len(encrypted)))
                stream.write(encrypted)
                frame_no += 1

            write(
                b"H",
                BSON.encode(
                    {"slots": len(dbs), "created_at": datetime.now(timezone.utc)}
                ),
            )
            for slot, db in enumerate(dbs):
                names = await db.list_collection_names()
                if LOCK in names or not names:
                    raise ArchiveError(
                        "Source database is empty or contains an incomplete restore."
                    )
                for name in sorted(n for n in names if not n.startswith("system.")):
                    _namespace(name)
                    write(b"C", BSON.encode({"slot": slot, "name": name}))
                    count = 0
                    cursor = (
                        db.get_collection(name, codec_options=RAW_CODEC)
                        .find({})
                        .batch_size(64)
                    )
                    async with cursor:
                        async for row in cursor:
                            write(b"D", row.raw)
                            count += 1
                    counts[f"{slot}/{name}"] = count
                    documents += count
            write(
                b"F", BSON.encode({"collections": len(counts), "documents": documents})
            )
            stream.flush()
            os.fsync(stream.fileno())
            size = stream.tell()
        # Atomic publication without replacing any file another process created.
        os.link(temporary.name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        os.unlink(temporary.name)
    return dict(databases=len(dbs), documents=documents, bytes=size, counts=counts)


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    data = stream.read(size)
    if len(data) != size:
        raise ArchiveError("Archive is truncated.")
    return data


def _records(stream: BinaryIO, key: str) -> Iterator[tuple[bytes, object]]:
    stream.seek(0)
    header = _read_exact(stream, len(MAGIC) + 32)
    if not header.startswith(MAGIC):
        raise ArchiveError("Unrecognized archive format or version.")
    cipher = _cipher(key, header[len(MAGIC) :])
    frame_no = 0
    slots = 0
    namespaces = set()
    documents = 0
    while True:
        length = struct.unpack(">I", _read_exact(stream, 4))[0]
        if not 17 <= length <= MAX_FRAME:
            raise ArchiveError("Invalid archive frame size.")
        try:
            frame = cipher.decrypt(
                frame_no.to_bytes(12, "big"), _read_exact(stream, length), header
            )
        except InvalidTag as exc:
            raise ArchiveError(
                "Archive authentication failed. Check the key and archive."
            ) from exc
        kind, data = frame[:1], frame[1:]
        record = BSON(data).decode(codec_options=CODEC)
        if frame_no == 0 and kind != b"H":
            raise ArchiveError("Missing archive manifest.")
        if kind == b"H":
            slots = record.get("slots")
            if frame_no or type(slots) is not int or not 1 <= slots <= 256:
                raise ArchiveError("Invalid archive database manifest.")
        elif kind == b"C":
            slot, name = record.get("slot"), record.get("name")
            _namespace(name)
            if (
                type(slot) is not int
                or not 0 <= slot < slots
                or (slot, name) in namespaces
            ):
                raise ArchiveError("Duplicate or invalid collection in archive.")
            namespaces.add((slot, name))
        elif kind == b"D":
            if not namespaces or "_id" not in record:
                raise ArchiveError("Invalid MongoDB document in archive.")
            record = data
            documents += 1
        elif kind == b"F":
            if (
                record.get("documents") != documents
                or record.get("collections") != len(namespaces)
                or len({slot for slot, _ in namespaces}) != slots
                or stream.read(1)
            ):
                raise ArchiveError(
                    "Archive final manifest or file length does not match its contents."
                )
            yield kind, record
            return
        else:
            raise ArchiveError("Unknown record type in archive.")
        yield kind, record
        frame_no += 1


def _verify(stream: BinaryIO, key: str, *, count_namespaces=False) -> dict:
    stats = {}
    counts = {}
    namespace = None
    meta_slot = None
    identities = {}
    registry = set()
    version = None
    for kind, data in _records(stream, key):
        if count_namespaces:
            if kind == b"C":
                namespace = (data["slot"], data["name"])
                counts[namespace] = 0
            elif kind == b"D":
                counts[namespace] += 1
        if kind == b"H":
            stats["databases"] = data["slots"]
        elif kind == b"C":
            meta_slot = data["slot"] if data["name"] == "_wovie_meta" else None
        elif kind == b"D" and meta_slot is not None:
            record = BSON(data).decode(codec_options=CODEC)
            if record["_id"] == "catalog_identity":
                identities[meta_slot] = record.get("uuid")
            if meta_slot == 0 and record["_id"] == "schema":
                version = record.get("version")
            if meta_slot == 0 and record["_id"] == "catalog_registry":
                registry = set(record.get("catalog_ids", []))
        if kind == b"F":
            if (
                version != 1
                or len(identities) != stats["databases"]
                or len(registry) != len(identities)
                or registry != set(identities.values())
            ):
                raise ArchiveError(
                    "Archive schema or registered catalog identities are inconsistent."
                )
            stats.update(data)
    if count_namespaces:
        stats["counts"] = counts
    return stats


def _restored_record(name: str, raw: bytes):
    if name == "inbox":
        update = BSON(raw).decode(codec_options=CODEC)
        if update.get("status") == "pending":
            update.update(
                status="paused",
                error="Review update outcome before manually retrying after restore.",
            )
            return update
    if name == "jobs":
        job = BSON(raw).decode(codec_options=CODEC)
        if job.get("status") in {"queued", "running"}:
            job.update(
                status="paused",
                error="Paused during restore. Review cursor and prior external sends before resuming.",
            )
            job.pop("worker_id", None)
            job.pop("lease_until", None)
            return job
    return RawBSONDocument(raw, RAW_CODEC)


async def _restore(dbs: Sequence, archive: Path, key: str, *, mark_drill=None) -> dict:
    with archive.open("rb") as stream:
        stats = await asyncio.to_thread(
            _verify, stream, key, count_namespaces=mark_drill is not None
        )
        if len(dbs) != stats["databases"]:
            raise ArchiveError(
                "Configured database count must equal archive database count."
            )
        for db in dbs:
            if await db.list_collection_names():
                raise ArchiveError(
                    "Every restore destination must be an empty dedicated database."
                )
        # Refuse overlapping/concurrent destinations and leave incomplete restores visibly locked.
        for db in dbs:
            await db.create_collection(LOCK)
            if mark_drill is not None:
                await mark_drill(db)
        current = None
        name = ""
        batch = []
        size = 0

        async def flush() -> None:
            nonlocal size
            if batch:
                await current.insert_many(batch, ordered=True)
                batch.clear()
                size = 0

        for kind, row in _records(stream, key):
            if kind == b"C":
                await flush()
                name = row["name"]
                current = await dbs[row["slot"]].create_collection(name)
            elif kind == b"D":
                batch.append(_restored_record(name, row))
                size += len(row)
                if size >= BATCH_BYTES or len(batch) >= 128:
                    await flush()
        await flush()
        for db in reversed(dbs):
            await db.drop_collection(LOCK)
        return stats


async def _cli(args) -> dict:
    load_dotenv(dotenv_path=Path(".env"))
    key = os.environ.get("BACKUP_KEY", "")
    if not key:
        raise ArchiveError("Set BACKUP_KEY in the environment or .env.")
    if args.action == "verify":
        with args.path.open("rb") as stream:
            return _verify(stream, key)
    if args.action == "restore" and not args.confirm_restore:
        raise ArchiveError(
            "Pass --confirm-restore to write verified data into empty databases."
        )
    primary = os.environ.get("MONGO_URI", "")
    if not primary:
        raise ArchiveError("Set MONGO_URI in the environment or .env.")
    additional = values("CATALOG_URIS")
    clients = []
    try:
        dbs = []
        for slot, uri in enumerate((primary, *additional)):
            client = AsyncMongoClient(
                uri,
                maxPoolSize=3,
                tz_aware=True,
                serverSelectionTimeoutMS=10000,
                w="majority",
            )
            clients.append(client)
            dbname = os.environ.get("MONGO_DB") or "wovie"
            db = client.get_default_database(dbname) if slot else client[dbname]
            if db.name in {"admin", "local", "config"}:
                raise ArchiveError("Select a dedicated application database.")
            dbs.append(db)
        if args.action == "export":
            return await _export(dbs, args.path, key, None)
        return await _restore(dbs, args.path, key)
    finally:
        for client in clients:
            await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("export", "verify", "restore"):
        command = commands.add_parser(action)
        command.add_argument("path", type=Path)
        if action == "restore":
            command.add_argument(
                "--confirm-restore",
                action="store_true",
                help="write to empty destinations",
            )
    try:
        print(json.dumps(asyncio.run(_cli(parser.parse_args())), sort_keys=True))
    except ArchiveError as exc:
        sys.exit(str(exc))
    except (ValueError, OSError, PyMongoError) as exc:
        # Driver exceptions can contain connection details.
        sys.exit(
            f"Backup operation failed ({type(exc).__name__}). Check paths, MongoDB access and archive format."
        )


if __name__ == "__main__":
    main()
