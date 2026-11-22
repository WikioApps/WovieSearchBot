<div align="center">

# 🎬 Wovie Auto Filter

**Search a Telegram media library. Tap an edition. Get the file in private.**

[![Deploy on Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/WikioApps/WovieSearchBot)
[![Deploy on Heroku](https://www.herokucdn.com/deploy/button.svg)](https://heroku.com/deploy?template=https://github.com/WikioApps/WovieSearchBot)

</div>

## ✦ 𝐅𝐞𝐚𝐭𝐮𝐫𝐞𝐬

| In Telegram | What you can do |
| --- | --- |
| 🗂 Forward indexing | Forward the last source post, confirm the range, watch progress, pause and resume. New source media and edits are indexed as they arrive. |
| 🔎 Auto filter | Search in private, in groups, or inline. Combine title, year, language, resolution, season and episode. Browse paginated buttons or a compact link list. |
| 🎞 File cards | See the edition and size, save to a watchlist, get one file or get the visible page. Optional TMDB posters and summaries. |
| 🛠 Bot settings | Edit the welcome photo and text, buttons, captions, channels, permissions, streaming, file protection and expiry from Telegram. |
| ⚙ Personal and group settings | Remember each user’s preferred releases and display. Group admins can customize their own group. |
| 📮 Community | Missing-title requests with moderator replies, trending releases, keyword replies, channel announcements, private and group broadcasts. |
| 💎 Membership | Grant expiring premium access and optionally reserve browser playback for members. Telegram delivery works with standard membership. |
| 💾 Storage | One MongoDB is sufficient. Additional databases hold file catalogs. Search results merge across all configured catalogs. |

## 🗝 𝐒𝐞𝐭𝐮𝐩

Use Python 3.11–3.14 and MongoDB 5.0 or later. Give the database user read/write and index creation access on each named database. Allow the host’s IP through your MongoDB network rules.

| Required secret | Where it comes from |
| --- | --- |
| `API_ID`, `API_HASH` | The app credentials issued at [my.telegram.org/apps](https://my.telegram.org/apps). |
| `BOT_TOKEN` | Create a bot with [@BotFather](https://t.me/BotFather). |
| `OWNER_ID` | Your numeric Telegram user ID. This account controls global settings. |
| `DATABASE_URI` | A MongoDB URI including the database name, e.g. `mongodb+srv://USER:PASSWORD@HOST/wovie`. Percent-encode reserved characters in credentials. |

Clone once. If you already have the checkout, open its directory and continue with the next block.

```sh
git clone https://github.com/WikioApps/WovieSearchBot.git
cd WovieSearchBot
```

On Ubuntu or Debian, install virtual environment support for the system Python. Run the package commands as root or with `sudo`.

```sh
deactivate 2>/dev/null || true
apt-get update && apt-get install -y python3 python3-venv nano
/usr/bin/python3 --version
```

If the version shown is 3.11–3.14, create the environment and install the dependencies. To use a separately installed Python, substitute its full path in the first command (for example, `/usr/bin/python3.14` with the Ubuntu package `python3.14-venv`).

```sh
/usr/bin/python3 -m venv .venv && \
  .venv/bin/python -m pip install -r requirements.txt
```

After installation succeeds, fill in the five values and save the file. The first command preserves an existing `.env`.

```sh
test -e .env || cp .env.example .env
nano .env
```

```sh
.venv/bin/python bot.py
```

Open the bot in private and send `/start`. Set your Updates, Support, Films and Guide URLs in **Bot Settings → Brand & links**. Empty links stay hidden. Your fork can also edit the defaults in [info.py](info.py) and the labels in [Script.py](Script.py). Telegram renders the Unicode small-cap labels, native buttons and bold captions on the user’s device.

## 🗂 𝐈𝐧𝐝𝐞𝐱 𝐚 𝐩𝐫𝐢𝐯𝐚𝐭𝐞 𝐜𝐡𝐚𝐧𝐧𝐞𝐥 𝐨𝐫 𝐠𝐫𝐨𝐮𝐩

1. Add the bot to the source as an **administrator**. Use a source whose files you intend to share with the bot’s audience.
2. Forward the **last post** in the source to the bot’s private chat. As the owner or a trusted catalog admin, you’ll see the source and exact range. Press **✓ ɪɴᴅᴇx**. The post can be text or media.
3. If Telegram hides a group forward’s origin, copy the original **message link** and send `/index LINK` instead. Private `t.me/c/…`, topic links and public post links work. You can also reply to the original post **in the group** with `/index`.
4. Use `/jobs` to check progress, stop or resume. For a specific range, `/index -1001234567890 2400 1000` scans IDs 1001 through 2400 inclusive. The cursor advances after the catalog write. Repeating a range updates the same source posts.

The default reader uses the bot token. **No user session string is required for a private source the bot can read.** Telegram access, visible history and content protection still apply. A hidden-origin forward alone cannot identify its original message. Deleted posts are skipped. Source chats with protected content cannot be published.

New media in registered sources is indexed while the bot is online. Forward a newer boundary to backfill a gap after downtime. For an optional second reader, mount an existing **Kurigram/Pyrogram-format** user session string as a private file named `index.session-string` in the working directory, restart, and run `/sindex LINK`. This opt-in account must belong to the channel or supergroup. Use the bot reader for basic groups, where message IDs are account-specific. The bot still checks its own source access and delivers from the original post.

## 🔎 𝐒𝐞𝐚𝐫𝐜𝐡 & 𝐝𝐞𝐥𝐢𝐯𝐞𝐫𝐲

- Send `Dune 2021 1080p`, `Dark S02E04` or `Vikram Tamil`. Tap **Filters** for language, quality, year, season or episode. A broad query shows up to 100 editions. Add a release detail to narrow it. Menus belong to their requester for 45 minutes.
- For normal text search in groups, grant the bot admin access or disable privacy via BotFather’s `/setprivacy` and re-add the bot. With privacy enabled, use `/search@YourBot TITLE`. To enable inline results, use BotFather’s `/setinline`, then type `@YourBot title` in a chat. A configured search-group allowlist disables inline results because inline queries do not include a verifiable chat ID.
- Tap an edition to open its private card, then **Get file** or **Save**. **Get page** delivers the editions on that page to the original requester. Each delivery checks the source post again.
- To enable browser playback, set **Stream URL** to this service’s public HTTPS origin. The card issues bearer links valid for 15 minutes. Anyone holding a link can use it during that period. Removing a source, revoking access or enabling file protection closes access on the next request. Browser playback needs a supported codec. Downloads and HTTP byte ranges work without storing media on disk.
- To enable posters, enter your TMDB v3 key under **Brand & links**. Results use the parsed release title and year. TMDB outages or missing matches leave the ordinary file card available. This product uses the TMDB API and is not endorsed or certified by TMDB.

## ⚙ 𝐒𝐞𝐭𝐭𝐢𝐧𝐠𝐬 & 𝐜𝐨𝐦𝐦𝐚𝐧𝐝𝐬

| Who | Commands and controls |
| --- | --- |
| Everyone | `/start`, `/help`, `/search TITLE`, `/saved`, `/request TITLE`, `/top`, `/myplan`, `/id`, `/ping` |
| Each user, in private | `/usetting` or `/usettings` → language, quality, buttons/links, newest/oldest, file sizes. **Reset** restores one preference. |
| Group administrator | `/settings` in the group → group search, spelling suggestions, welcome and result layout. The owner’s overall group access rules take precedence. |
| Bot owner, in private | `/bsetting` or `/bsettings` → choose a section, tap a value, edit or reset. Use `/cancel` to discard a pending text entry. Bot and user settings survive restarts. |
| Catalog administrator | `/index`, `/sindex`, `/jobs`, `/sources`, `/stats`, `/delete FILE_KEY`. A file key follows `f_` in its private link. Removing a catalog entry leaves the source post intact. |
| Community moderator¹ | `/requests` lists open requests. Mark a title available or closed to notify the requester. `/filter KEYWORD \| REPLY`, `/filters`, `/unfilter KEYWORD` manage exact keyword replies. |
| Audience management¹ | Reply in private to a message with `/broadcast` or `/grp_broadcast`, then confirm. `/ban ID`, `/unban ID`, `/add_premium USER_ID DAYS`, `/remove_premium USER_ID`. |

¹ The owner and trusted catalog admins have these permissions. In **Chats & access**, enter numeric chat IDs or user IDs separated by spaces. Set **Requests chat** to direct requests to a moderation group, **Updates chat** for new-file announcements and **Log chat** for index summaries. **Updates link** is a public-facing button and can point somewhere else. For required memberships, make the bot an admin of each required channel and provide an accessible invite link. The bot owner alone can edit global settings.

## 🚀 𝐃𝐞𝐩𝐥𝐨𝐲

Run **one always-on service per bot token**. Telegram uses an outbound MTProto connection. The service exposes `GET /health` and uses the host’s `PORT` or 8080. Set `PORT=0` for a worker with no HTTP or browser streaming.

| Platform | Deployment |
| --- | --- |
| Linux VPS · Docker | Fill in `.env`, run `docker compose up -d --build` and inspect `docker compose logs -f bot`. Compose binds port 8080 to localhost. For streaming, proxy your HTTPS domain to it. Update with `git pull --ff-only && docker compose up -d --build`. |
| Linux VPS · systemd | Install in `/opt/wovie`, create the virtualenv there and use the unit below. The service account needs read access to the checkout and its private environment file. |
| Render | Use the deploy button, supply the five values and choose an always-on plan. The included Blueprint builds the Dockerfile and probes `/health`. |
| Heroku | Use the deploy button and supply the five values. The included app manifest starts one web dyno. Use `heroku logs --tail` for logs. |
| Railway / Koyeb | Import this GitHub repo as a Docker service, add the five secrets, set one replica, disable scale-to-zero and expose the assigned HTTP port. Set the health path to `/health`. |
| Fly.io | Run `fly launch --no-deploy`, select the Dockerfile, set internal port 8080 and one always-on Machine. Set the five app secrets, run `fly deploy` and `fly scale count 1`. |

<details><summary>systemd service for a VPS</summary>

Save as `/etc/systemd/system/wovie.service` after creating the `wovie` service user.

```ini
[Unit]
Description=Wovie Telegram Auto Filter
Wants=network-online.target
After=network-online.target

[Service]
User=wovie
WorkingDirectory=/opt/wovie
EnvironmentFile=/opt/wovie/.env
ExecStart=/opt/wovie/.venv/bin/python /opt/wovie/bot.py
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

Run `sudo systemctl daemon-reload && sudo systemctl enable --now wovie`. Read logs with `journalctl -u wovie -f`.

</details>

## 💾 𝐏𝐞𝐫𝐬𝐢𝐬𝐭𝐞𝐧𝐜𝐞 & 𝐫𝐞𝐜𝐨𝐯𝐞𝐫𝐲

- A single URI is enough. For multiple databases, set `DATABASE_URI` to a JSON array, with the primary first. In a dotenv file, wrap the JSON in single quotes, e.g. `DATABASE_URI='["mongodb://mongo-a/wovie","mongodb://mongo-b/catalog2"]'`. In a hosting dashboard paste the JSON array directly.
- The primary holds settings, users, watchlists, requests, jobs, source routes and Telegram peer information. A source keeps its assigned catalog database when another database is added. Keep all registered databases available and the same primary first. The service checks catalog identities at startup.
- Back up **every configured database** using MongoDB snapshots or stop the bot and take a `mongodump` of each one. Restore the full set to recover file routes and settings together. Verify restored catalogs and settings in an isolated environment. Start the original bot token against the restored copy only after stopping the previous process. Telegram holds the media itself. Retain your source posts.
- The rewrite stores its data in `eva_*` collections. For an existing deployment of the previous Wovie implementation, keep its database backup. Register and re-index its sources in the new catalog. Previous bot/user settings use a different schema and can be re-entered in the panels.

## ♥ 𝐂𝐫𝐞𝐝𝐢𝐭𝐬

The forward-boundary index flow adapts the batched message-ID approach in [Nova-Filter-Bot](https://github.com/KannadaCinemaStore/Nova-Filter-Bot/tree/b44a2436a2aaddeec1948be63a459cd3c7a41fa0). The menu layout and source-post confirmation follow patterns reviewed in [Auto_Filter_Bot](https://github.com/chaudharysahab437/Auto_Filter_Bot/tree/5d145c1fae53e002fe7f9be2ca1375717b9789c8). The release cards and editable settings draw on [DR-DOOM-AutoFilter-Bot](https://github.com/TheBlackLion17/DR-DOOM-AutoFilter-Bot-/tree/cc2f3a7e84799bba42eb47db8de3e84b3a861f6c) and [WZML-X](https://github.com/weebzone/WZML-X). Built on [Kurigram](https://github.com/KurimuzonAkuma/pyrogram) and [PyMongo](https://github.com/mongodb/mongo-python-driver).

Released under the [GNU General Public License v3](LICENSE).
