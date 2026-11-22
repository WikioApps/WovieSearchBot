# Recover the database and media

**Database recovery.** The encrypted BSON archive covers the primary application database and every configured catalog. For an exact logical snapshot, pause all writers, including the bot, web worker, and any importers. MongoDB service snapshots or point-in-time recovery provide stronger guarantees while writers stay active. Keep a separately protected copy of your environment, keys and deployment configuration.

**Hourly offsite copies.** Install rclone on a host with enough temporary space for an encrypted archive, configure a remote under the service account, and set `SNAPSHOT_REMOTE=vault:wovie/db`. The host needs database credentials, `BACKUP_KEY`, and access to every catalog. `RCLONE_CONFIG` can point to the service account’s credential file. Keep the encryption key outside the backup destination.

```bash
python -m wovie.snapshot upload
sudo install -m 644 deploy/wovie-snapshot.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wovie-snapshot.timer
systemctl list-timers wovie-snapshot.timer
journalctl -u wovie-snapshot.service -n 20
```

The timer creates a live logical copy. The job verifies the encrypted archive, uploads it, downloads the remote bytes for a SHA-256 check, then uploads a checksum marker. Alert on overdue markers and failed timer runs. Allow roughly two transfers of the archive per run. Large databases should use a separate snapshot host.

Preview retention with `python -m wovie.snapshot retain --days 35`. Add `--apply` to remove eligible archives and markers. Retention preserves the three newest marked copies. Use independent remote versioning or immutability for additional recovery after accidental deletion.

**Restore drill.** Download an archive and its matching `.sha256` file with `rclone copyto`. Run `sha256sum -c NAME.wbak.sha256` followed by `python -m wovie.backup verify NAME.wbak`. Configure an isolated environment with the same number of fresh databases in archive order and the original backup key. Run `python -m wovie.backup restore NAME.wbak --confirm-restore`. Compare source, file, link, entitlement and job counts, representative searches and authorized sample deliveries before switching hosts. Keep the original bot stopped when activating a replacement with the same token. Check paused jobs and inbox updates for already completed external effects. Archive freshness determines the maximum gap after host loss.

**Automated staging drill.** Put an independent staging cluster URI in `DRILL_MONGO_URI` and supply `BACKUP_KEY`. The staging account needs create, index, write, read and drop privileges for randomly named `wovie_drill_*` databases. Do not give this account access to production data. The tool rejects matching configured production endpoints and never falls back to `MONGO_URI`.

```sh
python -m wovie.snapshot drill /srv/recovery/latest-verified.wbak
```

The command authenticates the archive, restores each slot into its own fresh database, checks every collection’s document count against authenticated archive records, rebuilds the production indexes, samples file IDs and source routes, and checks that unfinished jobs remain paused. It returns JSON totals and a nonzero exit status on failure. It issues no Telegram API calls. Cleanup requires this run’s random database name and ownership marker. A forced process kill can leave identifiable staging databases; inspect before removing them.

For a periodic drill, refresh a local copy of the latest verified offsite archive first. Run the command weekly with the deployment virtual environment, for example with a systemd oneshot service using the same `WorkingDirectory` and environment file as the snapshot service, `ExecStart=/opt/wovie/.venv/bin/python -m wovie.snapshot drill /srv/recovery/latest-verified.wbak` and a timer with `OnCalendar=weekly` and `Persistent=true`. Alert on service failure and stale archive age. To exercise configuration and actual delivery too, schedule a separate supervised cutover drill using your staging Telegram account.

**Independent media copies.** Use a separate recovery machine and an independently backed POSIX volume with working locks, fsync and atomic rename. Media files are stored unencrypted by the tool. Restrict volume access, use encrypted storage and keep offsite volume snapshots. Set `ARCHIVE_SESSION_STRING` to a separate Telethon user session, plus `API_ID`, `API_HASH`, `MONGO_URI` and `MONGO_DB`. The account needs legitimate source access. Keep bot, web and archive sessions separate.

```bash
python -m wovie.archive_media sync /mnt/recovery/wovie --source -1001234567890
python -m wovie.archive_media verify /mnt/recovery/wovie
```

Repeat `--source` for additional registered sources. Each run advances through new messages. Use `--rescan` to revisit older posts and retain edited document versions. Protected chats and messages are excluded. The archive streams each Telegram document to a temporary file, checkpoints every 8 MiB, validates the saved prefix on restart, and stores the completed object by SHA-256. Insufficient free space stops the run before the next file. The default free-space reserve is 128 MiB; use `--reserve-mib N` to change it. Run a single writer per archive volume. Verify the archive and snapshot it while idle.

The SQLite manifest maps source chat, message and Telegram document ID to the original filename, byte count and digest. To recover a file, look up its row with `sqlite3 /mnt/recovery/wovie/manifest.sqlite 'SELECT chat,msg,name,sha256 FROM media LIMIT 10'`. Copy `objects/FIRST_TWO_DIGEST_CHARACTERS/FULL_DIGEST` to the intended filename. To restore Telegram availability, upload the recovered file to an authorized source and index the new message. Database restoration alone cannot recreate a deleted Telegram file.
