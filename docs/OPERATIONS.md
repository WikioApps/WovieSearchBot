# Operations and recovery

## Capacity and status

Inspect `/stats`, `/jobs`, `/health` and process logs. [Operator diagnostics](OPERATORS.md) includes inbox recovery and audit access. [Recovery tools](RECOVERY.md) adds verified offsite snapshots/retention and independent media archival. Monitor RSS, catalog/database size, connection limits, failed jobs, free temporary disk and backup age. Protect logs and exports. Database failure never causes a switch to RAM-only storage.

The small-host profile assumes external MongoDB, metadata-only indexing and one process. More catalogs need more connections/query work. Session scans, remote providers and streaming add load. Measure before choosing a hard RAM limit; a tiny VPS cannot store an unlimited catalog or local videos.

## Encrypted backups

Generate `BACKUP_KEY` once and store a separate recovery copy:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Archives include records from primary and all catalog databases, encrypted with authenticated encryption. They contain personal/access data; protect archives and keys. They exclude media bytes, `.env` and session strings; preserve required configuration separately through a secret manager.

For a consistent cross-database archive, **stop every bot/web/indexing writer first**. From the repository root, with intended Mongo/key environment variables or `.env`:

```bash
mkdir -p backups
.venv/bin/python -m wovie.backup export backups/wovie-2026-11-22.encrypted
.venv/bin/python -m wovie.backup verify backups/wovie-2026-11-22.encrypted
```

Choose a new filename each time. Restart services after export, upload to independent storage and retain recoverable versions. Keep local space for the archive. For systemd, provide the CLI with the same secrets as `/etc/wovie/wovie.env`; the service environment is not automatically inherited by a shell.

`/backup` is a **live logical copy**, not an atomic cross-database snapshot while writers are active. It sends encrypted data to `BACKUP_CHAT` or the requesting owner. `/export` sends catalog CSV. Both are capped below 50 MB for Telegram upload; use the offline CLI for larger archives. A Telegram backup chat shares Telegram failure risks: retain an independent destination too.

Example retention: 24 hourly, 7 daily, 4 weekly versions. Hourly backups can lose roughly an hour of changes after a host failure. Provider-native point-in-time recovery can complement exports. Replication and sharding do not replace versioned backups.

## Restore

1. Stop writers. Create fresh, dedicated **empty** destination databases.
2. Set destination `MONGO_URI` / `CATALOG_URIS` in archive slot order: primary first, then additional catalogs; retain the same slot count. Set the original `BACKUP_KEY`.
3. Run:

```bash
.venv/bin/python -m wovie.backup verify backups/wovie-2026-11-22.encrypted
.venv/bin/python -m wovie.backup restore backups/wovie-2026-11-22.encrypted --confirm-restore
.venv/bin/python -m wovie --check
```

4. Compare counts, sample searches, stable links and authorized delivery using the intended bot identity. Keep old databases intact until recovery is accepted.
5. Review `/jobs`: unfinished restored jobs and pending inbox updates are paused. Review old inbox commands through `/inbox` and use `/retryupdate SEQ confirm` only after checking prior effects; they are not automatically replayed. Resume only after inspecting cursors and prior external sends.

The CLI authenticates an archive before writing and refuses occupied destinations. Interrupted restores leave a lock; do not remove it to disguise a partial restore. Repeat into fresh empty databases. Regular restore drills establish recoverability beyond successful uploads.

## Updates and moves

Stop services, take a consistent backup, update code/dependencies, run `--check`, start and verify a sample flow. Preserve a previous release and backup. An older binary may not understand a newer schema; data rollback means restoring into fresh destinations, not deleting migrations.

Keep the primary and registered catalog identities. Add capacity deliberately; never remove a URI while it owns sources. A host move must preserve bot identity, database mappings, session credentials, link state and backup key.

## Troubleshooting

| Symptom | Action |
|---|---|
| MongoDB unavailable/index permission denied | Check URI, TLS, provider allowlist and privileges. Fix storage before restarting repeatedly. |
| Scan cannot access private source | Confirm account membership, valid Telethon session and matching API credentials. |
| Search succeeds, delivery fails | Check bot source access, protected content, deleted messages and cached IDs belonging to another bot. |
| Group ignores messages | Activate group; check privacy/admin permissions and source scope. |
| Membership verification fails | Give the bot administrator access in required/source chats; verify IDs. |
| Flood/rate errors | Allow persisted retries; reduce traffic instead of repeatedly adding jobs. |
| Paused jobs after restore | Inspect cursor/outcomes and resume deliberately. |
| OOM/disk-full | Inspect usage; raise limits or separate optional services. Preserve databases and clean only disposable work files. |
| Bot API polling conflict | Stop another worker/webhook deployment using the same token. |

Telegram's Bot API update retention is limited; long outages may need history reconciliation. Deleting delivered messages does not remove source media. If media must survive loss of Telegram access, keep independent media copies on sufficiently large storage.

Set `CATALOG_MAX_MIB` below the provider quota to refuse new file registrations in a database after its allocated collection and index storage reaches that threshold. The bot samples `dbStats` at most every ten seconds per active database. Keep headroom for that interval, replica overhead and ordinary account/job writes. Existing records remain accessible. Resume a failed index or use `/retryupdate` for affected updates after increasing capacity. Telegram export jobs also reserve 16 MiB of local temporary disk.

The packaged Python 3.13 image also completed startup, polling, seven commands and graceful shutdown with real MongoDB and a simulated Telegram Bot API. The worker reached 181,680 KiB peak RSS (177.4 MiB). The image was about 186 MiB uncompressed at this revision. The build host's Docker daemon reported no memory-controller support, so its requested 192 MiB container ceiling was not enforced. These measurements exclude a full guest OS, a live session scan, concurrent streaming and provider traffic.
