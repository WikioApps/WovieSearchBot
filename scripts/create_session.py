#!/usr/bin/env python3
"""Generate a Telethon user session without writing an unencrypted session file.

Run: python scripts/create_session.py
Use only an account you control and sources that account is authorized to read.
The resulting session grants account access; store it like a password.
"""

from __future__ import annotations

import asyncio
from getpass import getpass
import os
from pathlib import Path
import sys

from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from dotenv import load_dotenv


async def main() -> None:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    if not sys.stdin.isatty():
        raise SystemExit("Run this interactive helper in a trusted terminal.")
    try:
        api_id = int(os.environ.get("API_ID") or input("Telegram API ID: ").strip())
    except ValueError:
        raise SystemExit("API_ID must be an integer.") from None
    api_hash = (
        os.environ.get("API_HASH") or getpass("Telegram API hash (hidden): ").strip()
    )
    if api_id <= 0 or len(api_hash) != 32:
        raise SystemExit("Use API_ID and API_HASH from https://my.telegram.org.")
    phone = getpass("Account phone number with country code (hidden): ").strip()
    client = TelegramClient(StringSession(), api_id, api_hash, flood_sleep_threshold=0)
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
        code = getpass("Telegram login code (hidden): ").strip().replace(" ", "")
        try:
            await client.sign_in(phone, code, phone_code_hash=sent.phone_code_hash)
        except errors.SessionPasswordNeededError:
            await client.sign_in(
                password=getpass("Two-step verification password (hidden): ")
            )
        account = await client.get_me()
        if account.bot:
            raise SystemExit("Use a user account, not a bot account.")
        value = client.session.save()
        destination = input(
            "Save to a new private file (path), or press Enter to display once: "
        ).strip()
        if destination:
            path = Path(destination).expanduser()
            # O_EXCL prevents overwriting a file or following an existing symlink.
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as file:
                file.write("SESSION_STRING=" + value + "\n")
            print("Session saved with owner-only permissions. Keep it outside Git.")
        else:
            print("\nSESSION_STRING=" + value)
        print("Do not share this session. Revoke it in Telegram Devices if exposed.")
    except errors.FloodWaitError as exc:
        raise SystemExit(
            f"Telegram requires a wait of {exc.seconds} seconds before retrying."
        ) from None
    finally:
        await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        raise SystemExit("\nSession generation cancelled.") from None
