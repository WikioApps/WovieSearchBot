# Operations and optional verification

Run these commands as a configured owner in private chat:

| Command | Result |
|---|---|
| `/health` | Current process RSS on Linux, filesystem headroom, uptime, Mongo ping, queue counts and last completed backup-job age. External Mongo disk/RAM are separate. |
| `/inbox [PAGE]` | Failed or paused updates retained by the durable inbox; no raw update contents. |
| `/retryupdate SEQ confirm` | Requeue one retained update after repairing its cause. Telegram sends can repeat if the previous outcome was uncertain. |
| `/audit [PAGE]` | Recent operation receipts with actor, chat, command and handler outcome. |
| `/audit export` | Download at most the latest 500 receipts as JSON Lines. No command arguments, tokens or session strings. |

Owners can assign `/operator add USER_ID` or remove `/operator remove USER_ID`. Operators may read only `/health`, `/stats` and `/jobs` in private chat. They cannot mutate sources, billing, users, groups, catalog data, inbox state, backups or role assignments. A ban also disables an operator's diagnostic access. Bot owners remain configured through `ADMINS`.

## Batch queue admission

An owner can enable `/set 0 batch_admission_enabled true`. The default is off. When enabled, each user may have at most **three outstanding delivery batches**. Queued, running, paused and failed jobs occupy a slot. A new request receives a clear busy response when all slots are occupied; no existing job is dropped. Completed or cancelled jobs release their slots automatically. Inspect `/jobs` and resume or cancel failed/paused work deliberately.

Admission and job creation share one MongoDB document insertion protected by a unique partial index, including on standalone MongoDB. Replaying an already accepted request reuses its original job ID and does not reserve another slot. Work accepted before enabling the limit is preserved; that user must finish or deliberately cancel their earlier unbounded batches before starting new ones. Enabling the limit does not discard an existing backlog. This is a per-user bound, not a global traffic guarantee. Keep search rate limits and delivery quotas appropriate to the host's capacity.

`handler_returned` means a command handler returned, including a permission-denied response. It is not proof of a successful mutation. Detailed mutation records appear alongside these receipts where available. Keep audit access limited to owners and include the primary database in backups. Backup-job completion alone does not prove restore readiness; perform the recovery guide's restore drill.

## Timed verification links

Optional environment settings:

```dotenv
VERIFICATION_REQUIRED=false
SHORTENER_API_URL=https://your-provider.example/api
SHORTENER_API_KEY=provider-key
VERIFICATION_TTL=43200
VERIFICATION_MIN_SECONDS=10
```

Enable only after configuring your own provider. Its HTTPS GET endpoint must accept `api` and `url` parameters and return JSON with `shortenedUrl`, `shortened_url` or `short_url`. Responses are bounded to 16 KiB and 12 seconds. Redirects from the API endpoint are not followed. The returned short link must be HTTPS and cannot expose the direct redemption token.

Users run `/verify`, follow the short link and return to the same bot account. Challenge records store only a hash of the random token. It is bound to the user, expires after 15 minutes and cannot grant extra time by replaying it. The configured minimum delay applies before redemption. Owners and active premium users bypass this optional gate. File access and required-channel membership checks still apply.

The bot confirms possession of the return link after the minimum delay. It cannot prove that an advertisement was watched or that a provider's external steps were completed. Use provider-signed completion callbacks if that stronger guarantee is required. Provider outages fail closed; the bot never reveals the direct verification destination as a fallback. Store provider credentials only in environment configuration and avoid proxy logging of API query strings.
