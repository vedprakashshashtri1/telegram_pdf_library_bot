# Telegram PDF Library Multi-Bot

A Streamlit-hosted Telegram bot manager for a searchable PDF catalog stored in MongoDB, with a separate Windows/Linux Super Indexer for fast historical channel indexing.

## Included features

- Multiple Telegram bots in one Streamlit process.
- Bot name, username and bot ID are fetched automatically from each token with Telegram `getMe`.
- `/add_bot <token>` starts an additional bot without a redeploy.
- Added bot tokens are encrypted before MongoDB storage using `APP_SECRET`.
- Multiple global Owners.
- Per-bot Super, Admin, Premium and Free roles.
- Role inheritance: `Owner > Super > Admin > Premium > Free`.
- Users are stored with both `bot_id` and `user_id`, so each bot has its own audience.
- Owner-only broadcast with preview, Approve/Deny and live counters.
- Broadcast supports command text or replying to a normal Telegram message, sticker, GIF, media or file.
- Telegram flood-waits and transient network errors are retried.
- `/restart` restarts the selected Telegram bot application inside the running Streamlit process.
- `/ping` and `/uptime`.
- Any ordinary private message or unknown command tells the user to open the website from the menu.
- Telegram menu button is set automatically to `WEBSITE_URL`.
- Bot-visible messages do not explain the storage/indexing/hosting implementation.
- Shared PDF catalog with title + cleaned description search fields.
- Removes `@usernames` and Telegram links from indexed title/caption text.
- Does not index text-only posts or non-PDF media.
- Live indexing of new/edited PDF channel posts.
- Resumable Telethon Super Indexer for historical posts.
- Free daily quota and unlimited Premium access.
- Forced required channels and join-request channels.
- Referral rewards with `/set_refer N`.
- Delivered PDF deletion after 5 minutes by default.

## Roles

| Role | Access |
|---|---|
| Free | Normal commands, daily file quota |
| Premium | Free + unlimited file delivery |
| Admin | Premium + Premium-user management |
| Super | Admin + forced-channel management + quota/referral settings |
| Owner | Everything + owners, bots, broadcasts, restart, Admin/Super management |

Owners are global across all registered bots. Admin/Super/Premium settings are scoped to the current bot.

## Commands

### Everyone

```text
/start
/help
/refer
/ping
/uptime
```

### Admin, Super and Owner

```text
/add_premium <telegram_id> <days>
/rem_premium <telegram_id>
/check_premium <telegram_id>
/list_premium
```

### Super and Owner

```text
/add_channel <channel_id|@username>
/rem_channel <channel_id|@username>
/list_channel
/add_request <channel_id|@username>
/rem_request <channel_id|@username>
/list_request
/set_free <number>
/set_refer <number>
```

Example:

```text
/set_refer 2
```

means 2 completed referrals = 1 Premium day, so 6 completed unclaimed referrals = 3 Premium days.

### Owner only

```text
/add_admin <telegram_id>
/rem_admin <telegram_id>
/list_admin

/add_super <telegram_id>
/rem_super <telegram_id>
/list_super

/add_owner <telegram_id>
/rem_owner <telegram_id>
/list_owner
/list_owner <telegram_id>

/add_bot <bot_token>
/list_bot
/rem_bot <bot_id|@username>
/restart
/restart <bot_id|@username>

/broadcast <message>
```

You can also reply to a Telegram message/media/file/sticker/GIF with:

```text
/broadcast
```

The bot shows a preview and then:

```text
[ Approve ] [ Deny ]
```

After approval, the tracking message updates like:

```text
Total Users: 1000
Sent to: 420
Left: 568
Failed: 12
```

Only users associated with the current `bot_id` receive that broadcast.

## Multiple owners

`OWNER_IDS` is only the initial bootstrap list when the `owners` collection is empty.

After that, these commands are authoritative and persist through restarts:

```text
/add_owner
/rem_owner
/list_owner
```

The final remaining Owner cannot be removed.

## Multiple bots

Set the first bot using `BOT_TOKEN` in Streamlit Secrets.

Then an Owner can run:

```text
/add_bot 123456789:AA...
```

The system validates the token and automatically fetches:

- current bot name
- current `@username`
- bot ID

The token itself is never displayed back to users and is encrypted in MongoDB using `APP_SECRET`.

Changing `APP_SECRET` later will make previously stored bot tokens unreadable. Keep it stable and private.

`/rem_bot` removes the bot registration but intentionally leaves its historical bot-user relationships and related records in MongoDB, so re-adding the same bot can reuse its existing audience/history.

## `/restart` on Streamlit

`/restart` works by gracefully stopping and starting the selected Telegram polling application **inside the current Streamlit Python process**.

It does not reboot Streamlit Community Cloud itself or force a new container. If Streamlit suspends or replaces the process, all Telegram bot applications stop with that process and restart when the Streamlit app starts again.

Examples:

```text
/restart
```

restarts the bot where the command was sent.

```text
/restart 123456789
```

restarts another registered bot.

## Website menu behavior

Set:

```env
WEBSITE_URL=https://your-site.vercel.app
```

Every bot automatically configures its Telegram menu button as **Open Website**.

