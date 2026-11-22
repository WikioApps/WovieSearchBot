# Discovery and series

| Command | Use |
|---|---|
| `/series Dark` | Pick a season, then an episode or its available variants. |
| `/variants Dune year:2024` | Up to 20 matching file variants, with private delivery links. |
| `/season Dark season:2 language:english quality:1080p` | Review and confirm a resumable private batch. |
| `/trending` | Up to three current TMDB cards. |
| `/pick` or `/pick comedy` | A random trend/search result excluding your recorded watched titles. |
| `/watched TITLE` | Save watched status; no title lists are exposed in groups. |
| `/watched` / `/watched remove TITLE` | List or remove watched records. |
| `/resultschannel GROUP_ID CHANNEL_ID` | Owner: route eligible group searches to a channel. Use `off` to disable. |

Series summaries inspect at most 100 matching files. Season batches refuse more than 100 candidates: add language or quality filters. One variant per episode is selected, preferring 1080p when quality is unspecified. An indexed season may be incomplete; confirmation states the actual file count. Delivery resumes through the durable job queue and checks access for every file.

Controls belong to the requester and expire after 30 minutes. Search links expire after one day; explicit administrator-created permanent links retain their configured lifetime. Private-source filenames are shown only in private chats or their own source group. Other group results require the owner to publish the source.

Results-channel routing requires an enabled group, an explicitly configured destination, and requester membership in that channel. Only published-source results go there. Otherwise normal scoped group results remain available. The bot must be a channel administrator with posting permission.

TMDB features need `TMDB_API_KEY`. Cards may show posters, genres, cast and trailers when the provider supplies them. Metadata discovery does not imply downloadable files are in your catalog. Catalog search remains available during provider outages.

Bot `/search` and `/recent` save up to 120 ordered result IDs, filters, source scope and page size for two hours. New indexing does not move those results between pages. Deleted or newly inaccessible files are hidden without shifting later pages; empty pages retain navigation. A capped search asks you to refine its title or filters. Each displayed file and each delivery is checked against current access rules. A new search creates a fresh snapshot.

`/browse [language:english quality:1080p year:2024]` shows distinct accessible titles from a bounded recent catalog window. `/suggest TITLE` returns up to five nearby titles from the same permitted sources. `/popular` ranks only opted-in successful deliveries of currently published public files; it never reads query history or exposes user identities/counts. Enable with owner command `/set 0 popularity_enabled true`. Only deliveries after opt-in count; each file needs at least three successes. Ranking uses the latest 10,000 eligible receipts within 30 days. Turning the setting off hides the ranking and stops new counting.
