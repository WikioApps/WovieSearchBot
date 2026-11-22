# Upgrade and rebuild search metadata

MongoDB holds the durable data; replacing the Python release must not replace your databases. Startup validates the schema version, catalog identities and required indexes. A newer stored schema refuses an older application. Adding a catalog preserves existing source-to-catalog routing.

When a release changes its filename parser or search-token format, inspect the required backfill:

```sh
python -m wovie.migrate --plan
```

The default is a read-only plan: current parser/schema versions, each catalog ID, record count and records needing refresh. It creates no indexes or migration records. Use only initialized Wovie catalogs; legacy bots use [IMPORT.md](IMPORT.md).

Before applying an upgrade, stop all bot/web/worker writers and make a consistent encrypted export. Verify it, keep the previous compatible release, and keep a copy away from the VPS:

```sh
python -m wovie.backup export /secure/wovie-before-upgrade.wovie
python -m wovie.backup verify /secure/wovie-before-upgrade.wovie
python -m wovie.migrate --apply
```

Follow [RECOVERY.md](RECOVERY.md) for backup keys, restore prerequisites and offsite storage. Backups are an operational requirement for recovery, not a superficial command flag: the migrator cannot prove an operator's copy is independent or restorable. It therefore gives no false assurance from a `--backup-done` switch.

`--limit 10000` processes at most that many records during one invocation. Rerun `--apply` to resume. The primary database stores per-catalog/parser-version cursors and expiring leases. A forced termination may require waiting up to two minutes for the lease. Replayed writes are idempotent. A newly found old record behind a completed cursor starts another pass.

Only derived search metadata changes: parsed title/year/language/season/episode/quality, codec/extension, normalized title, transliteration and search tokens. Manual corrections and aliases remain authoritative. Bot-specific handles, alternate source mappings, original filenames/captions, permissions, catalog identities, creation timestamps and soft-deletion tombstones remain intact. Concurrent metadata changes are detected with version/timestamp checks; pausing writers gives the most predictable upgrade.

Higher parser/schema versions are rejected rather than downgraded. Unknown or corrupt correction data stops the migration for inspection. This is one practical metadata backfill, not a placeholder migration framework.

For broken old delivery handles, use authorized `/index` reconciliation or the [alternate-location workflow](CURATION.md). A replacement bot must reacquire handles from source messages it can access. Renaming a token, copying an old `file_id`, or editing a database cannot confer access to an unavailable Telegram source.
