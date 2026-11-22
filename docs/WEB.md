# Telegram catalog and streaming

The optional Mini App runs in a separate web process. Members can search a selected catalog, open a file in their private bot chat, and play or save accessible media. Bot administrators can see catalog counts and hide an individual entry. The administration panel shows the 20 most recent web-hidden entries from accessible sources and restores them individually.

1. Deploy the bot, register the source chats and index their files. Make the bot an administrator in each web source so it can verify other users’ membership.
2. Use a dedicated HTTPS hostname, for example `https://catalog.example.com`. Set the following environment variables. Both the bot and web process need `WEB_PUBLIC_URL`. The web process uses the same bot token, admins and database configuration as the bot.
3. Start one web worker with `python -m wovie.web`. Run it as a separate service. Point your TLS reverse proxy at its listener. Open `/app` in a private Telegram conversation with the bot. A normal browser tab lacks Telegram’s signed login data.

```dotenv
WEB_PUBLIC_URL=https://catalog.example.com
WEB_HOST=127.0.0.1
WEB_PORT=8080
WEB_SOURCE_IDS=[-1001234567890,-1009876543210]
WEB_MAX_STREAMS=1
WEB_BYTES_PER_SECOND=2097152
```

Use 1–16 explicit source chat IDs. Each result requires current source membership, including sources marked public for bot delivery. The source account, bot and reader each need access. Search accepts the same year, language, quality, season and episode filters as the bot. “Get in Telegram” creates a one-hour private link bound to that reader.

**Enable playback.** Set `API_ID`, `API_HASH` and `WEB_SESSION_STRING` on the web worker. Generate a separate authorized Telethon session with `python scripts/create_session.py`. The streaming account must already belong to the source chats. Restart the worker after adding source chats. Without a streaming session readers can still search and open the file in Telegram.

The worker rechecks source membership, bot policy and Telegram content protection when a reader requests a byte range. Playback supports one HTTP range per request and browser-supported codecs. Other files download as attachments. Users subject to daily delivery limits retrieve files through Telegram. The default bandwidth ceiling is 2 MiB/s per active stream, with one stream running at a time. This worker consumes additional RAM and bandwidth. Deploy it separately from a constrained bot host.

A Caddy route for a private loopback listener:

```caddyfile
catalog.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

When using Docker, set `WEB_HOST=0.0.0.0` inside the web container and expose its port only to the proxy. Disable access logs for media URL paths at the proxy. Never cache API or media responses. The app validates a Telegram login up to five minutes old, then issues a secure one-hour browser cookie. Playback links expire after 15 minutes and require the original browser session. Reopen the file for another playback link or reopen `/app` after login expiry. The browser needs cookies enabled for this HTTPS origin.

**Owner uploads.** Set `WEB_UPLOAD_MIB=16` to enable uploads up to 16 MiB on the web worker. Default 0 disables uploads, and 48 MiB is the maximum. A smaller `MAX_FILE_SIZE` also applies. Choose a registered source in the administration panel, select one file, and confirm. The bot needs permission to post documents there. Each upload is indexed using the message returned by Telegram. Protected sources and enabled content-protection policies block upload.

Configure the reverse proxy to accept the selected size plus 16 KiB of multipart overhead. One upload can run at a time, with at least 32 MiB free beyond the declared request size. The worker streams into a private temporary directory and removes it at completion. Use an adequately sized `TMPDIR` on the web host. If the network drops after submission, check the source chat before starting a new upload. The same submission ID cannot resend the file.
