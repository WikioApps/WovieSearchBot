# Advanced search

Use `/search QUERY`, ordinary private-chat text, authorized group messages or inline mode. The same query parser powers the optional web, desktop and public Discord interfaces. Source permissions apply before results; Telegram rechecks them before delivery.

```text
Dune year:2021 lang:english quality:1080p
Kaadhal s01e03 codec:hevc type:mkv minsize:500MiB maxsize:2GB
"Blade Runner 2049"
```

| Filter | Accepted values / meaning |
|---|---|
| `phrase:` | Strict contiguous canonical-title phrase, e.g. `phrase:"lord of the rings"`. Original script and stored ASCII title forms are supported. |
| `year:` | 1888–2100. A trailing unquoted year also works: `Avatar 2009`. |
| `lang:` / `language:` | Full language or aliases: `eng`, `hin`, `tam`, `tel`, `mal`, `kan`; common native names such as `हिन्दी`, `தமிழ்`, `తెలుగు`. |
| `quality:` / `q:` | `360p`, `480p`, `720p`, `1080p`, `2160p`; `4k`/`uhd` → 2160p, `fhd` → 1080p, `hd` → 720p. |
| `s:` / `season:` | Season number, including 0 for specials. |
| `e:` / `episode:` | Episode number. Combined `S01E03` and `1x03` also work. |
| `codec:` | `h264`/`x264`/`avc`, `h265`/`x265`/`hevc`, `av1`, `vp9`, `vp8`, `mpeg4`. |
| `type:` | Telegram media kind: `document`, `video`, `audio`; or extension: `mkv`, `mp4`, `avi`, `mov`, `webm`, `m4v`, `ts`, `mp3`, `m4a`, `flac`, `wav`, `pdf`, `zip`, `rar`. |
| `minsize:` / `maxsize:` | Inclusive file-size limits, e.g. `750MB`, `1.5GB`, `512MiB`, `2GiB`, or a byte count. KB/MB/GB/TB are decimal; KiB/MiB/GiB/TiB are binary. |

A video uploaded to Telegram as a document matches `type:document`; use `type:mkv` or `type:mp4` when the container matters. Filters cannot infer metadata absent from the filename, caption or a manual correction. Sizes use recorded Telegram file sizes; indexing does not download the file.

## Titles, aliases and scripts

Unicode titles retain their original script and combining marks. Latin accents, letter case and punctuation are normalized. New indexing also saves deterministic ASCII transliterations using `anyascii`. Original-script exact titles rank first, ASCII exact titles next, then explicitly quoted phrase matches, title prefixes and token matches; ties use creation time and stable record ID.

Transliteration is mechanical, not translation or a universal pronunciation dictionary. Indian-language spellings often differ from the generated Latin form. Add familiar English/transliterated spellings as aliases through the curation workflow; `Kaadhal`, for example, can be an explicit alias for a Tamil title. Aliases remain searchable through reindexing.

Quote numeric titles such as `"Blade Runner 2049"` to stop the trailing number becoming a year filter. Quotes group title text and promote contiguous phrase matches; they do **not** exclude reordered token matches. Use `phrase:"lord of the rings"` for strict phrase filtering on the canonical title. The parser preserves both strict and ranking phrases in saved search filters, so pagination keeps the original meaning. Search requires the query's words and prefers exact titles. A leading number in `2001 A Space Odyssey` remains part of the title.

## Bounds and fallback

Queries are limited to 300 characters; normal matching uses up to 12 title tokens. Telegram searches save up to 120 ordered result IDs in the requester's expiring search session. Buttons paginate that fixed list using `PAGE_SIZE` (8 by default); new indexing does not shift the saved IDs. Every page and delivery rechecks access and availability; deleted or newly restricted items can leave a shorter page. A capped result set asks you to refine the query. Direct database searches used by operator interfaces support up to 51 pages and can reflect newer indexing.

MongoDB results merge incrementally across catalogs, keeping one current row plus a small cursor batch per catalog. Search projections exclude captions, token arrays and correction history; skipped deep-page rows are not accumulated in application RAM.

If token search finds nothing, spelling fallback checks at most 80 candidates per catalog selected by literal word prefixes, then compares title similarity. This keeps memory bounded; it may miss large spelling changes or changes at the beginning of every word. Use `/suggest title` for up to five existing searchable title suggestions from your visible sources. Suggestions inspect at most 80 projected candidates per catalog using literal prefixes and similarity; they focus on titles rather than carrying over release filters. Manual aliases give predictable results for large spelling differences. User text is never executed or used as an unrestricted regular expression.

## Existing data

New fields (`title_ascii`, `codec`, `extension`) and token enrichment are optional, backward-compatible metadata; database schema version remains 1. Startup builds MongoDB indexes but does not rewrite the whole catalog. Existing records retain original-script/token search. Codec and extension filters can use bounded-time filename/caption fallback when the new fields are absent. Reindex old sources to add transliteration tokens and faster structured release fields; stable IDs, saved links and curation overrides are retained. Large remote catalogs may take time to build their new indexes on first startup.
