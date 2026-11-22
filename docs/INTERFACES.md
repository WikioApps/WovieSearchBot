# Optional desktop and Discord interfaces

These are separate processes. Install/run them away from the 256 MB Telegram worker. They reuse MongoDB catalog state; they do not require new MongoDB databases.

## Local operator desktop

Run `python -m wovie.desktop` from the repository on a graphical computer with the normal Python requirements and tkinter installed. Debian/Ubuntu: `sudo apt install python3-tk`. Windows/macOS Python installers usually include Tk.

Use the normal `.env`, including `BOT_TOKEN`, `ADMINS`, `MONGO_URI`, `API_ID`, `API_HASH` and `SESSION_STRING`. The session must belong to a **user in ADMINS**. Generate a separate session for this computer; do not reuse an active server session across machines. No local web server is opened. The operator can browse/search active-source metadata, including private sources already indexed by the bot. Keep the computer, session and MongoDB credentials private.

Search supports the same `year:`, `lang:`, `quality:`, `s:` and `e:` filters as Telegram. Select a result and choose **Transfer selected** to copy, rename, create a thumbnail or transcode through `wovie.media`. Select a numeric destination, working directory and size budget; confirm before sending. Telegram source/destination access and protected-content checks remain active. FFmpeg is needed only for thumbnail/transcode. These operations download media and need substantial disk space. Closing the window stops an active transfer; already sent Telegram messages remain sent.

## Discord discovery

Create an application/bot in the Discord Developer Portal. Invite it to the permitted servers with `bot` and `applications.commands` scopes. Slash commands do not need Message Content Intent. Use a separate virtual environment:

```bash
python3 -m venv .venv-discord
. .venv-discord/bin/activate
pip install -r requirements-discord.txt
python -m wovie.discord_bot
```

Keep normal `MONGO_URI`, `MONGO_DB`, optional `CATALOG_URIS` and `TMDB_API_KEY`. Additional settings:

```dotenv
DISCORD_TOKEN=your-discord-bot-token
DISCORD_GUILD_IDS=123456789012345678,234567890123456789
DISCORD_PUBLIC_CATALOG=false
TELEGRAM_BOT_USERNAME=YourWovieBot
```

`DISCORD_GUILD_IDS` is a required allowlist (up to 25 servers). Commands are synchronized to those servers only. `/movie title` returns TMDB movie/TV information. `/catalog title` is disabled until `DISCORD_PUBLIC_CATALOG=true`; even then it searches **only sources explicitly marked active and public**, excluding protected/premium-only sources. New sources remain private by default. Never mark a source public unless its title metadata may be shared outside Telegram.

Catalog responses contain title/year/quality and Telegram links, never raw file handles, source IDs or media bytes. All responses are ephemeral; each user sees their own response. Telegram still checks access, membership, plans and quotas when a link is opened. Discord and Telegram identities are not linked, and Discord never grants Telegram permissions. Discovery links expire after seven days. Disable a source to stop later delivery; previously displayed metadata cannot be recalled.

The Discord process has two simultaneous-request slots and per-user cooldowns. Both interfaces need separate resource measurements and live credentials before deployment. The main Telegram deployment does not import tkinter or discord.py.
