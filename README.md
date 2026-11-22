# 🎬 WovieSearchBot

A Python Telegram catalog bot with advanced search, private delivery, required MongoDB persistence and optional authorized session-string indexing. Its interface uses compact emoji headers, bold text and inline buttons; Telegram supplies the fonts.

## Included

- PM/group/inline search, release filters, transliteration, series navigation, favorites, watched lists and requests.
- Authorized live/history indexing, manual ingestion, multiple MongoDB catalogs, metadata correction and alternate sources.
- Durable links, batches, memberships, quotas, premium plans, Telegram Stars, referrals and optional verification links.
- Persistent jobs, moderation, broadcasts, release alerts, weekly digests and diagnostics.
- Encrypted recovery, independent media archives and optional web, desktop, Discord, AI/provider and media-worker modules.

This is a new implementation. [Feature coverage](docs/COVERAGE.md) maps all 90 research points to actual behavior and remaining limits; it does not claim every reference-project behavior is identical.

## Start

Use Python 3.11–3.15 and a dedicated MongoDB database. The container uses Python 3.13.

```bash
git clone https://github.com/WikioApps/WovieSearchBot.git
cd WovieSearchBot
python3 -m venv .venv
.venv/bin/python -m pip install --no-cache-dir -r requirements.txt
cp .env.example .env
```

Set `BOT_TOKEN`, `ADMINS` and `MONGO_URI`. Then:

```bash
.venv/bin/python -m wovie --check
.venv/bin/python -m wovie
```

Register `/source add -1001234567890` for members-only visibility; append `public` only to publish its catalog to permitted bot users. Give the bot source access. For historical/private indexing, configure `API_ID`, `API_HASH`, `SESSION_STRING` and run `/index -1001234567890`. The session account must already have access.

## Guides

| Area | Documentation |
|---|---|
| Setup and commands | [Deployment](docs/DEPLOYMENT.md) · [Commands](docs/COMMANDS.md) · [Architecture](docs/ARCHITECTURE.md) |
| Search and metadata | [Search syntax](docs/SEARCH.md) · [Discovery/series](docs/DISCOVERY.md) · [Curation](docs/CURATION.md) |
| Administration | [Community](docs/COMMUNITY.md) · [Diagnostics/verification](docs/OPERATORS.md) |
| Recovery and migration | [Operations](docs/OPERATIONS.md) · [Recovery tools](docs/RECOVERY.md) · [Legacy imports](docs/IMPORT.md) |
| External providers | [Basic integrations/media worker](docs/EXTENSIONS.md) · [Advanced providers](docs/PROVIDERS.md) |
| Other interfaces | [Web/streaming](docs/WEB.md) · [Desktop/Discord](docs/INTERFACES.md) |
| Scope and verification | [90-feature crosswalk](docs/COVERAGE.md) |

The measured core peaked at **167.94 MiB RSS** with a simulated Bot API and no user session; the dependency environment used 87 MiB excluding OS/interpreter. A 256 MB/512 MB whole VPS is therefore **not guaranteed**. Use external MongoDB and keep optional streams/media processing on another host; see the measured conditions in [deployment](docs/DEPLOYMENT.md).

Real MongoDB multi-catalog/archive checks and simulated delivery checks were performed. No test suite is committed. Live Telegram/provider credentials and permissions still require deployment validation.

[Parser and data migrations](docs/MIGRATIONS.md) · [Caption and URL buttons](docs/BUTTONS.md)
