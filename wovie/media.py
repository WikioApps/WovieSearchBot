"""Operator-run media transfer and conversion on a host with working disk space."""

import argparse
import asyncio
from pathlib import Path
import shutil
import signal
from contextlib import suppress
from tempfile import TemporaryDirectory

from telethon import TelegramClient
from telethon.sessions import StringSession

from .config import Settings


async def convert(source, destination, *, thumbnail, budget):
    executable = shutil.which("ffmpeg")
    if not executable:
        raise ValueError("Install ffmpeg on the media worker")
    args = [
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-n",
        "-protocol_whitelist",
        "file,pipe",
        "-i",
        str(source),
    ]
    if thumbnail:
        args += ["-frames:v", "1", "-vf", "scale=320:-2"]
    else:
        args += [
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-preset",
            "veryfast",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
        ]
    args.extend(["-fs", str(budget), str(destination)])
    child = await asyncio.create_subprocess_exec(executable, *args)
    try:
        code = await asyncio.wait_for(child.wait(), 7200)
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
    if code or not destination.exists() or not 0 < destination.stat().st_size < budget:
        raise ValueError("Conversion failed or exceeded the output limit")


async def process(args):
    from .store import Store

    settings = Settings.from_env()
    if not settings.session_string:
        raise ValueError("Set SESSION_STRING, API_ID and API_HASH")
    if not 1 <= args.max_mib <= 4096:
        raise ValueError("--max-mib must be 1 through 4096")
    budget = args.max_mib * 1048576
    store = Store(settings)
    client = TelegramClient(
        StringSession(settings.session_string),
        settings.api_id,
        settings.api_hash,
        receive_updates=False,
        flood_sleep_threshold=0,
    )
    try:
        await store.connect()
        if not await store.db.sources.find_one({"_id": args.source, "active": True}):
            raise ValueError("Source must be registered and active")
        await client.connect()
        if not await client.is_user_authorized():
            raise ValueError("User session is no longer authorized")
        from .indexer import resolve_peer

        source = await resolve_peer(client, args.source)
        target = await resolve_peer(client, args.target)
        entity = await client.get_entity(source)
        msg = await client.get_messages(source, ids=args.message)
        if (
            not msg
            or not msg.document
            or getattr(msg, "noforwards", False)
            or getattr(entity, "noforwards", False)
        ):
            raise ValueError("Message unavailable, protected or missing a document")
        if not 0 < msg.document.size <= budget:
            raise ValueError("Input exceeds the worker file limit")
        filename = args.name or msg.file.name or f"file-{msg.id}"
        if (
            Path(filename).name != filename
            or filename in {"", ".", ".."}
            or len(filename.encode()) > 220
        ):
            raise ValueError("Use a plain filename of at most 220 bytes")
        args.work_dir.mkdir(parents=True, exist_ok=True)
        if (
            shutil.disk_usage(args.work_dir).free
            < msg.document.size + (budget if args.mode != "copy" else 0) + 16 * 1048576
        ):
            raise ValueError("Not enough working disk space")
        with TemporaryDirectory(dir=args.work_dir, prefix="wovie-") as tmp:
            local = Path(tmp) / "input" / filename
            local.parent.mkdir()
            with local.open("xb") as f:
                async for chunk in client.iter_download(
                    msg.media, request_size=256 * 1024
                ):
                    if f.tell() + len(chunk) > budget:
                        raise ValueError("Download exceeds the file limit")
                    f.write(chunk)
            if local.stat().st_size != msg.document.size:
                raise ValueError("Source transfer incomplete")
            if args.mode != "copy":
                result = Path(tmp) / (
                    local.stem + (".jpg" if args.mode == "thumbnail" else ".mp4")
                )
                await convert(
                    local, result, thumbnail=args.mode == "thumbnail", budget=budget
                )
                local = result
            sent = await client.send_file(
                target,
                local,
                caption=(args.caption or msg.message or "")[:1024],
                parse_mode=None,
                supports_streaming=args.mode == "transcode",
            )
            print(f"Uploaded message {sent.id}")
    finally:
        await client.disconnect()
        await store.close()


async def run(args):
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    with suppress(NotImplementedError):
        loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        await process(args)
    finally:
        with suppress(NotImplementedError):
            loop.remove_signal_handler(signal.SIGTERM)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("source", "message", "target"):
        parser.add_argument(field, type=int)
    parser.add_argument(
        "--mode", choices=("copy", "thumbnail", "transcode"), default="copy"
    )
    parser.add_argument("--name", help="Filename for the new upload")
    parser.add_argument("--caption")
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--max-mib", type=int, default=256)
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except asyncio.CancelledError:
        parser.exit(
            143,
            "Transfer stopped. Check the target before retrying an interrupted upload.\n",
        )
    except (ValueError, OSError, TimeoutError) as e:
        parser.exit(2, f"{e}\n")


if __name__ == "__main__":
    main()