If a user sends ordinary text, media, a sticker, file or unknown command, the bot tells them to use the website/menu instead of exposing any backend implementation details.

## PDF links

The PDF record stores an obfuscated start token whose digits correspond to the Telegram source post number.

Example for post `256`:

```text
https://t.me/YourBot?start=pbn2lG5B6nirOwTI
```

The digits inside the token are still `256`, but the bot also validates the complete random token against MongoDB. A user cannot simply invent another letter pattern containing a valid post number.

For multiple bots, the same catalog token can be used with whichever registered bot username the website chooses:

```text
https://t.me/BotOne?start=<token>
https://t.me/BotTwo?start=<token>
```

## Forced channels

### Required membership

```text
/add_channel -1001234567890
```

For a public channel the bot uses its public Telegram link. For a private channel it creates an invite link automatically.

### Join-request requirement

```text
/add_request -1009876543210
```

The bot creates a join-request invite link automatically.

For request channels, either of these satisfies access:

- user is already a member
- user has sent a join request recorded by the bot

The user must satisfy every configured channel requirement for that bot.

## Referrals

Referral format:

```text
https://t.me/<current_bot_username>?start=reff_<telegram_user_id>
```

A referral is incomplete until the referred user satisfies every forced-channel requirement for that bot.

`/set_refer N` is bot-specific.

Example with `/set_refer 2`:

```text
6 completed unclaimed referrals -> 3 Premium days
7 completed unclaimed referrals -> 3 Premium days + 1 referral remains
```

Claimed referrals cannot be reused.

## Daily limit

`/set_free N` is bot-specific.

Premium, Admin, Super and Owner are unlimited.

Free-user usage is tracked per bot and local date.

## Broadcast retry behavior

Broadcast sends sequentially with conservative pacing.

For Telegram `RetryAfter`/flood-wait responses, the sender waits for Telegram's requested duration and retries automatically. Transient network/time-out errors use backoff retries. Permanent errors such as a user blocking the bot are counted in `Failed` and the user is marked inactive for that bot.

## Super Indexer

The Super Indexer is separate from Streamlit and should run on your Windows or Linux machine using your normal Telegram account via Telethon.

It indexes only PDFs and skips:

- text-only messages
- photos
- videos
- stickers
- polls
- service messages
- non-PDF documents

It does **not** download the PDF bytes.

It stores both:

```text
last_scanned_message_id
last_indexed_pdf_id
```

so text/non-PDF messages still advance the resume checkpoint.

### Windows

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python super_indexer.py index
```

### Linux

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python super_indexer.py index
```

Commands:

```text
python super_indexer.py index
python super_indexer.py sync
python super_indexer.py status
python super_indexer.py index --from-start
```

If Telegram returns a FloodWait, it saves the current checkpoint, waits automatically and resumes.

Never upload the generated Telethon `.session` file.

## Streamlit Secrets

Use `app.py` as the Streamlit entry file.

```toml
BOT_TOKEN = "123456789:AA..."
MONGO_URI = "mongodb+srv://..."
MONGO_DB = "telegram_pdf_library"
OWNER_IDS = "123456789,987654321"
SOURCE_CHANNEL_ID = "-1001234567890"
WEBSITE_URL = "https://your-site.vercel.app"
APP_SECRET = "a-long-random-secret-you-keep-stable"
APP_TIMEZONE = "Asia/Kolkata"
DELETE_AFTER_SECONDS = "300"
DEFAULT_FREE_LIMIT = "5"
DEFAULT_REFER_RATE = "2"
```

After the first bot is active, other bot tokens are added with `/add_bot`; they do not need to be added to Streamlit Secrets.

## Super Indexer `.env`

The local indexer needs:

```env
MONGO_URI=...
MONGO_DB=telegram_pdf_library
SOURCE_CHANNEL_ID=-1001234567890
TG_API_ID=123456
TG_API_HASH=...
TG_SESSION=super_indexer
```

## Vercel website search

The website should query MongoDB only from server-side Vercel code. Never expose `MONGO_URI` to browser JavaScript.

The catalog creates a weighted MongoDB text index:

- title: weight 10
- description: weight 3

Example server-side search:

```javascript
const results = await db.collection("pdfs")
  .find(
    { $text: { $search: query }, active: true },
    {
      projection: {
        score: { $meta: "textScore" },
        title: 1,
        description: 1,
        start_token: 1
      }
    }
  )
  .sort({ score: { $meta: "textScore" } })
  .limit(50)
  .toArray();
```

The website can select an enabled bot from the `bots` collection and construct:

```text
https://t.me/<bot_username>?start=<start_token>
```

You can hide the raw destination from normal page text/UI, but a browser destination cannot be made absolutely undiscoverable to a determined technical user.

## Important permissions

The bot needs suitable administrator permissions in channels it must verify or create links for.

For join-request channels, it needs permission to invite users so Telegram can deliver join-request updates.

For the source PDF channel, any bot expected to do live indexing/delivery must have appropriate access to those source posts.

## Security

Never commit:

- bot tokens
- `MONGO_URI`
- `APP_SECRET`
- `TG_API_HASH`
- Telethon `.session` files

The repository `.gitignore` already excludes `.env`, Streamlit local secrets and Telethon session files.
