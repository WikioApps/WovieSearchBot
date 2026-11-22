# Optional community features

All bot-owner configuration commands run in private chat. Group moderation checks the caller's **current Telegram permissions** and the bot's permissions, and operates only on the group where the command was sent.

| Command | Behavior |
|---|---|
| `/modban` as a reply | Ban the replied-to user from this group; does not ban their whole bot account. |
| `/modunban USER_ID` | Lift a group ban; no forced rejoin. |
| `/mute MINUTES` as a reply | Restrict sending for 1–10080 minutes. |
| `/unmute` as a reply | Restore the group's current default permissions. |
| `/pin`, `/unpin` as a reply | Pin/unpin that single message. |
| `/joinmode CHAT_ID on` | Owner enables join-request autoapproval. Bot needs invite-user permission. Use `off` to disable. Banned users are excluded; other required-channel memberships must pass. |
| `/referral` | Get your persistent invitation link and credited count. |
| `/referralconfig DAYS` | Owner chooses 0–30 premium days for the inviter per newcomer. Default 0. Existing claims retain their original reward. |
| `/sponsor set LABEL \| HTTPS_URL` | Owner adds a clearly labeled sponsor link to `/menu`. `/sponsor off` disables it. |
| `/sponsor schedule START_ISO END_ISO` | Show the placement only within the chosen interval. Use timezone-aware timestamps such as `2026-12-01T00:00:00Z`; `/sponsor schedule clear` removes the interval. Applies whenever a menu is opened. |
| `/groupbroadcast on`, `/groupbroadcast off` | A current group administrator explicitly enables/disables owner announcements inside that group. The owner must also activate the group. |
| `/language en`, `/language hi`, `/language ta` | Save the language of the actionable `/menu`: English, Hindi or Tamil. Other bot responses remain English. |

Referral attribution is fixed once per account during its first five minutes in the bot. Self-referrals and banned accounts are excluded. A stored claim and receipt identity prevent credit duplication after retries; pending claims are reconciled after restart. Telegram account IDs do not prove that two accounts belong to different people. Keep rewards disabled unless that limitation is acceptable, and monitor attribution before enabling valuable rewards.

Sponsor URLs are displayed as links; the bot does not fetch them. Menu translations use Unicode text and Telegram's HTML formatting, with no font/image downloads. Group administrators cannot change source publication, global sponsorship, referral rewards or join policies.

An owner can reply to an announcement in private chat with `/broadcast groups` to see the eligible recipient count, then `/broadcast groups confirm` to queue it. `/broadcast users confirm` addresses users separately. Each job keeps its own recipient cursor; disabling the group's opt-in removes it from future selection. An already accepted Telegram send cannot be recalled.
