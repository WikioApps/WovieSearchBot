"""Upload and verify encrypted offsite snapshots using a configured rclone remote."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile

from types import SimpleNamespace
from urllib.parse import urlsplit

from .backup import ArchiveError
from dotenv import load_dotenv
from pymongo import AsyncMongoClient
from pymongo.errors import PyMongoError

NAME = re.compile(r"wovie-(\d{8}T\d{6}Z)-[0-9a-f]{8}\.wbak\Z")


def rclone(*args, **kwargs):
    return subprocess.run(["rclone", *args], check=True, **kwargs)


def upload(remote):
    name = (
        "wovie-"
        + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + secrets.token_hex(4)
        + ".wbak"
    )
    with tempfile.TemporaryDirectory(prefix="wovie-snapshot-") as tmp:
        archive = Path(tmp) / name
        for command in ("export", "verify"):
            subprocess.run(
                [sys.executable, "-m", "wovie.backup", command, str(archive)],
                check=True,
                stdout=subprocess.DEVNULL,
            )
        with archive.open("rb") as data:
            digest = hashlib.file_digest(data, "sha256").hexdigest()
        destination = remote + "/" + name
        rclone("copyto", "--immutable", str(archive), destination)
        with subprocess.Popen(
            ["rclone", "cat", destination], stdout=subprocess.PIPE
        ) as reader:
            actual = hashlib.file_digest(reader.stdout, "sha256").hexdigest()
            if reader.wait() or actual != digest:
                raise ValueError(
                    "Remote read-back checksum failed. The snapshot is unverified."
                )
        marker = archive.with_suffix(".wbak.sha256")
        marker.write_text(f"{digest}  {name}\n")
        rclone("copyto", "--immutable", str(marker), destination + ".sha256")
        print(f"Uploaded and verified {name}")


def retain(remote, days, apply):
    listing = json.loads(
        rclone(
            "lsjson", "--files-only", "--max-depth", "1", remote, capture_output=True
        ).stdout
    )
    names = {entry["Name"] for entry in listing}
    verified = sorted(n for n in names if NAME.fullmatch(n) and n + ".sha256" in names)
    cutoff = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y%m%dT%H%M%SZ")
    # Preserve at least the three newest snapshots with read-back markers.
    for name in verified[:-3]:
        if NAME.fullmatch(name)[1] >= cutoff:
            continue
        print(("Removing " if apply else "Would remove ") + name)
        if apply:
            rclone("deletefile", remote + "/" + name)
            rclone("deletefile", remote + "/" + name + ".sha256")


def _hosts(uri):
    authority = urlsplit(uri).netloc.rsplit("@", 1)[-1]
    return {
        (host.hostname.lower().rstrip("."), host.port or 27017)
        for entry in authority.split(",")
        if (host := urlsplit("//" + entry)).hostname
    }


async def drill(path):
    from .backup import DRILL_MARKER, _records, _restore
    from .config import values
    from .store import Store

    uri = os.getenv("DRILL_MONGO_URI", "").strip()
    key = os.getenv("BACKUP_KEY", "").strip()
    if (
        not uri.startswith(("mongodb://", "mongodb+srv://"))
        or not _hosts(uri)
        or not key
    ):
        raise ArchiveError(
            "Set DRILL_MONGO_URI to an isolated staging MongoDB and set BACKUP_KEY."
        )
    for production in (os.getenv("MONGO_URI", ""), *values("CATALOG_URIS")):
        if production and _hosts(uri).intersection(_hosts(production)):
            raise ArchiveError(
                "The drill destination shares a configured production MongoDB endpoint."
            )

    with path.open("rb") as stream:
        _, manifest = next(_records(stream, key))
    run_id = secrets.token_hex(16)
    client = AsyncMongoClient(
        uri,
        appname="WovieRestoreDrill",
        tz_aware=True,
        w="majority",
        serverSelectionTimeoutMS=10000,
        maxPoolSize=3,
    )
    dbs = [client[f"wovie_drill_{run_id}_{slot}"] for slot in range(manifest["slots"])]
    ownership = secrets.token_hex(24)
    acquired = []

    async def mark(db):
        # Called after the normal restore guards verified empty destinations
        # and won exclusive creation of each database's restore lock.
        await db[DRILL_MARKER].insert_one({"_id": ownership})
        acquired.append(db)

    try:
        stats = await _restore(dbs, path, key, mark_drill=mark)
        expected = stats.pop("counts")
        actual = {}
        for slot, db in enumerate(dbs):
            for name in await db.list_collection_names():
                if name != DRILL_MARKER:
                    actual[slot, name] = await db[name].count_documents({})
        if expected != actual:
            raise ArchiveError(
                "A restored collection or its document count differs from the authenticated archive."
            )
        # Rebuild production indexes after comparing counts, because TTL
        # indexes may immediately expire old cache and receipt records.
        model = Store(SimpleNamespace(page_size=8))
        model.db, model.catalogs = dbs[0], dbs
        await model._create_indexes()
        sample_count = 0
        for db in dbs:
            identity = await db["_wovie_meta"].find_one({"_id": "catalog_identity"})
            async for row in db.files.find(
                {}, {"source_chat": 1, "message_id": 1}
            ).limit(10):
                chat = row["source_chat"]
                file_id = hashlib.sha256(
                    f"{chat}:{row['message_id']}".encode()
                ).hexdigest()[:32]
                route = await dbs[0].source_routes.find_one(
                    {"_id": chat, "catalog_id": identity["uuid"]}
                )
                if row["_id"] != file_id or not route:
                    raise ArchiveError(
                        "A sampled file has an invalid stable ID or source route."
                    )
                sample_count += 1
        if await dbs[0].jobs.find_one(
            {"status": {"$in": ["queued", "running"]}}, {"_id": 1}
        ):
            raise ArchiveError(
                "Pending external jobs were not paused during restoration."
            )
        stats.update(sampled_files=sample_count, indexes_rebuilt=True)
        return stats
    finally:
        try:
            leftovers = []
            for db in reversed(acquired):
                try:
                    if await db[DRILL_MARKER].find_one({"_id": ownership}):
                        await client.drop_database(db.name)
                    else:
                        leftovers.append(db.name)
                except PyMongoError:
                    leftovers.append(db.name)
            if leftovers:
                raise ArchiveError(
                    "Inspect these drill databases after incomplete cleanup: "
                    + ", ".join(leftovers)
                )
        finally:
            await client.close()


def main():
    load_dotenv(dotenv_path=Path(".env"))
    p = argparse.ArgumentParser(description=__doc__)
    cmds = p.add_subparsers(dest="action", required=True)
    cmds.add_parser("upload")
    cmds.add_parser(
        "drill", help="restore and check an archive on isolated staging MongoDB"
    ).add_argument("archive", type=Path)
    retention = cmds.add_parser("retain")
    retention.add_argument("--days", type=int, default=35)
    retention.add_argument(
        "--apply", action="store_true", help="delete only eligible verified snapshots"
    )
    args = p.parse_args()
    remote = os.getenv("SNAPSHOT_REMOTE", "").strip().rstrip("/")
    if args.action != "drill" and not re.fullmatch(
        r"[A-Za-z0-9_][A-Za-z0-9_-]*:[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", remote
    ):
        p.error(
            "Set SNAPSHOT_REMOTE to an rclone remote and dedicated directory, e.g. vault:wovie/db."
        )
    if getattr(args, "days", 1) < 1:
        p.error("Retention must be at least one day.")
    try:
        if args.action == "drill":
            print(json.dumps(asyncio.run(drill(args.archive)), sort_keys=True))
        elif args.action == "upload":
            upload(remote)
        else:
            retain(remote, args.days, args.apply)
    except ArchiveError as exc:
        sys.exit(str(exc))
    except (OSError, ValueError, PyMongoError, subprocess.CalledProcessError) as exc:
        sys.exit(
            f"Snapshot operation failed ({type(exc).__name__}). Check the remote and the previous command."
        )


if __name__ == "__main__":
    main()
