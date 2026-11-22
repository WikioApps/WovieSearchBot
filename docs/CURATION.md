# Correct metadata without losing variants

Use these commands in an owner's private chat. Get catalog file IDs from `/export`. Corrections survive session scans, live edits, and legacy imports. Catalog files keep their original source chat/message identity.

| Command | Result |
|---|---|
| `/catalog FILE_ID` | Inspect effective metadata, title assignment, aliases and alternate locations. |
| `/correct FILE_ID title Correct Movie` | Override a parsed title. |
| `/correct FILE_ID aliases Other Name \| Local Title` | Add up to ten searchable aliases. |
| `/correct FILE_ID languages hindi \| english` | Set indexed language metadata. |
| `/correct FILE_ID year 2025` | Correct year; season, episode and quality are also supported. |
| `/uncorrect FILE_ID FIELD` | Remove that override and use the current filename/caption parser again. |
| `/title create Movie Name year:2025` | Create a canonical title and return its ID. |
| `/title assign TITLE_ID FILE_ID,FILE_ID` | Group up to twenty variants without deleting any file/location. |
| `/title merge FROM_ID INTO_ID confirm` | Queue a resumable merge of title assignments across catalogs. |
| `/alternate ORIGINAL_ID COPY_ID confirm` | Register an explicit alternate copy, up to five per original. |
| `/unalternate ORIGINAL_ID COPY_ID` | Remove an alternate mapping. |
| `/revoke_link TOKEN` | Permanently disable one saved start-link token. |
| `/repair_file FILE_ID` | Forward its authorized source message to your own private chat, extract a new Bot API handle, then clean up the temporary message. |
| `/duplicates FILE_ID` | Suggest up to ten accessible locations with the exact same Telegram `file_unique_id`; no automatic merge. |

To **split** an incorrectly grouped title, create a new title and assign only the selected file IDs to it. Quality, language, season, episode, source and file records remain separate. Assigning a title changes its canonical title/year metadata; it never combines or deletes media bytes.

Per-file corrections and their last ten audit entries are written atomically with a version check. Concurrent changes ask you to retry. Multi-file assignments are atomic per file, not globally across MongoDB servers; rerunning an interrupted assignment is safe. Merge jobs use a lease and repeatedly select remaining old-title assignments, so `/pause`, `/resume` and `/cancel` remain usable. Cancellation does not undo already moved assignments; split/reassign them deliberately. Soft-deleted variants are left untouched by merges.

Alternate registration checks both active sources through the normal owner delivery access path. Known Telegram unique IDs and file sizes must agree. If Telegram supplies no unique IDs, `confirm` is your assertion that both references represent the same file; filenames alone are not proof. Each alternate's membership, plan, source protection and group scope are rechecked at delivery. This mapping grants no new access and does not make Telegram copies an independent media backup.

Title merges change grouping only. Embedded audit history is bounded; keep versioned backups for recovery beyond those ten entries. Revoking a saved link prevents new use of that token, but cannot retract files already sent or jobs already authorized from it.

Handle repair is an explicit owner operation. The bot must access the original message, and the source must allow forwarding. Telegram's returned media type, known unique identity and size are checked before changing the stored handle. Each command has a persistent receipt: acknowledged forwards can resume catalog updates without sending again; ambiguous network outcomes never trigger an automatic resend. If the source changed, reindex it first. No user-session handle is converted into a bot handle.

The temporary forward is deleted best-effort; failed cleanup becomes a durable deletion job. Your private chat may retain that message if Telegram forbids deletion. Duplicate discovery uses exact unique IDs only, checks source access/protection, and leaves all locations/quality/language variants intact. Explicitly confirm a candidate with `/alternate ORIGINAL_ID COPY_ID confirm` after reviewing it.
