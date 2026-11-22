# Architecture

## Runtime

`python -m wovie` runs aiogram, PyMongo's asynchronous client, durable job workers and an optional Telethon session. Incoming updates are persisted before dispatch. Provider integrations use optional HTTP adapters. The web catalog/streaming server runs separately.

## Data ownership

| Storage | Contents |
|---|---|
| Primary MongoDB | Users/settings, sources/routing, jobs, links, search sessions, plans/receipts, requests, favorites/watchlists, incoming updates and primary catalog |
| Additional catalog databases | File records assigned through persistent source routing |
| Telegram | Actual media and source messages |
| Temporary disk | Bounded export/backup archives; ordinary indexing/delivery downloads no media |

Each catalog has a persistent identity. Sources retain their route across restarts and URI credential rotation; missing registered catalogs must be repaired instead of silently hiding records. More catalogs provide capacity, **not redundant copies**. Back up every catalog and primary.

## Index and search

File identity derives from source chat and message ID. Re-indexing upserts the same record and preserves existing bot-specific delivery handles when session indexing supplies only source references. Parsed fields retain the original filename/caption alongside normalized title, year, language, quality and series metadata.

Search uses indexed fields, bounded candidate ranking, release filters and pagination. It does not load entire collections into RAM. Query sessions belong to the requester. Group scopes and membership rules limit visible sources before links are created; delivery checks source access again.

Sources default to members-only visibility. Owners explicitly publish a source when its media should become available to ordinary permitted bot users. Telegram membership checks require the bot's corresponding permissions.

## Delivery and jobs

Delivery uses a cached ID valid for the current bot or copies an accessible source message. Bans, source visibility, membership, entitlement and quotas are checked. Protected content is not bypassed. A new bot identity needs fresh delivery handles or still-accessible source messages.

Persisted jobs contain payloads, progress, retry times, attempts and expiring worker leases. Indexing advances its cursor after saving records. Lease ownership fences scheduler writes. Message-deletion jobs do not delete catalog records.

MongoDB writes and Telegram sends are separate operations: a send can succeed while its response is lost. Durable jobs support recovery but cannot guarantee exactly-once external delivery. Database snapshots cannot recreate Telegram media bytes after source/account loss.

## Interface and modules

The interface uses escaped Telegram HTML, bold headings, emoji and inline keyboards; no external font assets or copied branding. Shared storage, access checks and delivery paths support PM/group/inline workflows. Optional providers and web services are configured independently; see [commands](COMMANDS.md), [providers](EXTENSIONS.md), [community tools](COMMUNITY.md), [imports](IMPORT.md) and [web](WEB.md).
