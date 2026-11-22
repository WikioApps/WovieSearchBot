"""Launch isolated bot profiles from operator-owned environment files."""

import argparse
import asyncio
from contextlib import suppress
import os
from pathlib import Path
import signal
import sys

from dotenv import dotenv_values


async def run(directory: Path):
    from pymongo import AsyncMongoClient

    paths = sorted(directory.glob("*.env"))
    if not paths:
        raise ValueError(
            "Supply a directory of .env profiles, for example profiles/movies.env"
        )
    tokens, namespaces = set(), set()
    profiles = []
    for path in paths:
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise ValueError(f"Protect {path.name} with chmod 600")
        cfg = {
            k: v
            for k, v in dotenv_values(path, interpolate=False).items()
            if v is not None
        }
        if not all(
            cfg.get(k) for k in ("BOT_TOKEN", "MONGO_URI", "MONGO_DB", "ADMINS")
        ):
            raise ValueError(f"Missing required settings in {path.name}")
        bot_id = cfg["BOT_TOKEN"].partition(":")[0]
        if bot_id in tokens:
            raise ValueError("Profiles must use distinct Telegram bot identities")
        tokens.add(bot_id)
        # Require distinct names for every fleet database so profiles cannot route into each other.
        import json

        extra = json.loads(cfg.get("CATALOG_URIS") or "[]")
        names = {cfg["MONGO_DB"]}
        for uri in extra:
            client = AsyncMongoClient(uri, connect=False)
            try:
                names.add(client.get_default_database(default=cfg["MONGO_DB"]).name)
            finally:
                await client.close()
        if namespaces.intersection(names):
            raise ValueError("Use distinct database names across profiles")
        namespaces.update(names)
        env = {
            k: os.environ[k]
            for k in ("PATH", "HOME", "LANG", "TZ", "TMPDIR", "SSL_CERT_FILE")
            if k in os.environ
        }
        profiles.append(
            (
                path.stem,
                {**env, **cfg, "PYTHON_DOTENV_DISABLED": "1", "PYTHONUNBUFFERED": "1"},
            )
        )
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)

    async def worker(name, env):
        delay = 2
        while not stop.is_set():
            child = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "wovie", env=env
            )
            try:
                code = await child.wait()
            finally:
                if child.returncode is None:
                    child.terminate()
                    await child.wait()
            if code == 2:
                print(f"Profile {name} needs configuration", file=sys.stderr)
                return
            await asyncio.sleep(delay)
            delay = min(60, delay * 2)

    tasks = [asyncio.create_task(worker(name, env)) for name, env in profiles]
    try:
        await stop.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    try:
        asyncio.run(run(parser.parse_args().directory))
    except ValueError as exc:
        parser.exit(2, f"{exc}\n")
