# Optional services

Configure these only for the commands you use. Restart after changing the environment.

| Command | Configuration | Behavior |
|---|---|---|
| `/weekly` | `TMDB_API_KEY` | Ten movie and TV picks from TMDB’s weekly trending list. TMDB accepts a v3 API key or read access token. |
| `/imdb TITLE` | `OMDB_API_KEY` | Look up the first matching title, genre, year, rating and plot on OMDb. |
| `/kino TITLE` | `KINO_API_KEY` | Query the Kinopoisk Unofficial API by keyword. |
| `/serverrequest movie 123` | `SEERR_URL`, `SEERR_API_KEY` | Owner-only request to a Seerr-compatible server by TMDB ID. Use `tv` to request all seasons. |
| `/serverstatus ID` | Same Seerr instance and credentials | Shows the request and media states for an ID submitted by the same bot owner through this bridge. |
| `/drive NAME` | Google OAuth credentials, optional `DRIVE_ID` | Owner-only search over accessible Google Drive metadata. Set a shared drive ID to restrict the results. |
| `/forward SOURCE TARGET FIRST LAST` | Owner account, bot permissions in both chats | Forward an inclusive range from an active source. Each job handles up to 5,000 messages and persists its cursor. |
| `/app` | `WEB_PUBLIC_URL` and the web process | Opens the authenticated catalog from a private Telegram chat. See [web deployment](WEB.md). |

Provider commands run on demand. Core file search and Telegram delivery use the local catalog service independently of these providers. Movie metadata provided by TMDB. This product uses the TMDB API but is not endorsed or certified by TMDB.

## Media worker

Run transfers on a separate host with its own authorized session and sufficient scratch disk. The source must be active in the same MongoDB registry. Use a group or channel ID for each endpoint. The session must be able to post in the target. Protected source media cannot be transferred.

```sh
python -m wovie.media -1001111111111 27 -1002222222222 --work-dir /srv/wovie-scratch --name 'Movie.1080p.mkv' --max-mib 2048
```

Use `--mode thumbnail` for a JPEG or `--mode transcode` for H.264/AAC MP4. Both require FFmpeg. The regular `copy` mode re-uploads with the selected filename and caption. `--max-mib` caps each input and conversion output. Allow disk space for both files. Temporary files are removed on normal completion or failure. A forced process kill can leave a temporary directory for the operator to clear.

**Owner uploads.** In a private chat, reply to an audio, video or document with `/ingest SOURCE_ID`. The bot copies it into that registered source and indexes its Telegram reference. It needs posting rights in the target. Treat a network interruption during copying as an uncertain send and inspect the target before retrying.

**Translation.** With an HTTPS `LIBRETRANSLATE_URL` and optional `LIBRETRANSLATE_API_KEY`, owners can use `/translate en TEXT` to translate up to 1,000 characters.

Use `/digest on` or `/digest off` privately for one TMDB digest per UTC calendar week. Owners use `/announce CHANNEL_ID` for new-file announcements to a channel where the bot can post. Only explicitly public, unrestricted sources qualify. Use `/announce off` to stop.

**Drive OAuth.** Enable the Google Drive API for your OAuth client and obtain an offline refresh token for the account with access to the intended files. Request the `https://www.googleapis.com/auth/drive.metadata.readonly` scope. Set `DRIVE_CLIENT_ID`, `DRIVE_CLIENT_SECRET` and `DRIVE_REFRESH_TOKEN`. The bot caches the short-lived bearer in process memory, refreshes it before expiry and retries a rejected bearer once. For temporary access set only `DRIVE_ACCESS_TOKEN`. Search returns existing Google web links and preserves Drive permissions.

**Seerr.** A successful `/serverrequest` returns the ID to use with `/serverstatus`. After an interrupted submission, check the configured server’s request history before submitting again. The bot records the originating owner and server instance.

Kinopoisk search accepts a title and optional numeric filters such as `/kino year:2024 rating:7 genre:1 country:34 page:2`. Filter IDs follow the Kinopoisk Unofficial API directory. Use `/kinobudget FILM_ID` with the ID shown in results for available production budget and box-office figures.
