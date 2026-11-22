# Optional providers

Use these commands in a private chat. Set the relevant variables in the bot’s environment and restart it. Search and Telegram delivery continue to use the catalog when an optional provider is unavailable.

| Command | Setup | Result |
|---|---|---|
| `/showtimes [title]` | `SHOWTIMES_REGION=amstelveen` | Upcoming screenings from Schouwburg Amstelveen in the Netherlands. Reads the first 50 listings, caches them for 10 minutes and shows up to eight title matches in local cinema time. |
| `/trailer TITLE` | `TMDB_API_KEY` | Trailer for the first matching movie or TV series. Official YouTube trailers rank first. |
| `/wherewatch TITLE` | `TMDB_API_KEY`, `WATCH_REGION=IN` | Country-specific subscription, ad-supported, free, rental and purchase listings from JustWatch through TMDB. The default region is US. |
| `/aisearch TITLE` | `OPENAI_API_KEY`, `OPENAI_MODEL`, optional `AI_DAILY_REQUESTS=50` | OpenAI’s Responses API normalizes the title and can reorder up to eight matching releases. Choose an available model supporting structured JSON output. |
| `/transfer URL` | `REAL_DEBRID_TOKEN` | A bot owner submits an authorized HTTPS link to Real-Debrid and receives a private download URL. |
| `/transfer MAGNET_URI` | Same | Adds a torrent to the configured owner account. Use `/transferstatus ID` for progress and `/transferselect ID` to select all files when ready. |
| `/publish FILE_ID` | `PUBLISH_URL`, `PUBLISH_TOKEN` | A bot owner queues a metadata event for a public, accessible catalog entry. Follow progress with `/jobs`. |

AI search sends the entered query and, if ranking is needed, title/year pairs from public sources the user can access. MongoDB caches verified model responses for `AI_CACHE_SECONDS` (default 3,600 seconds). The cache uses `AI_CACHE_SLOTS` fixed slots (default 512, maximum 8,192) and expiry indexes. Its keys include the user, model, visible public sources, query and, for ranking, the current ordered candidates and their metadata. File permissions are checked again before issuing links.

`AI_DAILY_REQUESTS` caps uncached remote calls per UTC day. Each uncached normalization or ranking counts once. Each call has `AI_MAX_OUTPUT_TOKENS` (default 192, range 32–1,024), one 160-character query and up to eight 120-character candidate titles. Calls can incur charges even if the provider response is incomplete. Set your provider account’s budget and select a model within it. Catalog search still runs when the AI budget or provider is unavailable. The bot validates candidate numbers and creates delivery links bound to the requesting user. The remote provider receives no Telegram file IDs or session keys.

Transfers run on the owner’s external account and consume its quota. A receipt records the submission before the remote call. If its outcome is uncertain, check the provider’s transfer history before creating another submission. Download links may expire. Provider credentials belong in the environment.

The publishing receiver must accept an HTTPS POST with a bearer token. Return a successful status with an empty body or JSON. Honor the `Idempotency-Key` header because an interrupted request can be sent again. The event has `event: catalog.release`, `version: 1`, `catalog_id`, title, year, season, episode, quality, languages and file size. The worker checks source visibility again at send time.

Provider references: [OpenAI Responses](https://platform.openai.com/docs/api-reference/responses), [Real-Debrid REST API](https://api.real-debrid.com/), [TMDB watch providers](https://developer.themoviedb.org/reference/movie-watch-providers), [Schouwburg Amstelveen](https://schouwburgamstelveen.nl/).
