# Commands and workflows

Send commands in a private bot chat unless a row says otherwise. IDs mean numeric Telegram chat/user IDs, catalog IDs returned by this bot, or the displayed job/request ID—not a bot file ID copied from another project.

## Search and personal tools

| Command/action | Use |
|---|---|
| `/start`, `/help` | Open the menu and usage guide. Start the bot privately before receiving files. |
| Plain title, `/search TITLE`, `/find TITLE` | Search accessible catalog records; ordinary group messages work only in enabled groups. |
| `/recent` | Browse recent accessible records. |
| Result buttons | File delivery, favorite toggle, requester-owned pages, title subscription and selected-page batch delivery. |
| `@BotUsername TITLE` | Inline search after enabling inline mode in BotFather. Delivery access is checked again. |
| `/favorites` | View saved files; remove with the supplied buttons. |
| `/watch TITLE`, `/subscribe TITLE` | Subscribe to newly indexed matching titles. |
| `/watchlist`, `/subscriptions` | List saved subscriptions and their IDs. |
| `/unwatch ID_OR_TITLE`, `/unsubscribe ID_OR_TITLE` | Remove a subscription. |
| `/request TITLE` | Save a missing-title request. |
| `/myrequests` | View your own saved requests and status. |
| `/movie TITLE`, `/tmdb TITLE` | Movie/TV metadata when TMDB is configured. |
| `/history`, `/clearhistory` | View recent searches or delete your search history. |
| `/premium`, `/buy` | View entitlement or buy a configured Telegram Stars plan. |
| `/redeem CODE` | Claim a valid voucher. |
| `/terms`, `/paysupport` | Payment terms/support destination. |

Search sessions last two hours; ordinary result links last one day. Owner-created `/batch` links are persistent. Private inline results use requester-bound deep links. Direct cached inline media requires an explicitly public inline source and compatible access settings.

For full size/codec/type/transliteration rules, see [SEARCH.md](SEARCH.md).

Query examples:

```text
Dune year:2021 lang:english quality:1080p
Breaking Bad s:2 e:3
"The Last of Us" S01E02
Leo lang:tam q:720p codec:hevc type:mkv maxsize:2GiB
```

Aliases: `language:`/`lang:`, `quality:`/`q:`, `season:`/`s:`, `episode:`/`e:`. Quality accepts 360p, 480p, 720p, 1080p, 2160p and 4k/uhd/fhd/hd aliases. Normalization preserves non-Latin scripts. Filters depend on metadata parsed from filenames/captions; they cannot recover missing or mislabeled release information automatically.

## Owner: sources, indexing and jobs

| Command | Behavior |
|---|---|
| `/admin` | Administration menu. |
| `/source list [PAGE]` | List registered sources and visibility. |
| `/source add CHAT_ID [public]` | Enable source; default members-only. `public` permits catalog access for ordinary authorized bot users. |
| `/source remove CHAT_ID` | Disable without erasing catalog records. |
| `/source publish CHAT_ID`, `/source unpublish CHAT_ID` | Change source visibility without deleting it. |
| `/index CHAT_ID [START_ID [END_ID]]` | Queue history scan using the authorized session. Explicit bounds are inclusive; omitted end snapshots current latest message. |
| `/jobs [PAGE]` | Job states, IDs and cursors. |
| `/pause JOB_ID`, `/resume JOB_ID`, `/cancel JOB_ID` | Control persisted work. A send already accepted by Telegram can still finish. |
| `/stats` | Catalog/application counts and scheduler state. |

An active source and valid session access are needed for history. Bot access is separately required for source-copy delivery. No command bypasses private-chat membership or protected-content restrictions.

## Owner/group administration

| Command | Behavior |
|---|---|
| `/group add GROUP_ID`, `/group remove GROUP_ID` | Enable/disable group search; owner-only. |
| `/settings [CHAT_ID]` | Inspect settings for current/specified chat. |
| `/set [CHAT_ID] KEY VALUE` | Change one permitted setting. |
| `/filter [global] KEYWORD \| RESPONSE` | Save an exact-keyword text reply. |
| Reply with `/filter KEYWORD` | Save the replied message as a reusable filter response. |
| `/filters [global] [PAGE]` | List current/global filters. |
| `/delfilter [global] KEYWORD` | Remove a filter; retain its last copy in database trash/backups. |

Owners activate groups, select sources and manage access policy. Current Telegram group administrators may edit only their group's display settings and local manual filters; they cannot publish sources or configure other groups. Global filters are owner-only. Local filters take precedence over automatic title search.

Settings:

| Key | Value |
|---|---|
| `enabled` | `true` / `false` |
| `source_ids` | Comma-separated active source IDs; `none` clears the explicit scope. |
| `required_channels` | Comma-separated negative chat IDs; `none` clears chat-level requirements. Global requirements still apply. |
| `premium_only` | `true` / `false` |
| `daily_limit` | Global delivery policy: `/set 0 daily_limit NUMBER`; `0` disables it. |
| `delete_after` | Seconds, 0–172799; `0` disables delayed deletion. |
| `protect_content` | `true` / `false` |
| `caption_template` | Up to 800 characters; placeholders `{filename}`/`{file_name}`, `{title}`, `{size}`, `{year}`, `{quality}`, `{language}`. |
| `welcome_text` | Up to 800 characters. |

Example group setup:

```text
/group add -1002222222222
/set -1002222222222 source_ids -1001234567890,-1009876543210
/set -1002222222222 protect_content true
```

## Owner: catalog, people and recovery

| Command | Behavior |
|---|---|
| `/batch FILE_ID FILE_ID …` | Create a persistent bundle link, up to 100 IDs. Access is checked at delivery. |
| `/delete FILE_ID` | Soft-delete a catalog record; source media remains untouched. |
| `/restore FILE_ID` | Restore a soft-deleted record. |
| `/purge FILE_ID` | Preview permanent deletion; repeat with `confirm` to execute. Backups are needed afterward. |
| Reply with `/broadcast` or `/broadcast groups` | Preview recipients; append `confirm` to queue users or explicitly opted-in enabled groups. |
| `/ban USER_ID`, `/unban USER_ID` | Toggle bot access. |
| `/grant USER_ID DAYS`, `/revoke USER_ID` | Extend or revoke premium entitlement. |
| `/redeemcode CODE DAYS [USES] [EXPIRES_DAYS]` | Create a bounded-use voucher; use `auto` instead of CODE to generate one. |
| `/requests [PAGE]`, `/requestdone REQUEST_ID` | Review pending requests and mark fulfilled/notify requester. |
| `/export` | Queue catalog CSV export. |
| `/backup` | Queue encrypted live logical database export; requires `BACKUP_KEY`. |

Most owner commands require a private chat. For consistent backups, restore procedures and export limits, see [OPERATIONS.md](OPERATIONS.md). Additional integrations have their own concise guides: [basic providers](EXTENSIONS.md), [provider workflows](PROVIDERS.md), [community](COMMUNITY.md), [imports](IMPORT.md), [web](WEB.md).

Further workflows: [series/discovery](DISCOVERY.md), [corrections/alternate files/link revocation](CURATION.md), [operator diagnostics/verification](OPERATORS.md), [offsite and media recovery](RECOVERY.md), [desktop/Discord](INTERFACES.md).
