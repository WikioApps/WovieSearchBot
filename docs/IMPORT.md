# Import an existing MongoDB catalog

Use this for catalogs with original Telegram **source chat and message IDs**. Register each source with `/source add CHAT_ID` first. Records without this provenance must be reindexed from accessible Telegram history using `/index`; the importer does not invent missing IDs.

The old connection is read-only: give it a MongoDB account with only `read` access. Wovie's configured `MONGO_URI` is the destination. Keep credentials in environment variables, never command arguments or Git.

```sh
export IMPORT_MONGO_URI='mongodb+srv://READ_ONLY_CONNECTION'
python -m wovie.importer --source-db oldbot --collection files --mapping mapping.json
python -m wovie.importer --source-db oldbot --collection files --mapping mapping.json --job my-migration --apply
```

Default preview checks up to 1,000 rows and performs **no writes to either database**. `--limit N` bounds either mode; apply without a limit scans the collection. Output reports valid/rejected rows, discarded file handles, and up to ten rejection samples. Apply saves every rejected row's ID/reason in the destination's `import_rejections` collection.

Mapping example; omitted fields use the same names as Wovie:

```json
{
  "source_chat": "chat_id",
  "message_id": "msg_id",
  "filename": "file_name",
  "caption": "caption",
  "file_size": "file_size",
  "file_id": "file_id",
  "file_unique_id": "file_unique_id",
  "bot_id": "bot_id"
}
```

Nested paths such as `source.chat_id` work. Supported target fields are `source_chat`, `message_id`, `filename`, `caption`, `file_size`, `media_type`, `file_id`, `file_unique_id`, `bot_id`, and `aliases`. Media type defaults to `document`; allowed types are `document`, `video`, and `audio`. Titles, languages, seasons, episodes, quality and search tokens are rebuilt by the current parser. Aliases must be an array of strings.

| Option | Use |
|---|---|
| `--source-chat -1001234567890` | Supply a known source only when the old row lacks one. Never use one channel ID for a collection containing multiple sources. |
| `--legacy-bot-id 123456789` | Confirm the old bot identity when rows omit it. Handles are retained only if it matches the current bot. |
| `--job my-migration` | Resume the same source, mapping and bot identity after interruption. |
| `--limit 10000` | Stop after this many rows in the current invocation; resume with the same job. |

The importer checkpoints after each successful target upsert or recorded rejection. A crash before checkpoint may replay a row; deterministic source/message IDs prevent duplicate catalog rows. A lease prevents two processes from advancing the same import concurrently. After a forced termination, wait up to two minutes for its lease to expire.

Source `_id` values must have one homogeneous BSON type: ObjectId, string, int, or long. Stop writes to the old catalog during migration: this is a resumable ordered scan, not a consistent snapshot of a changing database. An already completed job does not rescan later additions; use a new job name.

Old handles with unknown/different bot identity are discarded; original source references remain for Telegram-side copying. Same-bot handles still require live validation, particularly when migrating from another Telegram client library. Never assume a Pyrogram-encoded handle is a valid Bot API handle. Token rotation for the same bot is different from creating a new bot.

After fixing rejected rows or changing a mapping, choose a **new job name** and rerun. Existing valid records are upserted; soft-deleted Wovie records remain deleted. The tool does not migrate users, payment entitlements, roles or executable configuration. Use the encrypted Wovie backup/restore workflow for a complete Wovie-to-Wovie migration.
