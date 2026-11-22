# Deployment

## Install

Use Python 3.11–3.15, a Telegram bot from BotFather and a dedicated MongoDB database on a currently supported server release (MongoDB 7/8 recommended). The MongoDB user must read/write records and create indexes; allow the host's network address. Use TLS for remote connections. For a small VPS, host MongoDB externally. Polling needs no inbound port; run **one worker per bot token**.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --no-cache-dir -r requirements.txt
cp .env.example .env
chmod 600 .env
```

Set the required values below, then run `.venv/bin/python -m wovie --check`. This checks configuration, database indexes and Telegram credentials, including an optional session; it is not a complete live-flow certification.

## Measured small-host profile

Core peak RSS was **171,972 KiB (167.94 MiB)** on CPython 3.12.14/Linux x86-64 with `MALLOC_ARENA_MAX=2`: real MongoDB ran outside the measured process; a simulated Bot API covered startup, seven commands and shutdown; user-session indexing was disabled. The dependency virtual environment occupied 87 MiB, excluding the interpreter, OS and optional interfaces.

The supplied systemd/Docker settings include that allocator setting and a configurable 192 MiB process cap. Session-rich workloads, real network behavior and streams need their own measurements. External MongoDB capacity is separate. No whole 256 MB RAM/512 MB disk guest guarantee follows from these measurements.

## Configuration

| Variable | Meaning |
|---|---|
| `BOT_TOKEN` | Required BotFather token. Keep the same bot identity when moving hosts. |
| `BOT_API_BASE_URL` | Optional custom Bot API endpoint for the polling worker; leave blank for Telegram. |
| `ADMINS` | Required owner IDs: `[123456789,987654321]` or comma-separated numeric IDs. |
| `MONGO_URI` | Required primary MongoDB connection. |
| `MONGO_DB` | Primary database name, default `wovie`; always used for the primary URI. |
| `CATALOG_URIS` | Optional JSON list of distinct additional catalog connections; URI database paths override `MONGO_DB` for these catalogs. Maximum eight catalogs including primary. |
| `API_ID`, `API_HASH`, `SESSION_STRING` | Optional authorized Telethon account for historical/live private-source indexing. |
| `PUBLIC_SEARCH` | Allow ordinary users, default `true`. Groups also need activation. |
| `PAGE_SIZE` | 1–20 results, default 8. |
| `RATE_LIMIT` | Searches per user/minute, default 12. |
| `DAILY_LIMIT` | Daily delivery limit; `0` disables it. |
| `MAX_FILE_SIZE` | Media-size indexing filter in bytes; `0` disables it. Ordinary indexing downloads no media bytes. |
| `REQUIRED_CHANNELS` | JSON list of numeric IDs or `@usernames`; bot needs membership-check permissions. |
| `DELETE_AFTER` | Delivered-message deletion delay, 0–172799 seconds; `0` disables it. Telegram permissions/time limits apply. |
| `PROTECT_CONTENT` | Telegram's supported protection option for outgoing deliveries. |
| `TMDB_API_KEY` | Optional TMDB v3 key or API read access token. |
| `PREMIUM_STARS`, `PREMIUM_DAYS` | Price in Telegram Stars and duration; price `0` disables sales. |
| `SUPPORT_URL` | HTTPS payment-support destination; required for paid plans. |
| `BACKUP_KEY`, `BACKUP_CHAT` | Encryption key and optional `/backup` destination; see [operations](OPERATIONS.md). |
| `BOT_NAME`, `LOG_LEVEL` | Interface name and logging level, default `Wovie` / `INFO`. |
| `POLL_TIMEOUT`, `JOB_INTERVAL` | Poll timeout 2–55 seconds; scheduler idle interval 0.1–60 seconds. |

Use a JSON array for additional URIs, for example `CATALOG_URIS='["mongodb+srv://HOST/catalog2"]'`. URL-encode reserved characters in credentials. Environment variables override `.env`. Restart after changes. [Basic provider options](EXTENSIONS.md), [advanced provider workflows](PROVIDERS.md), [web options](WEB.md), [community tools](COMMUNITY.md) and [legacy imports](IMPORT.md) have separate guides.

## Sources and private sessions

1. Add the bot to the channel/group with access to posts and media. Group ingestion needs administrator status or suitable BotFather privacy settings.
2. Register `/source add -1001234567890`. This defaults to **members only**. Use `/source add -1001234567890 public` or `/source publish -1001234567890` only when its catalog should be available to all permitted bot users.
3. New media can be indexed without a user session. Historical scanning needs the optional session below.
4. Obtain application credentials from `my.telegram.org`; set `API_ID` and `API_HASH`. Run:

```bash
.venv/bin/python scripts/create_session.py
```

5. Complete login/2FA locally. Save the returned **Telethon** string as `SESSION_STRING`; Pyrogram strings are incompatible. Restart.
6. The session account must already have joined the private source. Run `/index -1001234567890`; use `/jobs` for progress and `/pause JOB_ID`, `/resume JOB_ID`, `/cancel JOB_ID` for control. Range syntax is documented in [COMMANDS.md](COMMANDS.md).

A session is an account credential: keep it out of chats, logs and Git. Session access alone does not grant the bot permission to deliver media. The bot must have a valid cached file ID or be able to copy the source message; protected content is not bypassed. Disabling a source preserves records while stopping visibility/delivery.

## VPS with systemd

Install supported Python, venv support and Git using your OS package manager. Place the repository in `/opt/wovie`, create its virtual environment, install requirements and configure `.env`. Run as root or with `sudo`:

```bash
useradd --system --home /opt/wovie --shell /usr/sbin/nologin wovie
install -d -m 750 -o root -g wovie /etc/wovie
install -m 640 -o root -g wovie /opt/wovie/.env /etc/wovie/wovie.env
install -m 644 /opt/wovie/deploy/wovie.service /etc/systemd/system/wovie.service
systemctl daemon-reload
systemctl enable --now wovie
systemctl status wovie
journalctl -u wovie -n 50 --no-pager
```

Keep `/opt/wovie` and its virtual environment readable/executable by `wovie`; code can remain root-owned. The service reads `/etc/wovie/wovie.env` and writes temporary exports under `/var/lib/wovie/tmp`. Remove the duplicate `/opt/wovie/.env` after verification or keep it synchronized for CLI work.

The supplied 192 MiB limit is containment, not proof that a workload fits. Adjust using `systemctl edit wovie` after measuring. Bound journald retention through the host's journald configuration.

## Docker Compose

Use a MongoDB URI reachable from the container. `127.0.0.1` points to the container itself.

```bash
docker compose build
docker compose run --rm bot python -m wovie --check
docker compose up -d
docker compose logs --tail=50 bot
docker compose stats --no-stream
```

The image runs as UID 10001 with read-only application files and a writable temporary-work volume. Compose bounds memory/logs. MongoDB holds durable state; the work volume is not a database backup. Build elsewhere when disk is tight. The image/layers, Docker engine and OS can exceed a 512 MB whole-disk quota.

## Render, Railway and other hosts

Deploy a continuously running **background worker** using the Dockerfile, or install `requirements.txt` and start `python -m wovie`. Configure secrets in the dashboard, use external MongoDB and one replica. Disable idle sleeping; extended outages can exceed Telegram's update-retention window. This polling process is unsuitable for request-only serverless functions.

Provider plans/quotas change; no free-tier uptime is assumed. The optional web catalog requires its own process and HTTPS routing: [WEB.md](WEB.md).

## First live verification

Run `--check`, register a source, post one sample media file, search for it in PM and open its file button. Enable a group and repeat if needed. Import a small historical range when using a session. Restart and confirm the catalog and links survive. Verify one independent backup/restore before relying on the catalog.

## Multiple bot identities

Use separate application instances or `python -m wovie.fleet /etc/wovie/profiles`. For fleet mode create a mode-0600 `.env` file per bot, each with its own token, explicit primary database name, admins and optional catalog URIs as a JSON array. Use separate session strings for concurrent MTProto clients. The supervisor restarts crashed workers and shuts them down on SIGTERM. List, add and remove profiles in the directory and restart the supervisor to reload. Every running bot consumes its own process memory. Deploy a fleet on a larger machine.

If using an operator-hosted Telegram Bot API endpoint, set `BOT_API_BASE_URL` to its HTTPS origin or loopback HTTP origin on the polling host. Leave it unset to use Telegram. The configured endpoint receives the bot token. The separate web service uses Telegram's hosted Bot API.

## Multiple bot profiles

Use `python -m wovie.fleet /secure/profiles` to supervise separate bot processes from owner-protected `*.env` files. Each file needs `BOT_TOKEN`, `ADMINS`, `MONGO_URI`, `MONGO_DB` and any selected options; run `chmod 600` on each profile. Profiles must use distinct bot identities and database names. Keep the directory outside Git. Every child uses a full bot process: this is not a shared 256 MB pool. Use per-service/container limits for independent resource containment. In-chat token provisioning is not implemented.

For builds behind a private TLS inspection proxy, provide the trusted CA bundle as a BuildKit secret. It is available to pip during dependency installation and is excluded from the image.

```sh
docker build --secret id=pip_ca,src=/etc/ssl/certs/ca-certificates.crt -t wovie .
```
